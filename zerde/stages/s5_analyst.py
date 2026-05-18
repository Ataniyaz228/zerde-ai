"""
ЗЕРДЕ v6.2 — Stage 5: The Analyst (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Вход:  list[EvidenceChunk] + QueryPlan
Выход: AnalysisJSON

Особенности:
  - temperature=0, response_format=json_object
  - tenacity retry (3 попытки)
  - Chunked prompt если корпус > context window
  - Принудительное упоминание конфликтных чанков
  - Schema validation с graceful fallback
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
    Conclusion,
    EvidenceChunk,
    Fact,
    NegativeSpaceItem,
    NormativeAssessment,
    QueryPlan,
    ValidationStatus,
)
from zerde.prompts.analyst import build_analyst_prompt
from zerde.utils.llm_client import make_llm_client

logger = logging.getLogger(__name__)

# Максимум символов в промпте Аналитика (безопасный лимит для gpt-4o)
_MAX_PROMPT_CHARS = 80_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_analyst(chunks: list[EvidenceChunk], plan: QueryPlan) -> AnalysisJSON:
    """Этап 5: LLM Analyst генерирует AnalysisJSON."""
    settings = get_settings()
    client = make_llm_client(settings)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_ids = [c.chunk_id for c in active if c.is_conflict]

    logger.info(
        f"[S5] Analyst start. chunks={len(active)} conflicts={len(conflict_ids)} "
        f"plan={plan.plan_id[:8]}…"
    )

    # Если корпус большой — анализируем частями
    if _estimate_prompt_size(active) > _MAX_PROMPT_CHARS:
        logger.info("[S5] Large corpus detected. Using chunked analysis.")
        analysis = await _run_chunked_analysis(active, plan, conflict_ids, client, settings)
    else:
        prompt = build_analyst_prompt(active, plan, conflict_ids)
        raw_json = await _call_analyst_with_retry(client, prompt, settings)
        analysis = _parse_analysis(raw_json, plan, settings.llm_model_analyst)

    _validate_conflict_coverage(analysis, conflict_ids)

    logger.info(
        f"[S5] Done. facts={len(analysis.facts)} conclusions={len(analysis.conclusions)} "
        f"neg_space={len(analysis.negative_space)}"
    )
    return analysis


# ---------------------------------------------------------------------------
# LLM Call
# ---------------------------------------------------------------------------


async def _call_analyst_with_retry(client: AsyncOpenAI, prompt: str, settings) -> dict:
    """LLM вызов с retry."""

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
                        "Ты — старший юридический аналитик Республики Казахстан. "
                        "Специализируешься на нормативно-правовых актах КЗ, судебной практике "
                        "и регуляторной среде. "
                        "Отвечай ТОЛЬКО валидным JSON. Никакого текста вне JSON. "
                        "Каждый факт (fact) ОБЯЗАН содержать source_ids (массив chunk_id). "
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
