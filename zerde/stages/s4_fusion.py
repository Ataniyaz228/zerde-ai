"""
Stage 4: Fusion & Quality Validation
Вход:  list[EvidenceChunk] (сырые)
Выход: list[EvidenceChunk] (с флагами)

Реализация:
  1. SHA256 дедупликация — O(n)
  2. Cosine Similarity деdup — OpenAI embeddings, батчи по 100, порог 0.92
  3. HIERARCHY конфликты — rank delta + domain check
  4. TEMPORAL конфликты — law_id + article + effective_date
  5. FACTUAL конфликты — regex числа + нормализация
  6. ENFORCEMENT_GAP — cosine distance норма vs. практика
"""

from __future__ import annotations

import logging
import re
from itertools import combinations

import numpy as np
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from zerde.config import get_settings
from zerde.models import ConflictType, EvidenceChunk, LegalRank, WebTier
from zerde.utils.llm_client import make_embedding_client

logger = logging.getLogger(__name__)

# Регулярка для чисел в юридическом тексте (суммы, проценты, сроки)
_NUMBER_RE = re.compile(
    r"\b(\d[\d\s]{0,4}(?:[.,]\d{1,3})?)"
    r"\s*(%|млн\.?|млрд\.?|тыс\.?|тг\.?|kzt|мрп|мзп|лет|месяц\w*|дн\w*|час\w*)\b",
    re.IGNORECASE,
)

# Regex для детектирования года в тексте (text-based temporal detection)
_YEAR_RE = re.compile(r"\b(20[0-2]\d)\s*(?:год|г\.?|жыл)?", re.IGNORECASE)

# Словарь секторов для SECTORAL конфликтов
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "financial": [
        "банк", "финансов", "кредит", "страхов", "ценные бумаги", "нбрк",
        "bank", "finance", "credit", "insurance",
    ],
    "government": [
        "государственн", "госорган", "акимат", "министерств", "уполномоченн",
        "government", "public authority", "ministry",
    ],
    "healthcare": [
        "медицин", "здравоохранен", "больниц", "клиник",
        "health", "medical", "hospital",
    ],
}

_EMBEDDING_BATCH_SIZE = 50  # чанков за один API вызов

# ---------------------------------------------------------------------------
# ContentSpamFilter — детерминированный фильтр мусора (перед дедупликацией)
# ---------------------------------------------------------------------------

# URL-паттерны которые гарантируют мусор
_SPAM_URL_PATTERNS: list[str] = [
    "egov.kz/cms", "egov.kz/news", "egov.kz/mobile",
    "/press/news/", "/press/article/", "/memleket/entities/",
    "akimat.", "oq.gov.kz", "cert.gov.kz/news",
    "pki.gov.kz", "eresidency.gov.kz",
]

# Контентные паттерны — новости, инструкции, пресс-релизы
_SPAM_CONTENT_RE = re.compile(
    r"eGov\s*Mobile|скачать\s*приложени|пошаговая\s*инструкц"
    r"|войти\s*через|QR.?код|биометрическ.*идентификаци.*как"
    r"|пресс.?релиз|новости\s*министерств"
    r"|ЦОН\s*находится|государственная\s*корпорация.*граждан"
    r"|удостоверение\s*личности.*заменить|eGov.*результат\w*",
    re.IGNORECASE,
)

# Юридические сигналы — если нет НИ ОДНОГО из них → web-чанк бесполезен
_LEGAL_SIGNAL_RE = re.compile(
    r"стать[яиью]\s*\d+"
    r"|№\s*\d+.{1,5}[IVXVIIIVIII]+"
    r"|Кодекс\s*РК|Закон\s*РК|УК\s*РК|КоАП\s*РК"
    r"|МРП|месячн\w+\s*расчетн"
    r"|штраф|санкци|ответственност"
    r"|пункт\s*\d+|часть\s*\d+"
    # Расширенные сигналы для Tavily-источников:
    r"|персональн\w+\s*данн"
    r"|субъект\w*\s*данн|оператор\w*\s*данн"
    r"|уведомлени|обработк\w+\s*данн"
    r"|законопроект|нормативн|регулирован"
    r"|согласи\w+\s*субъект|трансграничн"
    r"|локализаци\w+\s*данн|локализаци\w+\s*сервер"
    r"|уполномоченн\w+\s*орган|кибербезопасност|информационн\w+\s*безопасн"
    r"|защит\w+\s*данн|конфиденциальн"
    r"|биометрическ|идентификаци|дактилоскоп"
    r"|цифров\w+\s*Казахстан|электронн\w+\s*правительств"
    r"|административн\w+\s*правонарушен"
    r"|уголовн\w+\s*ответственност",
    re.IGNORECASE,
)

# URL-белый список: Tier-1 источники всегда пропускаются
_TRUSTED_URL_PATTERNS: list[str] = [
    "adilet.zan.kz", "online.zakon.kz", "prokuror.gov.kz",
    "supreme.kz", "parlam.kz", "tengrinews.kz/zakon",
    "informburo.kz", "zakon.kz",
]


def _is_spam(chunk: EvidenceChunk) -> bool:
    """
    Детерминированный спам-фильтр для web-чанков.
    Adilet-чанки всегда проходят.
    Tier-1 URL (доверенные источники) пропускаются при наличии юр. сигналов.
    """
    # Adilet-источники — всегда доверяем
    if chunk.adilet_fallback_used is not None:
        return False

    url = chunk.source_url.lower()
    content = chunk.content

    # 1. URL-блэклист
    if any(pattern in url for pattern in _SPAM_URL_PATTERNS):
        return True

    # 2. Контентный спам
    if _SPAM_CONTENT_RE.search(content):
        return True

    # 3. Слишком короткий web-чанк
    if len(content) < 150:
        return True

    # 4. Нет юридических сигналов вообще (но доверенные URL пропускаем)
    has_legal = _LEGAL_SIGNAL_RE.search(content)
    is_trusted = any(t in url for t in _TRUSTED_URL_PATTERNS)

    if not has_legal and not is_trusted:
        return True

    return False


def _apply_spam_filter(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    """Применяет спам-фильтр, логирует результат."""
    before = len(chunks)
    filtered = [c for c in chunks if not _is_spam(c)]
    dropped = before - len(filtered)
    if dropped:
        logger.info(f"[S4/SpamFilter] Dropped {dropped} spam chunks ({before} → {len(filtered)})")
    return filtered


def _get_sector(text: str) -> str | None:
    """Определяет сектор чанка по ключевым словам."""
    text_lower = text.lower()
    for sector, keywords in _SECTOR_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return sector
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fuse_and_validate(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    """Этап 4: Спам-фильтр + дедупликация + детерминированный поиск конфликтов."""
    settings = get_settings()
    logger.info(f"[S4] Fusion start. Input: {len(chunks)} chunks")

    # 0. ContentSpamFilter — убираем мусор до дедупликации
    chunks = _apply_spam_filter(chunks)

    # 1. SHA256
    chunks = _dedup_by_hash(chunks)

    # 2. Cosine Similarity (prefer local BGE-M3, fallback to OpenAI if key is present)
    chunks = await _dedup_by_cosine(chunks, settings.cosine_similarity_threshold)

    # 3. Конфликты
    chunks = _detect_conflicts(chunks, settings.hierarchy_conflict_rank_delta)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_count = sum(1 for c in active if c.is_conflict)
    dup_count = sum(1 for c in chunks if c.is_duplicate)

    logger.info(
        f"[S4] Done. active={len(active)} conflicts={conflict_count} duplicates={dup_count}"
    )
    return chunks


# ---------------------------------------------------------------------------
# 1. SHA256 Dedup
# ---------------------------------------------------------------------------


def _dedup_by_hash(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            chunk.is_duplicate = True
        else:
            seen.add(chunk.chunk_id)

    dupes = sum(1 for c in chunks if c.is_duplicate)
    if dupes:
        logger.info(f"[S4/SHA256] {dupes} exact duplicates marked")
    return chunks


# ---------------------------------------------------------------------------
# 2. Cosine Similarity Dedup
# ---------------------------------------------------------------------------


async def _dedup_by_cosine(chunks: list[EvidenceChunk], threshold: float) -> list[EvidenceChunk]:
    """Семантическая дедупликация через BGE-M3 (локально) или OpenAI embeddings."""
    import asyncio
    from zerde.utils.cache import CacheManager

    active = [c for c in chunks if not c.is_duplicate]
    if len(active) < 2:
        return chunks

    logger.info(f"[S4/Cosine] Getting embeddings for {len(active)} chunks (preferring local BGE-M3)...")

    embeddings = None
    try:
        cache = CacheManager()
        # Use asyncio.to_thread to run sync embedding computation in a thread pool
        embeddings = await asyncio.to_thread(cache.get_embeddings, active)
        logger.info(f"[S4/Cosine] Generated {len(embeddings)} local BGE-M3 embeddings successfully.")
    except Exception as e:
        logger.warning(f"[S4/Cosine] Local BGE-M3 embeddings failed: {e}. Trying OpenAI fallback...")
        
        settings = get_settings()
        client = make_embedding_client(settings)
        if client:
            try:
                embeddings = await _get_embeddings_batched(client, active, settings.embedding_model)
            except Exception as ex:
                logger.warning(f"[S4/Cosine] OpenAI embedding failed: {ex}. Skipping cosine dedup.")
                return chunks
        else:
            logger.warning("[S4/Cosine] OpenAI client not available. Skipping cosine dedup.")
            return chunks

    if not embeddings:
        return chunks

    # Сохраняем embeddings в чанки (exclude=True — не попадут в JSON)
    for chunk, emb in zip(active, embeddings):
        chunk.embedding = emb

    # Попарное сравнение O(n²) — для больших корпусов заменить на ANN
    cosine_dupes = 0
    seen_indices: set[int] = set()

    for i, j in combinations(range(len(active)), 2):
        if i in seen_indices or j in seen_indices:
            continue

        sim = _cosine_similarity(active[i].embedding, active[j].embedding)  # type: ignore[arg-type]
        if sim >= threshold:
            # Более низкий ранг → дубликат
            loser_idx = j if int(active[i].legal_rank) <= int(active[j].legal_rank) else i
            active[loser_idx].is_duplicate = True
            seen_indices.add(loser_idx)
            cosine_dupes += 1
            logger.debug(
                f"[S4/Cosine] sim={sim:.3f} → dup: {active[loser_idx].chunk_id[:8]}…"
            )

    if cosine_dupes:
        logger.info(f"[S4/Cosine] {cosine_dupes} semantic duplicates marked (threshold={threshold})")

    return chunks


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _get_embeddings_batched(
    client: AsyncOpenAI,
    chunks: list[EvidenceChunk],
    model: str,
) -> list[list[float]]:
    """Получает embeddings батчами по _EMBEDDING_BATCH_SIZE."""
    all_embeddings: list[list[float]] = []

    for i in range(0, len(chunks), _EMBEDDING_BATCH_SIZE):
        batch = chunks[i : i + _EMBEDDING_BATCH_SIZE]
        texts = [c.content[:8000] for c in batch]  # Лимит токенов

        response = await client.embeddings.create(model=model, input=texts)
        batch_embeddings = [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
        all_embeddings.extend(batch_embeddings)

        logger.debug(f"[S4/Cosine] Batch {i // _EMBEDDING_BATCH_SIZE + 1}: {len(batch)} embeddings")

    return all_embeddings


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    arr_a = np.array(a, dtype=np.float32)
    arr_b = np.array(b, dtype=np.float32)
    norm = np.linalg.norm(arr_a) * np.linalg.norm(arr_b)
    return float(np.dot(arr_a, arr_b) / norm) if norm > 0 else 0.0


# ---------------------------------------------------------------------------
# 3. Conflict Detection
# ---------------------------------------------------------------------------


def _detect_conflicts(chunks: list[EvidenceChunk], hierarchy_rank_delta: int) -> list[EvidenceChunk]:
    active = [c for c in chunks if not c.is_duplicate]

    _detect_hierarchy_conflicts(active, hierarchy_rank_delta)
    _detect_temporal_conflicts(active)
    _detect_factual_conflicts(active)
    _detect_enforcement_gap_conflicts(active)
    _detect_sectoral_conflicts(active)

    return chunks


# HIERARCHY
def _detect_hierarchy_conflicts(chunks: list[EvidenceChunk], rank_delta: int) -> None:
    """
    HIERARCHY: Разница legal_rank > rank_delta для источников, описывающих один домен.
    Домен = (law_id если есть) или (тема по ключевым словам).
    """
    # Группируем по law_id для точного сравнения
    by_law: dict[str, list[EvidenceChunk]] = {}
    for chunk in chunks:
        if chunk.law_id:
            by_law.setdefault(chunk.law_id, []).append(chunk)

    for law_id, group in by_law.items():
        if len(group) < 2:
            continue
        for a, b in combinations(group, 2):
            rank_diff = abs(int(a.legal_rank) - int(b.legal_rank))
            if rank_diff > rank_delta:
                _mark_conflict(a, b, ConflictType.HIERARCHY)
                logger.info(
                    f"[S4/HIERARCHY] {law_id}: rank {int(a.legal_rank)} vs {int(b.legal_rank)} "
                    f"(delta={rank_diff})"
                )

    # Дополнительно: проверяем Web-источники с сильно разными рангами по одной теме
    web_chunks = [c for c in chunks if c.web_tier is not None]
    adilet_chunks = [c for c in chunks if c.web_tier is None]

    for web_c in web_chunks:
        for adilet_c in adilet_chunks:
            rank_diff = abs(int(web_c.legal_rank) - int(adilet_c.legal_rank))
            # Только если оба ссылаются на один law_id
            if (
                web_c.law_id
                and adilet_c.law_id
                and web_c.law_id == adilet_c.law_id
                and rank_diff > rank_delta
            ):
                _mark_conflict(web_c, adilet_c, ConflictType.HIERARCHY)


# TEMPORAL
def _detect_temporal_conflicts(chunks: list[EvidenceChunk]) -> None:
    """
    TEMPORAL: Один law_id + article, разные effective_date.
    Также детектирует по годам из текста (для web-источников без метаданных).
    """
    key_map: dict[tuple[str, str], list[EvidenceChunk]] = {}

    for chunk in chunks:
        if chunk.law_id and chunk.article and chunk.effective_date:
            key = (chunk.law_id, chunk.article)
            key_map.setdefault(key, []).append(chunk)

    for (law_id, article), group in key_map.items():
        if len(group) < 2:
            continue
        dates = {c.effective_date for c in group}
        if len(dates) > 1:
            for a, b in combinations(group, 2):
                if a.effective_date != b.effective_date:
                    _mark_conflict(a, b, ConflictType.TEMPORAL)
                    logger.info(
                        f"[S4/TEMPORAL] {law_id} ст.{article}: "
                        f"{a.effective_date} vs {b.effective_date}"
                    )

    # Text-based temporal detection (для web-источников без метаданных effective_date)
    # Сравниваем только чанки из РАЗНЫХ источников (разные base URL).
    # Статьи одного закона из одного источника — это одна редакция, не конфликт.
    law_map: dict[str, list[EvidenceChunk]] = {}
    for chunk in chunks:
        if chunk.law_id and not chunk.effective_date:
            law_map.setdefault(chunk.law_id, []).append(chunk)

    for law_id, group in law_map.items():
        if len(group) < 2:
            continue
        chunk_years: list[set[int]] = []
        for chunk in group:
            years = {int(m.group(1)) for m in _YEAR_RE.finditer(chunk.content)}
            chunk_years.append(years)
        for (i, years_a), (j, years_b) in combinations(enumerate(chunk_years), 2):
            if not years_a or not years_b:
                continue
            # Skip: chunks from the same source document (same base URL)
            # e.g. different articles of the same law fetched in one batch
            base_i = group[i].source_url.split("#")[0].split("?")[0]
            base_j = group[j].source_url.split("#")[0].split("?")[0]
            if base_i == base_j:
                continue
            min_gap = min(abs(ya - yb) for ya in years_a for yb in years_b)
            if min_gap > 2:
                _mark_conflict(group[i], group[j], ConflictType.TEMPORAL)
                logger.info(
                    f"[S4/TEMPORAL/Text] {law_id}: year gap {min_gap}y "
                    f"({min(years_a)} vs {max(years_b)})"
                )


# FACTUAL
def _detect_factual_conflicts(chunks: list[EvidenceChunk]) -> None:
    """
    FACTUAL: Расхождение числовых значений в источниках, описывающих один закон и статью.
    Сравниваем числа с единицами измерения между чанками одного law_id + article.
    """
    by_law_and_art: dict[tuple[str, str], list[EvidenceChunk]] = {}
    for chunk in chunks:
        if chunk.law_id and chunk.article:
            by_law_and_art.setdefault((chunk.law_id, chunk.article), []).append(chunk)

    for (law_id, article), group in by_law_and_art.items():
        if len(group) < 2:
            continue

        # Извлекаем числа с единицами из каждого чанка
        chunk_numbers: list[dict[str, set[str]]] = []
        for chunk in group:
            nums = _extract_legal_numbers(chunk.content)
            chunk_numbers.append(nums)

        # Сравниваем попарно
        for (i, nums_a), (j, nums_b) in combinations(enumerate(chunk_numbers), 2):
            conflicts = _find_number_conflicts(nums_a, nums_b)
            if conflicts:
                _mark_conflict(group[i], group[j], ConflictType.FACTUAL)
                logger.info(
                    f"[S4/FACTUAL] {law_id} ст.{article}: conflicting values: {conflicts}"
                )


def _extract_legal_numbers(text: str) -> dict[str, set[str]]:
    """
    Извлекает числа с единицами из текста.
    Returns: {unit_type: {значение1, значение2, ...}}
    """
    result: dict[str, set[str]] = {}
    for m in _NUMBER_RE.finditer(text):
        num = m.group(1).replace(" ", "").replace(",", ".")
        unit = m.group(2).lower().rstrip(".")
        result.setdefault(unit, set()).add(num)
    return result


def _find_number_conflicts(
    nums_a: dict[str, set[str]],
    nums_b: dict[str, set[str]],
) -> list[str]:
    """
    Находит конфликты: одинаковые единицы, но разные значения.
    Только если оба источника содержат числа данной единицы И они различаются.
    """
    conflicts = []
    for unit in set(nums_a) & set(nums_b):  # Общие единицы
        vals_a = nums_a[unit]
        vals_b = nums_b[unit]
        # Конфликт: есть значения только в одном источнике
        only_in_a = vals_a - vals_b
        only_in_b = vals_b - vals_a
        if only_in_a and only_in_b:
            conflicts.append(f"{unit}: {sorted(only_in_a)} vs {sorted(only_in_b)}")
    return conflicts


# ENFORCEMENT_GAP
def _detect_enforcement_gap_conflicts(chunks: list[EvidenceChunk]) -> None:
    """
    ENFORCEMENT_GAP: Adilet норма (rank <= 7) vs Web практика (TIER_2/TIER_3).
    Ищем чанки одного law_id где один — норма, другой — практика.
    """
    norm_chunks = [
        c for c in chunks
        if c.web_tier is None and int(c.legal_rank) <= int(LegalRank.MINISTERIAL_ORDER)
    ]
    practice_chunks = [
        c for c in chunks
        if c.web_tier in (WebTier.TIER_2, WebTier.TIER_3)
    ]

    if not norm_chunks or not practice_chunks:
        return

    # Ищем пары с одинаковым law_id (явный разрыв)
    for norm in norm_chunks:
        for practice in practice_chunks:
            if not norm.law_id or not practice.law_id:
                continue
            if norm.law_id != practice.law_id:
                continue

            # Проверяем: есть ли в практике слова-маркеры несоответствия
            gap_markers = _has_enforcement_gap_markers(practice.content)
            if gap_markers:
                _mark_conflict(norm, practice, ConflictType.ENFORCEMENT_GAP)
                logger.info(
                    f"[S4/ENFORCEMENT_GAP] {norm.law_id}: "
                    f"norm={norm.chunk_id[:8]}… vs practice={practice.chunk_id[:8]}… "
                    f"markers={gap_markers}"
                )


def _has_enforcement_gap_markers(text: str) -> list[str]:
    """
    Ищет маркеры несоответствия нормы и практики (RU + EN + KK).
    """
    markers = [
        # RU
        "на практике", "фактически", "не исполняется", "игнорируется",
        "не применяется", "обходят", "коррупция", "не работает",
        "де-факто", "де факто", "в реальности", "нарушается",
        "не соблюдается", "слабо исполняется", "де юре",
        # EN
        "de facto", "in practice", "not enforced", "rarely applied",
        "compliance gap", "unenforced", "loophole", "workaround",
        "circumvented", "paper law",
        # KK
        "іс жүзінде", "орындалмайды", "сақталмайды", "бұзылады",
    ]
    found = [m for m in markers if m.lower() in text.lower()]
    return found


# SECTORAL
def _detect_sectoral_conflicts(chunks: list[EvidenceChunk]) -> None:
    """
    SECTORAL: Чанки об одном законе, описывающие разные секторы
    с разными числовыми требованиями → FACTUAL конфликт.
    """
    by_law: dict[str, list[EvidenceChunk]] = {}
    for chunk in chunks:
        if chunk.law_id:
            by_law.setdefault(chunk.law_id, []).append(chunk)

    for law_id, group in by_law.items():
        if len(group) < 2:
            continue

        chunk_sectors = [(chunk, _get_sector(chunk.content)) for chunk in group]
        sectored = [(c, s) for c, s in chunk_sectors if s is not None]

        if len(sectored) < 2:
            continue

        for (chunk_a, sector_a), (chunk_b, sector_b) in combinations(sectored, 2):
            if sector_a == sector_b:
                continue
            nums_a = _extract_legal_numbers(chunk_a.content)
            nums_b = _extract_legal_numbers(chunk_b.content)
            conflicts = _find_number_conflicts(nums_a, nums_b)
            if conflicts:
                _mark_conflict(chunk_a, chunk_b, ConflictType.FACTUAL)
                logger.info(
                    f"[S4/SECTORAL] {law_id}: {sector_a} vs {sector_b} "
                    f"→ conflicting values: {conflicts[:2]}"
                )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _mark_conflict(a: EvidenceChunk, b: EvidenceChunk, conflict_type: ConflictType) -> None:
    for chunk, other in [(a, b), (b, a)]:
        chunk.is_conflict = True
        if conflict_type not in chunk.conflict_types:
            chunk.conflict_types.append(conflict_type)
        if other.chunk_id not in chunk.conflict_with_ids:
            chunk.conflict_with_ids.append(other.chunk_id)
