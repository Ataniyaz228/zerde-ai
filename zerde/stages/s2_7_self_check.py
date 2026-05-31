"""
Stage 2.7: Document Self-Check (Intra-document Contradiction Detection)
Вход:  str (текст документа)
Выход: list[DocumentClaim] (обнаруженные внутренние противоречия)

Детерминированный анализ:
  1. Коллизии сроков — одна статья, разные числа для одного типа срока
  2. Хронологические аномалии — дата вступления раньше даты принятия
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from zerde.models import (
    ClaimSeverity,
    ClaimType,
    DocumentClaim,
    VerdictStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex паттерны
# ---------------------------------------------------------------------------

# Номер статьи: "Статья 5", "Статья 79-1", "Статья 10.1"
_ARTICLE_RE = re.compile(
    r"[Сс]тать[яиею]\s+(\d+(?:[-.]\d+)?)", re.UNICODE
)

# Сроки: "в течение 24 часов", "180 (ста восьмидесяти) календарных дней", "60 дней"
_DEADLINE_RE = re.compile(
    r"(?:в\s+течени[ие]\s+|по\s+истечени[ие]\s+)?(\d+)\s*"
    r"(?:\([^)]*\)\s*)?"  # Пропускаем "(ста восьмидесяти)" между числом и единицей
    r"(час\w*|рабочи[хм]\s*дн\w*|календарн\w*\s*дн\w*|дн\w*|месяц\w*|сут\w*)",
    re.IGNORECASE | re.UNICODE,
)

# Пункт внутри статьи: "пункт 1", "п. 2", "п.3"
_PUNKT_RE = re.compile(
    r"(?:пункт|п\.?)\s*(\d+)", re.IGNORECASE | re.UNICODE
)

# Дата принятия: "от 21 мая 2013 года", "принят 7 января 2003 года"
_ADOPTION_DATE_RE = re.compile(
    r"(?:от|принят\w*)\s+(\d{1,2})\s+"
    r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]\w*|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s+(\d{4})",
    re.IGNORECASE | re.UNICODE,
)

# Дата вступления в силу: "вступает в силу с 1 января 2012 года"
_EFFECTIVE_DATE_RE = re.compile(
    r"вступ\w+\s+в\s+силу\s+(?:с\s+)?(\d{1,2})\s+"
    r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]\w*|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s+(\d{4})",
    re.IGNORECASE | re.UNICODE,
)

_MONTH_MAP = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}


def _parse_month(month_str: str) -> int | None:
    """Парсит русское название месяца в число 1-12."""
    month_lower = month_str.lower()
    for prefix, num in _MONTH_MAP.items():
        if month_lower.startswith(prefix):
            return num
    return None


def _normalize_unit(unit: str) -> str:
    """Нормализует единицу времени к базовой форме."""
    u = unit.lower().strip()
    if "час" in u:
        return "часов"
    if "рабоч" in u and "дн" in u:
        return "рабочих дней"
    # "календарных дней" и "дней" объединяем в одну категорию
    if "дн" in u or "дне" in u:
        return "дней"
    if "месяц" in u:
        return "месяцев"
    if "сут" in u:
        # Отдельный бакет: раньше сутки сваливались в "часов" без перевода ×24,
        # из-за чего "1 сутки" и "5 часов" давали ложную коллизию (1 vs 5).
        return "суток"
    return u


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_self_check(doc_text: str) -> list[DocumentClaim]:
    """
    Детерминированная само-проверка документа на внутренние противоречия.

    Returns:
        Список DocumentClaim с выявленными коллизиями.
    """
    claims: list[DocumentClaim] = []

    claims.extend(_detect_deadline_collisions(doc_text))
    claims.extend(_detect_chronological_anomalies(doc_text))

    if claims:
        logger.info(f"[S2.7] Self-check found {len(claims)} internal contradictions")
    else:
        logger.info("[S2.7] Self-check: no internal contradictions found")

    return claims


# ---------------------------------------------------------------------------
# Детектор 1: Коллизии сроков в одной статье
# ---------------------------------------------------------------------------


def _detect_deadline_collisions(text: str) -> list[DocumentClaim]:
    """
    Ищет случаи, когда в одной статье указаны разные числовые сроки
    для одного типа единиц (например, 180 дней и 60 дней в одной статье).
    """
    results: list[DocumentClaim] = []

    # Разбиваем по заголовкам статей: "Статья N.", "Статья N-1", "Статья N.1"
    # Поддерживает точки и новые строки после номера статьи
    parts = re.split(r"(?=\n[Сс]тать[яиею]\s+\d+(?:[-.]\d+)?\b\s*(?:\.|\n|\r))", text)

    article_blocks: dict[str, list[str]] = defaultdict(list)
    for part in parts:
        m = _ARTICLE_RE.search(part[:50])  # Ищем в первых 50 символах блока
        if m:
            art_num = m.group(1)
            article_blocks[art_num].append(part)

    # В каждой статье ищем сроки
    counter = 0
    for art_num, blocks in article_blocks.items():
        block = "\n".join(blocks)
        # Группируем сроки по единицам
        deadlines_by_unit: dict[str, list[int]] = defaultdict(list)

        for m in _DEADLINE_RE.finditer(block):
            val = int(m.group(1))
            unit = _normalize_unit(m.group(2))
            deadlines_by_unit[unit].append(val)

        # Ищем коллизии: разные значения для одного типа единиц
        for unit, values in deadlines_by_unit.items():
            unique_vals = sorted(set(values))
            if len(unique_vals) >= 2:
                vals_str = " vs ".join(str(v) for v in unique_vals)
                claim_id = f"selfcheck_{counter:04d}"
                counter += 1
                results.append(DocumentClaim(
                    claim_id=claim_id,
                    claim_text=(
                        f"Внутренняя коллизия сроков в Статье {art_num}: "
                        f"{vals_str} {unit} в одной статье"
                    ),
                    claim_type=ClaimType.TEMPORAL,
                    severity=ClaimSeverity.CRITICAL,
                    entities=[f"ст.{art_num}"],
                    deterministic_verdict=(
                        f"Статья {art_num} содержит противоречивые сроки: "
                        f"{vals_str} {unit}. Требуется уточнение."
                    ),
                    deterministic_status=VerdictStatus.CONTRADICTED,
                ))

    return results


# ---------------------------------------------------------------------------
# Детектор 2: Хронологические аномалии
# ---------------------------------------------------------------------------


def _detect_chronological_anomalies(text: str) -> list[DocumentClaim]:
    """
    Ищет случаи, когда дата вступления в силу раньше даты принятия закона.
    Сравнивает только пары дат, находящиеся рядом в тексте (в пределах 500 символов).
    """
    results: list[DocumentClaim] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Собираем все даты принятия с их позициями в тексте
    adoption_dates: list[tuple[int, int, int, str, int]] = []
    for m in _ADOPTION_DATE_RE.finditer(text):
        day, month, year = int(m.group(1)), _parse_month(m.group(2)), int(m.group(3))
        if month:
            adoption_dates.append((year, month, day, m.group(0), m.start()))

    effective_dates: list[tuple[int, int, int, str, int]] = []
    for m in _EFFECTIVE_DATE_RE.finditer(text):
        day, month, year = int(m.group(1)), _parse_month(m.group(2)), int(m.group(3))
        if month:
            effective_dates.append((year, month, day, m.group(0), m.start()))

    # Сравниваем все найденные даты вступления в силу и принятия
    counter = 0
    for eff_year, eff_month, eff_day, eff_text, eff_pos in effective_dates:
        for adop_year, adop_month, adop_day, adop_text, adop_pos in adoption_dates:
            eff_tuple = (eff_year, eff_month, eff_day)
            adop_tuple = (adop_year, adop_month, adop_day)

            if eff_tuple < adop_tuple:
                # C1 Fix: Сравниваем только пары дат, находящиеся рядом в тексте (в пределах 500 символов)
                if abs(eff_pos - adop_pos) > 500:
                    continue

                pair_key = (adop_text, eff_text)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                claim_id = f"selfcheck_chrono_{counter:04d}"
                counter += 1
                results.append(DocumentClaim(
                    claim_id=claim_id,
                    claim_text=(
                        f"Хронологическая аномалия: вступление в силу "
                        f"({eff_day:02d}.{eff_month:02d}.{eff_year}) раньше "
                        f"принятия ({adop_day:02d}.{adop_month:02d}.{adop_year})"
                    ),
                    claim_type=ClaimType.TEMPORAL,
                    severity=ClaimSeverity.CRITICAL,
                    entities=[],
                    deterministic_verdict=(
                        f"Закон не может вступить в силу раньше принятия: "
                        f"принят «{adop_text}», вступает «{eff_text}»"
                    ),
                    deterministic_status=VerdictStatus.CONTRADICTED,
                ))

    return results
