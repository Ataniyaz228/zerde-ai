"""
Stage 6: The Auditor
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
from typing import NamedTuple

import numpy as np
from rank_bm25 import BM25Okapi

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimSeverity,
    ClaimVerdict,
    ConflictRecord,
    ConflictType,
    EvidenceChunk,
    Fact,
    ValidationStatus,
    VerdictStatus,
)

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
    # Prefix index: префиксы chunk_id (от 8 до 32 символов) → полный chunk_id
    # LLM может возвращать обрезанные ID разной длины (12, 16 и тд)
    prefix_index: dict[str, str] = {}
    for cid in corpus_index:
        for prefix_len in range(8, 33):
            pfx = cid[:prefix_len]
            if pfx not in prefix_index:
                prefix_index[pfx] = cid

    # H2 Fix: Стандартизируем виртуальные source_ids
    # LLM может обрезать ID, поэтому проверяем по началу строки ("reference_")
    VIRTUAL_SOURCES = {"UNLINKED", "reference_data"}

    # Downgrade "UNLINKED -> HIGH" loophole:
    # Any CONFIRMED LLM verdict with no real sources (i.e. only UNLINKED or empty source_ids, and not is_deterministic)
    # must be downgraded to UNVERIFIED and confidence to LOW.
    for v in analysis.verdicts:
        if v.status == VerdictStatus.CONFIRMED and not v.is_deterministic:
            real_sources = [sid for sid in v.source_ids if sid and sid != "UNLINKED" and not sid.startswith("reference_")]
            if not real_sources:
                logger.warning(
                    f"[S6/Downgrade] Downgrading verdict for claim '{v.claim_id}' from CONFIRMED to UNVERIFIED "
                    f"due to lack of real source links."
                )
                v.status = VerdictStatus.UNVERIFIED
                v.confidence = "LOW"

                # Also update corresponding Fact
                for fact in analysis.facts:
                    if fact.claim_id == v.claim_id:
                        fact.confidence = 0.4
                        fact.claim = f"[{v.claim_id}]: '{v.document_value}'"
                        fact.validation_status = ValidationStatus.UNVERIFIED

    logger.info(f"[S6] Audit start. facts={len(analysis.facts)} corpus={len(corpus_index)}")

    # Строим BM25 индекс
    bm25 = _build_bm25_index(corpus_index)

    scores: list[float] = []

    for fact in analysis.facts:
        try:
            # UNLINKED или пустые источники не могут быть проверены по корпусу
            if not fact.source_ids or "UNLINKED" in fact.source_ids:
                fact.validation_status = ValidationStatus.UNVERIFIED
                fact.bm25_score = 0.0
                continue

            # Факты с только виртуальными source_ids (reference_data)
            # были проверены детерминированно — оцениваем по confidence
            real_sources = [
                sid for sid in fact.source_ids
                if sid not in VIRTUAL_SOURCES and not sid.startswith("reference_")
            ]
            if not real_sources:
                # Детерминированный факт: score прямо из confidence
                scores.append(fact.confidence)
                fact.bm25_score = fact.confidence
                fact.validation_status = _confidence_to_status(
                    fact.confidence,
                    settings.validation_threshold,
                    settings.bm25_medium_threshold,
                )
                continue

            result = _audit_fact(
                fact,
                corpus_index,
                prefix_index,
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

    # V7.0: Override validation_status для CONTRADICTED verdicts
    # Если вердикт CONTRADICTED — статус всегда LOW (красный), независимо от BM25.
    # UNVERIFIED вердикты НЕ override'ятся в LOW — они остаются UNVERIFIED.
    verdict_map = {v.claim_id: v for v in analysis.verdicts if v.claim_id}
    for fact in analysis.facts:
        if fact.claim_id and fact.claim_id in verdict_map:
            v = verdict_map[fact.claim_id]
            if v.status == VerdictStatus.CONTRADICTED:
                fact.validation_status = ValidationStatus.LOW
                fact.bm25_score = 0.0
            # UNVERIFIED: сохраняем BM25-статус как есть, не применяем LOW

    # Audit выводов
    _audit_conclusions(analysis, corpus_index, prefix_index)

    # V8.0: Ratio-based reliability score с penalty-корректором.
    # reliability = ratio_score * (1.0 - penalty)
    # ratio_score = (n_confirmed + 0.3*n_unverified_neutral) / n_total
    # Это исключает ситуацию 71% при 0 confirmed claims.
    if analysis.verdicts:
        # Считаем только аналитические вердикты (structural не участвуют)
        analytical_verdicts = [
            v for v in analysis.verdicts
            if not (v.claim_id and v.claim_id.startswith("structural_"))
        ]
        n_total = len(analytical_verdicts)

        n_confirmed = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.CONFIRMED)
        n_unverified_neutral = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.UNVERIFIED
            and v.severity not in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
        )
        n_contradicted_critical = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.CRITICAL
        )
        n_contradicted_high = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.HIGH
        )
        n_unverified_risks = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.UNVERIFIED and v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
        )

        if n_total > 0:
            # Базовый confirmation ratio (0 confirmed → ratio_score ≈ 0)
            ratio_score = (n_confirmed + 0.3 * n_unverified_neutral) / n_total
            # Penalty: прямые противоречия существенно снижают
            penalty = (
                0.20 * n_contradicted_critical +
                0.10 * n_contradicted_high +
                0.05 * n_unverified_risks
            )
            reliability = max(0.05, min(1.0, ratio_score * (1.0 - penalty)))
        else:
            reliability = 0.05
        analysis.overall_reliability = reliability

        logger.info(
            f"[S6/Score] n_total={n_total} confirmed={n_confirmed} "
            f"unverified_neutral={n_unverified_neutral} crit_contrad={n_contradicted_critical} "
            f"high_contrad={n_contradicted_high} unverified_risks={n_unverified_risks} "
            f"ratio_score={ratio_score:.3f} penalty={penalty:.3f} → reliability={reliability:.3f}"
        )
    elif scores:
        analysis.overall_reliability = float(np.mean(scores))

    # Статистика
    status_counts = {s: 0 for s in ValidationStatus}
    for fact in analysis.facts:
        status_counts[fact.validation_status] += 1

    # V7.0: Conflicts Bridge — превращаем CONTRADICTED вердикты в ConflictRecord
    # для единой секции "Выявленные конфликты и коллизии" в отчёте
    analysis.conflicts = _build_conflicts_from_verdicts(analysis.verdicts)
    logger.info(f"[S6/Conflicts] Bridge created {len(analysis.conflicts)} conflict records")

    # C6 Fix: reliability может быть 0.0, проверяем на is not None
    logger.info(
        f"[S6] Done. HIGH={status_counts[ValidationStatus.HIGH]} "
        f"MEDIUM={status_counts[ValidationStatus.MEDIUM]} "
        f"LOW={status_counts[ValidationStatus.LOW]} "
        f"UNVERIFIED={status_counts[ValidationStatus.UNVERIFIED]} "
        f"reliability={analysis.overall_reliability:.3f}"
        if analysis.overall_reliability is not None else "[S6] Done. No scores."
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
    prefix_index: dict[str, str],
    bm25: ZerdeBM25,
    high_threshold: float,
    medium_threshold: float,
) -> AuditResult:
    """
    Аудит одного факта. Без catch — исключения всплывают наверх.
    """
    # Резолвим prefix IDs → полные chunk_ids
    resolved_ids = _resolve_source_ids(fact.source_ids, corpus_index, prefix_index)

    # 1. Topology
    if not _check_topology(fact, resolved_ids, corpus_index):
        return AuditResult(ValidationStatus.UNVERIFIED, None, False)

    # Специальный случай: UNLINKED facts от LLM без источников
    if resolved_ids == ["UNLINKED"]:
        return AuditResult(ValidationStatus.UNVERIFIED, 0.0, False)

    # 2. BM25
    bm25_score = bm25.score(fact.claim, resolved_ids)

    # 3. Arithmetic
    arithmetic_ok = _arithmetic_check(fact, resolved_ids, corpus_index)
    if not arithmetic_ok:
        # Числовое расхождение → понижаем статус
        adjusted_score = bm25_score * 0.5
        status = _score_to_status(adjusted_score, high_threshold, medium_threshold)
        return AuditResult(status, bm25_score, False)

    status = _score_to_status(bm25_score, high_threshold, medium_threshold)
    return AuditResult(status, bm25_score, True)


def _confidence_to_status(
    confidence: float,
    high_threshold: float,
    medium_threshold: float,
) -> ValidationStatus:
    """Конвертирует confidence score в ValidationStatus (для детерминированных фактов)."""
    if confidence >= high_threshold:
        return ValidationStatus.HIGH
    elif confidence >= medium_threshold:
        return ValidationStatus.MEDIUM
    else:
        return ValidationStatus.LOW


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


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _find_closest_source_id(
    sid: str,
    corpus_index: dict[str, EvidenceChunk],
    max_distance: int = 3,
) -> str | None:
    best_cid = None
    best_dist = max_distance + 1

    target_len = len(sid)
    if target_len < 4:
        return None

    for cid in corpus_index:
        pfx = cid[:target_len]
        dist = _levenshtein_distance(sid, pfx)
        if dist < best_dist:
            best_dist = dist
            best_cid = cid

    if best_dist <= max_distance:
        logger.info(f"[S6/Fuzzy] Resolved hallucinated ID '{sid}' to '{best_cid[:target_len]}' (dist={best_dist})")
        return best_cid
    return None


def _resolve_source_ids(
    source_ids: list[str],
    corpus_index: dict[str, EvidenceChunk],
    prefix_index: dict[str, str],
) -> list[str]:
    """
    Резолвит короткие (prefix) source_ids в полные chunk_ids.
    LLM возвращает '12-символьные' ID — ищем в prefix_index.
    """
    virtual = {"UNLINKED", "reference_data"}
    resolved = []
    for sid in source_ids:
        if sid in virtual or sid.startswith("reference_"):
            resolved.append(sid)
        elif sid in corpus_index:
            resolved.append(sid)  # Уже полный ID
        elif sid in prefix_index:
            resolved.append(prefix_index[sid])  # Найден по префиксу
        else:
            # Fuzzy matching fallback
            fuzzy_cid = _find_closest_source_id(sid, corpus_index)
            if fuzzy_cid:
                resolved.append(fuzzy_cid)
            else:
                resolved.append(sid)  # Оставляем как есть (для логирования)
    return resolved


def _check_topology(
    fact: Fact,
    resolved_ids: list[str],
    corpus_index: dict[str, EvidenceChunk],
) -> bool:
    """True если хотя бы один source_id (кроме виртуальных) существует в корпусе."""
    virtual = {"UNLINKED", "reference_data"}
    valid_ids = [sid for sid in resolved_ids if sid not in virtual and not sid.startswith("reference_")]
    if not valid_ids:
        return False

    found = [sid for sid in valid_ids if sid in corpus_index]
    missing = [sid for sid in valid_ids if sid not in corpus_index]

    if missing:
        logger.debug(
            f"[S6/Topology] Fact '{fact.fact_id}': "
            f"{len(missing)} unresolved source_ids: {[m[:12] for m in missing]}"
        )

    return len(found) > 0


# ---------------------------------------------------------------------------
# Arithmetic Check (sympy)
# ---------------------------------------------------------------------------


def _arithmetic_check(
    fact: Fact,
    resolved_ids: list[str],
    corpus_index: dict[str, EvidenceChunk],
) -> bool:
    """
    Проверяет числа в claim против оригинальных источников.
    """
    claim_numbers = _extract_numbers_with_units(fact.claim)
    if not claim_numbers:
        return True

    skipped = {"UNLINKED"}
    source_texts = " ".join(
        corpus_index[sid].content
        for sid in resolved_ids
        if sid in corpus_index and sid not in skipped and not sid.startswith("reference_")
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
    """Конвертирует строки в float через sympy, защищая дефисы."""
    result: set[float] = set()
    for s in num_strings:
        cleaned = s.replace(" ", "")
        # Сначала пробуем прямой парсинг во избежание оверхеда/ошибок SymPy
        try:
            result.add(float(cleaned))
            continue
        except ValueError:
            pass

        # Если содержит дефис, проверяем, не отрицательное ли это число
        if "-" in cleaned:
            if cleaned.startswith("-") and cleaned.count("-") == 1:
                try:
                    result.add(float(cleaned))
                    continue
                except ValueError:
                    pass
            # Иначе это диапазон или номер подстатьи (например, 196-1), пропускаем его
            continue

        try:
            from sympy import sympify
            val = float(sympify(cleaned))
            result.add(val)
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Conclusions Audit
# ---------------------------------------------------------------------------


def _audit_conclusions(
    analysis: AnalysisJSON,
    corpus_index: dict[str, EvidenceChunk],
    prefix_index: dict[str, str],
) -> None:
    """Простой аудит выводов: topology + проверка fact_ids."""
    fact_ids = {f.fact_id for f in analysis.facts}

    for conclusion in analysis.conclusions:
        # Резолвим prefix IDs
        resolved = _resolve_source_ids(conclusion.source_ids, corpus_index, prefix_index)
        valid_sources = [
            sid for sid in resolved
            if sid not in ("UNLINKED", "reference_data") and not sid.startswith("reference_") and sid in corpus_index
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


# ---------------------------------------------------------------------------
# V7.0: Conflicts Bridge
# ---------------------------------------------------------------------------


def _build_conflicts_from_verdicts(verdicts: list[ClaimVerdict]) -> list[ConflictRecord]:
    """
    Превращает CONTRADICTED вердикты в ConflictRecord для единой секции конфликтов.

    V8.0: Классификация ConflictType основана на структурированных приоритетах:
      1. HIERARCHY: только если в contradiction_detail есть явные иерархические сигналы
         (отсылки на КоАП, иерархию актов, подзаконные акты vs кодекс)
         Сигнал: "коап" | "иерарх" | "подзакон" | "ппрк" | "постановление правительства"
      2. TEMPORAL: если спор о сроках/датах, без иерархического конфликта
      3. FACTUAL: все остальные CONTRADICTED (числа, факты, ссылки на статьи)

    ЗАПРЕЩЕНО: пропаганда HIERARCHY только из-за отсутствия документа в корпусе.
    """
    conflicts: list[ConflictRecord] = []
    seen: set[str] = set()

    # Строгие сигналы для HIERARCHY иерархических конфликтов
    # Только явные ссылки на иерархию нормативных актов (КоАП > закон, ППРК > приказ)
    _HIERARCHY_SIGNALS = (
        "коап",               # Кодекс административных правонарушений
        "иерарх",             # иерархия актов
        "подзакон",           # подзаконный акт
        "ппрк",               # Постановление Правительства
        "постановление правительства",  # полный вариант
        "приказ министр",      # приказ министерства
        "legal_rank",           # технический маркер (rank_deltaом)
    )
    # Сигналы для TEMPORAL конфликтов (временные расхождения)
    _TEMPORAL_SIGNALS = (
        "срок", "дата", "дней", "часов", "месяц",
        "вступает", "действие", "времен", "срок уведомления",
    )

    for v in verdicts:
        if v.status != VerdictStatus.CONTRADICTED:
            continue
        if not v.claim_id or v.claim_id in seen:
            continue
        seen.add(v.claim_id)

        detail_lower = (v.contradiction_detail or "").lower()

        # Строгая приоритетная классификация:
        # 1. HIERARCHY — только если есть явные ссылки на иерархию актов
        has_hierarchy = any(sig in detail_lower for sig in _HIERARCHY_SIGNALS)
        # 2. TEMPORAL — конфликт сроков/дат, без иерархии
        has_temporal = any(sig in detail_lower for sig in _TEMPORAL_SIGNALS) and not has_hierarchy

        if has_hierarchy:
            ctype = ConflictType.HIERARCHY
        elif has_temporal:
            ctype = ConflictType.TEMPORAL
        else:
            # Все остальные CONTRADICTED (числа, ссылки, факты) → FACTUAL
            ctype = ConflictType.FACTUAL

        severity = ClaimSeverity.HIGH if v.confidence == "HIGH" else ClaimSeverity.MEDIUM
        conflicts.append(
            ConflictRecord(
                record_id=f"conflict_{len(conflicts):04d}",
                conflict_type=ctype,
                claim_id=v.claim_id,
                claim_text=v.contradiction_detail or "Противоречие найдено",
                document_value=v.document_value,
                found_value=v.found_value,
                detail=v.contradiction_detail or "",
                severity=severity,
            )
        )

    return conflicts
