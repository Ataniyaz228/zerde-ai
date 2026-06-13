"""
Тесты для Stage 3.5/3.6 (Local RAG / Claim-driven Injection) и
CacheManager.get_chunks_by_metadata — все offline (без torch/BGE/LLM),
не помечены `heavy`.
"""

from __future__ import annotations

import pytest

from zerde.models import (
    AdiletFallbackStrategy,
    AdiletQuery,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    DocumentClaim,
    EvidenceChunk,
    LegalRank,
    QueryPlan,
)
from zerde.stages.s3_5_local_rag import inject_claim_driven, inject_local_rag
from zerde.utils.cache import CacheManager


@pytest.fixture(autouse=True)
def _isolate_cache_db(monkeypatch):
    """Истинная изоляция: ZERDE_CACHE_DB (если выставлен, напр. на CI) ПЕРЕОПРЕДЕЛЯЕТ
    явный db_path в CacheManager.__init__ — без снятия env эти тесты писали бы в
    общую/коммиченную фикстуру. Снимаем env, чтобы CacheManager(db_path=tmp) был
    действительно изолированным."""
    monkeypatch.delenv("ZERDE_CACHE_DB", raising=False)


def _chunk(chunk_id: str, law_id: str, article: str | None, content: str = "текст статьи") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url="https://adilet.zan.kz/rus/docs/TEST",
        source_title=f"Закон {law_id}",
        content=content,
        legal_rank=LegalRank.LAW_RK,
        law_id=law_id,
        article=article,
    )


def _seed(manager: CacheManager, chunks: list[EvidenceChunk]) -> None:
    """Вставляет чанки прямо в evidence_cache, БЕЗ embedding-синка (_sync_new_chunks
    у put_many грузит BGE-M3/torch). Эти тесты — про чистый SQL get_chunks_by_metadata
    и метаданные-инжект, семантика им не нужна → остаются offline (не `heavy`)."""
    from datetime import UTC, datetime

    with manager._conn() as conn:
        for ch in chunks:
            conn.execute(
                """INSERT OR IGNORE INTO evidence_cache
                   (chunk_id, source_url, content_hash, chunk_json, cached_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (ch.chunk_id, ch.source_url, ch.chunk_id, ch.model_dump_json(),
                 datetime.now(UTC).isoformat()),
            )


# ---------------------------------------------------------------------------
# A. CacheManager.get_chunks_by_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chunks_by_metadata_article_filter(tmp_path):
    db_file = tmp_path / "meta_a.db"
    manager = CacheManager(db_path=str(db_file))

    c1 = _chunk("chunk_a1", "999-TEST", "15")
    c2 = _chunk("chunk_a2", "999-TEST", "16")
    c3 = _chunk("chunk_a3", "999-TEST", "15", content="вторая часть статьи 15")

    _seed(manager,[c1, c2, c3])

    results = manager.get_chunks_by_metadata("999-TEST", article="15")
    result_ids = {c.chunk_id for c in results}
    assert result_ids == {"chunk_a1", "chunk_a3"}


@pytest.mark.asyncio
async def test_get_chunks_by_metadata_no_article_returns_all(tmp_path):
    db_file = tmp_path / "meta_b.db"
    manager = CacheManager(db_path=str(db_file))

    c1 = _chunk("chunk_b1", "999-TEST", "15")
    c2 = _chunk("chunk_b2", "999-TEST", "16")
    c3 = _chunk("chunk_b3", "999-TEST", None)

    _seed(manager,[c1, c2, c3])

    results = manager.get_chunks_by_metadata("999-TEST")
    result_ids = {c.chunk_id for c in results}
    assert result_ids == {"chunk_b1", "chunk_b2", "chunk_b3"}


@pytest.mark.asyncio
async def test_get_chunks_by_metadata_limit_caps_no_article(tmp_path):
    db_file = tmp_path / "meta_c.db"
    manager = CacheManager(db_path=str(db_file))

    chunks = [_chunk(f"chunk_c{i}", "999-TEST", str(i)) for i in range(5)]
    _seed(manager,chunks)

    results = manager.get_chunks_by_metadata("999-TEST", limit=2)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_get_chunks_by_metadata_unknown_law_returns_empty(tmp_path):
    db_file = tmp_path / "meta_d.db"
    manager = CacheManager(db_path=str(db_file))

    _seed(manager,[_chunk("chunk_d1", "999-TEST", "15")])

    assert manager.get_chunks_by_metadata("000-UNKNOWN") == []
    assert manager.get_chunks_by_metadata("000-UNKNOWN", article="15") == []


@pytest.mark.asyncio
async def test_get_chunks_by_metadata_broken_row_increments_failures(tmp_path):
    db_file = tmp_path / "meta_e.db"
    manager = CacheManager(db_path=str(db_file))

    good = _chunk("chunk_e1", "999-TEST", "15")
    _seed(manager,[good])

    # Insert a row with VALID JSON (so json_extract matches law_id/article for
    # the SQL WHERE clause) but missing required EvidenceChunk fields, so
    # model_validate() raises a pydantic ValidationError per-row.
    with manager._conn() as conn:
        conn.execute(
            """INSERT INTO evidence_cache (chunk_id, source_url, content_hash, chunk_json, cached_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "chunk_e2",
                "https://example.com",
                "chunk_e2",
                '{"law_id": "999-TEST", "article": "15"}',  # valid JSON, missing required fields
                "2026-01-01T00:00:00+00:00",
            ),
        )

    assert manager.deserialization_failures == 0
    results = manager.get_chunks_by_metadata("999-TEST", article="15")

    # Valid chunk still returned despite the broken sibling row.
    assert [c.chunk_id for c in results] == ["chunk_e1"]
    # Broken row incremented the counter rather than being swallowed silently.
    assert manager.deserialization_failures == 1


# ---------------------------------------------------------------------------
# B. inject_claim_driven (Stage 3.6) — sync, pure SQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_claim_driven_injects_and_dedupes(tmp_path):
    db_file = tmp_path / "claim_driven.db"
    manager = CacheManager(db_path=str(db_file))

    target = _chunk("chunk_target", "999-TEST", "15")
    other_article = _chunk("chunk_other", "999-TEST", "16")

    _seed(manager,[target, other_article])

    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Документ ссылается на статью 15 закона 999-TEST",
        quote="согласно статье 15",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["999-TEST"],
    )
    # Sanity: extract_article_from_claim must yield "15" for this fixture text.
    from zerde.utils.claims import extract_article_from_claim
    assert extract_article_from_claim(claim) == "15"

    claims = ClaimExtractionResult(doc_id="doc1", claims=[claim], structural_claims=[])

    # Pre-existing chunk (already in raw_chunks) must NOT be re-injected.
    pre_existing = _chunk("chunk_pre", "999-TEST", "15", content="уже было в raw_chunks")
    raw_chunks: list[EvidenceChunk] = [pre_existing]

    result = inject_claim_driven(raw_chunks, claims, cache=manager)

    assert result is raw_chunks  # mutate-and-return
    result_ids = {c.chunk_id for c in result}

    # Only article 15 injected, article 16 not.
    assert "chunk_target" in result_ids
    assert "chunk_other" not in result_ids
    # Pre-existing chunk not duplicated.
    assert sum(1 for c in result if c.chunk_id == "chunk_pre") == 1

    injected = next(c for c in result if c.chunk_id == "chunk_target")
    assert injected.adilet_fallback_used == AdiletFallbackStrategy.LOCAL_CACHE

    # The pre-existing chunk's fallback marker is untouched (caller didn't relabel it).
    pre = next(c for c in result if c.chunk_id == "chunk_pre")
    assert pre.adilet_fallback_used is None


def test_inject_claim_driven_no_matching_article_is_noop(tmp_path):
    db_file = tmp_path / "claim_driven_empty.db"
    manager = CacheManager(db_path=str(db_file))

    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Документ без явной ссылки на статью",
        quote="общее утверждение",
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.LOW,
        target_law_ids=["999-TEST"],
    )
    claims = ClaimExtractionResult(doc_id="doc1", claims=[claim], structural_claims=[])

    raw_chunks: list[EvidenceChunk] = []
    result = inject_claim_driven(raw_chunks, claims, cache=manager)
    assert result == []


# ---------------------------------------------------------------------------
# C. inject_local_rag (Stage 3.5) — Step A offline (Step B stubbed)
# ---------------------------------------------------------------------------
#
# NOTE (behavior-neutral extraction — pre-existing condition, not introduced
# here): `law_id_to_articles.get(lid) is None` is also True for keys NEVER
# seen before (dict.get default), so the very first time any law_id is
# encountered the "уже None — take all" branch fires and `continue`s before
# `law_id_to_articles[lid]` is ever set. `law_id_to_articles` therefore stays
# `{}` for ANY input, and the whole `if law_id_to_articles:` block (Step A AND
# Step B) is unreachable in current production code. This was true of the
# original inline pipeline.py code too (verified against the pre-extraction
# source) — preserved verbatim per BEHAVIOR-NEUTRAL. The tests below assert
# the function is a faithful (currently no-op) mutate-and-return, matching
# pre-extraction behavior exactly. See PR description for a follow-up fix
# proposal (out of scope for this refactor).


@pytest.mark.asyncio
async def test_inject_local_rag_step_a_b_currently_unreachable_noop(tmp_path, monkeypatch):
    """Faithful extraction check: given adilet_queries with law_ids+articles
    that WOULD match cached chunks, no injection happens — because
    `law_id_to_articles` is always `{}` (pre-existing dead-code condition,
    preserved verbatim). raw_chunks is returned unchanged (mutate-and-return)."""
    db_file = tmp_path / "local_rag.db"
    manager = CacheManager(db_path=str(db_file))

    art15 = _chunk("chunk_lr_15", "999-TEST", "15")
    whole_law_chunk = _chunk("chunk_lr_whole", "888-TEST", "1")

    _seed(manager,[art15, whole_law_chunk])

    # Stub search_local so Step B (if it were reachable) would also be a
    # deterministic no-op — no BGE/torch involved either way.
    async def _stub_search_local(*args, **kwargs):
        return []

    monkeypatch.setattr(manager, "search_local", _stub_search_local)

    query_plan = QueryPlan(
        plan_id="plan1",
        source_doc_id="doc1",
        adilet_queries=[
            AdiletQuery(query_text="статья 15 закона 999-TEST", law_ids=["999-TEST"], articles=["15"]),
            AdiletQuery(query_text="весь закон 888-TEST", law_ids=["888-TEST"], articles=[]),
        ],
    )

    raw_chunks: list[EvidenceChunk] = []
    result = await inject_local_rag(raw_chunks, query_plan, cache=manager)

    assert result is raw_chunks  # mutate-and-return
    assert result == []  # unreachable injection block → unchanged


def test_inject_local_rag_law_id_to_articles_always_empty_for_fresh_lids():
    """Unit-checks the merge logic in isolation: for any set of (lid, articles)
    pairs where each lid is seen for the first time, `law_id_to_articles`
    ends up `{}` — the `.get(lid) is None` check is also True for missing
    keys, so the very first sighting of any lid always `continue`s before a
    value is stored. This is the condition gating Step A/B; documented here
    so a future fix has a regression test to update alongside it."""
    from zerde.utils.law_registry import get_registry

    registry = get_registry()
    law_id_to_articles: dict[str, set[str] | None] = {}

    adilet_queries = [
        ("999-TEST", ["15"]),
        ("888-TEST", []),
        ("777-TEST", ["1", "2"]),
    ]
    for raw_lid, articles in adilet_queries:
        aq_lids = [registry.resolve(lid) for lid in [raw_lid] if lid]
        for lid in aq_lids:
            if not lid:
                continue
            if law_id_to_articles.get(lid) is None:
                continue
            if not articles:
                law_id_to_articles[lid] = None
            else:
                current_arts = law_id_to_articles.get(lid, set())
                if current_arts is not None:
                    current_arts.update(articles)
                    law_id_to_articles[lid] = current_arts

    assert law_id_to_articles == {}


@pytest.mark.asyncio
async def test_inject_local_rag_dedupes_existing_chunks(tmp_path, monkeypatch):
    db_file = tmp_path / "local_rag_dedup.db"
    manager = CacheManager(db_path=str(db_file))

    art15 = _chunk("chunk_dup_15", "999-TEST", "15")
    _seed(manager,[art15])

    async def _stub_search_local(*args, **kwargs):
        return []

    monkeypatch.setattr(manager, "search_local", _stub_search_local)

    query_plan = QueryPlan(
        plan_id="plan1",
        source_doc_id="doc1",
        adilet_queries=[
            AdiletQuery(query_text="статья 15 закона 999-TEST", law_ids=["999-TEST"], articles=["15"]),
        ],
    )

    # Already present in raw_chunks — must not be duplicated.
    pre_existing = _chunk("chunk_dup_15", "999-TEST", "15", content="уже было")
    raw_chunks: list[EvidenceChunk] = [pre_existing]

    result = await inject_local_rag(raw_chunks, query_plan, cache=manager)

    assert len([c for c in result if c.chunk_id == "chunk_dup_15"]) == 1
    # Pre-existing chunk's fallback marker untouched (not re-labeled).
    pre = next(c for c in result if c.chunk_id == "chunk_dup_15")
    assert pre.adilet_fallback_used is None


@pytest.mark.asyncio
async def test_inject_local_rag_no_queries_is_noop(tmp_path, monkeypatch):
    db_file = tmp_path / "local_rag_empty.db"
    manager = CacheManager(db_path=str(db_file))

    async def _stub_search_local(*args, **kwargs):
        return []

    monkeypatch.setattr(manager, "search_local", _stub_search_local)

    query_plan = QueryPlan(plan_id="plan1", source_doc_id="doc1", adilet_queries=[])

    raw_chunks: list[EvidenceChunk] = []
    result = await inject_local_rag(raw_chunks, query_plan, cache=manager)
    assert result == []
