"""F-D4: транзиентные ошибки LLM (сеть/429/5xx) должны ретраиться с backoff,
а не валить вызов с первой попытки. Путь cached_llm_call раньше не имел
офлайн-покрытия — мок-клиент + temp-db + пропатченный sleep.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from zerde.utils.llm_client import _LLM_MAX_RETRIES, cached_llm_call


def _ok_response():
    msg = SimpleNamespace(content='{"ok": true}')
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeCreate:
    """Падает transient-ошибкой `fail_times` раз, затем отдаёт валидный ответ."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise APIConnectionError(request=httpx.Request("POST", "https://test.local"))
        return _ok_response()


def _client(fake_create):
    return SimpleNamespace(chat=SimpleNamespace(completions=fake_create))


@pytest.fixture()
def _no_sleep(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


async def test_transient_error_retried_then_succeeds(tmp_path, _no_sleep):
    fake = _FakeCreate(fail_times=_LLM_MAX_RETRIES)  # ровно столько, сколько успеваем
    settings = SimpleNamespace(cache_db_path=str(tmp_path / "llm.db"))

    result = await cached_llm_call(
        _client(fake),
        model="test/model",
        messages=[{"role": "user", "content": "hi"}],
        settings=settings,
    )

    assert result == {"ok": True}
    assert fake.calls == _LLM_MAX_RETRIES + 1  # 2 падения + 1 успех
    assert _no_sleep == [2 ** i for i in range(_LLM_MAX_RETRIES)]  # backoff 1s, 2s


async def test_transient_error_exhausts_retries_and_raises(tmp_path, _no_sleep):
    fake = _FakeCreate(fail_times=_LLM_MAX_RETRIES + 5)  # никогда не успевает
    settings = SimpleNamespace(cache_db_path=str(tmp_path / "llm.db"))

    with pytest.raises(APIConnectionError):
        await cached_llm_call(
            _client(fake),
            model="test/model",
            messages=[{"role": "user", "content": "hi"}],
            settings=settings,
        )

    assert fake.calls == _LLM_MAX_RETRIES + 1  # исчерпали попытки, не больше
