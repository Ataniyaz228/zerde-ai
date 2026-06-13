"""
Pipeline Orchestrator
Связывает все этапы в единый асинхронный пайплайн.

Этапы:
  - Stage 2.5: Claim Extractor (гибридный regex + LLM)
  - Stage 5: run_auditor() вместо run_analyst() — claim-by-claim верификация
  - reference_data.py: детерминированные вердикты без LLM
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
from zerde.stages.s5_5_analyst import run_policy_analyst
from zerde.stages.s5_5_verifier import verify_contradictions
from zerde.stages.s5_analyst import run_auditor
from zerde.stages.s6_auditor import audit_analysis
from zerde.stages.s7_render import render_report
from zerde.utils.cache import CacheManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ZerdePipelineResult:
    """
    Контейнер для результатов полного пайплайна.
    Доступ: result.doc_state, result.analysis, result.report_path, etc.
    """

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
    """Лёгкое уведомление о смене этапа пайплайна для вызывающей стороны (backend)."""

    stage: str
    message: str


def _emit_progress(
    progress: Callable[[ProgressEvent], None] | None,
    ev: ProgressEvent,
) -> None:
    """Вызывает progress(ev), если callback передан."""
    if progress is not None:
        progress(ev)


async def run_pipeline(
    file_path: str | Path,
    output_path: str | Path | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
) -> ZerdePipelineResult:
    """
    Запускает полный пайплайн от файла до Markdown-отчёта.

    Архитектура:
      S1 → S2 → S2.5 → S3 → S4 → S5 → S6 → S7

    Args:
        file_path: Путь к входному документу (PDF/DOCX/TXT).
        output_path: Путь для сохранения отчёта (опционально).
        progress: Опциональный callback, вызываемый синхронно при смене этапа
            (extract → search → verify → report) — для прогресс-баров (WS).

    Returns:
        ZerdePipelineResult с результатами всех этапов.
    """
    get_settings()
    start_time = time.perf_counter()

    logger.info("=" * 60)
    logger.info("Pipeline Start")
    logger.info(f"Input: {file_path}")
    logger.info("=" * 60)
    _emit_progress(progress, ProgressEvent("extract", "Загрузка и извлечение тезисов"))

    # ─── ЭТАП 1: Document Ingestion ───────────────────────────────────────
    t1 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 1: Document Ingestion")
    doc_state = await ingest_document(file_path)
    logger.info(f"[Pipeline] ✓ Stage 1 done ({time.perf_counter() - t1:.2f}s) — {doc_state.char_count} chars")

    # ─── ЭТАП 2 + 2.5 + 2.7: LLM Planner, Claim Extractor и Self-Check (ПАРАЛЛЕЛЬНО) ────────────
    t2 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 2 + 2.5 + 2.7: LLM Planner, Claim Extractor и Self-Check (параллельно)")
    query_plan, claims, selfcheck_claims = await asyncio.gather(
        build_query_plan(doc_state),
        extract_claims(doc_state),
        asyncio.to_thread(run_self_check, doc_state.normalized_text),
    )
    if selfcheck_claims:
        claims.claims.extend(selfcheck_claims)
        logger.info(f"[Pipeline] ✓ Stage 2.7 — {len(selfcheck_claims)} internal contradictions added")
    elapsed_2 = time.perf_counter() - t2
    logger.info(
        f"[Pipeline] ✓ Stage 2 done — {query_plan.total_queries} queries | "
        f"Stage 2.5 + 2.7 done — {claims.total_count} claims ({len(claims.critical_claims)} critical) "
        f"| время: {elapsed_2:.2f}s"
    )

    # ─── ЭТАП 3: Data Gathering ───────────────────────────────────────────
    t3 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 3: Evidence Gathering")
    _emit_progress(progress, ProgressEvent("search", "Поиск в базе НПА Казахстана"))
    raw_chunks = await gather_evidence(query_plan)
    logger.info(f"[Pipeline] ✓ Stage 3 done ({time.perf_counter() - t3:.2f}s) — {len(raw_chunks)} chunks")

    # ─── ЭТАП 3.5 + 3.6: Local RAG / Claim-driven Injection ────────────────
    rag_cache = CacheManager(get_settings().cache_db_path)
    raw_chunks = await inject_local_rag(raw_chunks, query_plan, cache=rag_cache)
    raw_chunks = inject_claim_driven(raw_chunks, claims, cache=rag_cache)

    # ─── ЭТАП 4: Fusion & Validation ─────────────────────────────────────
    t4 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 4: Fusion & Conflict Detection")

    # Language filtering removed as it was dropping cross-lingual web results
    fused_chunks = await fuse_and_validate(raw_chunks)
    active_chunks = [c for c in fused_chunks if not c.is_duplicate]
    logger.info(f"[Pipeline] ✓ Stage 4 done ({time.perf_counter() - t4:.2f}s) — {len(active_chunks)} active")

    # ─── ЭТАП 5 + 5.2: LLM Auditor & Contradiction Verifier ────────────────────────────
    t5 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 5 + 5.2: LLM Auditor & Contradiction Verifier")
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

    # V7.0: Переносим структурные claims в analysis для рендеринга
    analysis.structural_claims = claims.structural_claims
    from zerde.stages.s5_analyst import _validate_claim_coverage
    _validate_claim_coverage(analysis, claims)

    contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    logger.info(
        f"[Pipeline] ✓ Stage 5 + 5.2 done ({time.perf_counter() - t5:.2f}s) — "
        f"verdicts={len(analysis.verdicts)} contradicted={contradicted} "
        f"structural={len(claims.structural_claims)}"
    )

    # ─── ЭТАП 5.5 + 6: Policy Analyst и BM25 Audit (ПАРАЛЛЕЛЬНО) ──────
    t56 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 5.5 + 6: Policy Analyst ∥ BM25 Audit (параллельно)")

    # C2 Fix: Избегаем in-place мутаций разделяемого объекта analysis и chunks.
    # Создаем изолированные глубокие копии для обеих параллельных ветвей (S5.5 и S6).
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
        f"[Pipeline] ✓ Stage 5.5+6 done ({time.perf_counter() - t56:.2f}s) — "
        f"policy={'✓' if policy_analysis else '✗'}"
    )

    # ─── ЭТАП 7: Render ──────────────────────────────────────────────────
    t7 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 7: Report Rendering")
    _emit_progress(progress, ProgressEvent("report", "Формирование отчёта"))
    report_md = await render_report(audited_analysis, active_chunks, output_path, policy_analysis)
    report_path = str(output_path) if output_path else "output/zerde_report_*.md"
    logger.info(f"[Pipeline] ✓ Stage 7 done ({time.perf_counter() - t7:.2f}s)")

    total_elapsed = time.perf_counter() - start_time

    logger.info("=" * 60)
    logger.info(f"Pipeline Complete ({total_elapsed:.2f}s)")
    logger.info(
        f"Claims: {claims.total_count} | "
        f"Verdicts: {len(audited_analysis.verdicts)} | "
        f"Contradicted: {contradicted} | "
        f"Reliability: {audited_analysis.overall_reliability if audited_analysis.overall_reliability is not None else 'N/A'}"
    )
    logger.info("=" * 60)

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
