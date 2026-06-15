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
import os
import re
import time
import weakref
from collections import deque
from dataclasses import dataclass

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from zerde.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Транзиентные сетевые/upstream ошибки — безопасно повторить тот же запрос
# (idempotent: ничего не записано/не оплачено при ошибке соединения/таймауте/429/5xx).
# BadRequestError (400, напр. json_object не поддерживается моделью) сюда
# НЕ входит — повтор того же запроса даст ту же ошибку; для него уже есть
# отдельный fallback-без-json_mode ниже по коду.
# asyncio.TimeoutError — наш wall-clock потолок (см. _LLM_OVERALL_TIMEOUT_S):
# зависший/«капающий» upstream не ловится httpx read-таймаутом (он между байтами),
# поэтому оборачиваем create() в asyncio.wait_for и ретраим его таймаут как транзиент.
_TRANSIENT_LLM_ERRORS = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
    asyncio.TimeoutError,
)

# Жёсткий потолок на ОДИН LLM-вызов (весь запрос целиком, не между байтами).
# httpx read-таймаут (300с) ловит только паузы между чанками — провайдер,
# который медленно «капает» байтами, держит соединение и слот семафора(5)
# бесконечно, вешая весь S5. Этот wall-clock cap гарантирует освобождение
# семафора за конечное время. Переопределяется ZERDE_LLM_OVERALL_TIMEOUT_S.
_LLM_OVERALL_TIMEOUT_S = float(os.getenv("ZERDE_LLM_OVERALL_TIMEOUT_S", "240"))


# См. комментарий в zerde/utils/cache.py: per-loop регистрация, т.к. backend
# запускает анализы в отдельных asyncio.run(...) loop'ах.
_llm_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def get_llm_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _llm_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(5)
        _llm_semaphores[loop] = semaphore
    return semaphore


@dataclass(slots=True)
class LLMCallRecord:
    """Один LLM-вызов (или сводка отказов S5-батчей) — для наблюдаемости.

    Не влияет на возвращаемые данные пайплайна; чисто аддитивный манифест.
    """

    model: str
    cached: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_s: float
    attempts: int
    batch_failures: int = 0


# Манифест последних вызовов (ограниченный буфер — не растёт неограниченно
# в долгоживущем backend-процессе). Не персистентный, не часть кэша.
_MANIFEST: deque[LLMCallRecord] = deque(maxlen=512)


def record_llm_call(rec: LLMCallRecord) -> None:
    """Добавляет запись в манифест. Никогда не бросает наружу."""
    try:
        _MANIFEST.append(rec)
    except Exception:
        logger.debug("[LLMManifest] failed to record call", exc_info=True)


def record_batch_failures(n: int) -> None:
    """Отмечает в манифесте, сколько S5-батчей завершились ошибкой и были
    backfill'нуты как UNVERIFIED (см. s5_analyst.run_auditor). No-op при n=0."""
    if not n:
        return
    record_llm_call(LLMCallRecord(
        model="<s5_batch>",
        cached=False,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_s=0.0,
        attempts=0,
        batch_failures=n,
    ))


def get_manifest() -> list[LLMCallRecord]:
    """Снимок манифеста (копия — безопасно итерировать пока пишутся новые записи)."""
    return list(_MANIFEST)


def reset_manifest() -> None:
    """Очищает манифест. Используется тестами для изоляции между прогонами."""
    _MANIFEST.clear()


# Счётчики для наблюдаемости парсинга ответов LLM (Change 5). Чисто
# аддитивные — не меняют возвращаемый dict, текст/уровень логов не трогаем.
_repair_used_count = 0
_parse_failed_count = 0


def get_parse_stats() -> dict[str, int]:
    """Снимок счётчиков repair/parse_failed для наблюдаемости."""
    return {"repair_used": _repair_used_count, "parse_failed": _parse_failed_count}


def reset_parse_stats() -> None:
    """Сбрасывает счётчики repair/parse_failed. Для изоляции тестов."""
    global _repair_used_count, _parse_failed_count
    _repair_used_count = 0
    _parse_failed_count = 0



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

    timeout: connect=10s (быстрый фейл на мёртвом сокете), read=300s
    (Stage 5 analyst — non-streaming, до llm_max_tokens_analyst=16384 токенов
    рассуждений), write=30s, pool=10s — туже, чем дефолт SDK (600s).
    Безопасно при read=300s, потому что транзиентные ошибки (включая
    APITimeoutError) повторяются через tenacity в cached_llm_call.

    max_retries=0: ретраи SDK отключены — иначе они умножатся на ретраи
    tenacity (cached_llm_call) и дадут до 3×3=9 фактических вызовов вместо 3.
    """
    s = settings or get_settings()
    return AsyncOpenAI(
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
        default_headers=s.openrouter_headers,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
        max_retries=0,
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
    global _repair_used_count, _parse_failed_count

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
    cache_check_start = time.perf_counter()
    cached = await cache.get(model, prompt_key)
    if cached is not None:
        record_llm_call(LLMCallRecord(
            model=model,
            cached=True,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            latency_s=time.perf_counter() - cache_check_start,
            attempts=1,
        ))
        return cached

    # Делаем реальный LLM-вызов под семафором
    semaphore = get_llm_semaphore()
    live_start = time.perf_counter()
    attempts = 0
    response = None
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

                # Транзиентные ошибки (таймаут/соединение/429/5xx) повторяем до
                # 3 попыток с экспоненциальным backoff+jitter; BadRequestError
                # (json_object не поддерживается) НЕ в _TRANSIENT_LLM_ERRORS →
                # не ретраится здесь, падает в except ниже на fallback без json_mode.
                # reraise=True — исходное исключение, не RetryError, чтобы
                # except Exception ниже видел оригинальный тип/строку ошибки.
                @retry(
                    retry=retry_if_exception_type(_TRANSIENT_LLM_ERRORS),
                    wait=wait_random_exponential(multiplier=1, max=20),
                    stop=stop_after_attempt(3),
                    reraise=True,
                )
                async def _create_with_retry() -> object:
                    nonlocal attempts
                    attempts += 1
                    # wait_for отменяет висящий create() по wall-clock и
                    # освобождает слот семафора; asyncio.TimeoutError → транзиент,
                    # tenacity повторит (до 3), затем fail-closed в run_auditor.
                    return await asyncio.wait_for(
                        client.chat.completions.create(**kwargs),
                        timeout=_LLM_OVERALL_TIMEOUT_S,
                    )

                response = await _create_with_retry()
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

    # Манифест: usage из последнего успешного ответа (None если response без
    # usage — некоторые OpenRouter-модели его не возвращают).
    _usage = getattr(response, "usage", None)
    record_llm_call(LLMCallRecord(
        model=model,
        cached=False,
        prompt_tokens=getattr(_usage, "prompt_tokens", None),
        completion_tokens=getattr(_usage, "completion_tokens", None),
        total_tokens=getattr(_usage, "total_tokens", None),
        latency_s=time.perf_counter() - live_start,
        attempts=attempts,
    ))

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
                    _repair_used_count += 1
                else:
                    parsed = {}
                    parse_failed = True
                    _parse_failed_count += 1
        else:
            parsed = _repair_truncated_json(content)
            if parsed:
                logger.info(f"[LLMCall] Repaired truncated JSON. Keys: {list(parsed.keys())}")
                _repair_used_count += 1
            else:
                parsed = {}
                parse_failed = True
                _parse_failed_count += 1


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
