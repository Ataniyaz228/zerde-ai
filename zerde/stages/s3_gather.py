"""
Stage 3: Data Gathering Agents
Вход:  QueryPlan
Выход: list[EvidenceChunk]

Агент 1 (Adilet) — Triple Fallback:
  1. XHR: GET /api/law/{id}/articles JSON
  2. CSS: парсинг .law-text p[id^='st'] через selectolax / bs4
  3. PDF+OCR: скачивание PDF → pymupdf → LLM article splitter

Агент 2 (Web) — Tavily:
  - Поиск по запросам из QueryPlan
  - WebTier classification + BLACKLIST фильтр
  - SQLite кэш
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import httpx
from ddgs import DDGS as _DDGS
from openai import AsyncOpenAI

from zerde.config import get_settings
from zerde.models import (
    AdiletFallbackStrategy,
    AdiletQuery,
    EvidenceChunk,
    LegalRank,
    QueryPlan,
    WebQuery,
    WebTier,
)
from zerde.utils.cache import CacheManager
from zerde.utils.legal_scorer import (
    classify_web_tier,
    infer_legal_rank_from_tier,
    infer_legal_rank_from_web_content,
)
from zerde.utils.llm_client import make_llm_client

logger = logging.getLogger(__name__)

# Ограничение параллельности
_ADILET_SEMAPHORE = asyncio.Semaphore(3)
_WEB_SEMAPHORE = asyncio.Semaphore(5)

# ---------------------------------------------------------------------------
# Нормализация Law ID для Adilet
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Маппинг человекочитаемых названий НПА → короткий ID
# LLM Planner часто возвращает названия вместо кодов.
# ---------------------------------------------------------------------------
_LAW_NAME_TO_SHORT_ID: dict[str, str] = {
    # Кодексы
    "гк рк (общая часть)": "1000-XIII",
    "гражданский кодекс рк (общая часть)": "1000-XIII",
    "гражданский кодекс республики казахстан (общая часть)": "1000-XIII",
    "гк рк (особенная часть)": "409-I",
    "гражданский кодекс рк (особенная часть)": "409-I",
    "гражданский кодекс республики казахстан (особенная часть)": "409-I",
    "земельный кодекс рк": "442-II",
    "земельный кодекс республики казахстан": "442-II",
    "бюджетный кодекс рк": "95-IV",
    "бюджетный кодекс республики казахстан": "95-IV",
    "трудовой кодекс рк": "414-I",
    "трудовой кодекс республики казахстан": "414-I",
    "коап": "235-V",
    "коап рк": "235-V",
    "кодекс об административных правонарушениях": "235-V",
    "кодекс об административных правонарушениях рк": "235-V",
    "ук рк": "226-V",
    "уголовный кодекс рк": "226-V",
    "уголовный кодекс республики казахстан": "226-V",
    "упк рк": "350-VI",
    "уголовно-процессуальный кодекс рк": "350-VI",
    "налоговый кодекс рк": "120-VI",
    "кодекс о здоровье народа рк": "360-VI",
    "экологический кодекс рк": "400-VI",
    "водный кодекс рк": "481-II",
    "лесной кодекс рк": "477-II",
    "таможенный кодекс рк": "123-VI",
    "предпринимательский кодекс рк": "375-V",
    # Законы
    "закон о персональных данных": "94-V",
    "закон о персональных данных рк": "94-V",
    "закон о защите персональных данных": "94-V",
    "закон о государственном имуществе": "413-IV",
    "закон о государственном имуществе рк": "413-IV",
    "закон рк о государственном имуществе": "413-IV",
    "закон о местном государственном управлении": "148-II",
    "закон о местном государственном управлении рк": "148-II",
    "закон о местном государственном управлении и самоуправлении": "148-II",
    "закон рк о местном государственном управлении и самоуправлении": "148-II",
    "закон об оценочной деятельности": "368-II",
    "закон об оценочной деятельности рк": "368-II",
    "закон о государственном регулировании": "138-IV",
    "закон об образовании": "319-III",
    "закон об образовании рк": "319-III",
    "закон рк об образовании": "319-III",
    "закон о науке": "407-II",
    "закон о науке рк": "407-II",
    "закон рк о науке": "407-II",
    "закон о секьюритизации": "122-IV",
    "закон о секьюритизации рк": "122-IV",
    "закон рк о секьюритизации": "122-IV",
    "закон об электронном документе и электронной цифровой подписи": "370-II",
    "закон об эцп": "370-II",
    "закон о государственных закупках": "434-V",
    "закон о государственных закупках рк": "434-V",
    "закон о жилищных отношениях": "94-I",
    "закон о жилищных отношениях рк": "94-I",
    "закон о банках и банковской деятельности": "2444-XII",
    "закон о банках и банковской деятельности рк": "2444-XII",
    "закон о языках": "151-I",
    "закон о языках рк": "151-I",
    "закон о нотариате": "155-V",
    "закон о нотариате рк": "155-V",
    "закон о связи": "567-II",
    "закон о связи рк": "567-II",
    "закон о разрешениях и уведомлениях": "202-V",
    "закон о разрешениях и уведомлениях рк": "202-V",
    "закон о противодействии коррупции": "410-V",
    "закон о противодействии коррупции рк": "410-V",
}


def _resolve_law_name(raw_id: str) -> str:
    """Резолвит человекочитаемое название НПА в короткий ID.

    Если raw_id уже является коротким ID или Adilet-кодом — возвращает как есть.
    Иначе ищет по точному совпадению (case-insensitive), затем по подстроке.
    """
    stripped = raw_id.strip()

    # Уже короткий ID ("550-IV") или Adilet-код ("K940001000_")?
    if re.match(r"^\d+-[IVX]+$", stripped) or re.match(r"^[A-Z]\d{8,10}_?$", stripped):
        return stripped
    if re.match(r"^\d{9,10}$", stripped):
        return stripped

    key = stripped.lower()

    # 1. Точное совпадение
    if key in _LAW_NAME_TO_SHORT_ID:
        return _LAW_NAME_TO_SHORT_ID[key]

    # 2. Подстрочный поиск ("Закон РК о государственном имуществе" содержит "государственном имуществе")
    for name, short_id in _LAW_NAME_TO_SHORT_ID.items():
        if name in key:
            return short_id

    # Не удалось резолвить — вернуть как есть, as-is fallback обработает
    return stripped


# Известные маппинги коротких ID на полные Adilet-коды
_LAW_ID_KNOWN: dict[str, str] = {
    # Законы РК
    "94-V": "Z1300000094",
    "87-IV": "Z1300000094",   # исправленное отображение ошибки в документе
    "418-V": "Z1500000418",
    "370-II": "Z030000370_",
    "550-IV": "Z1300000550",
    "274-IV": "Z100000274_",   # О защите прав потребителей (2010)
    "11-VI": "Z1600000011",    # О платежах и платежных системах (2016)
    "239-VII": "Z2500000239",  # О республиканском бюджете на 2026-2028
    "401-II": "Z0300000401",
    "73-V": "Z1300000073",
    "223-VIII": "Z1700000223",
    "240-IV": "Z1100000240",
    # Законы из проекта 2009 года
    "148-II": "Z010000148_",    # О местном госуправлении (2001)
    "368-II": "Z030000368_",    # Об оценочной деятельности (2003)
    "95-IV": "Z080000095_",     # Бюджетный кодекс (2008)
    "138-IV": "Z060000138_",    # О государственном регулировании (2006)
    "413-IV": "Z1100000413",    # О государственном имуществе (2011)
    "414-IV": "Z1100000414",    # О внесении изменений (2011)
    # Кодексы РК
    "235-V": "K1400000235",     # КоАП
    "226-V": "K1400000226",     # УК
    "231-V": "K1400000231",     # УПК
    "377-V": "K1500000377",     # ГПК
    "214-VII": "K2500000214",   # Налоговый кодекс
    "414-I-NEW": "K1500000414", # Трудовой кодекс (новые файлы)
    "350-VI": "K2000000350",
    "212-IV": "K070000212_",
    "1000-XIII": "K940001000_", # ГК (Общая часть)
    "409-I": "K990000409_",     # ГК (Особенная часть)
    "442-II": "K030000442_",    # Земельный кодекс
    "414-I": "K150000414_",     # Трудовой кодекс
    # Уголовный кодекс
    "226-V-UK": "K1400000226",
}

_LAW_ID_PREFIX_MAP: dict[str, str] = {
    "I": "Z",    # Законы РК (I, II, III, IV, V, VI...)
    "V": "Z",
    "IV": "Z",
    "III": "Z",
    "II": "Z",
    "VI": "Z",
    "VII": "Z",
    "VIII": "Z",
}


def _normalize_law_id_to_adilet_urls(law_id: str, base: str) -> list[str]:
    """
    Преобразует ID (любого формата) в список URL-вариантов для Adilet.

    Поддерживаемые форматы:
      - '148-II' — краткий с римским суффиксом
      - 'Z010000148' — полу-нормализованный (от LLM planner)
      - '940001000' — голый числовой код (от LLM planner)
      - 'K940001000_' — полный Adilet-код
    """
    law_id = law_id.replace("\u0406", "I").replace("\u0456", "i")

    # Шаг 0: Резолвим человекочитаемые названия в короткие ID
    law_id = _resolve_law_name(law_id)

    urls: list[str] = []

    # 1. Known exact mapping
    if law_id in _LAW_ID_KNOWN:
        adilet_code = _LAW_ID_KNOWN[law_id]
        urls.append(f"{base}/rus/docs/{adilet_code}")
        return urls

    # 2. Уже полный Adilet-код? (K940001000_, Z1300000094, P090000447_)
    if re.match(r"^[A-Z]\d{9}", law_id):
        urls.append(f"{base}/rus/docs/{law_id}")
        # Пробуем с/без '_' суффикса
        if law_id.endswith("_"):
            urls.append(f"{base}/rus/docs/{law_id[:-1]}")
        else:
            urls.append(f"{base}/rus/docs/{law_id}_")
        return urls

    # 3. Полу-Adilet код от planner: "Z010000148" (без _), "Z110000413"
    if re.match(r"^[ZKP]\d{8,9}$", law_id):
        urls.append(f"{base}/rus/docs/{law_id}")
        urls.append(f"{base}/rus/docs/{law_id}_")
        return urls

    # 4. Голый числовой код от planner: "940001000"
    if re.match(r"^\d{9,10}$", law_id):
        for prefix in ("K", "Z"):
            urls.append(f"{base}/rus/docs/{prefix}{law_id}")
            urls.append(f"{base}/rus/docs/{prefix}{law_id}_")
        return urls

    # 5. Генерик: "{num}-{suffix}" → перебор годов + оба формата (с _ и без)
    m = re.match(r"^(\d+)-([A-Z]+)$", law_id)
    if m:
        num_str = m.group(1)
        suffix = m.group(2)

        year_map = {
            "I": ["90", "91", "92", "93", "94", "95", "96", "97", "98", "99"],
            "II": ["00", "01", "02", "03", "04", "05"],
            "III": ["06", "07"],
            "IV": ["08", "09", "10", "11", "12"],
            "V": ["13", "14", "15"],
            "VI": ["16", "17", "18"],
            "VII": ["19", "20"],
            "VIII": ["21", "22", "23", "24"],
        }
        years = year_map.get(suffix, ["13", "14", "15"])
        prefix = "K" if int(num_str) > 200 and suffix in ("IV", "V", "VI") else "Z"

        for yr in years:
            padded = num_str.zfill(7)
            code_base = f"{prefix}{yr}{padded}"

            url_underscore = f"{base}/rus/docs/{code_base}_"
            url_plain = f"{base}/rus/docs/{code_base}"

            try:
                yr_val = int(yr)
                is_legacy_format = yr_val < 12 or yr_val >= 90
            except ValueError:
                is_legacy_format = True

            if is_legacy_format:
                if url_underscore not in urls:
                    urls.append(url_underscore)
                if url_plain not in urls:
                    urls.append(url_plain)
            else:
                if url_plain not in urls:
                    urls.append(url_plain)
                if url_underscore not in urls:
                    urls.append(url_underscore)

    # 6. As-is fallback
    as_is = f"{base}/rus/docs/{law_id}"
    if as_is not in urls:
        urls.append(as_is)

    return urls


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def gather_evidence(plan: QueryPlan) -> list[EvidenceChunk]:
    """Этап 3: Параллельный сбор из Адилет и Web."""
    settings = get_settings()
    cache = CacheManager(settings.cache_db_path)

    logger.info(f"[S3] Gathering evidence. plan={plan.plan_id[:8]}… total_queries={plan.total_queries}")

    adilet_task = _run_adilet_agent(plan.adilet_queries, cache)
    web_queries = plan.web_queries_ru + plan.web_queries_kk + plan.web_queries_en
    web_task = _run_web_agent(web_queries, cache)

    adilet_chunks, web_chunks = await asyncio.gather(adilet_task, web_task)
    all_chunks = adilet_chunks + web_chunks

    # Сохраняем новые в кэш
    await cache.put_many([c for c in all_chunks if c.chunk_id])

    logger.info(f"[S3] Done. adilet={len(adilet_chunks)} web={len(web_chunks)} total={len(all_chunks)}")
    return all_chunks


# ---------------------------------------------------------------------------
# Agent 1: Adilet — Triple Fallback
# ---------------------------------------------------------------------------


async def _run_adilet_agent(queries: list[AdiletQuery], cache: CacheManager) -> list[EvidenceChunk]:
    tasks = [_fetch_adilet_with_fallback(q, cache) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    chunks: list[EvidenceChunk] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.warning(f"[S3/Adilet] Query {i} failed: {r}")
        elif isinstance(r, list):
            chunks.extend(r)
    return chunks


async def _fetch_adilet_with_fallback(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    async with _ADILET_SEMAPHORE:
        # XHR пропускаем — Adilet не имеет JSON API (всегда 404).
        # Сразу CSS → PDF.
        for strategy_fn in [_try_adilet_css_selectors, _try_adilet_pdf_ocr]:
            try:
                chunks = await strategy_fn(query, cache)
                if chunks:
                    logger.debug(f"[S3/Adilet] {strategy_fn.__name__} OK: {len(chunks)} chunks")
                    return chunks
            except NotImplementedError:
                continue
            except Exception as e:
                logger.warning(f"[S3/Adilet] {strategy_fn.__name__} failed: {e}")

        # Local DB fallback if CSS/PDF/API failed
        logger.warning(f"[S3/Adilet] All strategies failed for: '{query.query_text[:50]}'. Fallback to search_local.")
        try:
            chunks = await cache.search_local(query.query_text, law_ids=query.law_ids, articles=query.articles)
            if chunks:
                logger.info(f"[S3/Adilet] search_local found {len(chunks)} chunks for query: '{query.query_text[:50]}'")
                # Mark strategy
                for c in chunks:
                    c.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                return chunks
        except Exception as e:
            logger.error(f"[S3/Adilet] search_local fallback failed: {e}")

        logger.error(f"[S3/Adilet] All fallbacks failed for: '{query.query_text[:50]}'")
        return []


# Fallback 1: XHR API
async def _try_adilet_xhr(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """
    Пробует найти JSON API Адилет для каждого law_id.
    Известные endpoints:
      GET /api/docs/{id} — метаданные
      GET /api/docs/{id}/articles — список статей (если есть)
    """
    if not query.law_ids:
        return []

    settings = get_settings()
    chunks: list[EvidenceChunk] = []
    base = str(settings.adilet_base_url).rstrip("/")

    async with httpx.AsyncClient(
        timeout=settings.adilet_timeout_seconds,
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "ZERDE/6.2"},
    ) as client:
        for law_id in query.law_ids:
            # Получаем все варианты URL для этого law_id
            candidate_urls = _normalize_law_id_to_adilet_urls(law_id, base)

            for candidate_url in candidate_urls[:4]:  # Пробуем первые 4 варианта
                # Попытка 1: JSON API
                api_url = candidate_url.rstrip("/") + "/articles"
                try:
                    resp = await client.get(api_url.replace("/rus/docs/", "/rus/api/docs/"))
                    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                        data = resp.json()
                        parsed = _parse_adilet_json_response(data, law_id, query)
                        if parsed:
                            chunks.extend(parsed)
                            break
                except Exception:
                    pass

    return chunks


def _parse_adilet_json_response(data: dict | list, law_id: str, query: AdiletQuery) -> list[EvidenceChunk]:
    """Парсит JSON ответ API Адилет → EvidenceChunk list."""
    chunks = []
    items = data if isinstance(data, list) else data.get("articles", data.get("items", []))

    if not isinstance(items, list):
        return []

    settings = get_settings()
    base = str(settings.adilet_base_url).rstrip("/")

    for item in items[:settings.adilet_max_articles_per_law]:
        content = item.get("text", item.get("content", "")).strip()
        article_num = str(item.get("article", item.get("number", item.get("num", ""))))
        if not content or len(content) < 20:
            continue

        chunk_id = hashlib.sha256(content.encode()).hexdigest()
        url = f"{base}/rus/docs/{law_id}#{article_num}"

        chunks.append(EvidenceChunk(
            chunk_id=chunk_id,
            source_url=url,
            source_title=item.get("title", f"Статья {article_num}"),
            content=content,
            legal_rank=LegalRank.LAW_RK,
            law_id=law_id,
            article=article_num,
            effective_date=_parse_date_safe(item.get("date")),
            adilet_fallback_used=AdiletFallbackStrategy.XHR,
        ))

    return chunks


# Wayback Machine URL prefix для fallback при недоступности Adilet
_WAYBACK_PREFIX = "https://web.archive.org/web/2024/"


# Fallback 2: CSS Selectors (с Wayback Machine fallback)
async def _try_adilet_css_selectors(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """
    Парсит HTML страницы НПА через CSS-селекторы.
    Гранулярность: 1 чанк = 1 статья.
    Селекторы: p[id^='st'], p[id^='z'], .law-article, .article-content

    При ConnectTimeout к adilet.zan.kz автоматически пробует Wayback Machine.
    """
    settings = get_settings()
    base = str(settings.adilet_base_url).rstrip("/")
    chunks: list[EvidenceChunk] = []

    urls_to_try: list[str] = []

    # Строим URL для каждого law_id, нормализуя формат
    for law_id in query.law_ids:
        candidate_urls = _normalize_law_id_to_adilet_urls(law_id, base)
        urls_to_try.extend(candidate_urls[:3])  # Топ-3 варианта для каждого ID

    # Если нет law_ids — пробуем поиск
    if not urls_to_try:
        urls_to_try = await _search_adilet_for_query(query, base)

    adilet_is_down = False  # Флаг: прямой Adilet недоступен

    async with httpx.AsyncClient(
        timeout=settings.adilet_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        },
    ) as client:
        seen_urls: set[str] = set()
        for url in urls_to_try[:6]:  # Максимум 6 страниц
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # --- Попытка 1: Прямой запрос к Adilet ---
            if not adilet_is_down:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        html = resp.text
                        page_chunks = _parse_adilet_html(html, url, query)
                        chunks.extend(page_chunks)
                        logger.debug(f"[S3/CSS] {url}: {len(page_chunks)} articles parsed")
                        if page_chunks:
                            break
                        continue
                except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
                    logger.warning(f"[S3/CSS] Adilet unreachable ({type(e).__name__}), switching to Wayback Machine")
                    adilet_is_down = True  # Переключаемся на Wayback для всех следующих URL
                except Exception as e:
                    logger.warning(f"[S3/CSS] Failed {url}: {e}")

            # --- Попытка 2: Wayback Machine fallback ---
            wayback_url = _WAYBACK_PREFIX + url
            try:
                resp = await client.get(wayback_url)
                if resp.status_code != 200:
                    logger.debug(f"[S3/CSS/Wayback] {wayback_url}: status {resp.status_code}")
                    continue

                html = resp.text
                # Wayback добавляет свой toolbar — парсер справится
                page_chunks = _parse_adilet_html(html, url, query)  # source_url = оригинальный URL
                chunks.extend(page_chunks)
                if page_chunks:
                    logger.info(f"[S3/CSS/Wayback] {url}: {len(page_chunks)} articles via Wayback Machine")
                    break
                else:
                    logger.debug(f"[S3/CSS/Wayback] {url}: page fetched but 0 articles parsed")

            except Exception as e:
                logger.warning(f"[S3/CSS/Wayback] Failed {wayback_url}: {e}")

    return chunks


async def _search_adilet_for_query(query: AdiletQuery, base: str) -> list[str]:
    """Поиск на adilet.zan.kz по query_text."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{base}/rus/search",
                params={"q": query.query_text, "type": "docs"},
            )
            if resp.status_code != 200:
                return []

            # Извлекаем ссылки на НПА из результатов поиска
            from selectolax.parser import HTMLParser
            tree = HTMLParser(resp.text)
            links = []
            for a in tree.css("a.search-result-link, a.doc-link, .search-item a"):
                href = a.attributes.get("href", "")
                if "/docs/" in href or "/rus/docs/" in href:
                    full_url = urljoin(base, href)
                    links.append(full_url)
            return links[:5]
    except Exception:
        return []


def _parse_adilet_html(html: str, source_url: str, query: AdiletQuery) -> list[EvidenceChunk]:
    """
    Парсит HTML страницы Адилет в список EvidenceChunk.
    Один чанк = одна статья.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    chunks: list[EvidenceChunk] = []

    # Извлекаем мета: название закона
    law_title = ""
    for sel in ["h1.doc-title", "h1", ".document-title", "title"]:
        node = tree.css_first(sel)
        if node:
            law_title = node.text(strip=True)
            break

    # Извлекаем ID закона из URL
    law_id_match = re.search(r"/docs/([A-Z]\d+)", source_url, re.IGNORECASE)
    law_id = law_id_match.group(1) if law_id_match else ""

    # Эвристическая дата
    effective_date: date | None = None
    date_node = tree.css_first(".doc-date, .effective-date, [data-date]")
    if date_node:
        effective_date = _parse_date_safe(date_node.text(strip=True))

    # Основные CSS-селекторы для статей
    article_selectors = [
        "p[id^='st']",           # Адилет основной формат
        ".law-article",
        ".article-text",
        "div[class*='article']",
        "p[class*='article']",
    ]

    articles_found = False
    _ARTICLE_TITLE_RE = re.compile(r"^\s*(Статья|Бап|Article)\s+(\d+[\-\d]*)", re.IGNORECASE)

    for selector in article_selectors:
        nodes = tree.css(selector)
        if not nodes:
            continue
        articles_found = True

        for node in nodes[:80]:  # Максимум 80 статей
            article_text = node.text(strip=True)
            if len(article_text) < 30:
                continue

            # Извлекаем номер статьи
            article_num = _extract_article_number(node.attributes.get("id", ""), article_text)

            # Фильтр по запрошенным статьям
            if query.articles and article_num and article_num not in query.articles:
                continue

            chunk_id = hashlib.sha256(article_text.encode()).hexdigest()
            anchor = f"#{node.attributes.get('id', article_num)}" if article_num else ""

            chunks.append(EvidenceChunk(
                chunk_id=chunk_id,
                source_url=source_url + anchor,
                source_title=f"{law_title} | Ст. {article_num}" if article_num else law_title,
                content=article_text,
                legal_rank=_infer_adilet_rank(law_title),
                law_id=law_id,
                law_title=law_title,
                article=article_num,
                effective_date=effective_date,
                adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
            ))
        break

    # Стратегия h3: для Adilet/Wayback где статьи в <h3>
    # Каждый <h3> "Статья N." + текст siblings до следующего <h3>
    if not chunks:
        h3_nodes = tree.css("h3")
        art_h3 = [(n, _ARTICLE_TITLE_RE.match(n.text(strip=True))) for n in h3_nodes]
        art_h3 = [(n, m) for n, m in art_h3 if m]

        if art_h3:
            articles_found = True
            # Если есть фильтр по статьям — проходим все h3, иначе первые 80
            scan_limit = len(art_h3) if query.articles else 80
            for node, match in art_h3[:scan_limit]:
                article_num = match.group(2)

                # Фильтр по запрошенным статьям
                if query.articles and article_num and article_num not in query.articles:
                    continue

                # Собираем текст: h3 + все siblings до следующего h3
                text_parts = [node.text(strip=True)]
                sibling = node.next
                while sibling:
                    if sibling.tag == "h3":
                        break
                    sib_text = sibling.text(strip=True)
                    if sib_text:
                        text_parts.append(sib_text)
                    sibling = sibling.next

                article_text = " ".join(text_parts)
                if len(article_text) < 30:
                    continue

                chunk_id = hashlib.sha256(article_text.encode()).hexdigest()
                chunks.append(EvidenceChunk(
                    chunk_id=chunk_id,
                    source_url=source_url + f"#art{article_num}",
                    source_title=f"{law_title} | Ст. {article_num}",
                    content=article_text,
                    legal_rank=_infer_adilet_rank(law_title),
                    law_id=law_id,
                    law_title=law_title,
                    article=article_num,
                    effective_date=effective_date,
                    adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
                ))

    # Если статьи не найдены через селекторы — берём весь body как один чанк
    if not articles_found or not chunks:
        main_content = tree.css_first(".law-content, .document-body, main, article")
        if main_content:
            text = main_content.text(strip=True)
            if len(text) > 100:
                chunk_id = hashlib.sha256(text.encode()).hexdigest()
                chunks.append(EvidenceChunk(
                    chunk_id=chunk_id,
                    source_url=source_url,
                    source_title=law_title or "НПА",
                    content=text[:8000],  # Ограничение
                    legal_rank=_infer_adilet_rank(law_title),
                    law_id=law_id,
                    law_title=law_title,
                    adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
                ))

    return chunks


# Fallback 3: PDF OCR + LLM article splitter
async def _try_adilet_pdf_ocr(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """
    Скачивает PDF версию НПА → извлекает текст через pymupdf → LLM сплиттер.
    """
    if not query.law_ids:
        return []

    settings = get_settings()
    base = str(settings.adilet_base_url).rstrip("/")
    client = make_llm_client(settings)
    chunks: list[EvidenceChunk] = []

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
        for law_id in query.law_ids[:2]:  # Максимум 2 PDF
            candidate_urls = _normalize_law_id_to_adilet_urls(law_id, base)

            pdf_bytes: bytes | None = None
            pdf_url_used = ""
            for candidate_base_url in candidate_urls[:3]:
                # Попытка найти PDF
                for pdf_url in [candidate_base_url + "/download", candidate_base_url.replace("/docs/", "/download/")]:
                    try:
                        resp = await http.get(pdf_url)
                        if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", "").lower():
                            pdf_bytes = resp.content
                            pdf_url_used = pdf_url
                            break
                    except Exception:
                        pass
                if pdf_bytes:
                    break

            if not pdf_bytes or len(pdf_bytes) < 1000:
                continue

            try:
                text = _extract_pdf_bytes(pdf_bytes)
                if len(text.strip()) < 200:
                    continue

                article_chunks = await _llm_split_articles(
                    text=text,
                    law_id=law_id,
                    source_url=pdf_url_used,
                    client=client,
                    model=settings.llm_model_planner,
                    query=query,
                )
                chunks.extend(article_chunks)
                logger.info(f"[S3/PDFOcr] {law_id}: {len(article_chunks)} articles via LLM split")

            except Exception as e:
                logger.warning(f"[S3/PDFOcr] Failed {law_id}: {e}")

    return chunks


def _extract_pdf_bytes(pdf_bytes: bytes) -> str:
    """Извлекает текст из PDF bytes через pymupdf."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = [page.get_text("text") for page in doc]  # type: ignore[arg-type]
    doc.close()
    return "\n".join(pages)


async def _llm_split_articles(
    text: str,
    law_id: str,
    source_url: str,
    client: AsyncOpenAI,
    model: str,
    query: AdiletQuery,
) -> list[EvidenceChunk]:
    """Использует LLM для разбивки текста НПА на статьи."""
    # Берём первые 12000 символов для экономии токенов
    sample = text[:12000]

    prompt = f"""Разбей следующий текст нормативно-правового акта на отдельные статьи.
Для каждой статьи верни JSON-объект с полями: article_num (номер статьи), title (заголовок если есть), content (текст статьи).
Верни JSON массив: [{{"article_num": "1", "title": "...", "content": "..."}}]

Текст НПА:
{sample}
"""
    try:
        response = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=4096,
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON. Ключ верхнего уровня: 'articles'."},
                {"role": "user", "content": prompt},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        articles = data.get("articles", [])
    except Exception as e:
        logger.warning(f"[S3/PDFOcr/LLM] Split failed: {e}")
        # Fallback: regex сплиттер
        articles = _regex_split_articles(text)

    chunks = []
    for art in articles:
        content = str(art.get("content", "")).strip()
        article_num = str(art.get("article_num", ""))

        # Фильтр по запрошенным статьям
        if query.articles and article_num and article_num not in query.articles:
            continue

        if len(content) < 30:
            continue

        chunk_id = hashlib.sha256(content.encode()).hexdigest()
        chunks.append(EvidenceChunk(
            chunk_id=chunk_id,
            source_url=source_url,
            source_title=f"НПА {law_id} | Ст. {article_num}",
            content=content,
            legal_rank=LegalRank.LAW_RK,
            law_id=law_id,
            article=article_num,
            adilet_fallback_used=AdiletFallbackStrategy.PDF_OCR,
        ))

    return chunks


def _regex_split_articles(text: str) -> list[dict]:
    """Regex fallback для разбивки текста на статьи (поддерживает русский, казахский и английский форматы)."""
    pattern = re.compile(
        r"(?:(?:Статья|Article)\s+(\d+[\-\d]*)|(\d+[\-\d]*)-(?:бап|бабы|бабының|бапта))\s*[.\n]([^\n]*)\n(.*?)(?=(?:(?:Статья|Article)\s+\d|(?:\d+)-(?:бап|бабы|бабының|бапта))|$)",
        re.DOTALL | re.IGNORECASE,
    )
    articles = []
    for m in pattern.finditer(text):
        art_num = m.group(1) or m.group(2)
        content = (m.group(3).strip() + "\n" + m.group(4).strip()).strip()
        articles.append({"article_num": art_num, "title": "", "content": content[:3000]})
    return articles



# ---------------------------------------------------------------------------
# Agent 2: Web Search (Multi-Provider: Tavily, Serper, Google, DuckDuckGo)
# ---------------------------------------------------------------------------


async def _run_web_agent(queries: list[WebQuery], cache: CacheManager) -> list[EvidenceChunk]:
    tasks = [_fetch_web_query(q, cache) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    chunks: list[EvidenceChunk] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.warning(f"[S3/Web] Query {i} failed: {r}")
        elif isinstance(r, list):
            chunks.extend(r)
    return chunks


async def _search_tavily(query_text: str, max_results: int, include_domains: list[str] | None = None) -> list[dict]:
    """Поиск через Tavily API."""
    settings = get_settings()
    if not settings.tavily_api_key:
        return []
    from tavily import TavilyClient
    tavily = TavilyClient(api_key=settings.tavily_api_key)
    search_params: dict = {
        "query": query_text,
        "max_results": max_results,
        "search_depth": "advanced",
        "exclude_domains": ["reddit.com", "forum", "otvet.mail.ru", "pikabu.ru", "answers.yahoo.com"]
    }
    if include_domains:
        search_params["include_domains"] = include_domains

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: tavily.search(**search_params),
    )
    results = response.get("results", [])
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", r.get("raw_content", "")).strip()
        }
        for r in results
    ]


async def _search_serper(query_text: str, max_results: int) -> list[dict]:
    """Поиск через Serper.dev API."""
    settings = get_settings()
    if not settings.serper_api_key:
        return []
    import httpx
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json"
    }
    payload = {
        "q": query_text,
        "num": max_results
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            organic = data.get("organic", [])
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "content": r.get("snippet", "").strip()
                }
                for r in organic
            ]
        else:
            logger.warning(f"[S3/Web] Serper error: {resp.status_code} - {resp.text}")
    return []


async def _search_google(query_text: str, max_results: int) -> list[dict]:
    """Поиск через Google Custom Search API."""
    settings = get_settings()
    if not settings.google_api_key or not settings.google_cse_id:
        return []
    import httpx
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": settings.google_api_key,
        "cx": settings.google_cse_id,
        "q": query_text,
        "num": max_results
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "content": r.get("snippet", "").strip()
                }
                for r in items
            ]
        else:
            logger.warning(f"[S3/Web] Google CSE error: {resp.status_code} - {resp.text}")
    return []


async def _search_duckduckgo(query_text: str, max_results: int) -> list[dict]:
    """Асинхронный поиск через библиотеку ddgs (DuckDuckGo).

    Используем asyncio.to_thread() для запуска синхронного DDGS.text() без блокировки event loop.
    """
    def _sync_search() -> list[dict]:
        results = _DDGS().text(query_text, max_results=max_results)
        if results:
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("url", "")),
                    "content": r.get("body", r.get("snippet", "")).strip()
                }
                for r in results
            ]
        return []

    try:
        return await asyncio.to_thread(_sync_search)
    except Exception as e:
        logger.error(f"[S3/Web] DDGS search failed: {e}")
    return []


async def _search_web(query: WebQuery) -> tuple[list[dict], str]:
    """
    Поочередно опрашивает настроенных провайдеров с таймаутом 10.0 сек.
    Соблюдает приоритеты и автоматически откатывается на бесплатный DuckDuckGo / Local Cache.
    """
    settings = get_settings()
    providers = []

    # Формируем цепочку провайдеров на основе приоритета
    if settings.search_provider == "tavily" and settings.tavily_api_key:
        providers.append(("tavily", lambda q, m: _search_tavily(q, m, query.include_domains)))
    if settings.serper_api_key:
        providers.append(("serper", _search_serper))
    if settings.google_api_key and settings.google_cse_id:
        providers.append(("google", _search_google))

    # DuckDuckGo всегда идет как универсальный бесплатный fallback
    providers.append(("duckduckgo", _search_duckduckgo))

    for name, search_func in providers:
        try:
            logger.info(f"[S3/Web] Attempting search via {name} for query: '{query.query_text[:50]}'")
            
            # Подготовка запроса с ограничением домена для не-Tavily провайдеров
            final_query = query.query_text
            if query.include_domains and name != "tavily":
                site_queries = [f"site:{d}" for d in query.include_domains]
                if site_queries:
                    final_query = f"{final_query} ({' OR '.join(site_queries)})"

            async with asyncio.timeout(10.0):
                results = await search_func(final_query, query.max_results)
                if results:
                    logger.info(f"[S3/Web] Search successful using provider: {name}")
                    return results, name
        except asyncio.TimeoutError:
            logger.warning(f"[S3/Web] Provider {name} timed out after 10.0 seconds.")
        except Exception as e:
            logger.warning(f"[S3/Web] Provider {name} failed: {e}")

    return [], "none"


async def _fetch_web_query(query: WebQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """Выполняет один веб-запрос с автоматическим переключением провайдеров и оффлайн-кэшем."""
    async with _WEB_SEMAPHORE:
        results, provider = await _search_web(query)

        if not results:
            logger.warning(f"[S3/Web] All search providers failed or returned empty results for: '{query.query_text[:50]}'. Falling back to local offline search.")
            try:
                chunks = await cache.search_local(query.query_text)
                if chunks:
                    for chunk in chunks:
                        chunk.search_provider = "local"
                    logger.info(f"[S3/Web] local search found {len(chunks)} chunks for query: '{query.query_text[:50]}'")
                    return chunks
            except Exception as e:
                logger.error(f"[S3/Web] local search fallback failed: {e}")
            return []

        chunks: list[EvidenceChunk] = []
        for result in results:
            chunk = _build_web_chunk(result, query, provider)
            if chunk:
                # Проверяем кэш
                cached = await cache.get(chunk.chunk_id)
                if cached:
                    # Обновляем провайдера на кэшированном объекте для прозрачности
                    cached.search_provider = provider
                    chunks.append(cached)
                else:
                    chunks.append(chunk)

        logger.debug(f"[S3/Web] '{query.query_text[:40]}': {len(chunks)} chunks via {provider}")
        return chunks


def _extract_law_id_from_url(url: str) -> str | None:
    """Извлекает и нормализует law_id из URL-адреса Adilet/Wayback."""
    url_lower = url.lower()
    # Ищем паттерн /docs/[ZKP]\d{8,10}
    match = re.search(r"/docs/([zkp]\d{8,10}_?)", url_lower)
    if match:
        code = match.group(1).upper()
        # Проверяем известное соответствие короткому ID
        for short_id, full_code in _LAW_ID_KNOWN.items():
            if full_code.upper().rstrip("_") == code.rstrip("_"):
                return short_id
        return code
    return None


def _extract_law_id_from_text(title: str, content: str) -> str | None:
    """Извлекает закон (law_id) из веб-результатов с помощью регулярных выражений и ключевых слов."""
    text = (title + " " + content).lower()
    
    # 1. Regex match for standard law formats in Kazakhstan (e.g. 94-V, 226-V, 413-IV)
    import re
    match = re.search(r"\b\d+[-‐–][IVXivx\u0406\u0456]{1,5}\b", text)
    if match:
        return match.group(0).upper().replace(" ", "").replace("\u0406", "I").replace("\u0456", "i")
        
    # 2. Common code / major law mappings — Russian
    if "гражданск" in text or " гк" in text:
        return "1000-XIII" # Гражданский кодекс РК
    if "уголовн" in text or " ук рк" in text:
        return "226-V" # Уголовный кодекс РК
    if "коап" in text or "об административных правонарушениях" in text:
        return "235-V" # КоАП РК
    if "земельн" in text or " зк" in text:
        return "442-II" # Земельный кодекс РК
    if "предпринимательск" in text or " пк" in text:
        return "Предпринимательский кодекс"
    if "налогов" in text or " нк" in text:
        return "Налоговый кодекс"
    if "бюджетн" in text or " бк" in text:
        return "Бюджетный кодекс"
    if "трудов" in text or " тк" in text:
        return "Трудовой кодекс"
    if "экологическ" in text or " эк" in text:
        return "Экологический кодекс"
    if "персональных данных" in text or "закон о пд" in text or " 94-v" in text:
        return "94-V" # Закон о персональных данных

    # 3. Common code / major law mappings — Kazakh
    if "әқбтк" in text or "әкімшілік құқық бұзушылық" in text:
        return "235-V"  # КоАП РК (ӘҚБтК)
    if "қылмыстық кодекс" in text or " ққ" in text:
        return "226-V"  # УК РК (ҚК)
    if "азаматтық кодекс" in text or " ак" in text:
        return "1000-XIII"  # ГК РК (АК)
    if "жер кодексі" in text or " жк" in text:
        return "442-II"  # ЗК РК (ЖК)
    if "кәсіпкерлік кодекс" in text:
        return "Предпринимательский кодекс"  # ПК (Кәсіпкерлік кодексі)
    if "салық кодексі" in text:
        return "Налоговый кодекс"  # НК (Салық кодексі)
    if "еңбек кодексі" in text:
        return "Трудовой кодекс"  # ТК (Еңбек кодексі)
    if "экологиялық кодекс" in text:
        return "Экологический кодекс"  # ЭК (Экологиялық кодекс)
    if "дербес деректер" in text:
        return "94-V"  # Закон о персональных данных (Дербес деректер туралы)
    if "рақымшылық" in text or "амнистия" in text:
        return "235-V"  # Амнистия = КоАП/УК context
        
    return None


def _build_web_chunk(result: dict, query: WebQuery, provider: str | None = None) -> EvidenceChunk | None:
    """Конвертирует результат поиска → EvidenceChunk. None для BLACKLIST."""
    url: str = result.get("url", "")
    if not url:
        return None

    title: str = result.get("title", "").lower()
    url_lower = url.lower()
    trash_keywords = ["chsi", "исполнител", "судебный пристав", "пристав", "судебного исполнителя", "судебных исполнителей", "судебным исполнителям"]
    if any(kw in url_lower or kw in title for kw in trash_keywords):
        logger.info(f"[S3/Web] Blocking judicial executor/trash source: url={url} title='{result.get('title')}'")
        return None

    tier = classify_web_tier(url)
    if tier == WebTier.BLACKLIST:
        logger.debug(f"[S3/Web] Blacklisted: {url}")
        return None

    content = result.get("content", "").strip()
    if len(content) < 50:
        return None

    chunk_id = hashlib.sha256(content.encode()).hexdigest()

    title = result.get("title", "")
    inferred_rank, confidence, reason = infer_legal_rank_from_web_content(
        tier, title, content, url
    )

    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=url,
        source_title=title or urlparse(url).netloc,
        content=content,
        content_summary=content[:300],
        legal_rank=inferred_rank,
        web_tier=tier,
        search_provider=provider,
        law_id=_extract_law_id_from_url(url) or _extract_law_id_from_text(title, content),
        inferred_rank=inferred_rank,
        inferred_rank_confidence=confidence,
        inference_reason=reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_adilet_rank(law_title: str) -> LegalRank:
    """Определяет LegalRank по названию НПА (русский + казахский)."""
    title_lower = law_title.lower()
    if any(w in title_lower for w in ["кодекс", "kodeks", "кодексі"]):
        return LegalRank.CODE
    if any(w in title_lower for w in ["конституционный закон", "конституциялық заң"]):
        return LegalRank.CONSTITUTIONAL_LAW
    if any(w in title_lower for w in ["международный", "конвенция", "договор", "халықаралық", "конвенция", "шарт"]):
        return LegalRank.INTERNATIONAL_TREATY
    if any(w in title_lower for w in ["указ президента", "президент жарлығы"]):
        return LegalRank.PRESIDENTIAL_DECREE
    if any(w in title_lower for w in ["постановление правительства", "ппрк", "үкімет қаулысы"]):
        return LegalRank.GOVERNMENT_RESOLUTION
    if any(w in title_lower for w in ["приказ", "инструкция", "бұйрық", "нұсқаулық"]):
        return LegalRank.MINISTERIAL_ORDER
    return LegalRank.LAW_RK


def _extract_article_number(node_id: str, text: str) -> str:
    """Извлекает номер статьи из id атрибута или текста."""
    # Из id: st15, st15_1, article-15
    id_match = re.search(r"(?:st|article[-_]?)(\d+[\-_]?\d*)", node_id, re.IGNORECASE)
    if id_match:
        return id_match.group(1).replace("_", "-")

    # Из текста: "Статья 15.", "Бап 15."
    text_match = re.match(r"(?:Статья|Бап|Article)\s+(\d+[\-\d]*)", text[:50], re.IGNORECASE)
    if text_match:
        return text_match.group(1)

    return ""


def _parse_date_safe(val: object) -> date | None:
    """Безопасный парсинг даты из строки."""
    if not val:
        return None
    s = str(val).strip()
    # Форматы: YYYY-MM-DD, DD.MM.YYYY
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None
