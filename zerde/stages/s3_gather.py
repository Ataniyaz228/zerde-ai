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
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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
from zerde.utils.legal_scorer import classify_web_tier, infer_legal_rank_from_tier
from zerde.utils.llm_client import make_llm_client

logger = logging.getLogger(__name__)

# Ограничение параллельности
_ADILET_SEMAPHORE = asyncio.Semaphore(3)
_WEB_SEMAPHORE = asyncio.Semaphore(5)

# ---------------------------------------------------------------------------
# Нормализация Law ID для Adilet
# ---------------------------------------------------------------------------

# Известные маппинги коротких ID на полные Adilet-коды
_LAW_ID_KNOWN: dict[str, str] = {
    # Законы РК
    "94-V": "Z1300000094",
    "87-IV": "Z1300000094",   # исправленное отображение ошибки в документе
    "418-V": "Z1500000418",
    "370-II": "Z030000370_",
    "550-IV": "Z1300000550",
    "401-II": "Z0300000401",
    "73-V": "Z1300000073",
    "223-VIII": "Z1700000223",
    "240-IV": "Z1100000240",
    # Кодексы РК
    "235-V": "K1400000235",
    "226-V": "K1400000226",
    "350-VI": "K2000000350",
    "212-IV": "K070000212_",
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
    Преобразует краткий ID (напр. '94-V') в список URL-вариантов для Adilet.
    Приоритет: known map → минимальные эвристики.
    """
    urls: list[str] = []

    # 1. Known exact mapping — если есть, возвращаем ONLY this
    if law_id in _LAW_ID_KNOWN:
        adilet_code = _LAW_ID_KNOWN[law_id]
        urls.append(f"{base}/rus/docs/{adilet_code}")
        return urls  # Точный маппинг — не нужны переборы

    # 2. Генерик преобразование для неизвестных ID (3-4 варианта вместо 14)
    m = re.match(r"^(\d+)-([A-Z]+)$", law_id)
    if m:
        num_str = m.group(1).zfill(4)
        suffix = m.group(2)
        # Ограниченный набор годов: поколению суффикса
        year_map = {
            "I": ["90", "95"],
            "II": ["03", "04", "05"],
            "III": ["06", "07"],
            "IV": ["09", "10", "11", "12"],
            "V": ["13", "14", "15"],
            "VI": ["16", "17", "18"],
            "VII": ["19", "20"],
            "VIII": ["21", "22", "23"],
        }
        years = year_map.get(suffix, ["13", "14", "15"])
        prefix = "K" if int(m.group(1)) > 200 and suffix in ("IV", "V", "VI") else "Z"
        for yr in years:
            candidate = f"{prefix}{yr}0000{num_str}"
            url = f"{base}/rus/docs/{candidate}"
            if url not in urls:
                urls.append(url)

    # 3. As-is
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
    cache.put_many([c for c in all_chunks if c.chunk_id])

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
        verify=False,
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


# Fallback 2: CSS Selectors
async def _try_adilet_css_selectors(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """
    Парсит HTML страницы НПА через CSS-селекторы.
    Гранулярность: 1 чанк = 1 статья.
    Селекторы: p[id^='st'], .law-article, .article-content
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

    # НЕ используем Adilet Search как доп. источник если law_ids есть —
    # для known IDs он всегда 404

    async with httpx.AsyncClient(
        timeout=settings.adilet_timeout_seconds,
        follow_redirects=True,
        verify=False,
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
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue

                html = resp.text
                page_chunks = _parse_adilet_html(html, url, query)
                chunks.extend(page_chunks)
                logger.debug(f"[S3/CSS] {url}: {len(page_chunks)} articles parsed")
                if page_chunks:
                    break  # Удалось с этим URL

            except Exception as e:
                logger.warning(f"[S3/CSS] Failed {url}: {e}")

    return chunks


async def _search_adilet_for_query(query: AdiletQuery, base: str) -> list[str]:
    """Поиск на adilet.zan.kz по query_text."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, verify=False) as client:
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
        "p[id^='st']",           # Адилет основной
        ".law-article",
        ".article-text",
        "div[class*='article']",
        "p[class*='article']",
    ]

    articles_found = False
    for selector in article_selectors:
        nodes = tree.css(selector)
        if not nodes:
            continue
        articles_found = True

        for node in nodes[:50]:  # Максимум 50 статей
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

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, verify=False) as http:
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
    """Regex fallback для разбивки текста на статьи."""
    pattern = re.compile(
        r"(?:Статья|Бап|Article)\s+(\d+[\-\d]*)\s*[.\n]([^\n]*)\n(.*?)(?=(?:Статья|Бап|Article)\s+\d|$)",
        re.DOTALL | re.IGNORECASE,
    )
    articles = []
    for m in pattern.finditer(text):
        content = (m.group(2).strip() + "\n" + m.group(3).strip()).strip()
        articles.append({"article_num": m.group(1), "title": "", "content": content[:3000]})
    return articles


# ---------------------------------------------------------------------------
# Agent 2: Web (Tavily)
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


async def _fetch_web_query(query: WebQuery, cache: CacheManager) -> list[EvidenceChunk]:
    """Один запрос через Tavily API."""
    async with _WEB_SEMAPHORE:
        settings = get_settings()

        if not settings.tavily_api_key:
            logger.warning("[S3/Web] Tavily API key not set. Skipping web search.")
            return []

        try:
            from tavily import TavilyClient

            # Tavily клиент (синхронный — запускаем в executor)
            tavily = TavilyClient(api_key=settings.tavily_api_key)

            # Строим search params
            search_params: dict = {
                "query": query.query_text,
                "max_results": query.max_results,
                "search_depth": "advanced",
            }
            if query.include_domains:
                search_params["include_domains"] = query.include_domains

            # Исключаем BLACKLIST домены
            exclude_domains = [
                "reddit.com", "forum", "otvet.mail.ru", "pikabu.ru",
                "answers.yahoo.com",
            ]
            search_params["exclude_domains"] = exclude_domains

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: tavily.search(**search_params),
            )

            results = response.get("results", [])
            chunks: list[EvidenceChunk] = []

            for result in results:
                chunk = _build_web_chunk(result, query)
                if chunk:
                    # Проверяем кэш
                    cached = cache.get(chunk.chunk_id)
                    if cached:
                        chunks.append(cached)
                    else:
                        chunks.append(chunk)

            logger.debug(f"[S3/Web] '{query.query_text[:40]}': {len(chunks)} chunks")
            return chunks

        except ImportError:
            logger.error("[S3/Web] tavily-python not installed")
            return []
        except Exception as e:
            logger.warning(f"[S3/Web] Tavily error: {e}")
            return []


def _build_web_chunk(result: dict, query: WebQuery) -> EvidenceChunk | None:
    """Конвертирует Tavily result → EvidenceChunk. None для BLACKLIST."""
    url: str = result.get("url", "")
    if not url:
        return None

    tier = classify_web_tier(url)
    if tier == WebTier.BLACKLIST:
        logger.debug(f"[S3/Web] Blacklisted: {url}")
        return None

    content = result.get("content", result.get("raw_content", "")).strip()
    if len(content) < 50:
        return None

    chunk_id = hashlib.sha256(content.encode()).hexdigest()

    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=url,
        source_title=result.get("title", urlparse(url).netloc),
        content=content,
        content_summary=content[:300],
        legal_rank=infer_legal_rank_from_tier(tier),
        web_tier=tier,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_adilet_rank(law_title: str) -> LegalRank:
    """Определяет LegalRank по названию НПА."""
    title_lower = law_title.lower()
    if any(w in title_lower for w in ["кодекс", "kodeks"]):
        return LegalRank.CODE
    if any(w in title_lower for w in ["конституционный закон"]):
        return LegalRank.CONSTITUTIONAL_LAW
    if any(w in title_lower for w in ["международный", "конвенция", "договор"]):
        return LegalRank.INTERNATIONAL_TREATY
    if any(w in title_lower for w in ["указ президента"]):
        return LegalRank.PRESIDENTIAL_DECREE
    if any(w in title_lower for w in ["постановление правительства", "ппрк"]):
        return LegalRank.GOVERNMENT_RESOLUTION
    if any(w in title_lower for w in ["приказ", "инструкция"]):
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
