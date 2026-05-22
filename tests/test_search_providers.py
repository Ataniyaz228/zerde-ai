"""
test_search_providers.py
Тесты цепочки фоллбэка веб-поиска (Tavily → Serper → Google → DuckDuckGo → Local)
Использует только mock'u (asyncio/unittest.mock) — без сетевых запросов.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zerde.models import WebQuery, WebTier

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


async def _mock_timeout(query: str, max_results: int) -> list[dict]:
    await asyncio.sleep(15)  # больше 10 секундного timeout
    return FAKE_RESULTS


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
async def test_search_web_respects_10s_timeout():
    """
    Провайдер, висящий >10с, должен пропускаться, и следующий провайдер должен выполниться.
    """
    from zerde.stages.s3_gather import _search_web

    # Tavily висит (>10с), Serper висит (>10с), DDG отвечает быстро
    with patch("zerde.stages.s3_gather._search_tavily", new=_mock_timeout), \
         patch("zerde.stages.s3_gather._search_serper", new=_mock_timeout), \
         patch("zerde.stages.s3_gather._search_google", new=_mock_fail), \
         patch("zerde.stages.s3_gather._search_duckduckgo", new=_mock_success):

        q = _make_query("тест")
        start = asyncio.get_event_loop().time()
        results, provider = await _search_web(q)
        elapsed = asyncio.get_event_loop().time() - start

    # Два timeout по 10с + DDG — должно быть до 22с (с запасом)
    assert provider == "duckduckgo"
    assert len(results) > 0
    # Не висим вечно: каждый timeout ограничен 10с
    assert elapsed < 25, f"Timeout not enforced: elapsed {elapsed:.1f}s"


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
