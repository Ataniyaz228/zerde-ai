"""
ЗЕРДЕ v6.2 — Stage 6: The Auditor (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Вход:  AnalysisJSON + list[EvidenceChunk]
Выход: AnalysisJSON со статусами

Реализация:
  1. Topology check: source_ids существуют в корпусе
  2. BM25 scoring: rank_bm25.BM25Okapi, нормализованный score
  3. Arithmetic check: sympy парсинг чисел
  Строго без Retry: ошибка → UNVERIFIED
"""

from __future__ import annotations

import logging
import re
import string
from typing import NamedTuple

import numpy as np
from rank_bm25 import BM25Okapi

from zerde.config import get_settings
from zerde.models import AnalysisJSON, EvidenceChunk, Fact, ValidationStatus

logger = logging.getLogger(__name__)

# Стоп-слова для BM25 токенизации (русский + казахский + юридические)
_STOP_WORDS = frozenset([
    "и", "в", "на", "с", "по", "от", "до", "за", "при", "о", "об", "из",
    "или", "а", "но", "что", "как", "это", "не", "к", "для", "то",
    "же", "бы", "ли", "со", "да", "уж", "ведь", "вот", "ну", "ж",
    "the", "a", "an", "of", "in", "is", "are", "was", "were",
    "жəне", "немесе", "бойынша", "туралы",
])

# Regex для числовых утверждений
_NUMBER_CLAIM_RE = re.compile(
    r"\b(\d[\d\s]{0,5}(?:[.,]\d{1,4})?)"
    r"\s*(%|млн\.?|млрд\.?|тыс\.?|тг\.?|kzt|мрп|мзп|лет|месяц\w*|дн\w*|тенге|процент\w*)\b",
    re.IGNORECASE,
)


class AuditResult(NamedTuple):
    status: ValidationStatus
    bm25_score: float | None
    arithmetic_ok: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_analysis(
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
) -> AnalysisJSON:
    """
    Этап 6: Детерминированный аудит. Строго без Retry.
    Ошибка при любой проверке → UNVERIFIED.
    """
    settings = get_settings()
    corpus_index = {c.chunk_id: c for c in chunks if not c.is_duplicate}

    logger.info(f"[S6] Audit start. facts={len(analysis.facts)} corpus={len(corpus_index)}")

    # Строим BM25 индекс
    bm25 = _build_bm25_index(corpus_index)

    scores: list[float] = []

    for fact in analysis.facts:
        try:
            result = _audit_fact(
                fact,
                corpus_index,
                bm25,
                settings.validation_threshold,
                settings.bm25_medium_threshold,
            )
            fact.validation_status = result.status
            fact.bm25_score = result.bm25_score
            if result.bm25_score is not None:
                scores.append(result.bm25_score)
        except Exception as e:
            # Строго без Retry
            logger.warning(f"[S6] Fact '{fact.fact_id}' audit exception: {e}")
            fact.validation_status = ValidationStatus.UNVERIFIED

    # Audit выводов
    _audit_conclusions(analysis, corpus_index)

    # Overall reliability
    if scores:
        analysis.overall_reliability = float(np.mean(scores))

    # Статистика
    status_counts = {s: 0 for s in ValidationStatus}
    for fact in analysis.facts:
        status_counts[fact.validation_status] += 1

    logger.info(
        f"[S6] Done. HIGH={status_counts[ValidationStatus.HIGH]} "
        f"MEDIUM={status_counts[ValidationStatus.MEDIUM]} "
        f"LOW={status_counts[ValidationStatus.LOW]} "
        f"UNVERIFIED={status_counts[ValidationStatus.UNVERIFIED]} "
        f"reliability={analysis.overall_reliability:.3f}"
        if analysis.overall_reliability else f"[S6] Done. No scores."
    )
    return analysis


# ---------------------------------------------------------------------------
# BM25 Index
# ---------------------------------------------------------------------------


class ZerdeBM25:
    """
    BM25Okapi wrapper с нормализацией scores [0, 1].
    Корпус: все активные EvidenceChunk.
    """

    def __init__(self, corpus_index: dict[str, EvidenceChunk]) -> None:
        self._index = corpus_index
        self._ids = list(corpus_index.keys())

        if not self._ids:
            self._bm25 = None
            return

        tokenized_corpus = [
            _tokenize(corpus_index[cid].content)
            for cid in self._ids
        ]
        self._bm25 = BM25Okapi(tokenized_corpus)

        # Нормализационный фактор: max score по корпусу
        # Используем первый документ как запрос для оценки масштаба
        sample_query = _tokenize(corpus_index[self._ids[0]].content[:200])
        raw_scores = self._bm25.get_scores(sample_query)
        self._max_score = float(np.max(raw_scores)) if len(raw_scores) > 0 else 1.0
        if self._max_score < 0.001:
            self._max_score = 1.0

    def score(self, query: str, source_ids: list[str]) -> float:
        """
        Вычисляет BM25 score между query и текстами source_ids.
        Возвращает нормализованный score [0, 1].
        Если source_ids не в корпусе → 0.0.
        """
        if not self._bm25 or not source_ids:
            return 0.0

        valid_ids = [sid for sid in source_ids if sid in self._index]
        if not valid_ids:
            return 0.0

        query_tokens = _tokenize(query)
        if not query_tokens:
            return 0.0

        raw_scores = self._bm25.get_scores(query_tokens)

        # Собираем scores только для source_ids
        id_to_idx = {cid: i for i, cid in enumerate(self._ids)}
        source_scores = [
            raw_scores[id_to_idx[sid]]
            for sid in valid_ids
            if sid in id_to_idx
        ]

        if not source_scores:
            return 0.0

        # Берём максимальный из source scores и нормализуем
        best_raw = float(np.max(source_scores))
        normalized = min(1.0, best_raw / self._max_score)
        return max(0.0, normalized)


def _build_bm25_index(corpus_index: dict[str, EvidenceChunk]) -> ZerdeBM25:
    """Строит BM25 индекс. Логирует размер."""
    bm25 = ZerdeBM25(corpus_index)
    logger.info(f"[S6/BM25] Index built: {len(corpus_index)} documents")
    return bm25


def _tokenize(text: str) -> list[str]:
    """
    Токенизация для BM25: lowercase + удаление пунктуации + стоп-слова.
    """
    text = text.lower()
    # Удаляем пунктуацию (кроме дефиса в словах)
    text = re.sub(r"[^\w\s\-]", " ", text)
    tokens = text.split()
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


# ---------------------------------------------------------------------------
# Fact Audit
# ---------------------------------------------------------------------------


def _audit_fact(
    fact: Fact,
    corpus_index: dict[str, EvidenceChunk],
    bm25: ZerdeBM25,
    high_threshold: float,
    medium_threshold: float,
) -> AuditResult:
    """
    Аудит одного факта. Без catch — исключения всплывают наверх.
    """
    # 1. Topology
    if not _check_topology(fact, corpus_index):
        return AuditResult(ValidationStatus.UNVERIFIED, None, False)

    # Специальный случай: UNLINKED facts от LLM без источников
    if fact.source_ids == ["UNLINKED"]:
        return AuditResult(ValidationStatus.UNVERIFIED, 0.0, False)

    # 2. BM25
    bm25_score = bm25.score(fact.claim, fact.source_ids)

    # 3. Arithmetic
    arithmetic_ok = _arithmetic_check(fact, corpus_index)
    if not arithmetic_ok:
        # Числовое расхождение → понижаем статус
        adjusted_score = bm25_score * 0.5
        status = _score_to_status(adjusted_score, high_threshold, medium_threshold)
        return AuditResult(status, bm25_score, False)

    status = _score_to_status(bm25_score, high_threshold, medium_threshold)
    return AuditResult(status, bm25_score, True)


def _score_to_status(
    score: float,
    high_threshold: float,
    medium_threshold: float,
) -> ValidationStatus:
    if score >= high_threshold:
        return ValidationStatus.HIGH
    elif score >= medium_threshold:
        return ValidationStatus.MEDIUM
    else:
        return ValidationStatus.LOW


# ---------------------------------------------------------------------------
# Topology Check
# ---------------------------------------------------------------------------


def _check_topology(fact: Fact, corpus_index: dict[str, EvidenceChunk]) -> bool:
    """True если все source_ids (кроме UNLINKED) существуют в корпусе."""
    valid_ids = [sid for sid in fact.source_ids if sid != "UNLINKED"]
    if not valid_ids:
        return False

    missing = [sid for sid in valid_ids if sid not in corpus_index]
    if missing:
        logger.warning(
            f"[S6/Topology] Fact '{fact.fact_id}': "
            f"{len(missing)} missing source_ids: {[m[:12] for m in missing]}"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Arithmetic Check (sympy)
# ---------------------------------------------------------------------------


def _arithmetic_check(fact: Fact, corpus_index: dict[str, EvidenceChunk]) -> bool:
    """
    Проверяет числа в claim против оригинальных источников.
    Использует sympy для нормализации чисел.
    Возвращает True если конфликтов не найдено.
    """
    claim_numbers = _extract_numbers_with_units(fact.claim)
    if not claim_numbers:
        return True  # Нет чисел — проверять нечего

    source_texts = " ".join(
        corpus_index[sid].content
        for sid in fact.source_ids
        if sid in corpus_index and sid != "UNLINKED"
    )
    if not source_texts:
        return True

    source_numbers = _extract_numbers_with_units(source_texts)

    # Для каждого числа из claim ищем подтверждение в источниках
    for unit, claim_values in claim_numbers.items():
        if unit not in source_numbers:
            continue  # Эта единица не упоминается в источниках — OK

        source_values = source_numbers[unit]
        claim_sym = _normalize_numbers(claim_values)
        source_sym = _normalize_numbers(source_values)

        # Конфликт: claim утверждает значение, которого нет ни в одном источнике
        for cv in claim_sym:
            if source_sym and cv not in source_sym:
                # Допускаем 5% погрешность для округлений
                if not any(abs(cv - sv) / max(abs(sv), 0.001) < 0.05 for sv in source_sym):
                    logger.warning(
                        f"[S6/Arithmetic] Fact '{fact.fact_id}': "
                        f"claim value {cv} {unit} not in sources {source_sym}"
                    )
                    return False

    return True


def _extract_numbers_with_units(text: str) -> dict[str, set[str]]:
    """Извлекает числа с единицами. Returns: {unit: {val1, val2}}."""
    result: dict[str, set[str]] = {}
    for m in _NUMBER_CLAIM_RE.finditer(text):
        num_str = m.group(1).replace(" ", "").replace(",", ".")
        unit = m.group(2).lower().rstrip(".")
        result.setdefault(unit, set()).add(num_str)
    return result


def _normalize_numbers(num_strings: set[str]) -> set[float]:
    """Конвертирует строки в float через sympy."""
    result: set[float] = set()
    for s in num_strings:
        try:
            from sympy import Float, sympify
            val = float(sympify(s.replace(" ", "")))
            result.add(val)
        except Exception:
            try:
                result.add(float(s))
            except ValueError:
                pass
    return result


# ---------------------------------------------------------------------------
# Conclusions Audit
# ---------------------------------------------------------------------------


def _audit_conclusions(
    analysis: AnalysisJSON,
    corpus_index: dict[str, EvidenceChunk],
) -> None:
    """Простой аудит выводов: topology + проверка fact_ids."""
    fact_ids = {f.fact_id for f in analysis.facts}

    for conclusion in analysis.conclusions:
        # Topology
        valid_sources = [
            sid for sid in conclusion.source_ids
            if sid not in ("UNLINKED",) and sid in corpus_index
        ]
        missing_facts = [
            fid for fid in conclusion.supporting_fact_ids
            if fid not in fact_ids
        ]

        if not valid_sources and conclusion.source_ids != ["UNLINKED"]:
            conclusion.validation_status = ValidationStatus.UNVERIFIED
        elif missing_facts:
            conclusion.validation_status = ValidationStatus.LOW
        else:
            conclusion.validation_status = ValidationStatus.MEDIUM
