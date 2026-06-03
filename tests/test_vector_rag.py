import numpy as np
import pytest

from zerde.models import EvidenceChunk, LegalRank
from zerde.utils.cache import CacheManager


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_cache.db")

@pytest.mark.asyncio
async def test_vector_table_creation_and_lazy_init(temp_db_path):
    # Initialize cache manager with temporary DB
    cache = CacheManager(temp_db_path)

    # Verify tables exist by checking stats
    stats = await cache.stats()
    assert stats["total_chunks"] == 0

    # Lazy initialization should be False at start
    assert not getattr(cache, "_embeddings_loaded", False)

    # Trigger lazy init
    cache._lazy_init_embeddings()
    assert getattr(cache, "_embeddings_loaded", True)
    assert cache._embed_model is not None
    assert cache._embeddings_matrix.shape == (0, 1024)

@pytest.mark.asyncio
async def test_embedding_precomputation_and_storage(temp_db_path):
    cache = CacheManager(temp_db_path)

    # Create sample chunks (RU and KK equivalents)
    chunk_ru = EvidenceChunk(
        chunk_id="hash_ru_01",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Закон о персональных данных",
        content="Персональные данные — сведения, относящиеся к определенному физическому лицу.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
    )

    chunk_kk = EvidenceChunk(
        chunk_id="hash_kk_01",
        source_url="https://adilet.zan.kz/rus/docs/test#2",
        source_title="Дербес деректер туралы заң",
        content="Дербес деректер — белгілі бір жеке тұлғаға қатысты мәліметтер.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
    )

    # Store chunks in SQLite
    await cache.put_many([chunk_ru, chunk_kk])

    # Run embedding pre-computation
    cache.embed_chunks([chunk_ru, chunk_kk])

    # Verify they exist in SQLite evidence_embeddings table
    with cache._conn() as conn:
        rows = conn.execute("SELECT chunk_id, embedding FROM evidence_embeddings").fetchall()
        assert len(rows) == 2
        for r in rows:
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            assert emb.shape == (1024,)

    # Clear memory representation and force lazy-init reload from SQLite
    cache._embeddings_loaded = False
    cache._lazy_init_embeddings()

    assert len(cache._embeddings_keys) == 2
    assert cache._embeddings_matrix.shape == (2, 1024)
    assert len(cache._bm25_keys) == 2
    assert cache._bm25_index is not None

@pytest.mark.asyncio
async def test_parallel_hybrid_search_and_rrf(temp_db_path):
    cache = CacheManager(temp_db_path)

    chunk_ru = EvidenceChunk(
        chunk_id="hash_ru_02",
        source_url="https://adilet.zan.kz/rus/docs/test#3",
        source_title="Конфиденциальность данных",
        content="Сбор и обработка персональных данных осуществляются с согласия субъекта.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="2",
    )

    chunk_kk = EvidenceChunk(
        chunk_id="hash_kk_02",
        source_url="https://adilet.zan.kz/rus/docs/test#4",
        source_title="Деректердің құпиялылығы",
        content="Дербес деректерді жинау және өңдеу субъектінің келісімімен жүзеге асырылады.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="2",
    )

    await cache.put_many([chunk_ru, chunk_kk])
    cache.embed_chunks([chunk_ru, chunk_kk])

    # Test multilingual semantic match: search in Russian, should match Kazakh equivalent via vector space
    results_kk = await cache.search_local("согласие субъекта на сбор персональных данных")
    assert len(results_kk) >= 1
    # Check that both RU and KK chunks are retrieved because they are semantically equivalent
    retrieved_ids = [c.chunk_id for c in results_kk]
    assert "hash_ru_02" in retrieved_ids
    assert "hash_kk_02" in retrieved_ids

@pytest.mark.asyncio
async def test_dynamic_ingestion_sync(temp_db_path):
    cache = CacheManager(temp_db_path)

    # Initialize index empty
    cache._lazy_init_embeddings()
    assert cache._embeddings_matrix.shape == (0, 1024)

    chunk = EvidenceChunk(
        chunk_id="hash_dynamic_01",
        source_url="https://adilet.zan.kz/rus/docs/dynamic#1",
        source_title="Динамический чанк",
        content="Условия договора определяются по соглашению сторон.",
        legal_rank=LegalRank.LAW_RK,
        law_id="GK-RK",
        article="382",
    )

    # put() should automatically trigger embed & dynamic RAM sync
    await cache.put(chunk)

    # Verify RAM representation dynamically grew
    assert "hash_dynamic_01" in cache._embeddings_keys
    assert cache._embeddings_matrix.shape == (1, 1024)
    assert "hash_dynamic_01" in cache._bm25_keys
    assert cache._bm25_index is not None

    # Verify retrieval
    results = await cache.search_local("соглашение сторон по договору")
    assert len(results) == 1
    assert results[0].chunk_id == "hash_dynamic_01"


@pytest.mark.asyncio
async def test_cosine_dedup_via_bge_m3(temp_db_path, monkeypatch):
    # Set ZERDE_CACHE_DB env var so CacheManager in s4_fusion uses the temp DB
    monkeypatch.setenv("ZERDE_CACHE_DB", temp_db_path)

    from zerde.stages.s4_fusion import fuse_and_validate

    # Create two semantically identical chunks with slightly different phrasing
    chunk1 = EvidenceChunk(
        chunk_id="chunk_a",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Закон о персональных данных",
        content="Субъект персональных данных обязан дать письменное согласие на сбор и обработку его данных.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
        adilet_fallback_used="LOCAL_CACHE",  # Bypass ContentSpamFilter
    )

    chunk2 = EvidenceChunk(
        chunk_id="chunk_b",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Закон о персональных данных",
        content="Субъект персональных данных обязан предоставить письменное согласие на сбор и обработку его данных.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
        adilet_fallback_used="LOCAL_CACHE",  # Bypass ContentSpamFilter
    )

    # Run fusion and validation. One should be marked as duplicate.
    res = await fuse_and_validate([chunk1, chunk2])

    duplicates = [c for c in res if c.is_duplicate]
    active = [c for c in res if not c.is_duplicate]

    assert len(duplicates) == 1
    assert len(active) == 1

