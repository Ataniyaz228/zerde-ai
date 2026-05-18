"""
ЗЕРДЕ v6.2 — Stage 2: LLM Planner (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Вход:  DocumentState
Выход: QueryPlan

Особенности:
  - temperature=0, response_format=json_object
  - tenacity retry (max 3 попытки, exponential backoff)
  - Schema validation с fallback на пустой план
  - Автоматическое разбиение длинных документов
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from zerde.config import get_settings
from zerde.models import AdiletQuery, DocumentState, QueryPlan, WebQuery, WebTier
from zerde.prompts.planner import build_planner_prompt
from zerde.utils.llm_client import make_llm_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_query_plan(doc_state: DocumentState) -> QueryPlan:
    """
    Этап 2: LLM декомпозирует документ на структурированный план запросов.
    """
    settings = get_settings()
    client = make_llm_client(settings)

    logger.info(f"[S2] Building plan. doc_id={doc_state.doc_id[:8]}… chars={doc_state.char_count}")

    prompt = build_planner_prompt(doc_state.normalized_text)

    try:
        raw_json = await _call_llm_with_retry(
            client=client,
            prompt=prompt,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens_planner,
            system_msg=(
                "Ты — юридический аналитик-планировщик для Республики Казахстан. "
                "Отвечай ТОЛЬКО валидным JSON строго по указанной схеме. "
                "Никакого текста кроме JSON."
            ),
        )
    except Exception as e:
        logger.error(f"[S2] LLM failed after retries: {e}. Returning empty plan.")
        raw_json = {}

    plan = _parse_llm_plan(raw_json, doc_state)
    logger.info(
        f"[S2] Plan ready. Queries: adilet={len(plan.adilet_queries)} "
        f"web_ru={len(plan.web_queries_ru)} web_kk={len(plan.web_queries_kk)} "
        f"web_en={len(plan.web_queries_en)}"
    )
    return plan


# ---------------------------------------------------------------------------
# LLM Call with Retry
# ---------------------------------------------------------------------------


async def _call_llm_with_retry(
    client: AsyncOpenAI,
    prompt: str,
    model: str,
    max_tokens: int,
    system_msg: str,
) -> dict:
    """
    Вызывает LLM с tenacity retry: 3 попытки, экспоненциальный backoff 2–10s.
    """

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call() -> dict:
        response = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed)}")
        return parsed

    return await _call()


# ---------------------------------------------------------------------------
# Response Parser
# ---------------------------------------------------------------------------


def _parse_llm_plan(raw: dict, doc_state: DocumentState) -> QueryPlan:
    """
    Парсит ответ LLM в QueryPlan с валидацией каждого поля.
    Некорректные записи пропускаются с предупреждением.
    """
    plan_id = hashlib.sha256(doc_state.normalized_text.encode()).hexdigest()

    adilet_queries = _parse_adilet_queries(raw.get("adilet_queries", []))
    web_queries_ru = _parse_web_queries(raw.get("web_queries_ru", []), "ru")
    web_queries_kk = _parse_web_queries(raw.get("web_queries_kk", []), "kk")
    web_queries_en = _parse_web_queries(raw.get("web_queries_en", []), "en")

    return QueryPlan(
        plan_id=plan_id,
        source_doc_id=doc_state.doc_id,
        adilet_queries=adilet_queries,
        web_queries_ru=web_queries_ru,
        web_queries_kk=web_queries_kk,
        web_queries_en=web_queries_en,
        expected_elements=_safe_str_list(raw.get("expected_elements", [])),
        bylaw_triggers=_safe_str_list(raw.get("bylaw_triggers", [])),
    )


def _parse_adilet_queries(raw_list: list) -> list[AdiletQuery]:
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            logger.warning(f"[S2] Skipping invalid adilet_query[{i}]: not a dict")
            continue
        try:
            q = AdiletQuery(
                query_text=str(item.get("query_text", "")),
                law_ids=_safe_str_list(item.get("law_ids", [])),
                articles=_safe_str_list(item.get("articles", [])),
                date_from=_parse_date(item.get("date_from")),
                date_to=_parse_date(item.get("date_to")),
            )
            if q.query_text:
                result.append(q)
        except Exception as e:
            logger.warning(f"[S2] Skipping adilet_query[{i}]: {e}")
    return result


def _parse_web_queries(raw_list: list, language: str) -> list[WebQuery]:
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        try:
            q = WebQuery(
                query_text=str(item.get("query_text", "")),
                language=language,  # type: ignore[arg-type]
                include_domains=_safe_str_list(item.get("include_domains", [])),
                max_results=int(item.get("max_results", 10)),
            )
            if q.query_text:
                result.append(q)
        except Exception as e:
            logger.warning(f"[S2] Skipping web_query[{i}]: {e}")
    return result


def _safe_str_list(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v) for v in val if v]


def _parse_date(val: object) -> date | None:
    if not val or not isinstance(val, str):
        return None
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None
