"""
S6 BM25 retrieval: ZerdeBM25 index wrapper, corpus-wide fallback search.
"""

from __future__ import annotations

import logging
import re

import numpy as np
from rank_bm25 import BM25Okapi

from zerde.config import get_settings
from zerde.models import DocumentClaim, EvidenceChunk, Fact
from zerde.utils.claims import _COMMON_LAW_NAME_MAP
from zerde.utils.claims import are_law_ids_synonymous as _are_law_ids_synonymous
from zerde.utils.claims import extract_article_from_claim as _extract_article_from_claim
from zerde.utils.claims import extract_referenced_law_ids as _extract_referenced_law_ids
from zerde.utils.textproc import tokenize_simple as _tokenize

logger = logging.getLogger(__name__)


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

        # Стабильный нормализационный фактор: вычисляем self-scores для всех документов корпуса
        self_scores = []
        for cid in self._ids:
            tokens = _tokenize(corpus_index[cid].content[:200])
            if tokens:
                raw_scores = self._bm25.get_scores(tokens)
                if len(raw_scores) > 0:
                    self_scores.append(float(np.max(raw_scores)))

        # Берем медиану self-scores для стабильной шкалы, с минимумом 1.0
        self._max_score = float(np.median(self_scores)) if self_scores else 1.0
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


def _corpus_wide_bm25_search(
    fact: Fact,
    bm25: ZerdeBM25,
    corpus_index: dict[str, EvidenceChunk],
    claim: DocumentClaim | None = None,
) -> float | None:
    """
    Иерархический полнотекстовый поиск с фильтрацией по метаданным law_id.
    """
    if not bm25._bm25 or not bm25._ids:
        return None

    # Use raw claim text from DocumentClaim for BM25, not the formatted
    # fact.claim string which contains metadata like '[claim_0001]: ...' that
    # pollutes token matching. Fall back to fact.claim if no claim object.
    if claim:
        raw_query = claim.claim_text + " " + claim.quote
    else:
        # Strip formatting artifacts from fact.claim
        raw_query = re.sub(r"\[claim_\d{4}\]:\s*['\"]?", "", fact.claim).rstrip("'\"")
    query_tokens = _tokenize(raw_query)
    if not query_tokens:
        return None

    settings = get_settings()
    raw_scores = bm25._bm25.get_scores(query_tokens)
    if len(raw_scores) == 0:
        return None

    # Identify expected law IDs from the claim
    referenced_law_ids = []
    if claim:
        referenced_law_ids = _extract_referenced_law_ids(claim)

    # --- LAYER 1: Specific Law Match ---
    if referenced_law_ids:
        candidate_indices = [
            i for i, cid in enumerate(bm25._ids)
            if any(_are_law_ids_synonymous(corpus_index[cid].law_id, ref_id) for ref_id in referenced_law_ids)
            or corpus_index[cid].web_tier is not None
        ]
        if candidate_indices:
            best_idx = max(candidate_indices, key=lambda i: raw_scores[i])
            best_raw = float(raw_scores[best_idx])
            best_cid = bm25._ids[best_idx]
            normalized = min(1.0, best_raw / bm25._max_score)
            normalized = max(0.0, normalized)

            if normalized >= settings.bm25_medium_threshold:
                fact.source_ids = [best_cid]
                logger.info(f"[S6/Fallback/Layer1] Verified claim '{fact.claim_id}' inside law {referenced_law_ids} (or web): score={normalized:.3f}")
                return normalized
            else:
                logger.info(f"[S6/Fallback/Layer1] Match inside specific law or web was too weak ({normalized:.3f} < {settings.bm25_medium_threshold}). Falling through.")
        else:
            logger.info(f"[S6/Fallback] Expected law {referenced_law_ids} is not present in the corpus, and no web chunks. Falling through.")

    # --- LAYER 2: Code Family Fallback (if specific law was not found/loaded) ---
    # (If referenced_law_ids is defined but not loaded in the corpus, we try parent Codes)
    if referenced_law_ids:
        # Коды семейства берём из _COMMON_LAW_NAME_MAP (единый источник, CI-guard),
        # а не дублируем инлайн-списком (тот дрейфовал: «309-II» — устаревший id ГК
        # особенной части, в кэше лежит 409-I → exact-match его НЕ находил).
        text_lower = (claim.claim_text + " " + claim.quote).lower() if claim else fact.claim.lower()
        parent_codes: list[str] = []
        # Только длинные/защищённые доменные ключи: короткие («гк»,«зк») как
        # подстроки дают ложные срабатывания.
        for key in ("гражданск", "земельн", "коап"):
            if key in text_lower:
                parent_codes.extend(_COMMON_LAW_NAME_MAP.get(key, []))

        parent_codes = list(set(parent_codes))
        if parent_codes:
            # Синоним-aware матч (как в Layer 1): law_id чанка может быть каноном
            # (409-I), а код семейства — его псевдонимом (309-II), и наоборот.
            candidate_indices = [
                i for i, cid in enumerate(bm25._ids)
                if any(_are_law_ids_synonymous(corpus_index[cid].law_id, pc) for pc in parent_codes)
            ]
            if candidate_indices:
                best_idx = max(candidate_indices, key=lambda i: raw_scores[i])
                best_raw = float(raw_scores[best_idx])
                best_cid = bm25._ids[best_idx]
                normalized = min(1.0, best_raw / bm25._max_score)
                normalized = max(0.0, normalized)

                if normalized >= settings.bm25_medium_threshold:
                    fact.source_ids = [best_cid]
                    logger.info(f"[S6/Fallback/Layer2] Verified claim '{fact.claim_id}' inside parent family {parent_codes}: score={normalized:.3f}")
                    return normalized

    # --- LAYER 3: Strict Corpus-Wide Fallback ---
    # Either no law was identified, or Layer 1 & 2 failed/were empty.
    # We do a corpus-wide scan, but require the high fallback threshold!
    best_idx = int(np.argmax(raw_scores))
    best_raw = float(raw_scores[best_idx])
    best_cid = bm25._ids[best_idx]
    normalized = min(1.0, best_raw / bm25._max_score)
    normalized = max(0.0, normalized)

    if normalized >= settings.bm25_fallback_threshold:
        # Дополнительная проверка на статью (если упоминается в утверждении)
        article_num = _extract_article_from_claim(claim) if claim else _extract_article_from_claim(DocumentClaim(claim_id="tmp", claim_text=fact.claim, claim_type="factual", severity="medium"))
        if article_num:
            best_chunk = corpus_index[best_cid]
            chunk_text = (best_chunk.content or "").lower()
            # Проверяем, что номер статьи (например, "47") присутствует как число или слово
            # Ищем границу слова \b47\b или ст. 47
            pattern = rf"\b{re.escape(article_num)}\b"
            if best_chunk.article != article_num and not re.search(pattern, chunk_text):
                logger.info(f"[S6/Fallback/Layer3] Rejected Layer 3 match '{best_cid[:12]}' because it does not contain article '{article_num}'")
                return None

        fact.source_ids = [best_cid]
        logger.info(f"[S6/Fallback/Layer3] Verified claim '{fact.claim_id}' via strict corpus-wide search: score={normalized:.3f}")
        return normalized

    logger.info(f"[S6/Fallback/Layer3] Best corpus-wide match was too weak ({normalized:.3f} < {settings.bm25_fallback_threshold}). Rejecting.")
    return None
