"""Deterministic grounding check — reuses the real S6 matching code.

A claim is "grounded" iff, for its (resolved_law_id, article), the cache
contains a chunk that the production metadata matcher (`_exact_metadata_search`)
accepts. No LLM, no BM25 — purely the resolution + metadata layer that the
auditor relies on. This is the precondition for any (false) confirm.
"""

from __future__ import annotations

import json
from collections import defaultdict

from zerde.models import DocumentClaim, EvidenceChunk
from zerde.stages.s6_auditor import (
    _exact_metadata_search,
    _extract_article_from_claim,
    _extract_referenced_law_ids,
)
from zerde.utils.cache import CacheManager
from zerde.utils.law_registry import get_registry

# (law_id_as_stored, article) -> {chunk_id: EvidenceChunk}
ArticleIndex = dict[tuple[str, str], dict[str, EvidenceChunk]]


def build_article_index(cache: CacheManager) -> ArticleIndex:
    """Загружает все chunk'и из кеша один раз в индекс (law_id, article) → chunks.

    Так grounding-проверка для всей выборки делает поиск по памяти, а не
    json_extract-скан 20k строк на каждый claim.
    """
    idx: ArticleIndex = defaultdict(dict)
    with cache._conn() as conn:
        for row in conn.execute("SELECT chunk_json FROM evidence_cache"):
            try:
                chunk = EvidenceChunk.model_validate(json.loads(row["chunk_json"]))
            except Exception:
                continue
            if chunk.article:
                idx[(chunk.law_id, chunk.article)][chunk.chunk_id] = chunk
    return idx


def check_grounding(claim: DocumentClaim, article_index: ArticleIndex) -> dict:
    """Возвращает диагностику заземления одного claim.

    grounded=True ⇔ в кеше есть chunk для (resolved law_id × article),
    принятый `_exact_metadata_search` (та же логика, что в проде).
    """
    article = _extract_article_from_claim(claim)
    ref_ids = _extract_referenced_law_ids(claim)
    registry = get_registry()
    resolved = sorted({registry.resolve(r) for r in ref_ids}) if ref_ids else []

    corpus: dict[str, EvidenceChunk] = {}
    if article and resolved:
        for lid in resolved:
            for variant in registry.id_variants(lid):
                corpus.update(article_index.get((variant, article), {}))

    candidates = _exact_metadata_search(claim, corpus) if corpus else []
    return {
        "claim_id": getattr(claim, "claim_id", None),
        "article": article,
        "referenced": ref_ids,
        "resolved": resolved,
        "n_candidate_chunks": len(corpus),
        "grounded": bool(candidates),
    }
