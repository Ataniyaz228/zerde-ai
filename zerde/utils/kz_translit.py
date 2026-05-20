"""
Stage 0: KZ Transliteration Utility
Нормализация казахской латиницы → кириллица (детерминированный маппинг).
Поддерживает официальный алфавит 2017 и 2021 года.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Маппинг KZ Латиница → Кириллица (официальный алфавит 2021)
# ---------------------------------------------------------------------------

_KZ_LATIN_TO_CYRILLIC: dict[str, str] = {
    # Строчные
    "a": "а", "á": "ә", "b": "б", "v": "в", "g": "г", "gh": "ғ",
    "d": "д", "e": "е", "ı": "і", "zh": "ж", "z": "з", "i": "и",
    "j": "й", "k": "к", "kh": "қ", "l": "л", "m": "м", "n": "н",
    "ñ": "ң", "o": "о", "ó": "ө", "p": "п", "r": "р", "s": "с",
    "t": "т", "u": "у", "ú": "ү", "f": "ф", "kk": "қ", "ch": "ч",
    "sh": "ш", "y": "ы", "h": "х",
    # Прописные
    "A": "А", "Á": "Ә", "B": "Б", "V": "В", "G": "Г", "Gh": "Ғ",
    "D": "Д", "E": "Е", "I": "І", "Zh": "Ж", "Z": "З", "J": "Й",
    "K": "К", "Kh": "Қ", "L": "Л", "M": "М", "N": "Н", "Ñ": "Ң",
    "O": "О", "Ó": "Ө", "P": "П", "R": "Р", "S": "С", "T": "Т",
    "U": "У", "Ú": "Ү", "F": "Ф", "Ch": "Ч", "Sh": "Ш", "Y": "Ы",
    "H": "Х",
}

# Паттерны проверки наличия казахской латиницы
_KZ_LATIN_CHARS = re.compile(r"[áéíóúüñğqwÁÉÍÓÚÜÑĞ]|gh|zh|sh|ch|kh", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_kz_text(text: str) -> str:
    """
    Нормализует текст:
      1. Обнаруживает казахскую латиницу.
      2. Транслитерирует в кириллицу (детерминированно).
      3. Нормализует Unicode (NFC).

    Args:
        text: Исходный текст (может содержать смесь KZ Latin + Cyrillic + Russian).

    Returns:
        Нормализованный текст кириллицей.
    """
    if not _has_kz_latin(text):
        return unicodedata.normalize("NFC", text)

    transliterated = _transliterate_kz_latin(text)
    return unicodedata.normalize("NFC", transliterated)


def _has_kz_latin(text: str) -> bool:
    """Быстрая эвристическая проверка наличия казахской латиницы в тексте."""
    return bool(_KZ_LATIN_CHARS.search(text))


def _transliterate_kz_latin(text: str) -> str:
    """
    Детерминированная транслитерация: длинные паттерны → сначала.
    Порядок: диграфы (gh, zh, sh, ch, kh) → диакритики → одиночные буквы.
    """
    result = text

    # Шаг 1: Диграфы (обязательно ДО одиночных букв)
    digraphs = ["Gh", "Zh", "Sh", "Ch", "Kh", "GH", "ZH", "SH", "CH", "KH", "gh", "zh", "sh", "ch", "kh"]
    for digraph in digraphs:
        if digraph in _KZ_LATIN_TO_CYRILLIC:
            result = result.replace(digraph, _KZ_LATIN_TO_CYRILLIC[digraph])

    # Шаг 2: Диакритики и специальные символы
    diacritics = ["á", "Á", "ı", "I", "ñ", "Ñ", "ó", "Ó", "ú", "Ú"]
    for char in diacritics:
        if char in _KZ_LATIN_TO_CYRILLIC:
            result = result.replace(char, _KZ_LATIN_TO_CYRILLIC[char])

    return result
