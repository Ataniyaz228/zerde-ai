"""
Stage 3: Data Gathering Agents
Вход:  QueryPlan
Выход: list[EvidenceChunk]
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from urllib.parse import urljoin

import httpx

try:
    # `ddgs` — текущий пакет (объявлен в pyproject); тот же DDGS().text() API.
    from ddgs import DDGS as _DDGS
except ImportError:  # совместимость со старым пакетом duckduckgo-search
    from duckduckgo_search import DDGS as _DDGS

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

logger = logging.getLogger(__name__)

# Ограничение параллельности
_ADILET_SEMAPHORE = asyncio.Semaphore(3)
_WEB_SEMAPHORE = asyncio.Semaphore(2)

# Резолв названий/ID и law_id→adilet_code полностью делегирован LawRegistry
# (БД law_metadata + единый статический fallback внутри реестра). Раньше тут жили
# дублирующие словари _LAW_NAME_TO_SHORT_ID (стейл: налоговый=120-VI, нотариат=155-V,
# закупки=434-V) и _LAW_ID_KNOWN (адилет-коды) — оба удалены как источник дрейфа.


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

    # 1. Adilet code: РЕЕСТР авторитетен (law_metadata + его внутренний
    #    legacy-fallback для неингестированных законов). Единый источник.
    from zerde.utils.law_registry import get_registry
    adilet_code = get_registry().get_adilet_code(law_id)
    if adilet_code:
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

    # 3. Неизвестный short ID. Раньше здесь фабриковались URL перебором префиксов
    #    годов (Z13.., Z15.., Z16..) — это плодило мёртвые 404-запросы и могло молча
    #    попасть в чужой закон. Удалено: нет в _LAW_ID_KNOWN/реестре → не угадываем.
    #    Известные законы покрыты шагом 1 + реестром + локальным кешем (search_local).
    if re.match(r"^\d+-[IVXLCDM]+$", law_id, re.IGNORECASE):
        logger.debug(f"[S3/Adilet] Unknown law_id '{law_id}' — no adilet code; skipping URL fabrication.")
        return urls

    # 4. As-is fallback (для adilet-подобных строк, не пойманных шагом 2).
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
    from zerde.utils.law_registry import get_registry
    registry = get_registry()
    # Резолвим law_ids через реестр до любых операций с ними
    resolved_law_ids = [registry.resolve(lid) for lid in (query.law_ids or [])]
    if resolved_law_ids != (query.law_ids or []):
        logger.info(f"[S3/Adilet] Resolved law_ids: {query.law_ids} → {resolved_law_ids}")
    async with _ADILET_SEMAPHORE:
        for strategy_fn in [_try_adilet_css_selectors, _try_adilet_pdf_ocr]:
            try:
                chunks = await strategy_fn(query, cache, resolved_law_ids=resolved_law_ids)
                if chunks:
                    logger.info(f"[S3/Adilet] {strategy_fn.__name__} returned {len(chunks)} chunks for {resolved_law_ids}")
                    return chunks
            except Exception as e:
                logger.warning(f"[S3/Adilet] {strategy_fn.__name__} failed for {resolved_law_ids}: {e}")
                continue
        try:
            chunks = await cache.search_local(query.query_text, law_ids=resolved_law_ids, articles=query.articles)
            if chunks:
                logger.info(f"[S3/Adilet] search_local found {len(chunks)} chunks for query: '{query.query_text[:80]}'")
                for c in chunks:
                    c.adilet_fallback_used = AdiletFallbackStrategy.LOCAL_CACHE
                return chunks
        except Exception:
            pass
        logger.warning(f"[S3/Adilet] All strategies failed for {resolved_law_ids}. Returning empty.")
        return []

async def _try_adilet_css_selectors(query: AdiletQuery, cache: CacheManager, resolved_law_ids: list[str] | None = None) -> list[EvidenceChunk]:
    settings = get_settings()
    base = str(settings.adilet_base_url).rstrip("/")
    chunks = []
    urls_to_try = []
    law_ids_to_use = resolved_law_ids if resolved_law_ids is not None else (query.law_ids or [])
    for law_id in law_ids_to_use:
        urls_to_try.extend(_normalize_law_id_to_adilet_urls(law_id, base)[:3])
    if not urls_to_try:
        urls_to_try = await _search_adilet_for_query(query, base)
    # verify=settings.adilet_tls_verify (default False): сертификат Adilet часто не
    # валидируется → без этого запрос падает в HTTP 000 и live-fetch не работает вовсе.
    async with httpx.AsyncClient(timeout=settings.adilet_timeout_seconds, follow_redirects=True, verify=settings.adilet_tls_verify) as client:
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
        verify_tls = get_settings().adilet_tls_verify
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, verify=verify_tls) as client:
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

# Заголовок статьи в реальной разметке Adilet: <p><b>Статья N. Название</b></p>
# (рус) или <p><b>N-бап. Атауы</b></p> (каз). Тело — последующие <p> до след. заголовка.
_ADILET_ARTICLE_HEAD_RE = re.compile(
    r"^(?:Стать[яи]\s+(\d+(?:-\d+)?)|(\d+(?:-\d+)?)\s*-?\s*бап)\.?\s*(.*)",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)


def _extract_adilet_articles(tree) -> list[dict]:
    """Извлекает статьи из реальной структуры Adilet (server-rendered HTML).

    Старый парсер искал `p[id^='st']` — на актуальных страницах таких узлов 0,
    поэтому он всегда проваливался в грубый regex по всему тексту body. Здесь
    идём по <p> в порядке документа: <p> с жирным «Статья N. …» открывает статью,
    последующие <p> копятся в её тело до следующего заголовка.

    Возвращает [{article_num, title, body(list[str])}].
    """
    articles: list[dict] = []
    cur: dict | None = None
    for p in tree.css("p"):
        txt = p.text(strip=True)
        if not txt:
            continue
        m = _ADILET_ARTICLE_HEAD_RE.match(txt)
        # Заголовок = совпадение + жирный <b>: отсекает обычные абзацы,
        # начинающиеся со слова «Статья …» в перекрёстных ссылках.
        is_heading = bool(m) and p.css_first("b") is not None
        if is_heading:
            if cur:
                articles.append(cur)
            cur = {
                "article_num": (m.group(1) or m.group(2) or "").strip(),
                "title": (m.group(3) or "").strip(),
                "body": [],
            }
        elif cur is not None:
            cur["body"].append(txt)
    if cur:
        articles.append(cur)
    return articles


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

    articles = _extract_adilet_articles(tree)
    if articles:
        max_articles = get_settings().adilet_max_articles_per_law
        lang = "ru" if "/rus/" in source_url else "kk" if "/kaz/" in source_url else None
        for art in articles:
            article_num = art["article_num"]
            if query.articles and article_num not in query.articles:
                continue
            body = "\n".join(art["body"]).strip()
            content = f"Статья {article_num}. {art['title']}\n{body}".strip()
            if len(content) < 30:
                continue
            chunk_id = hashlib.sha256(content.encode()).hexdigest()
            chunks.append(EvidenceChunk(
                chunk_id=chunk_id,
                source_url=source_url,
                source_title=f"{law_title} | Ст. {article_num}" if article_num else law_title,
                content=content,
                legal_rank=_infer_adilet_rank(law_title),
                law_id=law_id,
                article=article_num,
                adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
                language=lang,
            ))
            # Без фильтра по статьям ограничиваем объём, чтобы не раздуть один закон.
            if not query.articles and len(chunks) >= max_articles:
                break
        return chunks

    # Fallback to regex splitting
    text = tree.body.text(separator="\n", strip=True) if tree.body else html
    articles_dict = _regex_split_articles(text)
    for art in articles_dict[:80]:
        article_num = art["article_num"]
        if query.articles and article_num not in query.articles:
            continue
        article_text = art["content"]
        if len(article_text) < 30:
            continue
        chunk_id = hashlib.sha256(article_text.encode()).hexdigest()
        lang = "ru" if "/rus/" in source_url else "kk" if "/kaz/" in source_url else None
        chunks.append(EvidenceChunk(
            chunk_id=chunk_id,
            source_url=source_url,
            source_title=f"{law_title} | Ст. {article_num}" if article_num else law_title,
            content=f"Статья {article_num}. {art['title']}\n{article_text}",
            legal_rank=_infer_adilet_rank(law_title),
            law_id=law_id,
            article=article_num,
            adilet_fallback_used=AdiletFallbackStrategy.CSS_SELECTOR,
            language=lang,
        ))

    return chunks

async def _try_adilet_pdf_ocr(query: AdiletQuery, cache: CacheManager, resolved_law_ids: list[str] | None = None) -> list[EvidenceChunk]:
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
    settings = get_settings()
    if not settings.tavily_api_key:
        raise ValueError("Tavily API key not set in config")

    import httpx
    url = f"{settings.tavily_base_url}/search"
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_raw_content": False
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", "")
        })
    return results

async def _search_serper(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("Serper not configured")

async def _search_google(query: str, max_results: int) -> list[dict]:
    raise RuntimeError("Google not configured")

async def _search_web(query: WebQuery) -> tuple[list[dict], str]:
    settings = get_settings()
    provider = settings.search_provider.lower()

    if provider == "tavily":
        try:
            res = await _search_tavily(query.query_text, query.max_results)
            return res, "tavily"
        except Exception as e:
            logger.warning(f"[S3/Web] Tavily search failed (limit reached?): {e}. Falling back to DuckDuckGo.")
            # Fall through to DDG

    # Fallback / Default: DuckDuckGo
    try:
        res = await _search_duckduckgo(query.query_text, query.max_results)
        return res, "duckduckgo"
    except Exception as e:
        logger.warning(f"[S3/Web] Web search failed for query '{query.query_text[:30]}': {e}")
        return [], "none"

async def _search_duckduckgo(query_text: str, max_results: int) -> list[dict]:
    def _sync_search():
        has_cyrillic = any('\u0400' <= ch <= '\u04FF' for ch in query_text)
        try:
            # DDGS sometimes fails on kz-kz natively depending on the proxy/IP. We try it first.
            results = _DDGS(timeout=15).text(query_text, max_results=max_results, region="kz-kz")
        except Exception as e:
            logger.warning(f"[DDGS] kz-kz region failed ({e}), falling back to wt-wt")
            results = _DDGS(timeout=15).text(query_text, max_results=max_results, region="wt-wt")
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

    # C1 fix: ранг web-источника выводится многофакторным скорером (домен + контент),
    # а НЕ хардкодом LAW_RK. Иначе сниппет zakon.kz/блога получал ранг «Закон РК» (4) —
    # ложно высокий авторитет, который завышал reliability (Q_auth/a_coef) и подписывал
    # его «Закон РК» в нормативной базе. Скорер даёт CODE/LAW_RK только при сильных
    # сигналах; иначе откатывается к tier-маппингу (TIER_2→EXPERT_ANALYTICS и т.д.).
    inferred_rank, rank_confidence, rank_reason = infer_legal_rank_from_web_content(
        tier=tier, title=title, content=content, url=url
    )
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=url,
        source_title=title or url,
        content=content,
        legal_rank=inferred_rank,
        web_tier=tier,
        inferred_rank=inferred_rank,
        inferred_rank_confidence=rank_confidence,
        inference_reason=rank_reason,
        search_provider=provider,
        language=getattr(query, "language", None),
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

def _extract_law_id_from_text(title: str, content: str) -> str | None:
    """
    Парсит и извлекает law_id из названия (title) или текста (content) веб-страницы/документа.
    Сначала ищет точные совпадения известных кодексов и законов,
    а затем пытается найти стандартный паттерн ID закона (например, '94-V' или '1000-XIII').
    """
    import re
    combined = (title or "") + " " + (content or "")
    combined_lower = combined.lower()

    from zerde.utils.law_registry import get_registry
    registry = get_registry()

    # 1. Обиходные кодовые слова кодексов → канонический law_id через реестр
    #    (а не локальный словарь). Реестр канонизирует короткое ядро в форму из
    #    law_metadata: 226-V → 226-V-UK, 414-I → 414-I-NEW и т.п.
    keyword_hints: list[tuple[tuple[str, ...], str]] = [
        (("гражданск", " гк"), "1000-XIII"),
        (("уголовн", " ук", " қк"), "226-V-UK"),
        (("коап", "административн", "әкімшілік құқық бұзушылық"), "235-V"),
        (("трудов", "еңбек кодекс"), "414-I-NEW"),
        (("земельн", "жер кодекс"), "442-II"),
        (("налогов", "салық кодекс"), "214-VII"),
        (("исполнительном производстве", "судебных исполнителей",
          "атқарушылық іс жүргізу"), "261-IV"),
    ]
    for needles, lid in keyword_hints:
        if any(n in combined_lower for n in needles):
            return registry.resolve(lid)

    # 2. Явный ID закона в тексте (94-V, 1000-XIII, 413-IV-NEW) → канонизируем
    #    через реестр (он же отбросит транспозиции, не совпавшие с известным).
    m = re.search(r"\b\d+-[IVXLCDM]+(?:-[A-Z]+)?\b", combined, re.IGNORECASE)
    if m:
        return registry.resolve(m.group(0).upper())

    return None

