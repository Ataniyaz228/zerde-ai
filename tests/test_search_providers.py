"""
test_search_providers.py
Тесты цепочки фоллбэка веб-поиска (Tavily → Serper → Google → DuckDuckGo → Local)
Использует только mock'u (asyncio/unittest.mock) — без сетевых запросов.
"""
from unittest.mock import MagicMock, patch

import pytest

from zerde.models import WebQuery

# ---------------------------------------------------------------------------
# Моки для каждого провайдера
# ---------------------------------------------------------------------------

FAKE_RESULTS = [
    {"title": "Тест норма", "url": "https://adilet.zan.kz/test", "content": "Текст статьи"}
]


def _make_query(text: str, max_results: int = 5) -> WebQuery:
    """Factory for WebQuery test fixtures."""
    return WebQuery(
        query_text=text,
        language="ru",
        include_domains=[],
        max_results=max_results,
    )


async def _mock_success(query: str, max_results: int) -> list[dict]:
    return FAKE_RESULTS


async def _mock_fail(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("provider unavailable")


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_web_duckduckgo_used_when_tavily_fails():
    """
    Если Tavily недоступен, _search_web должен успешно вернуть результаты через DuckDuckGo.
    """
    from zerde.stages.s3_gather import _search_web

    with patch("zerde.stages.s3_gather._search_tavily", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_serper", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_google", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_duckduckgo", new=_mock_success):

        q = _make_query("закон Казахстан")
        results, provider = await _search_web(q)

    assert len(results) > 0
    assert provider == "duckduckgo"


@pytest.mark.asyncio
async def test_search_web_first_available_wins():
    """
    DDG — всегда последний fallback. Проверяем: при отказе Tavily
    и без Serper/Google API ключей, DDG успешно выполняется и возвращает данные.
    """
    from zerde.stages.s3_gather import _search_web

    # Tavily фейлит, DDG отвечает. Serper/Google нет в цепочке (нет API ключа)
    with patch("zerde.stages.s3_gather._search_tavily", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_duckduckgo", new=_mock_success):

        q = _make_query("тест")
        results, provider = await _search_web(q)

    # DDG должен быть использован как фоллбэк
    assert provider == "duckduckgo"
    assert len(results) > 0


@pytest.mark.asyncio
async def test_search_web_returns_empty_when_all_fail():
    """
    Если все провайдеры недоступны, функция должна вернуть ([], "none").
    """
    from zerde.stages.s3_gather import _search_web

    with patch("zerde.stages.s3_gather._search_tavily", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_serper", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_google", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_duckduckgo", new=_mock_fail):

        q = _make_query("тест")
        results, provider = await _search_web(q)

    assert results == []
    assert provider == "none"


@pytest.mark.asyncio
async def test_search_web_falls_back_to_ddg_when_tavily_raises(monkeypatch):
    """Реальная цепочка: provider=tavily, Tavily падает → DDG. Serper/Google НЕ в цепочке.

    ПРИМЕЧАНИЕ (production gap): _search_web НЕ навешивает per-provider timeout
    (нет asyncio.wait_for). Прежний тест ассертил несуществующую фичу «cap 10s»
    и тратил ~21с на sleep. Если таймаут реализуют — добавить сюда тест на
    пропуск зависшего провайдера.
    """
    from zerde.config import get_settings
    from zerde.stages.s3_gather import _search_web

    monkeypatch.setattr(get_settings(), "search_provider", "tavily", raising=False)

    with patch("zerde.stages.s3_gather._search_tavily", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_duckduckgo", new=_mock_success):

        q = _make_query("тест")
        results, provider = await _search_web(q)

    assert provider == "duckduckgo"
    assert len(results) > 0


@pytest.mark.asyncio
async def test_duckduckgo_result_parsing():
    """
    Проверяет нормализацию DDG-результатов (href/body → url/content).
    ddgs v1 использует синхронный API + asyncio.to_thread — патчим _DDGS напрямую.
    """
    from zerde.stages.s3_gather import _search_duckduckgo

    fake_ddg_output = [
        {"title": "Test", "href": "https://example.com", "body": "Текст результата"}
    ]

    with patch("zerde.stages.s3_gather._DDGS") as mock_ddgs_cls:
        # ddgs v1 синхронный API — патчим на уровне модуля
        mock_instance = MagicMock()
        mock_instance.text = MagicMock(return_value=fake_ddg_output)
        mock_ddgs_cls.return_value = mock_instance

        results = await _search_duckduckgo("запрос", max_results=3)

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com"
    assert results[0]["content"] == "Текст результата"
    assert results[0]["title"] == "Test"
