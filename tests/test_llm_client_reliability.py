"""
test_llm_client_reliability.py
Phase 2a: tenacity-ретраи транзиентных ошибок + таймауты клиента + манифест
вызовов LLM (zerde/utils/llm_client.py), offline ($0, без реального LLM/torch).

Покрывает:
- retry на APITimeoutError/RateLimitError/... (3 попытки, reraise оригинала)
- BadRequestError (json_object не поддерживается) НЕ ретраится tenacity'ю —
  обрабатывается существующим fallback'ом без response_format
- cache-hit пропускает сетевой вызов
- make_llm_client: timeout (connect=10/read=300) + max_retries=0 (SDK-ретраи
  отключены, иначе 3×3=9 фактических вызовов)
- манифест фиксирует usage/latency/attempts, не меняя возвращаемый dict

cached_llm_call(client=..., model=..., messages=..., settings=...) — все
вызовы по ключевым словам, сигнатура/порядок параметров не менялись.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, BadRequestError, RateLimitError

from zerde.config import Settings
from zerde.utils.llm_client import (
    cached_llm_call,
    get_manifest,
    make_llm_client,
    reset_manifest,
)


@pytest.fixture(autouse=True)
def _reset_manifest():
    """Изолирует манифест между тестами этого файла."""
    reset_manifest()
    yield
    reset_manifest()


@pytest.fixture(autouse=True)
def _fast_tenacity_sleep(monkeypatch: pytest.MonkeyPatch):
    """tenacity (wait_random_exponential, max=20) спит между ретраями через
    asyncio.sleep — подменяем на мгновенный noop, чтобы тест не ждал секунды."""
    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _instant_sleep)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings с изолированным cache_db_path (tmp_path) и без чтения .env —
    LLMCache создаст свежую SQLite-БД, cache.get() на ней естественно пуст."""
    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        cache_db_path=str(tmp_path / "test_llm_cache.db"),
    )


def _fake_completion(content: str, *, prompt_tokens=10, completion_tokens=5, total_tokens=15):
    """SimpleNamespace, имитирующий openai ChatCompletion: .choices[0].message.content
    + .usage.{prompt,completion,total}_tokens."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def _httpx_request() -> httpx.Request:
    return httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def _httpx_response(status_code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        request=_httpx_request(),
        json={"error": {"message": message}},
    )


_VALID_RESPONSE = {"verdicts": [{"claim_id": "claim_0001", "status": "CONFIRMED"}]}
_VALID_CONTENT = json.dumps(_VALID_RESPONSE, ensure_ascii=False)

_MESSAGES = [
    {"role": "system", "content": "You are a legal auditor."},
    {"role": "user", "content": "Проверь статью 44 ГК РК."},
]


# ---------------------------------------------------------------------------
# 1. Retry on transient error → succeeds on 2nd attempt
# ---------------------------------------------------------------------------

async def test_retry_succeeds_after_transient(settings, monkeypatch):
    client = make_llm_client(settings)

    calls = {"n": 0}

    async def _create(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APITimeoutError(request=_httpx_request())
        return _fake_completion(_VALID_CONTENT)

    monkeypatch.setattr(client.chat.completions, "create", _create)

    result = await cached_llm_call(
        client=client,
        model="test-model/v1",
        messages=_MESSAGES,
        settings=settings,
    )

    assert result == _VALID_RESPONSE
    assert calls["n"] == 2  # 1 transient failure + 1 success

    manifest = get_manifest()
    assert len(manifest) == 1
    rec = manifest[-1]
    assert rec.cached is False
    assert rec.attempts == 2


# ---------------------------------------------------------------------------
# 2. BadRequestError (json_object unsupported) → NOT retried by tenacity,
#    handled by the existing no-json-mode fallback.
# ---------------------------------------------------------------------------

async def test_no_retry_on_bad_request(settings, monkeypatch):
    client = make_llm_client(settings)

    seen_kwargs: list[dict] = []

    async def _create(**kwargs):
        seen_kwargs.append(kwargs)
        if kwargs.get("response_format"):
            # json_object не поддерживается этой "моделью"
            raise BadRequestError(
                "json_object response_format is not supported by this model",
                response=_httpx_response(400, "json_object response_format is not supported"),
                body=None,
            )
        return _fake_completion(_VALID_CONTENT)

    monkeypatch.setattr(client.chat.completions, "create", _create)

    result = await cached_llm_call(
        client=client,
        model="test-model/v1",
        messages=_MESSAGES,
        settings=settings,
    )

    assert result == _VALID_RESPONSE
    # Ровно 2 вызова: 1-й с response_format=json_object (400), 2-й — fallback без него.
    # tenacity НЕ ретраила первый (BadRequestError не в _TRANSIENT_LLM_ERRORS).
    assert len(seen_kwargs) == 2
    assert seen_kwargs[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in seen_kwargs[1]

    manifest = get_manifest()
    assert len(manifest) == 1
    # attempts — суммарный счётчик реальных вызовов create() за весь
    # cached_llm_call: 1 (json_mode=True, 400) + 1 (fallback без json_mode, успех) = 2.
    assert manifest[-1].attempts == 2


# ---------------------------------------------------------------------------
# 3. Retries exhausted → original exception re-raised (not RetryError); cache
#    put NOT called.
# ---------------------------------------------------------------------------

async def test_retry_exhausts_and_reraises(settings, monkeypatch):
    client = make_llm_client(settings)

    calls = {"n": 0}

    async def _create(**_kwargs):
        calls["n"] += 1
        raise RateLimitError(
            "rate limited",
            response=_httpx_response(429, "rate limited"),
            body=None,
        )

    monkeypatch.setattr(client.chat.completions, "create", _create)

    put_called = {"flag": False}

    async def _put_spy(*_args, **_kwargs):
        put_called["flag"] = True

    from zerde.utils.cache import LLMCache
    monkeypatch.setattr(LLMCache, "put", _put_spy)

    with pytest.raises(RateLimitError):
        await cached_llm_call(
            client=client,
            model="test-model/v1",
            messages=_MESSAGES,
            settings=settings,
        )

    # 3 attempts for response_format=True (stop_after_attempt(3)).
    # BadRequest-string-check in `except` doesn't match RateLimitError → raised,
    # never reaching the no-json-mode fallback.
    assert calls["n"] == 3
    assert put_called["flag"] is False
    # Исключение распространилось ДО кода записи в манифест — записи нет.
    assert get_manifest() == []


# ---------------------------------------------------------------------------
# 4. Cache hit → network never called, manifest records cached=True.
# ---------------------------------------------------------------------------

async def test_cache_hit_skips_network_and_records_cached(settings, monkeypatch):
    client = make_llm_client(settings)

    create_called = {"flag": False}

    async def _create(**_kwargs):
        create_called["flag"] = True
        return _fake_completion(_VALID_CONTENT)

    monkeypatch.setattr(client.chat.completions, "create", _create)

    canned = {"verdicts": [{"claim_id": "claim_0001", "status": "CONTRADICTED"}]}

    from zerde.utils.cache import LLMCache

    async def _get_hit(self, model, prompt_key=None, prompt=None):
        return canned

    monkeypatch.setattr(LLMCache, "get", _get_hit)

    result = await cached_llm_call(
        client=client,
        model="test-model/v1",
        messages=_MESSAGES,
        settings=settings,
    )

    assert result == canned
    assert create_called["flag"] is False

    manifest = get_manifest()
    assert len(manifest) == 1
    assert manifest[-1].cached is True
    assert manifest[-1].attempts == 1


# ---------------------------------------------------------------------------
# 5. make_llm_client: timeout + max_retries=0
# ---------------------------------------------------------------------------

def test_make_llm_client_sets_timeout_and_no_sdk_retries(settings):
    client = make_llm_client(settings)

    assert client.max_retries == 0
    assert isinstance(client.timeout, httpx.Timeout)
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 300.0


# ---------------------------------------------------------------------------
# 6. Manifest captures usage/latency on live success.
# ---------------------------------------------------------------------------

async def test_manifest_captures_usage(settings, monkeypatch):
    client = make_llm_client(settings)

    async def _create(**_kwargs):
        return _fake_completion(_VALID_CONTENT, prompt_tokens=123, completion_tokens=45, total_tokens=168)

    monkeypatch.setattr(client.chat.completions, "create", _create)

    result = await cached_llm_call(
        client=client,
        model="test-model/v1",
        messages=_MESSAGES,
        settings=settings,
    )

    assert result == _VALID_RESPONSE

    manifest = get_manifest()
    assert len(manifest) == 1
    rec = manifest[-1]
    assert rec.cached is False
    assert rec.model == "test-model/v1"
    assert rec.prompt_tokens == 123
    assert rec.completion_tokens == 45
    assert rec.total_tokens == 168
    assert rec.latency_s >= 0
    assert rec.attempts == 1
