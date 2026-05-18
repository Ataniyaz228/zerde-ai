"""
ЗЕРДЕ v6.2 — Pipeline Orchestrator
Связывает все 7 этапов в единый асинхронный пайплайн.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from zerde.config import get_settings
from zerde.models import AnalysisJSON, DocumentState, EvidenceChunk, QueryPlan
from zerde.stages.s1_ingest import ingest_document
from zerde.stages.s2_planner import build_query_plan
from zerde.stages.s3_gather import gather_evidence
from zerde.stages.s4_fusion import fuse_and_validate
from zerde.stages.s5_analyst import run_analyst
from zerde.stages.s6_auditor import audit_analysis
from zerde.stages.s7_render import render_report

logger = logging.getLogger(__name__)


class ZerdePipelineResult(dict):
    """
    Контейнер для результатов полного пайплайна.
    Доступ: result.doc_state, result.analysis, result.report_path, etc.
    """

    doc_state: DocumentState
    query_plan: QueryPlan
    raw_chunks: list[EvidenceChunk]
    fused_chunks: list[EvidenceChunk]
    analysis: AnalysisJSON
    report_md: str
    report_path: str
    elapsed_seconds: float


async def run_pipeline(
    file_path: str | Path,
    output_path: str | Path | None = None,
) -> ZerdePipelineResult:
    """
    Запускает полный пайплайн ЗЕРДЕ v6.2 от файла до Markdown-отчёта.

    Args:
        file_path: Путь к входному документу (PDF/DOCX/TXT).
        output_path: Путь для сохранения отчёта (опционально).

    Returns:
        ZerdePipelineResult с результатами всех этапов.
    """
    settings = get_settings()
    start_time = time.perf_counter()

    logger.info("=" * 60)
    logger.info("ЗЕРДЕ v6.2 — Pipeline Start")
    logger.info(f"Input: {file_path}")
    logger.info("=" * 60)

    # ─── ЭТАП 1: Document Ingestion ───────────────────────────────────────
    t1 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 1: Document Ingestion")
    doc_state = await ingest_document(file_path)
    logger.info(f"[Pipeline] ✓ Stage 1 done ({time.perf_counter() - t1:.2f}s)")

    # ─── ЭТАП 2: LLM Planner ─────────────────────────────────────────────
    t2 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 2: LLM Planner")
    query_plan = await build_query_plan(doc_state)
    logger.info(f"[Pipeline] ✓ Stage 2 done ({time.perf_counter() - t2:.2f}s)")

    # ─── ЭТАП 3: Data Gathering ───────────────────────────────────────────
    t3 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 3: Evidence Gathering")
    raw_chunks = await gather_evidence(query_plan)
    logger.info(f"[Pipeline] ✓ Stage 3 done ({time.perf_counter() - t3:.2f}s) — {len(raw_chunks)} chunks")

    # ─── ЭТАП 4: Fusion & Validation ─────────────────────────────────────
    t4 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 4: Fusion & Conflict Detection")
    fused_chunks = await fuse_and_validate(raw_chunks)
    active_chunks = [c for c in fused_chunks if not c.is_duplicate]
    logger.info(f"[Pipeline] ✓ Stage 4 done ({time.perf_counter() - t4:.2f}s) — {len(active_chunks)} active")

    # ─── ЭТАП 5: LLM Analyst ─────────────────────────────────────────────
    t5 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 5: LLM Analyst")
    analysis = await run_analyst(active_chunks, query_plan)
    logger.info(f"[Pipeline] ✓ Stage 5 done ({time.perf_counter() - t5:.2f}s)")

    # ─── ЭТАП 6: Auditor ─────────────────────────────────────────────────
    t6 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 6: Auditor Validation")
    audited_analysis = audit_analysis(analysis, active_chunks)
    logger.info(f"[Pipeline] ✓ Stage 6 done ({time.perf_counter() - t6:.2f}s)")

    # ─── ЭТАП 7: Render ──────────────────────────────────────────────────
    t7 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 7: Report Rendering")
    report_md = await render_report(audited_analysis, active_chunks, output_path)
    report_path = str(output_path) if output_path else "output/zerde_report_*.md"
    logger.info(f"[Pipeline] ✓ Stage 7 done ({time.perf_counter() - t7:.2f}s)")

    total_elapsed = time.perf_counter() - start_time

    logger.info("=" * 60)
    logger.info(f"ЗЕРДЕ v6.2 — Pipeline Complete ({total_elapsed:.2f}s)")
    logger.info(
        f"Facts: {len(audited_analysis.facts)} | "
        f"Validated: {audited_analysis.validated_facts_count} | "
        f"Reliability: {audited_analysis.overall_reliability or 'N/A'}"
    )
    logger.info("=" * 60)

    result = ZerdePipelineResult(
        doc_state=doc_state,
        query_plan=query_plan,
        raw_chunks=raw_chunks,
        fused_chunks=fused_chunks,
        analysis=audited_analysis,
        report_md=report_md,
        report_path=report_path,
        elapsed_seconds=total_elapsed,
    )
    return result
