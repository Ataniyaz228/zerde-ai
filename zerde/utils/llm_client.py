"""
LLM Client Factory + Cached Calls
Создаёт AsyncOpenAI клиентов с кэшированием ответов.

Каждый стейдж выбирает модель напрямую через settings.llm_model_* :
  Stage 2/2.5 → llm_model_planner / llm_model_extractor
  Stage 5     → llm_model_analyst (reasoning)
  Stage 5.5   → llm_model_policy_analyst
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from openai import AsyncOpenAI

from zerde.config import Settings, get_settings

logger = logging.getLogger(__name__)


_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def get_llm_semaphore() -> asyncio.Semaphore:
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(5)
    return _LLM_SEMAPHORE



def _repair_truncated_json(raw: str) -> dict | None:
    """
    Пытается восстановить обрезанный JSON.
    DeepSeek Flash часто обрезает ответ посередине строки.
    Стратегия: обрезаем до последнего валидного } или ], потом закрываем.
    """
    if not raw:
        return None

    # Убираем возможный markdown
    if "```json" in raw:
        raw = raw.split("```json", 1)[1]
    if "```" in raw:
        raw = raw.split("```", 1)[0]

    raw = raw.strip()
    if not raw or not (raw.startswith("{") or raw.startswith("[")):
        return None

    # Стратегия 1: обрезать до последней закрытой структуры
    for end_char in ["}", "]"]:
        last_pos = raw.rfind(end_char)
        if last_pos > 0:
            candidate = raw[: last_pos + 1]
            # Балансируем скобки
            open_braces = candidate.count("{") - candidate.count("}")
            open_brackets = candidate.count("[") - candidate.count("]")
            candidate += "]" * max(0, open_brackets)
            candidate += "}" * max(0, open_braces)
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

    # Стратегия 2: грубая — закрываем все открытые скобки
    cleaned = raw.rstrip()
    # Убираем незавершённую строку (обрезанную посередине)
    if cleaned.count('"') % 2 != 0:
        last_quote = cleaned.rfind('"')
        cleaned = cleaned[:last_quote + 1]

    open_braces = cleaned.count("{") - cleaned.count("}")
    open_brackets = cleaned.count("[") - cleaned.count("]")
    cleaned += "]" * max(0, open_brackets)
    cleaned += "}" * max(0, open_braces)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    return None


def make_llm_client(settings: Settings | None = None) -> AsyncOpenAI:
    """
    Создаёт AsyncOpenAI клиент для LLM (Planner, Analyst).
    При OpenRouter — добавляет base_url и X-Title / HTTP-Referer заголовки.
    """
    s = settings or get_settings()
    return AsyncOpenAI(
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
        default_headers=s.openrouter_headers,
    )


def make_embedding_client(settings: Settings | None = None) -> AsyncOpenAI | None:
    """
    Создаёт AsyncOpenAI клиент для embeddings.
    Возвращает None если embeddings недоступны (OpenRouter без отдельного ключа).
    Embeddings всегда идут через api.openai.com — OpenRouter их не поддерживает.
    """
    s = settings or get_settings()

    if not s.can_use_embeddings:
        return None

    # Embeddings ВСЕГДА через OpenAI (даже если LLM через OpenRouter)
    return AsyncOpenAI(
        api_key=s.effective_embedding_key,
        base_url="https://api.openai.com/v1",  # Фиксировано — не OpenRouter
    )


async def cached_llm_call(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> dict:
    """
    LLM-вызов с кэшированием ответов в SQLite.

    Ключ кэша = SHA256(model + сериализованные messages).
    Одинаковый промпт → мгновенный ответ из кэша, 0 токенов.

    Args:
        client: AsyncOpenAI клиент.
        model: ID модели.
        messages: Список сообщений [{role, content}].
        settings: Settings (для cache_db_path).
        ttl_seconds: None = постоянный кэш, int = TTL в секундах.
        max_tokens: Макс. токенов ответа.
        temperature: Температура (0 для детерминированности).

    Returns:
        Parsed JSON dict от LLM (из кэша или свежий).
    """
    # Импортируем здесь чтобы избежать циклических импортов
    from zerde.utils.cache import LLMCache

    s = settings or get_settings()
    cache = LLMCache(s.cache_db_path)

    # Очищаем устаревшие при каждом вызове (дёшево — O(idx))
    await cache.invalidate_expired()

    # Ключ = model + messages + параметры генерации, влияющие на ответ.
    # БЕЗ temperature/max_tokens смена этих настроек вернула бы устаревший
    # закэшированный ответ (cache.py:_make_key добавляет ещё версию промпта).
    prompt_key = json.dumps(
        {"messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        ensure_ascii=False,
        sort_keys=True,
    )

    # Проверяем кэш
    cached = await cache.get(model, prompt_key)
    if cached is not None:
        return cached

    # Делаем реальный LLM-вызов под семафором
    semaphore = get_llm_semaphore()
    async with semaphore:
        # Пробуем с json_object, при ошибке — без него (fallback)
        content = None
        for use_json_mode in (True, False):
            try:
                kwargs: dict = dict(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                if use_json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                response = await client.chat.completions.create(**kwargs)
                # Защита от 200-ответа без choices (transient upstream 429 через
                # OpenRouter отдаёт пустой choices): не падаем с
                # 'NoneType'/'IndexError', а пробуем следующий режим → "{}".
                choices = getattr(response, "choices", None)
                if not choices:
                    logger.warning("[LLMCall] response has no choices (transient upstream error?), retrying...")
                    continue
                content = choices[0].message.content or "{}"
                break  # Успех — выходим
            except Exception as e:
                err_str = str(e)
                if "json" in err_str.lower() or "400" in err_str:
                    if use_json_mode:
                        logger.info("[LLMCall] json_object not supported, retrying without it...")
                        continue  # Попробуем без json_mode
                raise  # Другая ошибка — пробрасываем

    if content is None:
        content = "{}"

    # Если ответ не JSON — пробуем извлечь JSON из текста
    parse_failed = False
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Ищем JSON-блок в тексте (```json ... ``` или первый {/[ ... }/])
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', content)
        if not json_match:
            json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', content)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                logger.info("[LLMCall] Extracted JSON from text response.")
            except json.JSONDecodeError:
                parsed = _repair_truncated_json(content)
                if parsed:
                    logger.info(f"[LLMCall] Repaired truncated JSON. Keys: {list(parsed.keys())}")
                else:
                    parsed = {}
                    parse_failed = True
        else:
            parsed = _repair_truncated_json(content)
            if parsed:
                logger.info(f"[LLMCall] Repaired truncated JSON. Keys: {list(parsed.keys())}")
            else:
                parsed = {}
                parse_failed = True


    if not isinstance(parsed, dict):
        if isinstance(parsed, list):
            parsed = {"_raw": parsed}
        else:
            logger.warning("[LLMCall] LLM returned non-dict JSON, wrapping.")
            parsed = {"_raw": parsed}

    # НЕ кэшируем сломанные ответы: пустой dict или parse_failed
    if parse_failed or not parsed:
        logger.warning("[LLMCall] Skipping cache — response is empty or malformed.")
        return parsed

    # Сохраняем в кэш только валидные непустые ответы
    await cache.put(model, prompt_key, parsed, ttl_seconds=ttl_seconds)

    return parsed
