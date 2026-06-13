"""
Per-fact audit: BM25 scoring, article-boost rebound, arithmetic (number)
checks, and score/confidence -> ValidationStatus conversions.

Moved verbatim from zerde/stages/s6_auditor.py (Phase 1, Step 3). Magic
numbers replaced with references into scoring_config.CONFIG.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from zerde.models import DocumentClaim, EvidenceChunk, Fact, ValidationStatus
from zerde.stages.s6_audit.retrieval import ZerdeBM25
from zerde.stages.s6_audit.scoring_config import CONFIG
from zerde.stages.s6_audit.source_filter import _check_topology, _resolve_source_ids
from zerde.utils.claims import are_law_ids_synonymous as _are_law_ids_synonymous
from zerde.utils.claims import extract_article_from_claim as _extract_article_from_claim
from zerde.utils.claims import extract_referenced_law_ids as _extract_referenced_law_ids

logger = logging.getLogger(__name__)

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
# Fact Audit
# ---------------------------------------------------------------------------


def _audit_fact(
    fact: Fact,
    corpus_index: dict[str, EvidenceChunk],
    bm25: ZerdeBM25,
    high_threshold: float,
    medium_threshold: float,
    claim: DocumentClaim | None = None,
) -> AuditResult:
    """
    Аудит одного факта. Без catch — исключения всплывают наверх.
    """
    resolved_ids = _resolve_source_ids(fact.source_ids, corpus_index)

    # 1. Topology
    if not _check_topology(fact, resolved_ids, corpus_index, claim):
        return AuditResult(ValidationStatus.UNVERIFIED, None, False)

    # Специальный случай: UNLINKED facts от LLM без источников
    if resolved_ids == ["UNLINKED"]:
        return AuditResult(ValidationStatus.UNVERIFIED, 0.0, False)

    # 2. BM25
    bm25_score = bm25.score(fact.claim, resolved_ids)

    # V9.6: Если LLM привязала к общему закону (BM25 низкий),
    # но в claim есть номер статьи + target_law_id — найдём конкретный чанк статьи
    if claim and bm25_score < medium_threshold:
        article_num = _extract_article_from_claim(claim)
        referenced_law_ids = _extract_referenced_law_ids(claim)
        if article_num and referenced_law_ids:
            for cid, chunk in corpus_index.items():
                if (
                    chunk.article == article_num
                    and any(_are_law_ids_synonymous(chunk.law_id, ref_id) for ref_id in referenced_law_ids)
                ):
                    # Нашли точный чанк (закон + статья) — повышаем confidence
                    article_bm25 = bm25.score(fact.claim, [cid])
                    if article_bm25 > bm25_score:
                        logger.info(
                            f"[S6/ArticleBoost] Fact '{fact.fact_id}' rebound via article match: "
                            f"{bm25_score:.3f} → {article_bm25:.3f} (chunk={cid[:12]})"
                        )
                        # Дополняем source_ids найденным чанком
                        if cid not in fact.source_ids:
                            fact.source_ids = list(fact.source_ids) + [cid]
                        bm25_score = max(bm25_score, article_bm25, CONFIG.article_boost_min_medium)  # минимум MEDIUM
                    break

    # 3. Arithmetic
    arithmetic_ok = _arithmetic_check(fact, resolved_ids, corpus_index)
    if not arithmetic_ok:
        # Числовое расхождение → понижаем статус
        adjusted_score = bm25_score * CONFIG.arithmetic_penalty_multiplier
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
# Arithmetic Check (safe float parser, Phase 1 Step 4)
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


_SAFE_NUM_RE = re.compile(r"^[\d.]+$")


def _safe_float(s: str) -> float | None:
    """Safe replacement for sympy.sympify on digit/dot-only strings
    (Phase 1, Step 4). Inputs reaching here already failed a plain float()
    and contain no '-'."""
    import ast
    if not _SAFE_NUM_RE.match(s):
        return None
    try:
        v = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _normalize_numbers(num_strings: set[str]) -> set[float]:
    """Конвертирует строки в float, защищая дефисы."""
    result: set[float] = set()
    for s in num_strings:
        cleaned = s.replace(" ", "")
        # Сначала пробуем прямой парсинг во избежание оверхеда/ошибок
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

        val = _safe_float(cleaned)
        if val is not None:
            result.add(val)
    return result
