"""
Law Registry — авто-реестр законов РК.

Строится автоматически при инджесте документов.
Хранится в таблице `law_metadata` SQLite-базы.

Функции:
- resolve(raw: str) → canonical law_id (fuzzy, без галлюцинаций)
- get_adilet_url(law_id, lang) → URL на adilet.zan.kz
- extract_law_refs_from_text(text) → список упоминаний законов в тексте
"""

from __future__ import annotations

import difflib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Статический fallback-словарь (минимальный, только самые частые)
# Все законы из docs/ будут добавлены автоматически при инджесте.
# ---------------------------------------------------------------------------

_STATIC_FALLBACK: dict[str, str] = {
    # Кодексы
    "гражданский кодекс рк (общая часть)": "1000-XIII",
    "гк рк (общая часть)": "1000-XIII",
    "гражданский кодекс рк (особенная часть)": "409-I",
    "гк рк (особенная часть)": "409-I",
    "земельный кодекс рк": "442-II",
    # 95-IV (старый Бюджетный кодекс 2008) утратил силу — действует 171-VIII (2025),
    # проверено scripts/verify_law_registry.py против adilet.
    "бюджетный кодекс рк": "171-VIII",
    "трудовой кодекс рк": "414-I-NEW",
    "коап рк": "235-V",
    "коап": "235-V",
    "уголовный кодекс рк": "226-V",
    "ук рк": "226-V",
    "упк рк": "231-V",
    "уголовно-процессуальный кодекс рк": "231-V",
    "аппк рк": "350-VI",
    "аппк": "350-VI",
    "административный процедурно-процессуальный кодекс рк": "350-VI",
    "налоговый кодекс рк": "120-VI",
    # Законы
    "закон о персональных данных": "94-V",
    "закон об исполнительном производстве": "261-IV",
    "закон о чси": "261-IV",
    "закон о частных судебных исполнителях": "261-IV",
    "закон о государственных закупках": "434-V",
    "закон о нотариате": "155-V",
    "закон о языках": "151-I",
    "закон о связи": "567-II",
    "закон о противодействии коррупции": "410-V",
    # Известные галлюцинации LLM (ID которые модели путают)
    # Ключи — строки, которые LLM пишет; значения — правильный canonical ID
    "233-IV": "261-IV",   # LLM путает номер закона об исполнительном производстве
    "262-IV": "261-IV",   # опечатка ±1
    "269-IV": "261-IV",   # опечатка
    "260-IV": "261-IV",   # опечатка ±1
}

# Полный код Адилет → короткий ID
_ADILET_CODE_TO_SHORT: dict[str, str] = {
    "Z100000261_": "261-IV",
    "K1400000235": "235-V",
    "K1400000226": "226-V",
    "K1400000231": "231-V",
    "K2000000350": "350-VI",
    "K070000212_": "212-IV",
    "K940001000_": "1000-XIII",
    "K990000409_": "409-I",
    "K030000442_": "442-II",
    "K150000414_": "414-I",
    "Z1300000094": "94-V",
    "Z1500000418": "418-V",
}

# Короткий ID → полный код Адилет
_SHORT_TO_ADILET_CODE: dict[str, str] = {v: k for k, v in _ADILET_CODE_TO_SHORT.items()}

# Ядро law_id = "<число>-<римское>", без тег-суффикса (-UK, -NEW и т.п.).
# Используется для безопасного base-ID матчинга: "226-V" ↔ "226-V-UK",
# "414-I" ↔ "414-I-NEW" — БЕЗ fuzzy (транспозиция цифр не должна матчить).
_LAW_ID_CORE_RE = re.compile(r"^(\d+-[IVXLCDM]+)(?:-[A-Z]+)?$")


def _law_id_core(s: str) -> str | None:
    m = _LAW_ID_CORE_RE.match(s.strip().upper())
    return m.group(1) if m else None


# Явные alias'ы перенумерованных законов (короткий ID → канонический короткий ID).
# Заменяют опасный fuzzy-матчинг коротких ID. Дополнять по мере обнаружения.
# 233-IV — прежний номер Закона об исполнительном производстве (ныне 261-IV).
_LAW_ID_ALIASES: dict[str, str] = {
    "233-IV": "261-IV",
}

# Паттерны для извлечения ссылок из текста законопроекта
_LAW_REF_PATTERNS = [
    # «от 2 апреля 2010 года № 261-IV»
    re.compile(r"от\s+\d+\s+\w+\s+\d{4}\s+года?\s*№\s*(\d+-[IVXLCD]+)", re.IGNORECASE),
    # «№ 261-IV Закона»
    re.compile(r"№\s*(\d+-[IVXLCD]+)", re.IGNORECASE),
    # «Закон РК от ... № Z100000261»
    re.compile(r"№\s*([A-Z]\d{8,10}_?)", re.IGNORECASE),
    # Прямое упоминание «261-IV»
    re.compile(r"\b(\d{2,4}-[IVXLCD]{1,6})\b"),
]


class LawRegistry:
    """
    Авто-реестр законов РК.

    Приоритет резолюции:
      1. Точное совпадение law_id из БД
      2. Точное совпадение adilet_code из БД
      3. Fuzzy совпадение с title_ru / title_kz из БД
      4. Точное совпадение из статического словаря _STATIC_FALLBACK
      5. Fuzzy совпадение с ключами _STATIC_FALLBACK
      6. Возвращаем исходную строку (не ломаем пайплайн)
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._by_law_id: dict[str, dict] = {}     # "261-IV" → {title_ru, title_kz, adilet_code}
        self._by_adilet_code: dict[str, str] = {}  # "Z100000261_" → "261-IV"
        self._title_index: list[tuple[str, str]] = []  # (normalized_title, law_id)
        self._load()

    # ------------------------------------------------------------------
    # Загрузка из БД
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает реестр из таблицы law_metadata. Безопасно если таблица пуста."""
        if not self._db_path.exists():
            logger.debug("[LawRegistry] DB not found, using static fallback only.")
            self._load_static()
            return

        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT law_id, adilet_code, title_ru, title_kz FROM law_metadata"
            ).fetchall()
            conn.close()
        except sqlite3.OperationalError:
            # Таблица ещё не создана
            logger.debug("[LawRegistry] law_metadata table not found, using static fallback.")
            self._load_static()
            return

        for row in rows:
            law_id = row["law_id"]
            adilet_code = row["adilet_code"] or ""
            title_ru = row["title_ru"] or ""
            title_kz = row["title_kz"] or ""

            self._by_law_id[law_id] = {
                "adilet_code": adilet_code,
                "title_ru": title_ru,
                "title_kz": title_kz,
            }
            if adilet_code:
                self._by_adilet_code[adilet_code.rstrip("_")] = law_id
                self._by_adilet_code[adilet_code] = law_id

            for title in (title_ru, title_kz):
                if title:
                    self._title_index.append((_normalize(title), law_id))

        logger.info(f"[LawRegistry] Loaded {len(self._by_law_id)} laws from DB.")
        self._load_static()

    def _load_static(self) -> None:
        """Добавляет статический словарь как fallback."""
        for name, law_id in _STATIC_FALLBACK.items():
            norm = _normalize(name)
            # Только если нет дубликата из БД
            if not any(n == norm for n, _ in self._title_index):
                self._title_index.append((norm, law_id))

    def reload(self) -> None:
        """Перечитывает реестр из БД (вызывается после инджеста)."""
        self._by_law_id.clear()
        self._by_adilet_code.clear()
        self._title_index.clear()
        self._load()

    # ------------------------------------------------------------------
    # Основной API
    # ------------------------------------------------------------------

    def resolve(self, raw: str) -> str:
        """
        Разрешает любую строку в канонический law_id.

        Args:
            raw: Строка: "261-IV", "233-IV", "закон об ИП", "Z100000261_", и т.д.

        Returns:
            Канонический law_id ("261-IV") или исходную строку если не найдено.
        """
        raw = raw.strip()
        if not raw:
            return raw

        # 1. Точное совпадение law_id (case-insensitive)
        upper = raw.upper()
        for lid in self._by_law_id:
            if lid.upper() == upper:
                return lid

        # 2. Точное совпадение adilet_code
        clean_code = raw.rstrip("_")
        if clean_code in self._by_adilet_code:
            return self._by_adilet_code[clean_code]
        if raw in self._by_adilet_code:
            return self._by_adilet_code[raw]

        # 3. Статический список adilet_code
        if clean_code in _ADILET_CODE_TO_SHORT:
            return _ADILET_CODE_TO_SHORT[clean_code]

        # 4. Base-ID match: ссылка без тег-суффикса ("226-V", "414-I") сопоставляется
        #    с хранимым ID, имеющим то же <число>-<римское> ядро, но с суффиксом
        #    ("226-V-UK", "414-I-NEW"). СТРОГО по ядру, БЕЗ fuzzy — иначе транспозиция
        #    цифр (253-V) ложно резолвится в реальный закон (235-V) → false-grounding
        #    (см. eval Фазы 0: law_false_grounding_rate). Опечатки/перенумерации законов
        #    должны жить в явных alias-таблицах, а не угадываться по похожести.
        core = _law_id_core(upper)
        if core:
            for lid in self._by_law_id:
                if _law_id_core(lid.upper()) == core:
                    if lid.upper() != upper:
                        logger.info(f"[LawRegistry] Base-ID resolved '{raw}' → '{lid}'")
                    return lid

        # 4b. Явные alias'ы перенумерованных законов (вместо fuzzy).
        if upper in _LAW_ID_ALIASES:
            return _LAW_ID_ALIASES[upper]

        # 4c. Вход выглядит как law_id ("253-V"), но не совпал ни с чем известным —
        #     возвращаем как есть. НЕ уходим в title-матчинг (шаги 5-7): он ложно
        #     резолвит короткие ID-строки в реальные законы ("253-V"→"261-IV") и
        #     даёт false-grounding. Title-матчинг только для текстовых названий.
        if core or re.match(r"^\d+-[IVXLCDM]+$", upper):
            logger.debug(f"[LawRegistry] Unknown law_id '{raw}', returning as-is.")
            return raw

        # 5. Точное совпадение заголовка
        norm_raw = _normalize(raw)
        for norm_title, law_id in self._title_index:
            if norm_title == norm_raw:
                return law_id

        # 6. Substring совпадение
        for norm_title, law_id in self._title_index:
            if norm_title and (norm_title in norm_raw or norm_raw in norm_title):
                logger.info(f"[LawRegistry] Substring resolved '{raw}' → '{law_id}'")
                return law_id

        # 7. Fuzzy по заголовкам
        all_titles = [t for t, _ in self._title_index]
        if all_titles:
            close = difflib.get_close_matches(norm_raw, all_titles, n=1, cutoff=0.6)
            if close:
                for norm_title, law_id in self._title_index:
                    if norm_title == close[0]:
                        logger.info(f"[LawRegistry] Fuzzy resolved '{raw}' → '{law_id}' (title match)")
                        return law_id

        logger.debug(f"[LawRegistry] Could not resolve '{raw}', returning as-is.")
        return raw

    def get_adilet_code(self, law_id: str) -> str | None:
        """Возвращает adilet_code закона из БД/статики. None если неизвестен.

        НЕ фабрикует код. Раньше get_adilet_url угадывал `Z{num.zfill(10)}_`,
        что давало НЕВЕРНЫЕ коды для неингестированных законов (напр. 138-IV →
        Z0000000138). Единый источник — law_metadata (БД) + минимальная статика.
        """
        canon = self.resolve(law_id)
        if canon in self._by_law_id:
            code = self._by_law_id[canon].get("adilet_code")
            if code:
                return code
        if canon in _SHORT_TO_ADILET_CODE:
            return _SHORT_TO_ADILET_CODE[canon]
        # Вход уже является adilet_code.
        if re.match(r"^[A-Z]\d{8,10}_?$", canon):
            return canon
        return None

    def get_adilet_url(self, law_id: str, lang: str = "rus") -> str:
        """URL на adilet.zan.kz для закона; "" если код неизвестен (без фабрикации).

        Раньше для неизвестных законов возвращался угаданный (неверный) URL.
        Теперь — пусто: "нет в реестре = явный пробел", а не фальшивая ссылка.
        """
        code = self.get_adilet_code(law_id)
        if not code:
            return ""
        return f"https://adilet.zan.kz/{lang}/docs/{code}"

    def get_all_law_ids(self) -> list[str]:
        """Возвращает все известные law_id."""
        return list(self._by_law_id.keys())

    def get_title(self, law_id: str, lang: str = "ru") -> str:
        """Возвращает заголовок закона."""
        info = self._by_law_id.get(law_id, {})
        if lang == "kz":
            return info.get("title_kz") or info.get("title_ru") or law_id
        return info.get("title_ru") or law_id

    # ------------------------------------------------------------------
    # Извлечение ссылок из текста документа
    # ------------------------------------------------------------------

    def extract_law_refs_from_text(self, text: str) -> list[str]:
        """
        Извлекает упоминания законов из текста законопроекта.

        Возвращает список canonical law_id (дедуплицированных).
        Используется как подсказки для планировщика — не как истина.

        Args:
            text: Нормализованный текст документа.

        Returns:
            Список law_id, найденных в тексте.
        """
        found: set[str] = set()
        # Ищем только в первых 10 000 символов (заголовок + первые статьи)
        sample = text[:10_000]

        for pattern in _LAW_REF_PATTERNS:
            for match in pattern.finditer(sample):
                raw_ref = match.group(1).strip()
                resolved = self.resolve(raw_ref)
                if resolved and resolved != raw_ref:
                    # Успешно резолвнули — это реальный закон
                    found.add(resolved)
                elif re.match(r"^\d{2,4}-[IVXLCD]{1,6}$", raw_ref, re.IGNORECASE):
                    # Выглядит как ID закона, даже если не в реестре
                    found.add(raw_ref.upper())

        return list(found)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: Optional[LawRegistry] = None


def get_registry(db_path: str | Path | None = None) -> LawRegistry:
    """
    Возвращает singleton реестра.

    Args:
        db_path: Путь к БД. Если None — использует ZERDE_CACHE_DB или "zerde_cache.db".
    """
    global _registry
    if _registry is None or db_path is not None:
        import os
        if db_path is None:
            db_path = os.getenv("ZERDE_CACHE_DB", "zerde_cache.db")
        _registry = LawRegistry(db_path)
    return _registry


def reload_registry() -> None:
    """Перечитывает реестр из БД. Вызывать после инджеста."""
    global _registry
    if _registry is not None:
        _registry.reload()
    else:
        get_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Нормализует строку для сравнения."""
    text = text.lower()
    text = text.replace("республики казахстан", "рк")
    text = text.replace("республикасы", "рк")
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text
