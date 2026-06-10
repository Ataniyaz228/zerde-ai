"""
Stage 5: The Auditor (Claim-based Verification)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimVerdict,
    EvidenceChunk,
    Fact,
    NegativeSpaceItem,
    QueryPlan,
    VerdictStatus,
)
from zerde.prompts.auditor import build_auditor_prompt
from zerde.utils.llm_client import cached_llm_call, make_llm_client

if TYPE_CHECKING:
    from zerde.utils.cache import CacheManager

logger = logging.getLogger(__name__)


async def run_auditor(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    doc_text: str = "",
) -> AnalysisJSON:
    settings = get_settings()
    client = make_llm_client(settings)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_ids = [c.chunk_id for c in active if c.is_conflict]

    batch_size = 5
    claim_list = claims.claims
    batches = [claim_list[i:i + batch_size] for i in range(0, len(claim_list), batch_size)]

    # BM25-индекс по `active` не зависит от батча — строим один раз и
    # переиспользуем в _retrieve_for_claims, вместо перетокенизации всего корпуса
    # на каждой итерации.
    retrieval_index = _build_retrieval_index(active)

    async def _run_batch(batch_claims_list: list) -> AnalysisJSON:
        batch_claims = ClaimExtractionResult(
            doc_id=claims.doc_id,
            claims=batch_claims_list,
        )

        # Per-claim retrieval: контекст этого батча = объединение релевантных
        # чанков КАЖДОГО claim'а (точное совпадение law_id+article + BM25 по
        # тексту claim'а) + forced (adilet/conflict). Раньше во все батчи слался
        # один и тот же doc-level top-30 — чанк-первоисточник для claim'а №38 мог
        # вообще не попасть в контекст.
        batch_chunks = _retrieve_for_claims(active, batch_claims_list, conflict_ids, retrieval_index=retrieval_index)

        prompt, id_map = build_auditor_prompt(
            chunks=batch_chunks,
            claims=batch_claims,
            plan=plan,
            doc_text=doc_text,
        )

        system_msg = (
            "JSON. Аудитор юр. документов РК. Вердикт на каждый claim. "
            "ОШИБКИ = наивысший приоритет. Без пояснений вне JSON."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ]

        raw_json = await cached_llm_call(
            client=client,
            model=settings.llm_model_analyst,
            messages=messages,
            settings=settings,
            ttl_seconds=86400,
            max_tokens=settings.llm_max_tokens_analyst,
        )

        analysis = _parse_auditor_response(raw_json, plan, batch_claims, settings.llm_model_analyst)
        # Короткие ID источников (S1..) из ответа → полные chunk_id (по карте батча).
        _remap_source_ids(analysis, id_map)
        return analysis

    # Батчи независимы (свой prompt/id_map/claims) → шлём параллельно. Глобальный
    # семафор в llm_client (5) ограничивает фактическую конкурентность LLM-вызовов.
    all_analysis_jsons = list(await asyncio.gather(*(_run_batch(b) for b in batches)))

    if not all_analysis_jsons:
        return AnalysisJSON(
            analysis_id="empty",
            source_doc_id=plan.source_doc_id,
            plan_id=plan.plan_id,
            facts=[],
            conclusions=[],
            negative_space=[],
            normative=[],
            cons=[],
            llm_model_used=settings.llm_model_analyst,
            verdicts=[]
        )

    final_analysis = all_analysis_jsons[0]
    for a in all_analysis_jsons[1:]:
        final_analysis.facts.extend(a.facts)
        final_analysis.conclusions.extend(a.conclusions)
        final_analysis.negative_space.extend(a.negative_space)
        final_analysis.normative.extend(a.normative)
        final_analysis.cons.extend(a.cons)
        final_analysis.verdicts.extend(a.verdicts)

    # Дедуп пробелов регулирования по описанию: разные батчи (по 5 claim) могут
    # независимо вернуть одну и ту же находку.
    if final_analysis.negative_space:
        seen_desc: set[str] = set()
        uniq_ns = []
        for ns in final_analysis.negative_space:
            key = ns.description.strip().lower()[:120]
            if key in seen_desc:
                continue
            seen_desc.add(key)
            uniq_ns.append(ns)
        final_analysis.negative_space = uniq_ns

    _validate_claim_coverage(final_analysis, claims)
    return final_analysis


def _parse_auditor_response(
    raw: dict,
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    model: str,
) -> AnalysisJSON:
    import uuid as _uuid
    analysis_id = str(_uuid.uuid4())

    verdicts: list[ClaimVerdict] = []
    claimed_ids = {c.claim_id for c in claims.claims}
    claim_map = {c.claim_id: c for c in claims.claims}

    # Детерминированные вердикты
    for claim in claims.claims:
        if claim.deterministic_verdict:
            status = claim.deterministic_status or VerdictStatus.UNVERIFIED
            is_error = status == VerdictStatus.CONTRADICTED
            verdicts.append(ClaimVerdict(
                claim_id=claim.claim_id,
                status=status,
                source_ids=["reference_data"],
                found_value=claim.deterministic_verdict,
                document_value=", ".join(claim.entities),
                contradiction_detail=claim.deterministic_verdict if is_error else None,
                confidence="HIGH",
                severity=claim.severity,
                is_deterministic=True,
            ))

    deterministic_ids = {v.claim_id for v in verdicts}
    seen_verdict_ids = set(deterministic_ids)
    for item in raw.get("verdicts", []):
        claim_id = item.get("claim_id", "")
        if not claim_id or claim_id not in claimed_ids or claim_id in seen_verdict_ids:
            continue
        seen_verdict_ids.add(claim_id)

        raw_status = str(item.get("status", "UNVERIFIED")).upper()
        if "CONTRADIC" in raw_status or "OPROVERG" in raw_status:
            status = VerdictStatus.CONTRADICTED
        elif "CONFIRM" in raw_status or "PODTVERZH" in raw_status:
            status = VerdictStatus.CONFIRMED
        else:
            status = VerdictStatus.UNVERIFIED

        orig_claim = claim_map.get(claim_id)
        claim_severity = orig_claim.severity if orig_claim else ClaimSeverity.MEDIUM

        verdicts.append(ClaimVerdict(
            claim_id=claim_id,
            status=status,
            source_ids=item.get("source_ids", []),
            found_value=item.get("found_value"),
            document_value=item.get("document_value"),
            contradiction_detail=item.get("contradiction_detail"),
            confidence=item.get("confidence", "MEDIUM"),
            severity=claim_severity,
            is_deterministic=False,
        ))

    facts = []
    for v in verdicts:
        if v.status == VerdictStatus.CONTRADICTED:
            claim_text = f"[{v.claim_id}]: документ='{v.document_value}' vs найдено='{v.found_value}'"
            confidence = 0.1
        elif v.status == VerdictStatus.CONFIRMED:
            claim_text = f"[{v.claim_id}]: '{v.found_value or v.document_value}'"
            confidence = 0.9
        else:
            claim_text = f"[{v.claim_id}]: '{v.document_value}'"
            confidence = 0.4
        facts.append(Fact(
            fact_id=f"fact_{v.claim_id}",
            claim_id=v.claim_id,
            claim=claim_text,
            source_ids=v.source_ids if v.source_ids else ["UNLINKED"],
            confidence=confidence,
        ))

    # additional_findings из ответа аудитора → negative_space. Раньше эти находки
    # (пробелы/коллизии регулирования) запрашивались в схеме промпта, но молча
    # отбрасывались парсером — секция «Пробелы регулирования» и n_gaps всегда были
    # пустыми. gap_type вне допустимого множества (напр. "error") → regulatory_hole.
    negative_space: list[NegativeSpaceItem] = []
    _VALID_GAP = {"regulatory_hole", "intentional_silence", "delegation_gap"}
    for idx, finding in enumerate(raw.get("additional_findings", []) or []):
        if not isinstance(finding, dict):
            continue
        desc = str(finding.get("description", "")).strip()
        if not desc:
            continue
        gtype = str(finding.get("gap_type", "regulatory_hole")).strip().lower()
        if gtype not in _VALID_GAP:
            gtype = "regulatory_hole"
        negative_space.append(NegativeSpaceItem(
            item_id=f"gap_{analysis_id[:8]}_{idx:02d}",
            description=desc,
            gap_type=gtype,  # type: ignore[arg-type]
            affected_domain=str(finding.get("affected_domain", "")).strip() or "не указано",
            source_ids=[],
        ))

    return AnalysisJSON(
        analysis_id=analysis_id,
        source_doc_id=plan.source_doc_id,
        plan_id=plan.plan_id,
        facts=facts,
        conclusions=[],
        negative_space=negative_space,
        normative=[],
        cons=raw.get("cons", []),
        llm_model_used=model,
        verdicts=verdicts,
    )


def _validate_claim_coverage(analysis: AnalysisJSON, claims: ClaimExtractionResult) -> None:
    verdict_ids = {v.claim_id for v in analysis.verdicts}
    missing = [c for c in claims.claims if c.claim_id not in verdict_ids]
    for claim in missing:
        analysis.verdicts.append(ClaimVerdict(
            claim_id=claim.claim_id,
            status=VerdictStatus.UNVERIFIED,
            source_ids=[],
            document_value=claim.claim_text[:100],
            confidence="LOW",
            severity=claim.severity,
        ))


def _remap_source_ids(analysis: AnalysisJSON, id_map: dict[str, str]) -> None:
    """Переводит короткие ID источников (S1..) из ответа LLM в полные chunk_id.

    `reference_data`/`UNLINKED` сохраняются как есть. Неизвестные метки (модель
    выдумала S99) отбрасываются — иначе они всё равно не зарезолвятся в S6.
    """
    _keep = {"reference_data", "UNLINKED"}

    def _remap(ids: list[str] | None) -> list[str]:
        out: list[str] = []
        for sid in ids or []:
            if sid in id_map:
                out.append(id_map[sid])
            elif sid in _keep:
                out.append(sid)
        return out

    for v in analysis.verdicts:
        v.source_ids = _remap(v.source_ids)
    for f in analysis.facts:
        f.source_ids = _remap(f.source_ids)


class _RetrievalIndex:
    """BM25-индекс по корпусу батча, переиспользуемый между всеми батчами S5."""

    def __init__(self, chunks: list[EvidenceChunk], cache: CacheManager) -> None:
        self.cache = cache
        corpus_tokens = [
            cache._tokenize_for_bm25((c.content or "") + " " + (c.source_title or ""))
            for c in chunks
        ]
        self.bm25 = None
        if any(corpus_tokens):
            try:
                from rank_bm25 import BM25Okapi
                self.bm25 = BM25Okapi(corpus_tokens)
            except Exception:
                self.bm25 = None


def _build_retrieval_index(chunks: list[EvidenceChunk]) -> _RetrievalIndex | None:
    if not chunks:
        return None
    from zerde.utils.cache import CacheManager
    cache = CacheManager(get_settings().cache_db_path)
    return _RetrievalIndex(chunks, cache)


def _retrieve_for_claims(
    chunks: list[EvidenceChunk],
    claims: list,
    conflict_ids: list[str],
    per_claim_k: int = 8,
    max_total: int = 50,
    retrieval_index: _RetrievalIndex | None = None,
) -> list[EvidenceChunk]:
    """Per-claim retrieval для одного батча аудитора.

    Контекст = forced (adilet/conflict) + точные метаданные-совпадения каждого
    claim'а (law_id+article — это и есть первоисточник, НИКОГДА не отбрасываем)
    + добивка по BM25 (по тексту каждого claim'а) до max_total.

    BM25-индекс строится один раз (см. `_build_retrieval_index`, переиспользуется
    между батчами), затем скорится под текст каждого claim'а (get_scores). Если
    `retrieval_index` не передан, строится на месте (прямые вызовы/тесты).
    """
    if not chunks:
        return []

    from zerde.stages.s6_auditor import (
        _are_law_ids_synonymous,
        _extract_article_from_claim,
        _extract_referenced_law_ids,
    )

    if retrieval_index is None:
        retrieval_index = _build_retrieval_index(chunks)

    selected: dict[str, EvidenceChunk] = {}

    # 1. Forced: adilet + conflict — всегда.
    for c in chunks:
        if c.chunk_id in conflict_ids or c.adilet_fallback_used is not None:
            selected[c.chunk_id] = c

    # 2. Точные метаданные-совпадения каждого claim'а (первоисточник) — всегда.
    for claim in claims:
        ref_ids = _extract_referenced_law_ids(claim)
        article = _extract_article_from_claim(claim)
        if not (ref_ids and article):
            continue
        for c in chunks:
            if c.article == article and any(_are_law_ids_synonymous(c.law_id, r) for r in ref_ids):
                selected[c.chunk_id] = c

    # 3. BM25-добивка по тексту каждого claim'а.
    bm25 = retrieval_index.bm25 if retrieval_index else None
    cache = retrieval_index.cache if retrieval_index else None
    if bm25 is not None and cache is not None:
        for claim in claims:
            if len(selected) >= max_total:
                break
            q = cache._tokenize_for_bm25(
                (getattr(claim, "claim_text", "") or "") + " " + (getattr(claim, "quote", "") or "")
            )
            if not q:
                continue
            scores = bm25.get_scores(q)
            ranked = sorted(range(len(chunks)), key=lambda idx: scores[idx], reverse=True)
            added = 0
            for idx in ranked:
                if added >= per_claim_k or len(selected) >= max_total:
                    break
                if scores[idx] <= 0:
                    break
                c = chunks[idx]
                if c.chunk_id not in selected:
                    selected[c.chunk_id] = c
                    added += 1

    return list(selected.values())


