"""
Stage 6: The Auditor — orchestrator.
"""

from __future__ import annotations

import logging

import numpy as np

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    EvidenceChunk,
    ValidationStatus,
)
from zerde.stages.s6_audit.checks import _audit_fact, _confidence_to_status, _score_to_status
from zerde.stages.s6_audit.conflicts import _audit_conclusions, _build_conflicts_from_verdicts
from zerde.stages.s6_audit.reliability import _compute_reliability_and_stats
from zerde.stages.s6_audit.retrieval import _build_bm25_index, _corpus_wide_bm25_search
from zerde.stages.s6_audit.source_filter import (
    VIRTUAL_SOURCES,
    _apply_source_domain_filter,
    _downgrade_unsupported_confirmed,
)
from zerde.stages.s6_audit.verdict_sync import _sync_verdicts_with_facts
from zerde.utils.claims import are_law_ids_synonymous as _are_law_ids_synonymous
from zerde.utils.claims import extract_article_from_claim as _extract_article_from_claim
from zerde.utils.claims import extract_referenced_law_ids as _extract_referenced_law_ids

logger = logging.getLogger(__name__)


def _exact_metadata_search(
    claim,
    corpus_index: dict[str, EvidenceChunk],
) -> list[str]:
    """
    Выполняет прямой поиск в корпусе по метаданным (law_id и article).
    Используется как приоритетный точный Шаг 1 в верификации.
    """
    referenced_law_ids = _extract_referenced_law_ids(claim)
    article_num = _extract_article_from_claim(claim)

    if not referenced_law_ids or not article_num:
        return []

    candidates = []
    for cid, chunk in corpus_index.items():
        if any(_are_law_ids_synonymous(chunk.law_id, ref_id) for ref_id in referenced_law_ids) and chunk.article == article_num:
            candidates.append(cid)

    return candidates


def audit_analysis(
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
    claims: ClaimExtractionResult | None = None,
) -> AnalysisJSON:
    """
    Этап 6: Детерминированный аудит. Строго без Retry.
    Ошибка при любой проверке → UNVERIFIED.
    """
    from zerde.stages.s6_audit.checks import _arithmetic_check

    settings = get_settings()

    # Map claim_ids to DocumentClaim objects for metadata filtering
    claim_map = {}
    if claims and claims.claims:
        claim_map = {c.claim_id: c for c in claims.claims}
    corpus_index = {c.chunk_id: c for c in chunks if not c.is_duplicate}

    # source_ids уже переведены в полные chunk_id в S5 (_remap_source_ids:
    # короткие метки S1.. → chunk_id), поэтому здесь достаточно точного совпадения
    # по corpus_index — обрезанных hex-ID и prefix-recovery больше нет.
    _apply_source_domain_filter(analysis, corpus_index)

    # Downgrade "UNLINKED -> HIGH" loophole.
    _downgrade_unsupported_confirmed(analysis, corpus_index)

    logger.info(f"[S6] Audit start. facts={len(analysis.facts)} corpus={len(corpus_index)}")

    # Строим BM25 индекс
    bm25 = _build_bm25_index(corpus_index)

    scores: list[float] = []

    for fact in analysis.facts:
        try:
            claim = claim_map.get(fact.claim_id) if (fact.claim_id and fact.claim_id in claim_map) else None

            # --- METADATA-FIRST SEARCH ---
            # Совпадение (law_id, article) — это привязка к ПЕРВОИСТОЧНИКУ, НЕ
            # доказательство, что текст статьи подтверждает СУТЬ claim. Раньше тут
            # жёстко ставилось bm25=1.0 + HIGH, и sync-блок ниже апгрейдил
            # UNVERIFIED→CONFIRMED только по факту совпадения номера статьи — для
            # поправочных биллей (claim ссылается на существующую статью) это
            # массовый false-confirm. Теперь метадата даёт привязку источника, но
            # СТАТУС считаем по реальному BM25 claim↔чанк: наличие статьи в корпусе
            # ≠ подтверждение содержания (CITE-OR-ABSTAIN).
            if claim:
                exact_candidates = _exact_metadata_search(claim, corpus_index)
                if exact_candidates:
                    fact.source_ids = exact_candidates
                    meta_query = (claim.claim_text + " " + claim.quote).strip() or fact.claim
                    meta_score = bm25.score(meta_query, exact_candidates)
                    fact.bm25_score = meta_score

                    # Арифметика: числовое расхождение с источником — сигнал ошибки,
                    # давим в LOW независимо от лексического совпадения.
                    arithmetic_ok = _arithmetic_check(fact, exact_candidates, corpus_index)
                    if not arithmetic_ok:
                        fact.validation_status = ValidationStatus.LOW
                    else:
                        fact.validation_status = _score_to_status(
                            meta_score,
                            settings.validation_threshold,
                            settings.bm25_medium_threshold,
                        )

                    scores.append(meta_score)
                    logger.info(
                        f"[S6/Metadata-First] Fact '{fact.fact_id}' (claim '{claim.claim_id}') metadata match "
                        f"ids={exact_candidates} bm25={meta_score:.3f} arithmetic_ok={arithmetic_ok} "
                        f"→ {fact.validation_status.value}"
                    )
                    continue

            # UNLINKED или пустые источники — LLM не привязал к корпусу.
            # Fallback: BM25 corpus-wide search по тексту claim.
            if not fact.source_ids or "UNLINKED" in fact.source_ids:
                corpus_score = _corpus_wide_bm25_search(fact, bm25, corpus_index, claim)
                if corpus_score is not None and corpus_score >= settings.bm25_fallback_threshold:
                    fact.bm25_score = corpus_score
                    fact.validation_status = _score_to_status(
                        corpus_score,
                        settings.validation_threshold,
                        settings.bm25_medium_threshold,
                    )
                    scores.append(corpus_score)
                    logger.info(
                        f"[S6/Fallback] Fact '{fact.fact_id}' recovered via corpus BM25: "
                        f"score={corpus_score:.3f}"
                    )
                else:
                    fact.validation_status = ValidationStatus.UNVERIFIED
                    fact.bm25_score = corpus_score or 0.0
                continue

            # Факты с только виртуальными source_ids (reference_data)
            # были проверены детерминированно — оцениваем по confidence
            real_sources = [
                sid for sid in fact.source_ids
                if sid not in VIRTUAL_SOURCES and not sid.startswith("reference_")
            ]
            if not real_sources:
                # Детерминированный факт: score прямо из confidence
                scores.append(fact.confidence)
                fact.bm25_score = fact.confidence
                fact.validation_status = _confidence_to_status(
                    fact.confidence,
                    settings.validation_threshold,
                    settings.bm25_medium_threshold,
                )
                continue

            result = _audit_fact(
                fact,
                corpus_index,
                bm25,
                settings.validation_threshold,
                settings.bm25_medium_threshold,
                claim,
            )
            fact.validation_status = result.status
            fact.bm25_score = result.bm25_score
            if result.bm25_score is not None:
                scores.append(result.bm25_score)
        except Exception as e:
            # Строго без Retry
            logger.warning(f"[S6] Fact '{fact.fact_id}' audit exception: {e}")
            fact.validation_status = ValidationStatus.UNVERIFIED

    # Синхронизация verdicts <-> facts (CONTRADICTED override + tiered upgrade/downgrade)
    _sync_verdicts_with_facts(analysis)

    # Audit выводов
    _audit_conclusions(analysis, corpus_index)

    # Calibrated Legal Confidence Metric & Statistics Aggregator
    if analysis.verdicts:
        early_return = _compute_reliability_and_stats(analysis, corpus_index)
        if early_return:
            return analysis
    elif scores:
        analysis.overall_reliability = float(np.mean(scores))

    # Статистика
    status_counts = {s: 0 for s in ValidationStatus}
    for fact in analysis.facts:
        status_counts[fact.validation_status] += 1

    # Conflicts Bridge — превращаем CONTRADICTED вердикты в ConflictRecord
    # для единой секции "Выявленные конфликты и коллизии" в отчёте
    analysis.conflicts = _build_conflicts_from_verdicts(analysis.verdicts)
    logger.info(f"[S6/Conflicts] Bridge created {len(analysis.conflicts)} conflict records")

    # reliability может быть 0.0, проверяем на is not None
    logger.info(
        f"[S6] Done. HIGH={status_counts[ValidationStatus.HIGH]} "
        f"MEDIUM={status_counts[ValidationStatus.MEDIUM]} "
        f"LOW={status_counts[ValidationStatus.LOW]} "
        f"UNVERIFIED={status_counts[ValidationStatus.UNVERIFIED]} "
        f"reliability={analysis.overall_reliability:.3f}"
        if analysis.overall_reliability is not None else "[S6] Done. No scores."
    )
    return analysis
