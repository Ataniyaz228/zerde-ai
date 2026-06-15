"""
Stage 3.5 / 3.6: Local RAG / Claim-driven Injection

Догружают chunks из локального SQLite-кэша (`evidence_cache`), которые S3
(adilet/web gather) мог пропустить:

  - S3.5 (`inject_local_rag`): прямой RAG по `query_plan.adilet_queries`
    (law_id [+ статьи]) — Шаг A (точный SQL по метаданным) + Шаг B
    (семантический `search_local`, ловит чанки без точных метаданных).
  - S3.6 (`inject_claim_driven`): Planner (S2) не видит будущих claims, поэтому
    S3.5 пропускает статьи, упоминаемые ТОЛЬКО в S2.5 claim extractor. Здесь
    догружаем chunks по парам (target_law_id × article из claim).

Обе функции — best-effort: ошибки логируются как warning и НЕ всплывают в
оркестратор (top-level try/except), мутируют и возвращают `raw_chunks`.
"""

from __future__ import annotations

import asyncio
import logging

from zerde.config import get_settings
from zerde.models import (
    AdiletFallbackStrategy,
    ClaimExtractionResult,
    EvidenceChunk,
    QueryPlan,
)
from zerde.utils.cache import CacheManager
from zerde.utils.claims import extract_article_from_claim
from zerde.utils.law_registry import get_registry

logger = logging.getLogger(__name__)


async def inject_local_rag(
    raw_chunks: list[EvidenceChunk],
    query_plan: QueryPlan,
    cache: CacheManager | None = None,
) -> list[EvidenceChunk]:
    """
    Stage 3.5: Прямой RAG-инжект чанков из локального кэша по
    `query_plan.adilet_queries` (law_id [+ статьи]).

    Шаг A: точный SQL по метаданным (`CacheManager.get_chunks_by_metadata`) —
    гарантированное попадание.
    Шаг B: дополнительный семантический поиск (`search_local`) — ловит чанки
    без точных метаданных, но семантически релевантные.

    Мутирует и возвращает `raw_chunks` (порядок: Шаг A перед Шагом B).
    """
    try:
        registry = get_registry()
        rag_cache = cache or CacheManager(get_settings().cache_db_path)
        existing_chunk_ids = {c.chunk_id for c in raw_chunks}

        law_id_to_articles: dict[str, set[str] | None] = {}
        for aq in query_plan.adilet_queries:
            aq_lids = [registry.resolve(lid) for lid in (aq.law_ids or []) if lid]

            for lid in aq_lids:
                if not lid:
                    continue

                # Если уже стоит None (взять всё), не ограничиваем статьями
                if law_id_to_articles.get(lid) is None:
                    continue

                if not aq.articles:
                    law_id_to_articles[lid] = None
                else:
                    current_arts = law_id_to_articles.get(lid, set())
                    if current_arts is not None:
                        current_arts.update(aq.articles)
                        law_id_to_articles[lid] = current_arts

        if law_id_to_articles:
            logger.info(f"[Pipeline/S3.5] Прямой RAG-запрос: {law_id_to_articles}")

            # Шаг A: точный SQL по метаданным — гарантированное попадание.
            injected = 0
            for lid, arts in law_id_to_articles.items():
                if arts:
                    # Для каждой статьи — отдельный запрос чтобы не пропустить ни одну
                    for art in arts:
                        for chunk in rag_cache.get_chunks_by_metadata(lid, article=art):
                            if chunk.chunk_id not in existing_chunk_ids:
                                chunk.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                                raw_chunks.append(chunk)
                                existing_chunk_ids.add(chunk.chunk_id)
                                injected += 1
                else:
                    # Без фильтра по статьям — все чанки закона (limit 150)
                    for chunk in rag_cache.get_chunks_by_metadata(lid, limit=150):
                        if chunk.chunk_id not in existing_chunk_ids:
                            chunk.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                            raw_chunks.append(chunk)
                            existing_chunk_ids.add(chunk.chunk_id)
                            injected += 1

            # Шаг B: семантический поиск — добирает релевантные чанки без точных метаданных.
            rag_tasks = [
                rag_cache.search_local(
                    query_text=aq.query_text,
                    law_ids=[registry.resolve(lid) for lid in (aq.law_ids or [])],
                    limit=15,
                    law_only=True,
                )
                for aq in query_plan.adilet_queries
            ]
            rag_results = await asyncio.gather(*rag_tasks, return_exceptions=True)
            for result in rag_results:
                if isinstance(result, list):
                    for c in result:
                        if c.chunk_id not in existing_chunk_ids:
                            c.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                            raw_chunks.append(c)
                            existing_chunk_ids.add(c.chunk_id)
                            injected += 1

            if injected:
                logger.info(f"[Pipeline/S3.5] Инъекцировано {injected} локальных RAG-чанков (всего {len(raw_chunks)} чанков)")
    except Exception as _e:
        logger.warning(f"[Pipeline/S3.5] Local RAG injection ошибка (не критично): {_e}")

    return raw_chunks


def inject_claim_driven(
    raw_chunks: list[EvidenceChunk],
    claims: ClaimExtractionResult,
    cache: CacheManager | None = None,
) -> list[EvidenceChunk]:
    """
    Stage 3.6: Claim-driven Injection.

    Planner (S2) не видит будущих claims, поэтому S3.5 пропускает статьи,
    упоминаемые ТОЛЬКО в S2.5 claim extractor. Здесь догружаем chunks по
    парам (target_law_id × article из claim).

    Мутирует и возвращает `raw_chunks`.
    """
    try:
        registry = get_registry()
        rag_cache = cache or CacheManager(get_settings().cache_db_path)
        existing_chunk_ids = {c.chunk_id for c in raw_chunks}

        # Собираем пары (resolved_law_id, article) из всех claims
        claim_pairs: set[tuple[str, str]] = set()
        all_claims = list(claims.claims) + list(claims.structural_claims)
        for c in all_claims:
            article = extract_article_from_claim(c)
            if not article:
                continue
            for raw_lid in (c.target_law_ids or []):
                resolved = registry.resolve(raw_lid)
                if resolved:
                    claim_pairs.add((resolved, article))

        if claim_pairs:
            logger.info(f"[Pipeline/S3.6] Claim-driven запросы: {len(claim_pairs)} пар (law_id × article)")
            injected_c = 0
            for lid, art in claim_pairs:
                # Все эквивалентные написания law_id (short ID + adilet-код)
                # из реестра — устойчивый матчинг чанков в кеше.
                lid_variants = registry.id_variants(lid)
                for variant in lid_variants:
                    for chunk in rag_cache.get_chunks_by_metadata(variant, article=art):
                        if chunk.chunk_id not in existing_chunk_ids:
                            chunk.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                            raw_chunks.append(chunk)
                            existing_chunk_ids.add(chunk.chunk_id)
                            injected_c += 1

            if injected_c:
                logger.info(
                    f"[Pipeline/S3.6] Инъекцировано {injected_c} чанков по claim-парам "
                    f"(всего {len(raw_chunks)} чанков)"
                )
    except Exception as _e:
        logger.warning(f"[Pipeline/S3.6] Claim-driven injection ошибка (не критично): {_e}")

    return raw_chunks
