"""
Stage 5: The Auditor (Claim-based Verification)
Вход:  list[EvidenceChunk] + QueryPlan + ClaimExtractionResult
Выход: AnalysisJSON с verdicts на каждый claim

Особенности:
  - Режим AUDITOR: проверяет чеклист claims, не пересказывает корпус
  - Детерминированные вердикты из reference_data (без LLM)
  - LLM получает отсортированный чеклист с 🔴 CRITICAL первыми
  - Verdict Validator: проверяет что все claim_id получили вердикт
  - temperature=0, response_format=json_object
  - tenacity retry (3 попытки)
"""

from __future__ import annotations

import json
import logging

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

logger = logging.getLogger(__name__)

# Максимум символов в промпте Аналитика — DeepSeek/GPT-4o укладывается в 128K
_MAX_PROMPT_CHARS = 120_000

# Макс чанков в финальном корпусе для аналитика
_MAX_CORPUS_CHUNKS = 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_auditor(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    doc_text: str = "",
) -> AnalysisJSON:
    """Этап 5 v2: LLM Auditor в режиме claim-by-claim верификации.

    НОВАЯ ПАРАДИГМА: Auditor, не Summarizer.
    Получает чеклист claims и проверяет каждый против корпуса.

    Args:
        chunks: Активные чанки из Stage 4.
        plan: План запросов из Stage 2.
        claims: Результат Stage 2.5 (DocumentClaim[]).
        doc_text: Текст исходного документа.
    """
    settings = get_settings()
    client = make_llm_client(settings)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_ids = [c.chunk_id for c in active if c.is_conflict]

    logger.info(
        f"[S5/Auditor] Start. chunks={len(active)} claims={claims.total_count} "
        f"critical={len(claims.critical_claims)}"
    )

    # BM25 ранкинг — топ чанки наиболее релевантные к документу
    if len(active) > _MAX_CORPUS_CHUNKS:
        active = _rank_by_relevance(active, doc_text or plan.plan_id, conflict_ids)
        logger.info(f"[S5/Auditor] Corpus trimmed to {len(active)} chunks")

    # Строим auditor prompt (чеклист-режим)
    prompt = build_auditor_prompt(
        chunks=active,
        claims=claims,
        plan=plan,
        doc_text=doc_text,
    )

    # system_msg: минимальный (не повторяем правила из user-промпта)
    system_msg = (
        "JSON. Аудитор юр. документов РК. Вердикт на каждый claim. "
        "ОшИБКИ = наивысший приоритет. Без пояснений вне JSON."
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    # TTL=86400: корпус может обновиться через день
    raw_json = await cached_llm_call(
        client=client,
        model=settings.llm_model_analyst,
        messages=messages,
        settings=settings,
        ttl_seconds=86400,
        max_tokens=settings.llm_max_tokens_analyst,
    )
    _save_raw_response(raw_json)

    # Парсим вердикты
    analysis = _parse_auditor_response(raw_json, plan, claims, settings.llm_model_analyst)

    # Verdict Validator: проверяем что все claim_id получили вердикт
    _validate_claim_coverage(analysis, claims)

    contradicted = [v for v in analysis.verdicts if v.status == VerdictStatus.CONTRADICTED]
    logger.info(
        f"[S5/Auditor] Done. verdicts={len(analysis.verdicts)} "
        f"contradicted={len(contradicted)} unverified={sum(1 for v in analysis.verdicts if v.status == VerdictStatus.UNVERIFIED)}"
    )
    for v in contradicted:
        logger.warning(
            f"[S5/Auditor] CONTRADICTION {v.claim_id}: "
            f"doc='{v.document_value}' vs found='{v.found_value}'"
        )

    return analysis


def _save_raw_response(data: dict) -> None:
    """Сохраняет сырой JSON на случай падения парсера для отладки без траты токенов."""
    try:
        with open("last_analyst_raw_response.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("[S5] Raw Analyst JSON saved to last_analyst_raw_response.json")
    except Exception as e:
        logger.warning(f"[S5] Failed to save raw response: {e}")


# ---------------------------------------------------------------------------
# LLM Call
# ---------------------------------------------------------------------------


def _safe_str_list(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v) for v in val if v]


def _rank_by_relevance(
    chunks: list[EvidenceChunk],
    doc_text: str,
    conflict_ids: list[str],
    top_k: int = 60,
) -> list[EvidenceChunk]:
    """
    BM25-ранкинг чанков по тексту документа.

    Composite score = BM25(chunk | doc_text) + legal_rank_bonus + conflict_bonus
    Adilet-чанки всегда попадают в топ.

    Args:
        chunks: Активные чанки (не дублики).
        doc_text: Текст анализируемого документа (query для BM25).
        conflict_ids: ID конфликтных чанков (форсированно включаются).
        top_k: Максимум чанков на выходе.

    Returns:
        Отсортированный список топ-K чанков.
    """
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("[S5/Ranker] rank_bm25 не установлен, пропускаем ранкинг")
        return chunks[:top_k]

    if not chunks:
        return chunks

    # Токенизация корпуса
    tokenized_corpus = [c.content.lower().split() for c in chunks]
    bm25 = BM25Okapi(tokenized_corpus)

    # Query = текст документа (первые 3000 символов — достаточно для ранкинга)
    query_tokens = doc_text[:3000].lower().split()
    bm25_scores = bm25.get_scores(query_tokens)

    # Composite score
    conflict_id_set = set(conflict_ids)
    scored: list[tuple[EvidenceChunk, float]] = []

    for chunk, bm25_score in zip(chunks, bm25_scores):
        # Бонус за юридический ранг: чем выше ранг (меньше число) — тем больше бонус
        rank_bonus = max(0, (12 - int(chunk.legal_rank)) * 8)

        # Бонус за конфликтность
        conflict_bonus = 20.0 if chunk.chunk_id in conflict_id_set else 0.0

        # Бонус за Adilet-источник (всегда в топе)
        adilet_bonus = 50.0 if chunk.adilet_fallback_used is not None else 0.0

        total = bm25_score + rank_bonus + conflict_bonus + adilet_bonus
        scored.append((chunk, total))

    scored.sort(key=lambda x: x[1], reverse=True)

    result = [c for c, _ in scored[:top_k]]

    # Форсируем конфликтные чанки которые могли выпасть
    result_ids = {c.chunk_id for c in result}
    forced = [c for c in chunks if c.chunk_id in conflict_id_set and c.chunk_id not in result_ids]
    result.extend(forced[:5])  # максимум +5 конфликтных

    logger.info(
        f"[S5/Ranker] {len(chunks)} → {len(result)} chunks "
        f"(top_k={top_k}, forced_conflicts={len(forced)})"
    )
    return result


# ---------------------------------------------------------------------------
# Auditor Response Parser
# ---------------------------------------------------------------------------


def _parse_auditor_response(
    raw: dict,
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    model: str,
) -> AnalysisJSON:
    """Парсит ответ LLM в режиме Auditor и создаёт AnalysisJSON с verdicts."""
    import uuid as _uuid

    analysis_id = str(_uuid.uuid4())

    verdicts: list[ClaimVerdict] = []
    claimed_ids = {c.claim_id for c in claims.claims}
    claim_map = {c.claim_id: c for c in claims.claims}

    # Сначала добавляем детерминированные вердикты (из reference_data — без LLM)
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

    # Парсим LLM вердикты
    deterministic_ids = {v.claim_id for v in verdicts}
    seen_verdict_ids = set(deterministic_ids)
    for item in raw.get("verdicts", []):
        if not isinstance(item, dict):
            continue
        claim_id = item.get("claim_id", "")
        if not claim_id or claim_id not in claimed_ids:
            continue
        if claim_id in seen_verdict_ids:
            # Детерминированный вердикт надёжнее — дополняем source_ids из LLM
            if claim_id in deterministic_ids:
                llm_sources = _safe_str_list(item.get("source_ids", []))
                for v in verdicts:
                    if v.claim_id == claim_id:
                        v.source_ids = list(set(v.source_ids + llm_sources))
            continue

        seen_verdict_ids.add(claim_id)

        raw_status = str(item.get("status", "UNVERIFIED")).upper().strip()
        if any(syn in raw_status for syn in ["OPROVERG", "CONTRADIC", "FALSE"]):
            status = VerdictStatus.CONTRADICTED
        elif any(syn in raw_status for syn in ["PODTVERZH", "CONFIRM", "TRUE"]):
            status = VerdictStatus.CONFIRMED
        else:
            status = VerdictStatus.UNVERIFIED

        orig_claim = claim_map.get(claim_id)
        claim_severity = orig_claim.severity if orig_claim else ClaimSeverity.MEDIUM

        verdicts.append(ClaimVerdict(
            claim_id=claim_id,
            status=status,
            source_ids=_safe_str_list(item.get("source_ids", [])),
            found_value=item.get("found_value"),
            document_value=item.get("document_value"),
            contradiction_detail=item.get("contradiction_detail"),
            confidence=item.get("confidence", "MEDIUM"),
            severity=claim_severity,
            is_deterministic=False,
        ))

    # Конвертируем вердикты в Facts для совместимости с рендером
    facts: list[Fact] = []
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
            fact_id=f"fact_{len(facts):04d}",
            claim_id=v.claim_id,
            claim=claim_text,
            source_ids=v.source_ids if v.source_ids else ["UNLINKED"],
            confidence=confidence,
        ))

    # Дополнительные находки (пробелы регулирования) — дедуплицированные
    neg_space: list[NegativeSpaceItem] = []
    seen_neg: set[str] = set()
    for item in raw.get("additional_findings", []):
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        if not desc or desc[:60] in seen_neg:
            continue
        seen_neg.add(desc[:60])
        gap_type = item.get("gap_type", "regulatory_hole")
        if gap_type not in {"regulatory_hole", "intentional_silence", "delegation_gap"}:
            gap_type = "regulatory_hole"
        neg_space.append(NegativeSpaceItem(
            item_id=f"neg_{len(neg_space):04d}",
            description=desc,
            gap_type=gap_type,  # type: ignore[arg-type]
            affected_domain=str(item.get("affected_domain", "")),
            source_ids=[],
        ))

    contradictions = [v for v in verdicts if v.status == VerdictStatus.CONTRADICTED]

    cons = raw.get("cons", [])
    for v in contradictions:
        if v.contradiction_detail:
            cons.append(f"{v.claim_id}: {v.contradiction_detail[:200]}")

    return AnalysisJSON(
        analysis_id=analysis_id,
        source_doc_id=plan.source_doc_id,
        plan_id=plan.plan_id,
        facts=facts,
        conclusions=[],
        negative_space=neg_space,
        normative=[],
        custom_pros=[],  # Empty for dynamic computed_field evaluation in v9.4
        cons=cons if isinstance(cons, list) else [str(cons)],
        affected_parties=[],
        custom_recommendation="",  # Empty for dynamic computed_field evaluation in v9.4
        conflict_chunk_ids_referenced=[],
        llm_model_used=model,
        verdicts=verdicts,
    )


def _validate_claim_coverage(analysis: AnalysisJSON, claims: ClaimExtractionResult) -> None:
    """Verdict Validator: проверяет что каждый claim получил вердикт."""
    verdict_ids = {v.claim_id for v in analysis.verdicts}
    missing = [c for c in claims.claims if c.claim_id not in verdict_ids]
    if missing:
        logger.warning(
            f"[S5/Validator] {len(missing)} claims без вердикта! "
            f"IDs: {[c.claim_id for c in missing[:5]]}"
        )
        for claim in missing:
            analysis.verdicts.append(ClaimVerdict(
                claim_id=claim.claim_id,
                status=VerdictStatus.UNVERIFIED,
                source_ids=[],
                document_value=", ".join(claim.entities) or claim.claim_text[:100],
                confidence="LOW",
                severity=claim.severity,
                is_deterministic=False,
            ))
            analysis.facts.append(Fact(
                fact_id=f"fact_{len(analysis.facts):04d}",
                claim_id=claim.claim_id,
                claim=f"[{claim.claim_id}]: {claim.claim_text[:150]}",
                source_ids=["UNLINKED"],
                confidence=0.3,
            ))
    else:
        logger.info(f"[S5/Validator] Все {len(claims.claims)} claims получили вердикты")
