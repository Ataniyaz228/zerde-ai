"""
ЗЕРДЕ v6.2 — KZ Transliteration (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Детерминированная транслитерация казахской латиницы → кириллица.
Поддерживает:
  - Официальный алфавит 2021 (Указ Президента №165)
  - Переходный алфавит 2017 (апострофная система)
  - Смешанные тексты (кириллица + латиница)
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Алфавит 2021 (без апострофов, с диакритиками)
# ---------------------------------------------------------------------------
# Источник: Указ Президента РК от 19 февраля 2021 года №165

_DIGRAPHS_2021: dict[str, str] = {
    # Прописные диграфы
    "Gh": "Ғ", "GH": "Ғ",
    "Sh": "Ш", "SH": "Ш",
    "Ch": "Ч", "CH": "Ч",
    "Zh": "Ж", "ZH": "Ж",
    "Kh": "Х", "KH": "Х",
    # Строчные диграфы
    "gh": "ғ",
    "sh": "ш",
    "ch": "ч",
    "zh": "ж",
    "kh": "х",
}

_SINGLE_2021: dict[str, str] = {
    # Специфически казахские с диакритиками (прописные)
    "Á": "Ә", "á": "ə",  # Ә
    "Ó": "Ө", "ó": "ó",  # Ө
    "Ú": "Ү", "ú": "ü",  # Ү
    "Ñ": "Ң", "ñ": "ŋ",  # Ң
    "I": "І", "ı": "ı",  # І (казахская і без точки)
    # Базовые (прописные)
    "A": "А", "B": "В", "D": "Д", "E": "Е",
    "F": "Ф", "G": "Г", "H": "Х",
    "I": "И", "J": "Й", "K": "К",
    "L": "Л", "M": "М", "N": "Н",
    "O": "О", "P": "П", "Q": "Қ",
    "R": "Р", "S": "С", "T": "Т",
    "U": "У", "V": "В", "W": "У",
    "X": "КС", "Y": "Ы", "Z": "З",
    # Строчные
    "a": "а", "b": "б", "d": "д", "e": "е",
    "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "й", "k": "к",
    "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "қ",
    "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "w": "у",
    "x": "кс", "y": "ы", "z": "з",
}

# ---------------------------------------------------------------------------
# Алфавит 2017 (апострофная система, Постановление Правительства №637)
# Многие тексты всё ещё используют эту систему
# ---------------------------------------------------------------------------

_DIGRAPHS_2017: dict[str, str] = {
    "G\'": "Ғ", "g\'": "ғ",
    "O\'": "Ө", "o\'": "ө",
    "U\'": "Ү", "u\'": "ү",
    "N\'": "Ң", "n\'": "ң",
    "Sh": "Ш", "sh": "ш",
    "Ch": "Ч", "ch": "ч",
    "Zh": "Ж", "zh": "ж",
    "Gh": "Ғ", "gh": "ғ",
    "I\'": "І", "i\'": "і",
    "A\'": "Ə", "a\'": "ə",
}

# ---------------------------------------------------------------------------
# Маркеры наличия KZ латиницы
# ---------------------------------------------------------------------------

# Диакритики и типичные для казахского паттерны
_KZ_LATIN_DETECT = re.compile(
    r"[áéíóúüáóúñıÁÓÚÑI]"  # Диакритики
    r"|(?:gh|zh|sh|ch|kh)"   # Диграфы
    r"|[qwQW]",               # Q и W — типично для KZ транслитерации
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_kz_text(text: str) -> str:
    """
    Нормализует текст: KZ латиница → кириллица + Unicode NFC.

    Алгоритм:
      1. Быстрая проверка наличия KZ латиницы
      2. Если есть — транслитерируем посимвольно/по диграфам
      3. NFC нормализация

    Args:
        text: Исходный текст (может быть смешанным).

    Returns:
        Нормализованный текст кириллицей.
    """
    if not _has_kz_latin(text):
        return unicodedata.normalize("NFC", text)

    result = _transliterate(text)
    return unicodedata.normalize("NFC", result)


def has_kz_latin(text: str) -> bool:
    """Публичная функция проверки наличия KZ латиницы."""
    return _has_kz_latin(text)


# ---------------------------------------------------------------------------
# Private Implementation
# ---------------------------------------------------------------------------


def _has_kz_latin(text: str) -> bool:
    """Быстрая эвристическая проверка."""
    return bool(_KZ_LATIN_DETECT.search(text))


def _transliterate(text: str) -> str:
    """
    Транслитерирует текст с приоритетом: диграфы → диакритики → одиночные.
    Работает с любым порядком алфавита.
    """
    # Объединяем обе версии алфавита (2021 имеет приоритет)
    all_digraphs = {**_DIGRAPHS_2017, **_DIGRAPHS_2021}

    result = text

    # Шаг 1: Апострофные диграфы 2017 (G', O', etc.) — строго до одиночных
    for pattern, replacement in _DIGRAPHS_2017.items():
        if "'" in pattern:
            result = result.replace(pattern, replacement)

    # Шаг 2: Диграфы без апострофа — длинные паттерны первыми
    for pattern in sorted(_DIGRAPHS_2021.keys(), key=len, reverse=True):
        result = result.replace(pattern, _DIGRAPHS_2021[pattern])

    # Шаг 3: Диакритики (одиночные специальные символы)
    diacritics = {k: v for k, v in _SINGLE_2021.items() if not k.isascii()}
    for char, replacement in diacritics.items():
        result = result.replace(char, replacement)

    # Шаг 4: Только если слово выглядит как казахская латиница
    # (избегаем транслитерации обычных латинских слов вроде "PDF", "API")
    result = _smart_transliterate_latin_words(result)

    return result


def _smart_transliterate_latin_words(text: str) -> str:
    """
    Транслитерирует только слова, похожие на казахскую латиницу.
    Критерии KZ латиницы: содержит q, w (не английские слова), ы/і-кандидаты.
    Сохраняет: URL, email, аббревиатуры, цифры, уже кириллические слова.
    """
    # Разбиваем на слова и не-слова
    token_pattern = re.compile(r"(\b[a-zA-Z][a-zA-Z\-\']*\b|[^a-zA-Z]+)", re.UNICODE)

    parts = []
    for token in token_pattern.finditer(text):
        word = token.group()
        if _is_kz_latin_word(word):
            parts.append(_transliterate_word(word))
        else:
            parts.append(word)

    return "".join(parts)


def _is_kz_latin_word(word: str) -> bool:
    """
    Определяет, является ли слово казахской латиницей.
    Возвращает False для: аббревиатур (PDF, API), URL, обычных английских слов.
    """
    if len(word) <= 1:
        return False

    word_lower = word.lower()

    # Явные маркеры KZ латиницы
    kz_markers = ["gh", "zh", "sh", "ch", "kh"]
    if any(m in word_lower for m in kz_markers):
        return True

    # q, w — типично для KZ транслитерации (но не для english)
    # Простая эвристика: если слово содержит q/w + другие согласные
    if re.search(r"[qw]", word_lower):
        # Но не если это явное английское слово (will, what, quote)
        common_english = {"will", "what", "when", "where", "why", "who", "how",
                         "with", "was", "were", "week", "work", "word",
                         "quite", "quick", "question"}
        if word_lower not in common_english:
            return True

    return False


def _transliterate_word(word: str) -> str:
    """Транслитерирует одно слово побуквенно."""
    result = []
    i = 0
    while i < len(word):
        # Двухсимвольные паттерны
        two = word[i:i+2]
        if two in _DIGRAPHS_2021:
            result.append(_DIGRAPHS_2021[two])
            i += 2
            continue

        # Одиночный символ
        char = word[i]
        if char in _SINGLE_2021:
            result.append(_SINGLE_2021[char])
        else:
            result.append(char)
        i += 1

    return "".join(result)
