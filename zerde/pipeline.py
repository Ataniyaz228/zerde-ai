"""Оркестратор пайплайна: связывает стадии S1→S7 в один асинхронный прогон.

Каждая стадия — отдельная функция в zerde/stages/; здесь только порядок их
вызова, распараллеливание независимых стадий и передача результатов дальше.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    DocumentState,
    EvidenceChunk,
    QueryPlan,
)
from zerde.stages.s1_ingest import ingest_document
from zerde.stages.s2_5_claim_extractor import extract_claims
from zerde.stages.s2_7_self_check import run_self_check
from zerde.stages.s2_planner import build_query_plan
from zerde.stages.s3_5_local_rag import inject_claim_driven, inject_local_rag
from zerde.stages.s3_gather import gather_evidence
from zerde.stages.s4_fusion import fuse_and_validate
from zerde.stages.s5_2_verifier import verify_contradictions
from zerde.stages.s5_5_analyst import run_policy_analyst
from zerde.stages.s5_analyst import run_auditor
from zerde.stages.s6_auditor import audit_analysis
from zerde.stages.s7_render import render_report
from zerde.utils.cache import CacheManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ZerdePipelineResult:
    """Результаты полного прогона: документ, план, claims, чанки, анализ, отчёт."""

    doc_state: DocumentState
    query_plan: QueryPlan
    claims: ClaimExtractionResult
    raw_chunks: list[EvidenceChunk]
    fused_chunks: list[EvidenceChunk]
    analysis: AnalysisJSON
    report_md: str
    report_path: str
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Уведомление о смене этапа — backend транслирует его в WebSocket прогресс-бара."""

    stage: str
    message: str


def _emit_progress(
    progress: Callable[[ProgressEvent], None] | None,
    ev: ProgressEvent,
) -> None:
    if progress is not None:
        progress(ev)


async def run_pipeline(
    file_path: str | Path,
    output_path: str | Path | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
) -> ZerdePipelineResult:
    """Прогоняет документ через весь пайплайн и возвращает отчёт + промежуточные результаты.

    file_path   — входной документ (PDF/DOCX/TXT).
    output_path — куда сохранить Markdown-отчёт (None → не сохранять на диск).
    progress    — синхронный callback на смену этапа; backend шлёт его в WebSocket.
    """
    get_settings()
    start_time = time.perf_counter()

    logger.info("Pipeline start: %s", file_path)
    _emit_progress(progress, ProgressEvent("extract", "Загрузка и извлечение тезисов"))

    # S1 — извлечение текста из файла.
    t1 = time.perf_counter()
    doc_state = await ingest_document(file_path)
    logger.info("Stage 1 done (%.2fs) — %d chars", time.perf_counter() - t1, doc_state.char_count)

    # S2/S2.5/S2.7 независимы друг от друга — гоним их параллельно.
    # (self-check синхронный, поэтому уезжает в отдельный поток.)
    t2 = time.perf_counter()
    query_plan, claims, selfcheck_claims = await asyncio.gather(
        build_query_plan(doc_state),
        extract_claims(doc_state),
        asyncio.to_thread(run_self_check, doc_state.normalized_text),
    )
    if selfcheck_claims:
        claims.claims.extend(selfcheck_claims)
        logger.info("Stage 2.7 — %d internal contradictions added", len(selfcheck_claims))
    logger.info(
        "Stage 2 done (%.2fs) — %d queries, %d claims (%d critical)",
        time.perf_counter() - t2,
        query_plan.total_queries,
        claims.total_count,
        len(claims.critical_claims),
    )

    # S3 — сбор доказательств из adilet + web.
    t3 = time.perf_counter()
    _emit_progress(progress, ProgressEvent("search", "Поиск в базе НПА Казахстана"))
    raw_chunks = await gather_evidence(query_plan)
    logger.info("Stage 3 done (%.2fs) — %d chunks", time.perf_counter() - t3, len(raw_chunks))

    # S3.5/S3.6 — добиваем недостающие статьи из локального корпуса:
    # сначала по парам (law_id, article) из плана, затем по тем же парам из claims.
    rag_cache = CacheManager(get_settings().cache_db_path)
    raw_chunks = await inject_local_rag(raw_chunks, query_plan, cache=rag_cache)
    raw_chunks = inject_claim_driven(raw_chunks, claims, cache=rag_cache)

    # S4 — дедупликация и детекция конфликтов.
    t4 = time.perf_counter()
    fused_chunks = await fuse_and_validate(raw_chunks)
    active_chunks = [c for c in fused_chunks if not c.is_duplicate]
    logger.info("Stage 4 done (%.2fs) — %d active", time.perf_counter() - t4, len(active_chunks))

    # S5 — LLM-аудитор, затем детерминированная проверка вердиктов CONTRADICTED.
    t5 = time.perf_counter()
    _emit_progress(progress, ProgressEvent("verify", "Верификация и анализ коллизий"))

    if not claims.claims:
        import uuid as _uuid
        analysis = AnalysisJSON(
            analysis_id=str(_uuid.uuid4()),
            source_doc_id=query_plan.source_doc_id,
            plan_id=query_plan.plan_id,
            facts=[],
            conclusions=[],
            negative_space=[],
            normative=[],
            cons=[],
            llm_model_used=get_settings().llm_model_analyst,
            verdicts=[],
        )
    else:
        analysis = await run_auditor(
            chunks=active_chunks,
            plan=query_plan,
            claims=claims,
            doc_text=doc_state.normalized_text,
        )
        analysis = await verify_contradictions(analysis, active_chunks)

    # Структурные claims идут в отчёт отдельным чеклистом, а не как вердикты.
    analysis.structural_claims = claims.structural_claims
    from zerde.stages.s5_analyst import _validate_claim_coverage
    _validate_claim_coverage(analysis, claims)

    contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    logger.info(
        "Stage 5 done (%.2fs) — %d verdicts, %d contradicted, %d structural",
        time.perf_counter() - t5,
        len(analysis.verdicts),
        contradicted,
        len(claims.structural_claims),
    )

    # S5.5 (policy) и S6 (аудит) независимы — гоним параллельно. Обе ветви мутируют
    # analysis/chunks in-place, поэтому каждой даём свою глубокую копию.
    t56 = time.perf_counter()
    analysis_for_policy = analysis.model_copy(deep=True)
    active_chunks_for_policy = [c.model_copy(deep=True) for c in active_chunks]

    analysis_for_audit = analysis.model_copy(deep=True)
    active_chunks_for_audit = [c.model_copy(deep=True) for c in active_chunks]

    async def _run_s6():
        return audit_analysis(analysis_for_audit, active_chunks_for_audit, claims)

    policy_analysis, audited_analysis = await asyncio.gather(
        run_policy_analyst(
            doc_text=doc_state.normalized_text,
            analysis=analysis_for_policy,
            chunks=active_chunks_for_policy,
        ),
        _run_s6(),
    )
    logger.info(
        "Stage 5.5+6 done (%.2fs) — policy=%s",
        time.perf_counter() - t56,
        "ok" if policy_analysis else "none",
    )

    # S7 — рендер Markdown-отчёта.
    t7 = time.perf_counter()
    _emit_progress(progress, ProgressEvent("report", "Формирование отчёта"))
    report_md = await render_report(audited_analysis, active_chunks, output_path, policy_analysis)
    report_path = str(output_path) if output_path else "output/zerde_report_*.md"
    logger.info("Stage 7 done (%.2fs)", time.perf_counter() - t7)

    total_elapsed = time.perf_counter() - start_time
    logger.info(
        "Pipeline complete (%.2fs) — %d claims, %d verdicts, %d contradicted, reliability=%s",
        total_elapsed,
        claims.total_count,
        len(audited_analysis.verdicts),
        contradicted,
        audited_analysis.overall_reliability if audited_analysis.overall_reliability is not None else "N/A",
    )

    result = ZerdePipelineResult(
        doc_state=doc_state,
        query_plan=query_plan,
        claims=claims,
        raw_chunks=raw_chunks,
        fused_chunks=fused_chunks,
        analysis=audited_analysis,
        report_md=report_md,
        report_path=report_path,
        elapsed_seconds=total_elapsed,
    )
    return result
