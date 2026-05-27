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
from pathlib import Path

from zerde.config import get_settings
from zerde.models import (
    AdiletFallbackStrategy,
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
from zerde.stages.s3_gather import gather_evidence
from zerde.stages.s4_fusion import fuse_and_validate
from zerde.stages.s5_5_analyst import run_policy_analyst
from zerde.stages.s5_5_verifier import verify_contradictions
from zerde.stages.s5_analyst import run_auditor
from zerde.stages.s6_auditor import audit_analysis
from zerde.stages.s7_render import render_report
from zerde.utils.law_registry import get_registry

logger = logging.getLogger(__name__)


class ZerdePipelineResult(dict):
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

    def __getattr__(self, name: str) -> any:
        if name.startswith("_"):
            raise AttributeError(f"'ZerdePipelineResult' object has no attribute '{name}'")
        import typing
        hints = typing.get_type_hints(type(self))
        if name not in hints:
            raise AttributeError(f"'ZerdePipelineResult' object has no attribute '{name}'")
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'ZerdePipelineResult' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
            return
        import typing
        hints = typing.get_type_hints(type(self))
        if name not in hints:
            raise AttributeError(f"Cannot set invalid attribute '{name}' on 'ZerdePipelineResult'")
        self[name] = value


async def run_pipeline(
    file_path: str | Path,
    output_path: str | Path | None = None,
) -> ZerdePipelineResult:
    """
    Запускает полный пайплайн от файла до Markdown-отчёта.

    Архитектура:
      S1 → S2 → S2.5 → S3 → S4 → S5 → S6 → S7

    Args:
        file_path: Путь к входному документу (PDF/DOCX/TXT).
        output_path: Путь для сохранения отчёта (опционально).

    Returns:
        ZerdePipelineResult с результатами всех этапов.
    """
    get_settings()
    start_time = time.perf_counter()

    logger.info("=" * 60)
    logger.info("Pipeline Start")
    logger.info(f"Input: {file_path}")
    logger.info("=" * 60)

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
    raw_chunks = await gather_evidence(query_plan)
    logger.info(f"[Pipeline] ✓ Stage 3 done ({time.perf_counter() - t3:.2f}s) — {len(raw_chunks)} chunks")

    # ─── ЭТАП 3.5: Local RAG Injection ─────────────────────────────
    # Прямой запрос локальных чанков для всех law_ids из плана.
    # Гарантирует что локальные чанки попадают в S5/S6 даже если adilet.zan.kz HTTP недоступен.
    #
    # v2: Используем ПРЯМОЙ SQL-запрос по метаданным (law_id + article) вместо
    #     search_local, который через reranker отсеивает нужные статьи.
    try:
        from zerde.config import get_settings as _gs
        from zerde.utils.cache import CacheManager as _CM
        registry = get_registry()
        _cache_for_rag = _CM(_gs().cache_db_path)
        existing_chunk_ids = {c.chunk_id for c in raw_chunks}

        # Собираем все уникальные law_ids из всех adilet запросов, резолвим через реестр
        all_law_ids: list[str] = []
        all_articles: list[str] = []
        for aq in query_plan.adilet_queries:
            for lid in (aq.law_ids or []):
                resolved = registry.resolve(lid)
                if resolved and resolved not in all_law_ids:
                    all_law_ids.append(resolved)
            for art in (aq.articles or []):
                if art and art not in all_articles:
                    all_articles.append(art)

        if all_law_ids:
            logger.info(f"[Pipeline/S3.5] Прямой RAG-запрос для law_ids={all_law_ids} articles={all_articles}")

            # ── Шаг A: Прямой SQL по метаданным (гарантированное попадание) ──
            import json as _json
            import sqlite3 as _sqlite3
            injected = 0
            with _cache_for_rag._conn() as conn:
                for lid in all_law_ids:
                    if all_articles:
                        # Для каждой статьи — отдельный запрос чтобы не пропустить ни одну
                        for art in all_articles:
                            rows = conn.execute(
                                """SELECT chunk_json FROM evidence_cache 
                                   WHERE json_extract(chunk_json, '$.law_id') = ?
                                   AND json_extract(chunk_json, '$.article') = ?""",
                                (lid, art)
                            ).fetchall()
                            for r in rows:
                                try:
                                    chunk = EvidenceChunk.model_validate(_json.loads(r["chunk_json"]))
                                    if chunk.chunk_id not in existing_chunk_ids:
                                        chunk.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                                        raw_chunks.append(chunk)
                                        existing_chunk_ids.add(chunk.chunk_id)
                                        injected += 1
                                except Exception:
                                    pass
                    else:
                        # Без фильтра по статьям — все чанки закона (limit 50)
                        rows = conn.execute(
                            """SELECT chunk_json FROM evidence_cache 
                               WHERE json_extract(chunk_json, '$.law_id') = ?
                               LIMIT 50""",
                            (lid,)
                        ).fetchall()
                        for r in rows:
                            try:
                                chunk = EvidenceChunk.model_validate(_json.loads(r["chunk_json"]))
                                if chunk.chunk_id not in existing_chunk_ids:
                                    chunk.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                                    raw_chunks.append(chunk)
                                    existing_chunk_ids.add(chunk.chunk_id)
                                    injected += 1
                            except Exception:
                                pass

            # ── Шаг B: Дополнительный семантический поиск (search_local) ──
            # Ловит чанки без точных метаданных, но семантически релевантные
            rag_tasks = [
                _cache_for_rag.search_local(
                    query_text=aq.query_text,
                    law_ids=[registry.resolve(lid) for lid in (aq.law_ids or [])],
                    limit=15,
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


    # ─── ЭТАП 4: Fusion & Validation ─────────────────────────────────────
    t4 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 4: Fusion & Conflict Detection")
    fused_chunks = await fuse_and_validate(raw_chunks)
    active_chunks = [c for c in fused_chunks if not c.is_duplicate]
    logger.info(f"[Pipeline] ✓ Stage 4 done ({time.perf_counter() - t4:.2f}s) — {len(active_chunks)} active")

    # ─── ЭТАП 5 + 5.2: LLM Auditor & Contradiction Verifier (BATCHED & PARALLEL) ────────────────────────────
    t5 = time.perf_counter()
    logger.info("[Pipeline] ► Stage 5 + 5.2: LLM Auditor & Contradiction Verifier (batched & parallel)")
    
    # Разделяем claims на батчи по 5 штук
    claim_items = list(claims.claims)
    batch_size = 5
    batches = [claim_items[i:i + batch_size] for i in range(0, len(claim_items), batch_size)]
    
    if not batches:
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
        async def _process_batch(batch_claims: list[DocumentClaim]) -> AnalysisJSON:
            batch_result = ClaimExtractionResult(
                doc_id=claims.doc_id,
                claims=batch_claims,
                structural_claims=[],
            )
            # Запускаем аудитора для батча
            batch_analysis = await run_auditor(
                chunks=active_chunks,
                plan=query_plan,
                claims=batch_result,
                doc_text=doc_state.normalized_text,
            )
            # Сразу верифицируем противоречия для этого батча
            batch_analysis = await verify_contradictions(batch_analysis, active_chunks)
            return batch_analysis

        batch_results = await asyncio.gather(*[_process_batch(b) for b in batches])
        
        # Объединяем результаты всех батчей
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
            llm_model_used=batch_results[0].llm_model_used if batch_results else get_settings().llm_model_analyst,
            verdicts=[],
        )
        
        seen_cons = set()
        for br in batch_results:
            analysis.verdicts.extend(br.verdicts)
            analysis.facts.extend(br.facts)
            analysis.conclusions.extend(br.conclusions)
            analysis.negative_space.extend(br.negative_space)
            analysis.normative.extend(br.normative)
            for con in br.cons:
                if con not in seen_cons:
                    analysis.cons.append(con)
                    seen_cons.add(con)

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
        f"Reliability: {audited_analysis.overall_reliability or 'N/A'}"
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
