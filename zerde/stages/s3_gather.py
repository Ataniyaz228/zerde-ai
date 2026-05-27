"""
Stage 3: Data Gathering Agents
Вход:  QueryPlan
Выход: list[EvidenceChunk]
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
    infer_legal_rank_from_web_content,
)
from zerde.utils.llm_client import make_llm_client

logger = logging.getLogger(__name__)

# Ограничение параллельности
_ADILET_SEMAPHORE = asyncio.Semaphore(3)
_WEB_SEMAPHORE = asyncio.Semaphore(2)

# Reverse lookup for known codes
_LAW_NAME_TO_SHORT_ID = {
    "гк рк (общая часть)": "1000-XIII",
    "гражданский кодекс рк (общая часть)": "1000-XIII",
    "гк рк (особенная часть)": "409-I",
    "гражданский кодекс рк (особенная часть)": "409-I",
    "земельный кодекс рк": "442-II",
    "бюджетный кодекс рк": "95-IV",
    "трудовой кодекс рк": "414-I",
    "коап": "235-V",
    "коап рк": "235-V",
    "ук рк": "226-V",
    "уголовный кодекс рк": "226-V",
    "упк рк": "350-VI",
    "налоговый кодекс рк": "120-VI",
    "закон о персональных данных": "94-V",
    "закон о персональных данных рк": "94-V",
    "закон о государственном имуществе": "413-IV",
    "закон о местном государственном управлении": "148-II",
    "закон об оценочной деятельности": "368-II",
    "закон о государственном регулировании": "138-IV",
    "закон об образовании": "319-III",
    "закон о науке": "407-II",
    "закон о секьюритизации": "122-IV",
    "закон об эцп": "370-II",
    "закон о государственных закупках": "434-V",
    "закон о жилищных отношениях": "94-I",
    "закон о банках": "2444-XII",
    "закон о языках": "151-I",
    "закон о нотариате": "155-V",
    "закон о связи": "567-II",
    "закон о разрешениях и уведомлениях": "202-V",
    "закон о противодействии коррупции": "410-V",
}

_LAW_ID_KNOWN = {
    "94-V": "Z1300000094",
    "87-IV": "Z1300000094",
    "418-V": "Z1500000418",
    "370-II": "Z030000370_",
    "550-IV": "Z1300000550",
    "274-IV": "Z100000274_",
    "11-VI": "Z1600000011",
    "239-VII": "Z2500000239",
    "401-II": "Z0300000401",
    "73-V": "Z1300000073",
    "223-VIII": "Z1700000223",
    "240-IV": "Z1100000240",
    "148-II": "Z010000148_",
    "368-II": "Z030000368_",
    "95-IV": "Z080000095_",
    "138-IV": "Z060000138_",
    "413-IV": "Z1100000413",
    "414-IV": "Z1100000414",
    "235-V": "K1400000235",
    "226-V": "K1400000226",
    "231-V": "K1400000231",
    "377-V": "K1500000377",
    "214-VII": "K2500000214",
    "414-I-NEW": "K1500000414",
    "350-VI": "K2000000350",
    "212-IV": "K070000212_",
    "1000-XIII": "K940001000_",
    "409-I": "K990000409_",
    "442-II": "K030000442_",
    "414-I": "K150000414_",
    "226-V-UK": "K1400000226",
    "152-VII": "Z2200000152",
    "400-VI": "K210000400_",
    "375-V": "K1500000375_",
    "481-II": "K030000481_",
    "360-VI": "K200000360_",
    "125-VI": "K170000125_",
    "171-VIII": "K2500000171",
    "178-VIII": "K2500000178",
    "360-VI-NEW": "K2000000360",
    "125-VI-NEW": "K1700000125",
    "261-IV": "Z100000261_",
    "258-VIII": "Z2600000258",
    "66-III": "Z050000066_",
    "106-VIII": "Z2400000106",
    "94-I": "Z970000094_",
    "155-I": "Z970000155_",
    "410-V-NEW": "Z1500000410",
    "202-V-NEW": "Z1400000202",
    "567-II-NEW": "Z040000567_",
    "151-I": "Z970000151_",
    "319-III": "Z070000319_",
    "133-VI": "Z1800000133",
    "375-V-NEW": "K1500000375",
    "400-VI-NEW": "K2100000400",
}

def _resolve_law_name(raw_id: str) -> str:
    """
    Разрешает название/ID закона в канонический short ID.
    Использует LawRegistry с fuzzy matching — без хардкода.
    """
    from zerde.utils.law_registry import get_registry
    registry = get_registry()
    return registry.resolve(raw_id.strip())


def _normalize_law_id_to_adilet_urls(law_id: str, base: str) -> list[str]:
    law_id = law_id.replace("\u0406", "I").replace("\u0456", "i").strip()
    law_id = _resolve_law_name(law_id)
    urls = []
    
    # 1. Known mapping
    if law_id in _LAW_ID_KNOWN:
        adilet_code = _LAW_ID_KNOWN[law_id]
        urls.append(f"{base}/rus/docs/{adilet_code}")
        # also append without trailing underscore
        if adilet_code.endswith("_"):
            urls.append(f"{base}/rus/docs/{adilet_code[:-1]}")
        return urls
        
    # 2. If it is already an Adilet ID
    if re.match(r"^[A-Z]\d{9}", law_id):
        urls.append(f"{base}/rus/docs/{law_id}")
        if law_id.endswith("_"):
            urls.append(f"{base}/rus/docs/{law_id[:-1]}")
        else:
            urls.append(f"{base}/rus/docs/{law_id}_")
        return urls
        
    # 3. Guessing/generating variants for standard format like "999-VI" or "94-V"
    match = re.match(r"^(\d+)-([IVX]+)$", law_id, re.IGNORECASE)
    if match:
        num = match.group(1)
        roman = match.group(2).upper()
        # Guessing prefixes like Z1300000000 or similar
        # Let's generate a couple of variants:
        # Z1300000 + num, Z1500000 + num, Z1600000 + num, etc.
        num_padded = num.zfill(4)
        for yr in ["13", "14", "15", "16", "20", "23"]:
            urls.append(f"{base}/rus/docs/Z{yr}0000{num_padded}")
            urls.append(f"{base}/rus/docs/Z{yr}0000{num}")
            
    # 4. As-is fallback
    as_is = f"{base}/rus/docs/{law_id}"
    if as_is not in urls:
        urls.append(as_is)
        
    return urls

async def gather_evidence(plan: QueryPlan) -> list[EvidenceChunk]:
    settings = get_settings()
    cache = CacheManager(settings.cache_db_path)
    logger.info(f"[S3] Gathering evidence. total_queries={plan.total_queries}")
    adilet_task = _run_adilet_agent(plan.adilet_queries, cache)
    web_queries = plan.web_queries_ru + plan.web_queries_kk + plan.web_queries_en
    web_task = _run_web_agent(web_queries, cache)
    adilet_chunks, web_chunks = await asyncio.gather(adilet_task, web_task)
    all_chunks = adilet_chunks + web_chunks
    await cache.put_many([c for c in all_chunks if c.chunk_id])
    return all_chunks

async def _run_adilet_agent(queries: list[AdiletQuery], cache: CacheManager) -> list[EvidenceChunk]:
    tasks = [_fetch_adilet_with_fallback(q, cache) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    chunks = []
    for r in results:
        if isinstance(r, list):
            chunks.extend(r)
    return chunks

async def _fetch_adilet_with_fallback(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    async with _ADILET_SEMAPHORE:
        for strategy_fn in [_try_adilet_css_selectors, _try_adilet_pdf_ocr]:
            try:
                chunks = await strategy_fn(query, cache)
                if chunks:
                    return chunks
            except Exception:
                continue
        try:
            chunks = await cache.search_local(query.query_text, law_ids=query.law_ids, articles=query.articles)
            if chunks:
                for c in chunks:
                    c.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                return chunks
        except Exception:
            pass
        return []

async def _try_adilet_css_selectors(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    settings = get_settings()
    base = str(settings.adilet_base_url).rstrip("/")
    chunks = []
    urls_to_try = []
    for law_id in query.law_ids:
        urls_to_try.extend(_normalize_law_id_to_adilet_urls(law_id, base)[:3])
    if not urls_to_try:
        urls_to_try = await _search_adilet_for_query(query, base)
    async with httpx.AsyncClient(timeout=settings.adilet_timeout_seconds, follow_redirects=True) as client:
        for url in urls_to_try[:6]:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    page_chunks = _parse_adilet_html(resp.text, url, query)
                    chunks.extend(page_chunks)
                    if page_chunks:
                        break
            except Exception:
                pass
    return chunks

async def _search_adilet_for_query(query: AdiletQuery, base: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"{base}/rus/search", params={"q": query.query_text, "type": "docs"})
            if resp.status_code != 200:
                return []
            from selectolax.parser import HTMLParser
            tree = HTMLParser(resp.text)
            links = []
            for a in tree.css("a.search-result-link, a.doc-link, .search-item a"):
                href = a.attributes.get("href", "")
                if "/docs/" in href:
                    links.append(urljoin(base, href))
            return links[:5]
    except Exception:
        return []

def _parse_adilet_html(html: str, source_url: str, query: AdiletQuery) -> list[EvidenceChunk]:
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    chunks = []
    law_title = ""
    node = tree.css_first("h1")
    if node:
        law_title = node.text(strip=True)
    law_id_match = re.search(r"/docs/([A-Z]\d+)", source_url, re.IGNORECASE)
    law_id = law_id_match.group(1) if law_id_match else ""
    nodes = tree.css("p[id^='st']")
    for node in nodes[:80]:
        article_text = node.text(strip=True)
        if len(article_text) < 30:
            continue
        article_num = _extract_article_number(node.attributes.get("id", ""), article_text)
        if query.articles and article_num not in query.articles:
            continue
        chunk_id = hashlib.sha256(article_text.encode()).hexdigest()
        chunks.append(EvidenceChunk(
            chunk_id=chunk_id,
            source_url=source_url + f"#{node.attributes.get('id', '')}",
            source_title=f"{law_title} | Ст. {article_num}" if article_num else law_title,
            content=article_text,
            legal_rank=_infer_adilet_rank(law_title),
            law_id=law_id,
            article=article_num,
            adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
        ))
    return chunks

async def _try_adilet_pdf_ocr(query: AdiletQuery, cache: CacheManager) -> list[EvidenceChunk]:
    return []

async def _run_web_agent(queries: list[WebQuery], cache: CacheManager) -> list[EvidenceChunk]:
    tasks = [_fetch_web_query(q, cache) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    chunks = []
    for r in results:
        if isinstance(r, list):
            chunks.extend(r)
    return chunks

async def _fetch_web_query(query: WebQuery, cache: CacheManager) -> list[EvidenceChunk]:
    async with _WEB_SEMAPHORE:
        results, provider = await _search_web(query)
        if not results:
            try:
                chunks = await cache.search_local(query.query_text)
                return chunks
            except Exception:
                return []
        chunks = []
        for result in results:
            chunk = _build_web_chunk(result, query, provider)
            if chunk:
                chunks.append(chunk)
        return chunks

async def _search_tavily(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("Tavily not configured")

async def _search_serper(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("Serper not configured")

async def _search_google(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("Google not configured")

async def _search_web(query: WebQuery) -> tuple[list[dict], str]:
    # Simple DuckDuckGo search fallback
    try:
        res = await _search_duckduckgo(query.query_text, query.max_results)
        return res, "duckduckgo"
    except Exception:
        return [], "none"

async def _search_duckduckgo(query_text: str, max_results: int) -> list[dict]:
    def _sync_search():
        has_cyrillic = any('\u0400' <= ch <= '\u04FF' for ch in query_text)
        region = "kz-kz" if has_cyrillic else "wt-wt"
        try:
            results = _DDGS(timeout=15).text(query_text, max_results=max_results, region=region)
        except Exception:
            results = _DDGS(timeout=15).text(query_text, max_results=max_results)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", r.get("url", "")),
                "content": r.get("body", r.get("snippet", "")).strip()
            }
            for r in results
        ] if results else []
    return await asyncio.to_thread(_sync_search)

def _build_web_chunk(result: dict, query: WebQuery, provider: str) -> EvidenceChunk | None:
    url = result.get("url", "")
    if not url:
        return None
    tier = classify_web_tier(url)
    if tier == WebTier.BLACKLIST:
        return None
    content = result.get("content", "").strip()
    if len(content) < 50:
        return None
    chunk_id = hashlib.sha256(content.encode()).hexdigest()
    title = result.get("title", "")
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=url,
        source_title=title or url,
        content=content,
        legal_rank=LegalRank.LAW_RK,
        web_tier=tier,
        search_provider=provider,
    )

def _regex_split_articles(text: str) -> list[dict]:
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

def _infer_adilet_rank(law_title: str) -> LegalRank:
    title_lower = law_title.lower()
    if "кодекс" in title_lower or "кодексі" in title_lower:
        return LegalRank.CODE
    return LegalRank.LAW_RK

def _extract_article_number(node_id: str, text: str) -> str:
    id_match = re.search(r"st(\d+)", node_id, re.IGNORECASE)
    if id_match:
        return id_match.group(1)
    text_match = re.match(r"(?:Статья|Бап)\s+(\d+)", text[:50], re.IGNORECASE)
    if text_match:
        return text_match.group(1)
    return ""


def _extract_law_id_from_text(title: str, content: str) -> str | None:
    """
    Парсит и извлекает law_id из названия (title) или текста (content) веб-страницы/документа.
    Сначала ищет точные совпадения известных кодексов и законов,
    а затем пытается найти стандартный паттерн ID закона (например, '94-V' или '1000-XIII').
    """
    import re
    combined = (title or "") + " " + (content or "")
    combined_lower = combined.lower()

    # 1. Поиск известных названий/аббревиатур из _LAW_NAME_TO_SHORT_ID
    # Отсортируем по длине ключа по убыванию, чтобы сначала сопоставить самые специфичные фразы
    sorted_names = sorted(_LAW_NAME_TO_SHORT_ID.keys(), key=len, reverse=True)
    for name in sorted_names:
        # Для аббревиатур типа "коап рк", "ук рк", "гк рк" или полных названий
        # Проверяем границы слов или просто вхождение с пробелами/знаками препинания
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, combined_lower):
            return _LAW_NAME_TO_SHORT_ID[name]

    # Отдельно проверим краткие/русские/казахские кодовые слова, которые могут не быть в словаре:
    # "гражданский кодекс" -> "1000-XIII"
    # "уголовный кодекс" -> "226-V"
    # "коап" / "административных правонарушениях" -> "235-V"
    # "трудовой кодекс" -> "414-I"
    # "земельный кодекс" -> "442-II"
    if "гражданск" in combined_lower or " гк" in combined_lower:
        return "1000-XIII"
    if "уголовн" in combined_lower or " ук" in combined_lower or " қк" in combined_lower:
        return "226-V"
    if "коап" in combined_lower or "административн" in combined_lower:
        return "235-V"
    if "трудов" in combined_lower:
        return "414-I"
    if "земельн" in combined_lower:
        return "442-II"

    # 2. Поиск стандартного паттерна вида: 94-V, 1000-XIII, 413-IV, 122-IV, etc.
    # Паттерн: число, за которым следует дефис, а затем римские цифры I, V, X, L, C, D, M (в верхнем или нижнем регистре)
    pattern = r"\b\d+-[IVX]+(?:-NEW)?\b"
    matches = re.findall(pattern, combined, re.IGNORECASE)
    if matches:
        # Возвращаем в верхнем регистре
        return matches[0].upper()

    return None

