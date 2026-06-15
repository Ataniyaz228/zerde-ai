"""S2 — LLM строит QueryPlan: какие запросы слать в adilet и web.

Вызов детерминированный (temperature=0, JSON-режим) и кэшируется. Длинные
документы режутся на части. Если ответ не проходит схему — отдаём пустой план,
а не падаем: пустой план просто означает «нет внешних запросов».
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date

from zerde.config import get_settings
from zerde.models import AdiletQuery, DocumentState, QueryPlan, WebQuery
from zerde.prompts.planner import build_planner_prompt
from zerde.utils.llm_client import cached_llm_call, make_llm_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_query_plan(doc_state: DocumentState) -> QueryPlan:
    """
    Этап 2: LLM декомпозирует документ на структурированный план запросов.
    Один документ = один план. При повторном запуске берётся из кэша (0 токенов).
    """
    settings = get_settings()
    client = make_llm_client(settings)

    logger.info(f"[S2] Building plan. doc_id={doc_state.doc_id[:8]}… chars={doc_state.char_count}")

    prompt = build_planner_prompt(doc_state.normalized_text)
    system_msg = "JSON. Юридический планировщик РК. Только JSON по схеме. Без пояснений."

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    raw_json = {}
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_json = await cached_llm_call(
                client=client,
                model=settings.llm_model_planner,
                messages=messages,
                settings=settings,
                ttl_seconds=None,
                max_tokens=settings.llm_max_tokens_planner,
            )
        except Exception as e:
            logger.error(f"[S2] LLM failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            raw_json = {}

        # Проверяем что ответ содержит хоть какие-то запросы
        has_queries = (
            raw_json.get("adilet_queries")
            or raw_json.get("web_queries_ru")
            or raw_json.get("web_queries_kk")
            or raw_json.get("web_queries_en")
        )
        if has_queries:
            break

        logger.warning(
            f"[S2] Attempt {attempt}/{MAX_RETRIES}: planner returned 0 queries. "
            f"Response keys: {list(raw_json.keys()) if raw_json else 'empty'}"
        )
        # Инвалидируем кэш чтобы при retry получить новый ответ
        from zerde.utils.cache import LLMCache
        llm_cache = LLMCache(settings.cache_db_path)
        prompt_key = json.dumps([{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}], ensure_ascii=False, sort_keys=True)
        cache_key = LLMCache._make_key(settings.llm_model_planner, prompt_key)
        await llm_cache._delete(cache_key)
        logger.info(f"[S2] Cache invalidated for retry {attempt + 1}")

    # Сохраняем для отладки
    with open("last_planner_raw_response.json", "w", encoding="utf-8") as f:
        json.dump(raw_json, f, ensure_ascii=False, indent=2)
    logger.info("[S2] Raw Planner JSON saved to last_planner_raw_response.json")

    plan = _parse_llm_plan(raw_json, doc_state)
    logger.info(
        f"[S2] Plan ready. Queries: adilet={len(plan.adilet_queries)} "
        f"web_ru={len(plan.web_queries_ru)} web_kk={len(plan.web_queries_kk)} "
        f"web_en={len(plan.web_queries_en)}"
    )
    return plan


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
    from zerde.utils.law_registry import get_registry
    registry = get_registry()
    result = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            logger.warning(f"[S2] Skipping invalid adilet_query[{i}]: not a dict")
            continue
        try:
            raw_law_ids = _safe_str_list(item.get("law_ids", []))
            # Fix #4: Резолвим каждый law_id через реестр (233-IV → 261-IV и т.д.)
            resolved_law_ids = []
            for lid in raw_law_ids:
                resolved = registry.resolve(lid)
                canonical = resolved if resolved else lid
                if canonical not in resolved_law_ids:
                    resolved_law_ids.append(canonical)

            # Fix #4: Также патчим query_text — заменяем неправильные ID прямо в тексте
            query_text = str(item.get("query_text", ""))
            for lid in raw_law_ids:
                resolved = registry.resolve(lid)
                if resolved and resolved != lid and lid in query_text:
                    query_text = query_text.replace(lid, resolved)
                    logger.debug(f"[S2/Fix4] query_text: {lid} → {resolved}")

            q = AdiletQuery(
                query_text=query_text,
                law_ids=resolved_law_ids,
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
