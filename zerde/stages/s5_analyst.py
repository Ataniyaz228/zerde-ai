"""
ЗЕРДЕ v7.0 — Stage 5: The Auditor (Claim-based Verification)
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
import uuid
from typing import Literal

from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from zerde.config import get_settings
from zerde.models import (
    AffectedParty,
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimVerdict,
    Conclusion,
    EvidenceChunk,
    Fact,
    NegativeSpaceItem,
    NormativeAssessment,
    QueryPlan,
    ValidationStatus,
    VerdictStatus,
)
from zerde.prompts.analyst import build_analyst_prompt
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


async def run_analyst(chunks: list[EvidenceChunk], plan: QueryPlan, doc_text: str = "") -> AnalysisJSON:
    """Этап 5: LLM Analyst генерирует AnalysisJSON.

    Args:
        chunks: Активные чанки из Stage 4.
        plan: План запросов из Stage 2.
        doc_text: Текст исходного документа (для BM25 ранкинга корпуса).
    """
    settings = get_settings()
    client = make_llm_client(settings)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_ids = [c.chunk_id for c in active if c.is_conflict]

    logger.info(
        f"[S5] Analyst start. chunks={len(active)} conflicts={len(conflict_ids)} "
        f"plan={plan.plan_id[:8]}…"
    )

    # BM25 ранкинг по тексту документа — оставляем топ-60
    if len(active) > _MAX_CORPUS_CHUNKS:
        active = _rank_by_relevance(active, doc_text or plan.plan_id, conflict_ids)
        logger.info(f"[S5] Corpus ranked and trimmed to {len(active)} chunks")

    # Если корпус большой — анализируем частями
    if _estimate_prompt_size(active) > _MAX_PROMPT_CHARS:
        logger.info("[S5] Large corpus detected. Using chunked analysis.")
        analysis = await _run_chunked_analysis(active, plan, conflict_ids, client, settings)
    else:
        prompt = build_analyst_prompt(active, plan, conflict_ids, doc_text=doc_text)
        raw_json = await _call_analyst_with_retry(client, prompt, settings)
        _save_raw_response(raw_json)
        analysis = _parse_analysis(raw_json, plan, settings.llm_model_analyst)

    _validate_conflict_coverage(analysis, conflict_ids)

    logger.info(
        f"[S5] Done. facts={len(analysis.facts)} conclusions={len(analysis.conclusions)} "
        f"neg_space={len(analysis.negative_space)}"
    )
    return analysis


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
            f"[S5/Auditor] ❌ CONTRADICTION {v.claim_id}: "
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


async def _call_analyst_with_retry(client: AsyncOpenAI, prompt: str, settings) -> dict:
    """LLM вызов с retry (при прямом вызове без кэша, для fallback)."""

    system_msg = (
        "JSON. Аудитор юр. документов РК. Вердикт на каждый claim. "
        "Ошибки = наивысший приоритет. Без пояснений вне JSON."
    )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call() -> dict:
        response = await client.chat.completions.create(
            model=settings.llm_model_analyst,
            temperature=settings.llm_temperature,
            response_format={"type": "json_object"},
            max_tokens=settings.llm_max_tokens_analyst,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — старший юридический аналитик-верификатор Республики Казахстан. "
                        "Твоя задача — ВЕРИФИЦИРОВАТЬ каждое утверждение анализируемого документа "
                        "путём сравнения с доказательствами из корпуса. "
                        "Специализируешься на нормативно-правовых актах КЗ. "
                        "ОШИБКИ В ДОКУМЕНТЕ — наивысший приоритет. "
                        "Отвечай ТОЛЬКО валидным JSON. Никакого текста вне JSON. "
                        "Каждый факт (fact) ОБЯЗАН содержать source_ids (массив chunk_id). "
                        "НЕ ДУБЛИРУЙ выводы и пробелы — каждый должен быть уникальным. "
                        "Все конфликтные источники ОБЯЗАНЫ быть упомянуты в анализе."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM returned non-dict JSON")
        return parsed

    return await _call()


# ---------------------------------------------------------------------------
# Chunked Analysis (для больших корпусов)
# ---------------------------------------------------------------------------


async def _run_chunked_analysis(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    conflict_ids: list[str],
    client: AsyncOpenAI,
    settings,
) -> AnalysisJSON:
    """
    Анализирует большой корпус по частям, затем синтезирует итог.
    Стратегия: сначала конфликтные + высокоранговые, потом остальные.
    """
    # Сортируем: конфликты + высокий ранг первыми
    sorted_chunks = sorted(
        chunks,
        key=lambda c: (not c.is_conflict, int(c.legal_rank)),
    )

    # Разбиваем на части ~40k символов каждая
    batches: list[list[EvidenceChunk]] = []
    current_batch: list[EvidenceChunk] = []
    current_size = 0
    batch_limit = 40_000

    for chunk in sorted_chunks:
        size = len(chunk.content)
        if current_size + size > batch_limit and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(chunk)
        current_size += size

    if current_batch:
        batches.append(current_batch)

    logger.info(f"[S5/Chunked] {len(batches)} batches for analysis")

    # Анализируем каждый батч
    partial_analyses: list[dict] = []
    for i, batch in enumerate(batches):
        logger.info(f"[S5/Chunked] Batch {i+1}/{len(batches)}: {len(batch)} chunks")
        prompt = build_analyst_prompt(batch, plan, conflict_ids)
        try:
            raw = await _call_analyst_with_retry(client, prompt, settings)
            partial_analyses.append(raw)
        except Exception as e:
            logger.warning(f"[S5/Chunked] Batch {i+1} failed: {e}")

    # Синтез: объединяем все результаты
    merged = _merge_partial_analyses(partial_analyses)
    _save_raw_response(merged)
    return _parse_analysis(merged, plan, settings.llm_model_analyst)


def _merge_partial_analyses(parts: list[dict]) -> dict:
    """Объединяет результаты нескольких батчей в один AnalysisJSON."""
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]

    merged: dict = {
        "facts": [],
        "conclusions": [],
        "negative_space": [],
        "normative": [],
        "pros": [],
        "cons": [],
        "affected_parties": [],
        "recommendation": "",
        "conflict_chunk_ids_referenced": [],
    }

    seen_claims: set[str] = set()

    for part in parts:
        # Дедуплицируем факты по claim
        for fact in part.get("facts", []):
            claim = fact.get("claim", "")
            if claim and claim not in seen_claims:
                merged["facts"].append(fact)
                seen_claims.add(claim)

        merged["conclusions"].extend(part.get("conclusions", []))
        merged["negative_space"].extend(part.get("negative_space", []))
        merged["normative"].extend(part.get("normative", []))
        merged["pros"].extend(part.get("pros", []))
        merged["cons"].extend(part.get("cons", []))
        merged["affected_parties"].extend(part.get("affected_parties", []))
        merged["conflict_chunk_ids_referenced"].extend(
            part.get("conflict_chunk_ids_referenced", [])
        )

        # Рекомендация: берём последнюю непустую
        rec = part.get("recommendation", "")
        if rec:
            merged["recommendation"] = rec

    # Дедуплицируем списки
    merged["pros"] = list(dict.fromkeys(merged["pros"]))
    merged["cons"] = list(dict.fromkeys(merged["cons"]))
    merged["conflict_chunk_ids_referenced"] = list(set(merged["conflict_chunk_ids_referenced"]))

    return merged


# ---------------------------------------------------------------------------
# Parse & Validate
# ---------------------------------------------------------------------------


def _parse_analysis(raw: dict, plan: QueryPlan, model: str) -> AnalysisJSON:
    """Конвертирует raw LLM JSON → AnalysisJSON с полной валидацией."""
    analysis_id = str(uuid.uuid4())

    facts = _parse_facts(raw.get("facts", []))
    conclusions = _parse_conclusions(raw.get("conclusions", []))
    negative_space = _parse_negative_space(raw.get("negative_space", []))
    normative = _parse_normative(raw.get("normative", []))
    affected_parties = _parse_affected_parties(raw.get("affected_parties", []))

    return AnalysisJSON(
        analysis_id=analysis_id,
        source_doc_id=plan.source_doc_id,
        plan_id=plan.plan_id,
        facts=facts,
        conclusions=conclusions,
        negative_space=negative_space,
        normative=normative,
        pros=_safe_str_list(raw.get("pros", [])),
        cons=_safe_str_list(raw.get("cons", [])),
        affected_parties=affected_parties,
        recommendation=str(raw.get("recommendation", "")),
        conflict_chunk_ids_referenced=_safe_str_list(raw.get("conflict_chunk_ids_referenced", [])),
        llm_model_used=model,
    )


def _parse_facts(raw_list: list) -> list[Fact]:
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        source_ids = _safe_str_list(item.get("source_ids", []))
        if not claim:
            continue
        if not source_ids:
            logger.warning(f"[S5] Fact[{i}] missing source_ids: '{claim[:50]}…'")
            source_ids = ["UNLINKED"]  # Маркер для Аудитора
        result.append(Fact(
            fact_id=f"fact_{i:04d}",
            claim=claim,
            source_ids=source_ids,
            confidence=float(item.get("confidence", 1.0)),
        ))
    return result


def _parse_conclusions(raw_list: list) -> list[Conclusion]:
    VALID_TYPES = {"deduction", "analogy", "induction"}
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement", "")).strip()
        if not statement:
            continue
        reasoning = item.get("reasoning_type", "deduction")
        if reasoning not in VALID_TYPES:
            reasoning = "deduction"
        result.append(Conclusion(
            conclusion_id=f"conc_{i:04d}",
            statement=statement,
            reasoning_type=reasoning,  # type: ignore[arg-type]
            supporting_fact_ids=_safe_str_list(item.get("supporting_fact_ids", [])) or ["UNLINKED"],
            source_ids=_safe_str_list(item.get("source_ids", [])) or ["UNLINKED"],
        ))
    return result


def _parse_negative_space(raw_list: list) -> list[NegativeSpaceItem]:
    VALID_GAP_TYPES = {"regulatory_hole", "intentional_silence", "delegation_gap"}
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        gap_type = item.get("gap_type", "regulatory_hole")
        if gap_type not in VALID_GAP_TYPES:
            gap_type = "regulatory_hole"
        result.append(NegativeSpaceItem(
            item_id=f"neg_{i:04d}",
            description=desc,
            gap_type=gap_type,  # type: ignore[arg-type]
            affected_domain=str(item.get("affected_domain", "")),
            source_ids=_safe_str_list(item.get("source_ids", [])),
        ))
    return result


def _parse_normative(raw_list: list) -> list[NormativeAssessment]:
    VALID_RISKS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        desc = str(item.get("norm_description", "")).strip()
        if not desc:
            continue
        risk = str(item.get("risk_level", "MEDIUM")).upper()
        if risk not in VALID_RISKS:
            risk = "MEDIUM"
        source_ids = _safe_str_list(item.get("source_ids", [])) or ["UNLINKED"]
        result.append(NormativeAssessment(
            assessment_id=f"norm_{i:04d}",
            norm_description=desc,
            economic_impact=item.get("economic_impact"),
            social_impact=item.get("social_impact"),
            risk_level=risk,  # type: ignore[arg-type]
            source_ids=source_ids,
        ))
    return result


def _parse_affected_parties(raw_list: list) -> list[AffectedParty]:
    VALID_ROLES = {"beneficiary", "obligated", "regulator", "third_party"}
    result = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        role = item.get("role", "third_party")
        if role not in VALID_ROLES:
            role = "third_party"
        result.append(AffectedParty(
            name=name,
            role=role,  # type: ignore[arg-type]
            description=str(item.get("description", "")),
        ))
    return result


def _validate_conflict_coverage(analysis: AnalysisJSON, conflict_ids: list[str]) -> None:
    """Все конфликтные чанки должны быть упомянуты в source_ids."""
    all_source_ids: set[str] = set()
    for fact in analysis.facts:
        all_source_ids.update(fact.source_ids)
    for conc in analysis.conclusions:
        all_source_ids.update(conc.source_ids)
    all_source_ids.update(analysis.conflict_chunk_ids_referenced)

    missing = [cid for cid in conflict_ids if cid not in all_source_ids]
    if missing:
        logger.warning(f"[S5] {len(missing)} conflict chunks not referenced!")
        analysis.conflict_chunk_ids_referenced.extend(missing)
    else:
        logger.info(f"[S5] All {len(conflict_ids)} conflict chunks referenced ✓")


def _estimate_prompt_size(chunks: list[EvidenceChunk]) -> int:
    return sum(len(c.content) for c in chunks)


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

    # Сначала добавляем детерминированные вердикты (из reference_data — без LLM)
    for claim in claims.claims:
        if claim.deterministic_verdict:
            is_error = "❌" in claim.deterministic_verdict or "ОШИБКА" in claim.deterministic_verdict
            is_ok = "✅" in claim.deterministic_verdict
            status = (
                VerdictStatus.CONTRADICTED if is_error
                else VerdictStatus.CONFIRMED if is_ok
                else VerdictStatus.UNVERIFIED
            )
            verdicts.append(ClaimVerdict(
                claim_id=claim.claim_id,
                status=status,
                source_ids=["reference_data"],
                found_value=claim.deterministic_verdict,
                document_value=", ".join(claim.entities),
                contradiction_detail=claim.deterministic_verdict if is_error else None,
                confidence="HIGH",
                is_deterministic=True,
            ))

    # Парсим LLM вердикты
    deterministic_ids = {v.claim_id for v in verdicts}
    for item in raw.get("verdicts", []):
        if not isinstance(item, dict):
            continue
        claim_id = item.get("claim_id", "")
        if not claim_id or claim_id not in claimed_ids:
            continue
        if claim_id in deterministic_ids:
            # Детерминированный вердикт надёжнее — дополняем source_ids из LLM
            llm_sources = _safe_str_list(item.get("source_ids", []))
            for v in verdicts:
                if v.claim_id == claim_id:
                    v.source_ids = list(set(v.source_ids + llm_sources))
            continue

        raw_status = item.get("status", "UNVERIFIED")
        try:
            status = VerdictStatus(raw_status)
        except ValueError:
            status = VerdictStatus.UNVERIFIED

        verdicts.append(ClaimVerdict(
            claim_id=claim_id,
            status=status,
            source_ids=_safe_str_list(item.get("source_ids", [])),
            found_value=item.get("found_value"),
            document_value=item.get("document_value"),
            contradiction_detail=item.get("contradiction_detail"),
            confidence=item.get("confidence", "MEDIUM"),
            is_deterministic=False,
        ))

    # Конвертируем вердикты в Facts для совместимости с рендером
    facts: list[Fact] = []
    for v in verdicts:
        if v.status == VerdictStatus.CONTRADICTED:
            claim_text = f"❌ ОШИБКА [{v.claim_id}]: документ='{v.document_value}' vs найдено='{v.found_value}'"
            confidence = 0.1
        elif v.status == VerdictStatus.CONFIRMED:
            claim_text = f"✅ ПОДТВЕРЖДЕНО [{v.claim_id}]: '{v.found_value or v.document_value}'"
            confidence = 0.9
        else:
            claim_text = f"⚠️ НЕ ПРОВЕРЕНО [{v.claim_id}]: '{v.document_value}'"
            confidence = 0.4
        facts.append(Fact(
            fact_id=f"fact_{len(facts):04d}",
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
    confirmed_list = [v for v in verdicts if v.status == VerdictStatus.CONFIRMED]
    unverified_list = [v for v in verdicts if v.status == VerdictStatus.UNVERIFIED]

    pros = raw.get("pros", [f"Подтверждено {len(confirmed_list)} утверждений"])
    cons = raw.get("cons", [])
    for v in contradictions:
        if v.contradiction_detail:
            cons.append(f"❌ {v.claim_id}: {v.contradiction_detail[:200]}")

    recommendation = raw.get("recommendation", "")
    if not recommendation:
        recommendation = (
            f"Аудит: проверено {len(verdicts)}, подтверждено {len(confirmed_list)}, "
            f"опровергнуто {len(contradictions)}, не верифицировано {len(unverified_list)}."
        )
        if contradictions:
            recommendation += f" КРИТИЧЕСКИ: {len(contradictions)} ошибок."

    return AnalysisJSON(
        analysis_id=analysis_id,
        source_doc_id=plan.source_doc_id,
        plan_id=plan.plan_id,
        facts=facts,
        conclusions=[],
        negative_space=neg_space,
        normative=[],
        pros=pros if isinstance(pros, list) else [str(pros)],
        cons=cons if isinstance(cons, list) else [str(cons)],
        affected_parties=[],
        recommendation=recommendation,
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
                is_deterministic=False,
            ))
            analysis.facts.append(Fact(
                fact_id=f"fact_{len(analysis.facts):04d}",
                claim=f"⚠️ НЕ ПРОВЕРЕНО [{claim.claim_id}]: {claim.claim_text[:150]}",
                source_ids=["UNLINKED"],
                confidence=0.3,
            ))
    else:
        logger.info(f"[S5/Validator] ✅ Все {len(claims.claims)} claims получили вердикты")
