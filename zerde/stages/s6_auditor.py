"""
ЗЕРДЕ v6.3 — Stage 6: The Auditor (HYBRID VALIDATOR)
Вход:  AnalysisJSON + list[EvidenceChunk]
Выход: AnalysisJSON со статусами

Улучшения v6.3:
  1. Hybrid Scoring: score = 0.4*BM25 + 0.6*TF-IDF_cosine
  2. Cross-Lingual Alignment: если языки claim и source разные — BM25 компонент = 0
  3. Arithmetic check (sympy)
  Строго без Retry: ошибка → UNVERIFIED
"""

from __future__ import annotations

import logging
import re
import string
from typing import NamedTuple

import numpy as np
from langdetect import detect as _langdetect_detect
from langdetect import LangDetectException
from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine

from zerde.config import get_settings
from zerde.models import AnalysisJSON, EvidenceChunk, Fact, ValidationStatus

logger = logging.getLogger(__name__)

# Стоп-слова для токенизации (русский + казахский + юридические + английский)
_STOP_WORDS = frozenset([
    "и", "в", "на", "с", "по", "от", "до", "за", "при", "о", "об", "из",
    "или", "а", "но", "что", "как", "это", "не", "к", "для", "то",
    "же", "бы", "ли", "со", "да", "уж", "ведь", "вот", "ну", "ж",
    "the", "a", "an", "of", "in", "is", "are", "was", "were", "to", "for",
    "жəне", "немесе", "бойынша", "туралы",
])

# Regex для числовых утверждений
_NUMBER_CLAIM_RE = re.compile(
    r"\b(\d[\d\s]{0,5}(?:[.,]\d{1,4})?)"
    r"\s*(%|млн\.?|млрд\.?|тыс\.?|тг\.?|kzt|мрп|мзп|лет|месяц\w*|дн\w*|тенге|процент\w*)\\b",
    re.IGNORECASE,
)

# Кэш для определения языка (chunk_id → lang)
_lang_cache: dict[str, str] = {}


class AuditResult(NamedTuple):
    status: ValidationStatus
    bm25_score: float | None
    cosine_score: float | None
    hybrid_score: float | None
    arithmetic_ok: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_analysis(
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
) -> AnalysisJSON:
    """
    Этап 6: Детерминированный аудит с Hybrid Validator. Строго без Retry.
    Ошибка при любой проверке → UNVERIFIED.
    """
    settings = get_settings()
    corpus_index = {c.chunk_id: c for c in chunks if not c.is_duplicate}

    logger.info(f"[S6] Audit start. facts={len(analysis.facts)} corpus={len(corpus_index)}")

    # Строим BM25 индекс
    bm25 = _build_bm25_index(corpus_index)

    # Строим TF-IDF индекс для cosine similarity (multilingual-ready)
    tfidf = _build_tfidf_index(corpus_index)

    scores: list[float] = []

    for fact in analysis.facts:
        try:
            result = _audit_fact(
                fact,
                corpus_index,
                bm25,
                tfidf,
                settings.hybrid_threshold_high,
                settings.hybrid_threshold_medium,
                settings.hybrid_bm25_weight,
                settings.hybrid_cosine_weight,
            )
            fact.validation_status = result.status
            fact.bm25_score = result.hybrid_score  # Сохраняем hybrid как основной score
            if result.hybrid_score is not None:
                scores.append(result.hybrid_score)
        except Exception as e:
            logger.warning(f"[S6] Fact '{fact.fact_id}' audit exception: {e}")
            fact.validation_status = ValidationStatus.UNVERIFIED

    # Audit выводов
    _audit_conclusions(analysis, corpus_index)

    # Overall reliability на основе hybrid scores
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

        # Нормализационный фактор
        sample_query = _tokenize(corpus_index[self._ids[0]].content[:200])
        raw_scores = self._bm25.get_scores(sample_query)
        self._max_score = float(np.max(raw_scores)) if len(raw_scores) > 0 else 1.0
        if self._max_score < 0.001:
            self._max_score = 1.0

    def score(self, query: str, source_ids: list[str]) -> float:
        """Вычисляет BM25 score. Возвращает нормализованный score [0, 1]."""
        if not self._bm25 or not source_ids:
            return 0.0

        valid_ids = [sid for sid in source_ids if sid in self._index]
        if not valid_ids:
            return 0.0

        query_tokens = _tokenize(query)
        if not query_tokens:
            return 0.0

        raw_scores = self._bm25.get_scores(query_tokens)
        id_to_idx = {cid: i for i, cid in enumerate(self._ids)}
        source_scores = [
            raw_scores[id_to_idx[sid]]
            for sid in valid_ids
            if sid in id_to_idx
        ]

        if not source_scores:
            return 0.0

        best_raw = float(np.max(source_scores))
        normalized = min(1.0, best_raw / self._max_score)
        return max(0.0, normalized)


def _build_bm25_index(corpus_index: dict[str, EvidenceChunk]) -> ZerdeBM25:
    bm25 = ZerdeBM25(corpus_index)
    logger.info(f"[S6/BM25] Index built: {len(corpus_index)} documents")
    return bm25


# ---------------------------------------------------------------------------
# TF-IDF Cosine Index (Hybrid Validator — Cross-Lingual aware)
# ---------------------------------------------------------------------------


class ZerdeTfidfIndex:
    """
    TF-IDF + cosine similarity index для семантического сравнения.
    Работает cross-lingually через символьные n-gramы (char_wb, analyzer='char_wb').
    Не требует PyTorch, весит ~10МБ.
    """

    def __init__(self, corpus_index: dict[str, EvidenceChunk]) -> None:
        self._index = corpus_index
        self._ids = list(corpus_index.keys())
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None

        if not self._ids:
            return

        # char-level n-gramы: работают для RU/KK/EN без предобучения
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            max_features=30_000,
            sublinear_tf=True,
        )
        corpus_texts = [corpus_index[cid].content[:4000] for cid in self._ids]
        try:
            self._matrix = self._vectorizer.fit_transform(corpus_texts)
            logger.info(f"[S6/TF-IDF] Index built: {len(self._ids)} docs, "
                        f"{self._matrix.shape[1]} features")
        except Exception as e:
            logger.warning(f"[S6/TF-IDF] Build failed: {e}")
            self._vectorizer = None

    def score(self, query: str, source_ids: list[str]) -> float:
        """Cosine similarity между query и лучшим из source_ids."""
        if self._vectorizer is None or self._matrix is None or not source_ids:
            return 0.0

        valid_ids = [sid for sid in source_ids if sid in self._index]
        if not valid_ids:
            return 0.0

        try:
            query_vec = self._vectorizer.transform([query[:2000]])
            id_to_idx = {cid: i for i, cid in enumerate(self._ids)}
            source_indices = [id_to_idx[sid] for sid in valid_ids if sid in id_to_idx]

            if not source_indices:
                return 0.0

            source_matrix = self._matrix[source_indices]
            sims = sklearn_cosine(query_vec, source_matrix)[0]
            return float(np.max(sims))
        except Exception as e:
            logger.debug(f"[S6/TF-IDF] Score error: {e}")
            return 0.0


def _build_tfidf_index(corpus_index: dict[str, EvidenceChunk]) -> ZerdeTfidfIndex:
    return ZerdeTfidfIndex(corpus_index)


# ---------------------------------------------------------------------------
# Language Detection (Cross-Lingual Alignment)
# ---------------------------------------------------------------------------


def _detect_lang(text: str, chunk_id: str | None = None) -> str:
    """
    Определяет язык текста через langdetect.
    Возвращает ISO 639-1 код или 'unknown'.
    Кэширует результат по chunk_id если передан.
    """
    if chunk_id and chunk_id in _lang_cache:
        return _lang_cache[chunk_id]

    try:
        lang = _langdetect_detect(text[:1000])
    except LangDetectException:
        lang = "unknown"

    if chunk_id:
        _lang_cache[chunk_id] = lang

    return lang


# ---------------------------------------------------------------------------
# Fact Audit — Hybrid
# ---------------------------------------------------------------------------


def _audit_fact(
    fact: Fact,
    corpus_index: dict[str, EvidenceChunk],
    bm25: ZerdeBM25,
    tfidf: ZerdeTfidfIndex,
    high_threshold: float,
    medium_threshold: float,
    bm25_weight: float,
    cosine_weight: float,
) -> AuditResult:
    """
    Аудит одного факта с Hybrid Scoring.
    Без catch — исключения всплывают наверх.
    """
    # 1. Topology
    if not _check_topology(fact, corpus_index):
        return AuditResult(ValidationStatus.UNVERIFIED, None, None, None, False)

    # Специальный случай: UNLINKED facts
    if fact.source_ids == ["UNLINKED"]:
        return AuditResult(ValidationStatus.UNVERIFIED, 0.0, 0.0, 0.0, False)

    # 2. Определяем языки для Cross-Lingual Alignment
    claim_lang = _detect_lang(fact.claim)
    source_langs = set()
    for sid in fact.source_ids:
        if sid in corpus_index and sid != "UNLINKED":
            chunk = corpus_index[sid]
            source_langs.add(_detect_lang(chunk.content[:500], chunk_id=sid))

    # Cross-lingual: если claim и ВСЕ источники на разных языках → BM25 не работает
    is_cross_lingual = bool(source_langs) and all(
        lang != claim_lang and lang != "unknown" for lang in source_langs
    )

    # 3. BM25 score
    bm25_score = bm25.score(fact.claim, fact.source_ids)

    # 4. TF-IDF Cosine score (работает cross-lingually через char n-gramы)
    cosine_score = tfidf.score(fact.claim, fact.source_ids)

    # 5. Hybrid score
    if is_cross_lingual:
        # Cross-lingual: только cosine (char n-gramы частично инвариантны к языку)
        hybrid = cosine_score
        logger.debug(
            f"[S6/CrossLingual] {fact.fact_id}: claim={claim_lang} "
            f"sources={source_langs} → BM25 bypassed, cosine={cosine_score:.3f}"
        )
    else:
        hybrid = bm25_weight * bm25_score + cosine_weight * cosine_score

    logger.debug(
        f"[S6/Hybrid] {fact.fact_id}: bm25={bm25_score:.3f} "
        f"cosine={cosine_score:.3f} hybrid={hybrid:.3f} "
        f"cross_lingual={is_cross_lingual}"
    )

    # 6. Arithmetic check
    arithmetic_ok = _arithmetic_check(fact, corpus_index)
    if not arithmetic_ok:
        hybrid *= 0.5

    status = _score_to_status(hybrid, high_threshold, medium_threshold)
    return AuditResult(status, bm25_score, cosine_score, hybrid, arithmetic_ok)


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
    Возвращает True если конфликтов не найдено.
    """
    claim_numbers = _extract_numbers_with_units(fact.claim)
    if not claim_numbers:
        return True

    source_texts = " ".join(
        corpus_index[sid].content
        for sid in fact.source_ids
        if sid in corpus_index and sid != "UNLINKED"
    )
    if not source_texts:
        return True

    source_numbers = _extract_numbers_with_units(source_texts)

    for unit, claim_values in claim_numbers.items():
        if unit not in source_numbers:
            continue

        source_values = source_numbers[unit]
        claim_sym = _normalize_numbers(claim_values)
        source_sym = _normalize_numbers(source_values)

        for cv in claim_sym:
            if source_sym and cv not in source_sym:
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
            from sympy import sympify
            val = float(sympify(s.replace(" ", "")))
            result.add(val)
        except Exception:
            try:
                result.add(float(s))
            except ValueError:
                pass
    return result


def _tokenize(text: str) -> list[str]:
    """Токенизация для BM25: lowercase + пунктуация + стоп-слова."""
    text = text.lower()
    text = re.sub(r"[^\w\s\-]", " ", text)
    tokens = text.split()
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


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
