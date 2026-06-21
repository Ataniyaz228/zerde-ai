"""Автофолбэк аудита OpenRouter→openmodel (_FallbackLLMClient).

Без сети: реальный OpenAI-клиент и низкоуровневый Anthropic-запрос мокаются.
Проверяем: при 403 от OpenRouter шим уходит на openmodel, маппит имя модели
(снимает вендор-префикс) и отдаёт OpenAI-форму ответа; при успехе OpenRouter —
ответ возвращается как есть; при падении openmodel — пробрасывается исходный 403.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import PermissionDeniedError

import zerde.utils.llm_client as llm


def _settings_stub():
    return SimpleNamespace(
        openai_api_key="sk-or-test",
        openai_base_url="https://openrouter.ai/api/v1",
        openrouter_headers={},
        audit_fallback_enabled=True,
        translator_base_url="https://api.openmodel.ai",
        translator_api_key="om-key",
    )


def _make_403() -> PermissionDeniedError:
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(403, request=req)
    return PermissionDeniedError("Key limit exceeded (total limit)", response=resp, body=None)


def _real_raising(err: Exception):
    async def create(**kwargs):
        raise err
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_fallback_on_403_routes_to_openmodel(monkeypatch):
    seen: dict = {}

    async def fake_req(base_url, api_key, model, messages, max_tokens, temperature):
        seen.update(base_url=base_url, api_key=api_key, model=model,
                    max_tokens=max_tokens, temperature=temperature)
        data = {
            "content": [
                {"type": "thinking", "thinking": "размышляю"},
                {"type": "text", "text": '{"verdict":"UNVERIFIED"}'},
            ],
            "usage": {"input_tokens": 11, "output_tokens": 22},
        }
        return data, 1

    monkeypatch.setattr(llm, "_anthropic_messages_request", fake_req)

    client = llm._FallbackLLMClient(_real_raising(_make_403()), _settings_stub())
    resp = await client.chat.completions.create(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        max_tokens=321,
        temperature=0.0,
        response_format={"type": "json_object"},  # шим это игнорирует на openmodel
    )

    # OpenAI-форма: cached_llm_call читает choices[0].message.content и usage.*
    assert resp.choices[0].message.content == '{"verdict":"UNVERIFIED"}'  # thinking отброшен
    assert resp.usage.prompt_tokens == 11
    assert resp.usage.completion_tokens == 22
    # вендор-префикс снят для openmodel; ключ/база — из translator_*
    assert seen["model"] == "deepseek-v4-flash"
    assert seen["base_url"] == "https://api.openmodel.ai"
    assert seen["api_key"] == "om-key"
    assert seen["max_tokens"] == 321


async def test_no_fallback_when_openrouter_succeeds(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("openmodel не должен вызываться при успехе OpenRouter")
    monkeypatch.setattr(llm, "_anthropic_messages_request", boom)

    async def ok_create(**kwargs):
        return SimpleNamespace(choices=["REAL"], usage=None)
    real = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=ok_create)))

    client = llm._FallbackLLMClient(real, _settings_stub())
    out = await client.chat.completions.create(model="x", messages=[])
    assert out.choices == ["REAL"]


async def test_fallback_reraises_original_when_openmodel_down(monkeypatch):
    async def fake_req_boom(*a, **k):
        raise RuntimeError("openmodel недоступен")
    monkeypatch.setattr(llm, "_anthropic_messages_request", fake_req_boom)

    client = llm._FallbackLLMClient(_real_raising(_make_403()), _settings_stub())
    with pytest.raises(PermissionDeniedError):
        await client.chat.completions.create(model="deepseek/deepseek-v4-flash", messages=[])


async def test_non_403_error_not_caught(monkeypatch):
    # Не-403 (напр. таймаут) шим НЕ перехватывает — пусть его обрабатывает
    # tenacity/fail-closed в cached_llm_call.
    async def boom(*a, **k):
        raise AssertionError("не должно дойти до openmodel")
    monkeypatch.setattr(llm, "_anthropic_messages_request", boom)

    client = llm._FallbackLLMClient(_real_raising(ValueError("boom")), _settings_stub())
    with pytest.raises(ValueError):
        await client.chat.completions.create(model="x", messages=[])


def test_make_llm_client_returns_shim_when_enabled():
    client = llm.make_llm_client(_settings_stub())
    assert isinstance(client, llm._FallbackLLMClient)


def test_make_llm_client_plain_when_disabled():
    s = _settings_stub()
    s.audit_fallback_enabled = False
    client = llm.make_llm_client(s)
    assert not isinstance(client, llm._FallbackLLMClient)
