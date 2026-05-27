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
    DocumentClaim,
    ClaimExtractionResult,
)
from zerde.reference_data import LAW_REGISTRY

logger = logging.getLogger(__name__)

# Стоп-слова для BM25 токенизации (русский + казахский + юридические)
_STOP_WORDS = frozenset([
    # --- Русские служебные слова ---
    "и", "в", "на", "с", "по", "от", "до", "за", "при", "о", "об", "из",
    "или", "а", "но", "что", "как", "это", "не", "к", "для", "то",
    "же", "бы", "ли", "со", "да", "уж", "ведь", "вот", "ну", "ж",
    # --- Английские служебные слова ---
    "the", "a", "an", "of", "in", "is", "are", "was", "were",
    # --- Казахские служебные слова (союзы, послелоги, частицы) ---
    "және", "жəне",  # и/также (оба варианта Unicode)
    "немесе",        # или
    "бойынша",       # по/согласно
    "туралы",        # о/про
    "бірақ",         # но
    "алайда",        # однако
    "сондай-ақ",     # а также
    "сонымен",       # итак/таким образом
    "сонымен қатар", # кроме того
    "яғни",          # то есть
    "мен",           # и/с (послелог)
    "үшін",          # для
    "арқылы",        # через/посредством
    "дейін",         # до
    "кейін",         # после
    "бұл",           # это/этот
    "сол",           # тот
    "осы",           # этот/данный
    "ол",            # он/она/оно
    "оның",          # его/её
    "оған",          # ему
    "олар",          # они
    "олардың",       # их
    "bar",           # есть/имеется
    "жоқ",           # нет
    "болып",         # являясь
    "болады",        # является
    "болса",         # если является
    "болған",        # бывший/являвшийся
    "керек",         # нужно/должен
    "тиіс",         # должен/обязан
    "деп",           # что/как (цитирование)
    "деген",         # называемый/сказанный
    "мынадай",       # следующий
    "қарай",         # к/в сторону
    "қарсы",         # против
    "ретінде",       # в качестве
    "тиісінше",     # соответственно
    "жағдайда",     # в случае
    "сәйкес",       # согласно/в соответствии
    "белгіленген",  # установленный
    "көзделген",    # предусмотренный
    "айқындалған",  # определённый
    "тәртіппен",    # в порядке
    "негізінде",    # на основании
    "орай",          # в связи с
    # --- Казахский юридический канцелярит ---
    "республикасы",  # республика (притяжательная форма)
    "қазақстан",     # казахстан
    "қолданысқа",   # в действие (о вступлении закона)
    "енгізіледі",   # вводится
    "жарияланған",  # опубликованный
    # --- Русский юридический канцелярит (казахстанский контекст) ---
    "настоящий", "вводится", "действие", "истечении", "официального",
    "опубликования", "республика", "казахстан",
    "внести", "изменения", "дополнения", "некоторые", "законодательные",
    "акты", "вопросам",
])

# Regex для числовых утверждений
_NUMBER_CLAIM_RE = re.compile(
    r"\b(\d[\d\s]{0,5}(?:[.,]\d{1,4})?)"
    r"\s*(%|млн\.?|млрд\.?|тыс\.?|тг\.?|kzt|мрп|мзп|лет|месяц\w*|дн\w*|тенге|процент\w*)\b",
    re.IGNORECASE,
)

_COMMON_LAW_NAME_MAP = {
    "гк": ["1000-XIII", "309-II"],
    "гкрк": ["1000-XIII", "309-II"],
    "гражданск": ["1000-XIII", "309-II"],
    "азаматтық кодекс": ["1000-XIII", "309-II"],
    "ак": ["1000-XIII", "309-II"],
    "акрк": ["1000-XIII", "309-II"],
    "зк": ["442-II"],
    "зкрк": ["442-II"],
    "земельн": ["442-II"],
    "жк": ["442-II"],
    "жкрк": ["442-II"],
    "жер кодекс": ["442-II"],
    "коап": ["235-V"],
    "коапрк": ["235-V"],
    "әқбтк": ["235-V"],
    "әқбткрк": ["235-V"],
    "әкімшілік құқық бұзушылық": ["235-V"],
    "ук": ["226-V"],
    "укрк": ["226-V"],
    "уголовн": ["226-V"],
    "ққ": ["226-V"],
    "ққрк": ["226-V"],
    "қылмыстық кодекс": ["226-V"],
    "аппк": ["350-VI"],
    "госимуществ": ["413-IV"],
    "государственному имуществу": ["413-IV"],
    "государственного имущества": ["413-IV"],
    # Новые расширения для покрытия косвенных отсылок к другим Кодексам РК
    "налогов": ["Налоговый кодекс"],
    " нк": ["Налоговый кодекс"],
    "бюджетн": ["Бюджетный кодекс"],
    " бк": ["Бюджетный кодекс"],
    "трудов": ["Трудовой кодекс"],
    " тк": ["Трудовой кодекс"],
    "экологическ": ["Экологический кодекс"],
    " эк": ["Экологический кодекс"],
    "предпринимательск": ["Предпринимательский кодекс"],
    " пк": ["Предпринимательский кодекс"],
    "электронном документе": ["370_"],
    "эцп": ["370_"],
    "информатизации": ["418-V"],
    "искусственном интеллекте": ["230-VIII"],
    "цифровой кодекс": ["255-VIII"],
    "цифрового кодекса": ["255-VIII"],
    "исполнительном производстве": ["261-IV"],
    "исполнительного производства": ["261-IV"],
    "судебных исполнителей": ["261-IV"],
    "сот орындаушы": ["261-IV"],
    "атқарушылық іс жүргізу": ["261-IV"],
    "233-IV": ["261-IV"],
}

_LAW_ID_SYNONYMS = {
    "1000-XIII": {"K940001000", "1000-XIII"},
    "K940001000": {"K940001000", "1000-XIII"},
    
    "309-II": {"K990000409", "309-II"},
    "K990000409": {"K990000409", "309-II"},
    
    "442-II": {"K030000442_", "K030000442", "442-II"},
    "K030000442_": {"K030000442_", "K030000442", "442-II"},
    "K030000442": {"K030000442_", "K030000442", "442-II"},
    
    "413-IV": {"Z1100000413", "413-IV"},
    "Z1100000413": {"Z1100000413", "413-IV"},
    
    "235-V": {"K1400000235", "235-V"},
    "K1400000235": {"K1400000235", "235-V"},
    
    "226-V": {"K1400000226", "226-V"},   # УК РК
    "K1400000226": {"K1400000226", "226-V"},
    
    "231-V": {"K1400000231", "231-V"},   # УПК РК
    "K1400000231": {"K1400000231", "231-V"},
    
    "350-VI": {"K2000000350", "350-VI"}, # АППК
    "K2000000350": {"K2000000350", "350-VI"},

    "261-IV": {"Z100000261_", "261-IV", "233-IV"},
    "233-IV": {"Z100000261_", "261-IV", "233-IV"},
}


def _are_law_ids_synonymous(law_a: str, law_b: str) -> bool:
    if not law_a or not law_b:
        return False
    a = law_a.strip().upper()
    b = law_b.strip().upper()
    if a == b:
        return True
    syns_a = _LAW_ID_SYNONYMS.get(a, {a})
    syns_b = _LAW_ID_SYNONYMS.get(b, {b})
    return not syns_a.isdisjoint(syns_b)


def _extract_referenced_law_ids(claim: DocumentClaim) -> list[str]:
    law_ids = []
    
    # 1. Search entities
    for ent in claim.entities:
        ent_clean = str(ent).strip().upper().replace(" ", "")
        ent_clean = re.sub(r"ЗРК|зрк", "", ent_clean).strip("-").strip()
        if re.match(r"^\d+-[-‐–A-Z]+$", ent_clean) or ent_clean in LAW_REGISTRY:
            law_ids.append(ent_clean)
            
    # 2. Search text and entities for common aliases
    text_lower = (claim.claim_text + " " + claim.quote).lower()
    for alias, resolved in _COMMON_LAW_NAME_MAP.items():
        clean_alias = alias.strip()
        if not clean_alias:
            continue
        # Используем границы слов для коротких аббревиатур (длиной <= 3 символа),
        # чтобы избежать ложных совпадений внутри слов (например, "ак" в "акт", "актісі", "жақсы").
        if len(clean_alias) <= 3:
            pattern = rf"\b{re.escape(clean_alias)}\b"
        else:
            pattern = re.escape(clean_alias)
            
        if re.search(pattern, text_lower, re.I | re.U):
            law_ids.extend(resolved)
            
    # 3. Regex match standard law formats in text (e.g. № 413-IV or 1000-XIII)
    matches = re.findall(r"\b\d+[-‐–][IVXivx\u0406\u0456]{1,5}\b", text_lower)
    for m in matches:
        clean_m = m.upper().replace(" ", "").replace("\u0406", "I").replace("\u0456", "i")
        law_ids.append(clean_m)
        
    return list(set(law_ids))


def _extract_article_from_claim(claim: DocumentClaim) -> str | None:
    """Извлекает номер статьи из утверждения с использованием regex."""
    text = (claim.claim_text + " " + claim.quote).lower()
    
    # 1. Русский вариант: слово статья/ст в разных падежах, затем число
    match_ru = re.search(r"\b(?:стать[яиюе]|ст\.?)\s*(\d+[\-\d]*)", text, re.IGNORECASE)
    if match_ru:
        return match_ru.group(1).strip()
        
    # 2. Казахский вариант: число, затем дефис и бап/бабы/бапта/баптың
    match_kk = re.search(r"\b(\d+[\-\d]*)-(?:бап|бабы|бабының|бапта|баптың|бабына)", text, re.IGNORECASE)
    if match_kk:
        return match_kk.group(1).strip()
        
    # 3. Fallback на просто бап/бабы, если число идет после
    match_kk_fallback = re.search(r"\b(?:бап|бабы|бабының|бапта|баптың|бабына)\s*(\d+[\-\d]*)", text, re.IGNORECASE)
    if match_kk_fallback:
        return match_kk_fallback.group(1).strip()
        
    return None


def _exact_metadata_search(
    claim: DocumentClaim,
    corpus_index: dict[str, EvidenceChunk],
) -> list[str]:
    """
    Выполняет прямой поиск в корпусе по метаданным (law_id и article).
    Используется как приоритетный точный Шаг 1 в верификации.
    """
    referenced_law_ids = _extract_referenced_law_ids(claim)
    article_num = _extract_article_from_claim(claim)
    
    if not referenced_law_ids or not article_num:
        return []
        
    candidates = []
    for cid, chunk in corpus_index.items():
        if any(_are_law_ids_synonymous(chunk.law_id, ref_id) for ref_id in referenced_law_ids) and chunk.article == article_num:
            candidates.append(cid)
            
    return candidates


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
    claims: ClaimExtractionResult | None = None,
) -> AnalysisJSON:
    """
    Этап 6: Детерминированный аудит. Строго без Retry.
    Ошибка при любой проверке → UNVERIFIED.
    """
    settings = get_settings()

    # Map claim_ids to DocumentClaim objects for metadata filtering
    claim_map = {}
    if claims and claims.claims:
        claim_map = {c.claim_id: c for c in claims.claims}
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

    # FIX 1: Resolve LLM-truncated source_ids to full corpus IDs via prefix_index
    # LLM gets chunk_ids truncated to 16 chars in the prompt (_format_chunk),
    # so it returns those truncated IDs. We must resolve them before any checks.
    for v in analysis.verdicts:
        resolved_ids = []
        for sid in v.source_ids:
            if sid in VIRTUAL_SOURCES or sid.startswith("reference_"):
                resolved_ids.append(sid)
                continue
            # Try exact match first, then prefix resolution
            full_id = None
            if sid in corpus_index:
                full_id = sid
            elif sid in prefix_index:
                full_id = prefix_index[sid]
                logger.debug(f"[S6/Resolve] Resolved truncated source_id '{sid}' → '{full_id}'")
            
            if full_id:
                # Apply Source Domain Filtering (excluding Wikipedia/non-KZ non-authoritative)
                chunk = corpus_index[full_id]
                if not chunk.law_id:
                    # Web source check
                    from zerde.models import WebTier
                    from urllib.parse import urlparse
                    is_official = False
                    url = (chunk.source_url or "").lower()
                    try:
                        parsed = urlparse(url)
                        domain = (parsed.hostname or "").removeprefix("www.")
                        if domain.endswith(".kz") and chunk.web_tier in (WebTier.TIER_1, WebTier.TIER_2):
                            is_official = True
                    except Exception:
                        pass
                    
                    if not is_official:
                        logger.info(f"[S6/SourceFilter] Filtering out non-authoritative/non-KZ source '{full_id[:12]}': {chunk.source_url}")
                        continue
                resolved_ids.append(full_id)
            else:
                resolved_ids.append(sid)  # keep as-is, might be invalid
        v.source_ids = resolved_ids

    # Downgrade "UNLINKED -> HIGH" loophole:
    # Any CONFIRMED LLM verdict with no real sources (i.e. only UNLINKED or empty source_ids, and not is_deterministic)
    # must be downgraded to UNVERIFIED and confidence to LOW.
    for v in analysis.verdicts:
        if v.status == VerdictStatus.CONFIRMED and not v.is_deterministic:
            real_sources = [
                sid for sid in v.source_ids
                if sid and sid not in VIRTUAL_SOURCES and not sid.startswith("reference_")
                and sid in corpus_index  # Only count IDs that actually exist in corpus
            ]
            if not real_sources:
                logger.warning(
                    f"[S6/Downgrade] Downgrading verdict for claim '{v.claim_id}' from CONFIRMED to UNVERIFIED "
                    f"due to lack of real source links. source_ids={v.source_ids}"
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
            claim = claim_map.get(fact.claim_id) if (fact.claim_id and fact.claim_id in claim_map) else None

            # --- METADATA-FIRST SEARCH (v9.4 Step 1 bypass) ---
            if claim:
                exact_candidates = _exact_metadata_search(claim, corpus_index)
                if exact_candidates:
                    fact.source_ids = exact_candidates
                    fact.bm25_score = 1.0
                    
                    # Run arithmetic check
                    arithmetic_ok = _arithmetic_check(fact, exact_candidates, corpus_index)
                    if not arithmetic_ok:
                        fact.validation_status = ValidationStatus.LOW
                    else:
                        fact.validation_status = ValidationStatus.HIGH
                        
                    scores.append(fact.bm25_score)
                    logger.info(
                        f"[S6/Metadata-First] Fact '{fact.fact_id}' (claim '{claim.claim_id}') verified exactly via metadata match: "
                        f"ids={exact_candidates} arithmetic_ok={arithmetic_ok}"
                    )
                    continue

            # UNLINKED или пустые источники — LLM не привязал к корпусу.
            # Fallback: BM25 corpus-wide search по тексту claim.
            if not fact.source_ids or "UNLINKED" in fact.source_ids:
                corpus_score = _corpus_wide_bm25_search(fact, bm25, corpus_index, claim)
                if corpus_score is not None and corpus_score >= settings.bm25_fallback_threshold:
                    fact.bm25_score = corpus_score
                    fact.validation_status = _score_to_status(
                        corpus_score,
                        settings.validation_threshold,
                        settings.bm25_medium_threshold,
                    )
                    scores.append(corpus_score)
                    logger.info(
                        f"[S6/Fallback] Fact '{fact.fact_id}' recovered via corpus BM25: "
                        f"score={corpus_score:.3f}"
                    )
                else:
                    fact.validation_status = ValidationStatus.UNVERIFIED
                    fact.bm25_score = corpus_score or 0.0
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
                claim,
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

    # Синхронизируем verdicts со статусами facts после аудита
    for fact in analysis.facts:
        if fact.claim_id and fact.claim_id in verdict_map:
            v = verdict_map[fact.claim_id]
            if v.status == VerdictStatus.CONTRADICTED:
                continue

            if fact.validation_status in (ValidationStatus.UNVERIFIED, ValidationStatus.LOW):
                if v.status == VerdictStatus.CONFIRMED:
                    logger.info(f"[S6/Sync] Downgrading verdict for '{v.claim_id}' because fact validation status is {fact.validation_status.value}")
                    v.status = VerdictStatus.UNVERIFIED
                    v.confidence = "LOW"
            elif fact.validation_status in (ValidationStatus.HIGH, ValidationStatus.MEDIUM):
                if v.status != VerdictStatus.CONFIRMED:
                    # Tiered BM25 threshold для апгрейда UNVERIFIED → CONFIRMED:
                    # HIGH validation (множественные совпадения) → порог 0.4
                    # MEDIUM validation → порог 0.6
                    # None/0 → блокируем
                    if v.status == VerdictStatus.UNVERIFIED:
                        bm25 = fact.bm25_score or 0.0
                        threshold = 0.4 if fact.validation_status == ValidationStatus.HIGH else 0.6
                        if bm25 < threshold:
                            logger.info(
                                f"[S6/Sync] Blocking upgrade of verdict '{v.claim_id}' from UNVERIFIED to CONFIRMED "
                                f"(bm25={bm25:.3f} < threshold={threshold})"
                            )
                            continue

                    logger.info(f"[S6/Sync] Upgrading verdict for '{v.claim_id}' to CONFIRMED because fact validation status is {fact.validation_status.value}")
                    v.status = VerdictStatus.CONFIRMED
                    v.confidence = "HIGH" if fact.validation_status == ValidationStatus.HIGH else "MEDIUM"
                    v.source_ids = fact.source_ids

    # Audit выводов
    _audit_conclusions(analysis, corpus_index, prefix_index)

    # V9.4: Calibrated Legal Confidence Metric & Statistics Aggregator
    if analysis.verdicts:
        # Считаем только аналитические вердикты (structural не участвуют)
        analytical_verdicts = [
            v for v in analysis.verdicts
            if not (v.claim_id and v.claim_id.startswith("structural_"))
        ]
        n_total = len(analytical_verdicts)

        n_confirmed = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.CONFIRMED)
        n_contradicted = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.CONTRADICTED)
        n_unverified = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.UNVERIFIED)

        n_contradicted_critical = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.CRITICAL
        )
        n_contradicted_high = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.HIGH
        )
        n_contradicted_medium = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.MEDIUM
        )
        n_contradicted_low = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.LOW
        )

        n_unverified_neutral = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.UNVERIFIED
            and v.severity not in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
        )
        n_unverified_risks = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.UNVERIFIED and v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
        )

        n_real_confirmed = sum(
            1 for v in analytical_verdicts
            if v.status == VerdictStatus.CONFIRMED
            and any(sid and sid != "UNLINKED" and not sid.startswith("reference_") for sid in v.source_ids)
        )

        if n_total > 0:
            # 1. Verification Coverage Score (V_ratio) weighted by severity
            severity_weights = {
                ClaimSeverity.CRITICAL: 3.0,
                ClaimSeverity.HIGH: 2.0,
                ClaimSeverity.MEDIUM: 1.0,
                ClaimSeverity.LOW: 0.5,
            }
            w_total = sum(severity_weights.get(v.severity, 1.0) for v in analytical_verdicts)
            
            w_confirmed = 0.0
            for v in analytical_verdicts:
                if v.status == VerdictStatus.CONFIRMED:
                    real_sources = [sid for sid in v.source_ids if sid and sid != "UNLINKED" and not sid.startswith("reference_")]
                    if not real_sources:
                        # Fallback for virtual reference-data verified claims
                        a_coef = 0.5
                    else:
                        best_rank = 11
                        for sid in real_sources:
                            if sid in corpus_index:
                                best_rank = min(best_rank, int(corpus_index[sid].legal_rank))
                        
                        if best_rank <= 3:
                            a_coef = 1.0
                        elif best_rank <= 6:
                            a_coef = 0.8
                        elif best_rank <= 9:
                            a_coef = 0.5
                        else:
                            a_coef = 0.2
                    w_confirmed += severity_weights.get(v.severity, 1.0) * a_coef

            v_ratio = w_confirmed / w_total if w_total > 0 else 0.0

            # 2. Authority Quality Score (Q_auth)
            # Ranks 1-3 = 1.0, Ranks 4-6 = 0.8, Ranks 7-9 = 0.5, Ranks 10-11 = 0.2
            auth_scores = []
            for v in analytical_verdicts:
                if v.status == VerdictStatus.CONFIRMED:
                    real_sources = [sid for sid in v.source_ids if sid and sid != "UNLINKED" and not sid.startswith("reference_")]
                    if not real_sources:
                        # Fallback for virtual reference-data verified claims
                        auth_scores.append(0.5)
                        continue
                    
                    # Find min (strongest) rank of its sources
                    best_rank = 11
                    for sid in real_sources:
                        if sid in corpus_index:
                            best_rank = min(best_rank, int(corpus_index[sid].legal_rank))
                    
                    if best_rank <= 3:
                        auth_scores.append(1.0)
                    elif best_rank <= 6:
                        auth_scores.append(0.8)
                    elif best_rank <= 9:
                        auth_scores.append(0.5)
                    else:
                        auth_scores.append(0.2)
            q_auth = float(np.mean(auth_scores)) if auth_scores else 0.0

            # 3. Retrieval Quality Score (Q_retrieval)
            ret_scores = []
            fact_by_claim = {f.claim_id: f for f in analysis.facts if f.claim_id}
            for v in analytical_verdicts:
                if v.status == VerdictStatus.CONFIRMED and v.claim_id in fact_by_claim:
                    f = fact_by_claim[v.claim_id]
                    if f.bm25_score is not None:
                        ret_scores.append(f.bm25_score)
            q_retrieval = float(np.mean(ret_scores)) if ret_scores else 0.5
            q_retrieval = max(0.0, min(1.0, q_retrieval))

            # 4. Contradiction Penalty (P_conflict)
            # v9.5: Смягчённые веса. Нахождение противоречий показывает что анализ РАБОТАЕТ,
            # а не что он ненадёжный. Reliability = надёжность анализа, не качество документа.
            p_conflict = (
                0.08 * n_contradicted_critical +
                0.05 * n_contradicted_high +
                0.03 * n_contradicted_medium +
                0.01 * n_contradicted_low
            )
            p_conflict = max(0.0, min(0.85, p_conflict))

            # Calculate Reliability (R)
            # R = (0.50 * V_ratio + 0.30 * Q_auth + 0.20 * Q_retrieval) * (1.0 - P_conflict)
            base_score = 0.50 * v_ratio + 0.30 * q_auth + 0.20 * q_retrieval
            reliability = base_score * (1.0 - p_conflict)
            
            # Если есть реально подтвержденные факты, ограничиваем надежность снизу 5%
            # (чтобы избежать 0% при наличии подтвержденного и противоречивого контента одновременно)
            if n_real_confirmed > 0:
                reliability = max(0.05, min(1.0, reliability))
            else:
                reliability = max(0.0, min(1.0, reliability))

            # Epistemic Honesty Fallback
            if n_real_confirmed == 0:
                reliability = None
        else:
            reliability = None

        analysis.overall_reliability = reliability

        # Compile and attach AnalysisStats (v9.4 Immutable Stage)
        from zerde.models import AnalysisStats
        
        # FIX 3 + FIX 5: Conditional pros — don't claim "Confirmed 0" as a positive.
        # FIX 5: Don't generate false positive "no contradictions" when corpus is empty.
        # corpus_index может быть пустым (0 локальных чанков) — в этом случае
        # "противоречий не обнаружено" — ложный позитив, т.к. мы ничего не проверяли.
        has_real_evidence = len(corpus_index) > 0
        has_local_evidence = any(
            c.adilet_fallback_used is not None or c.law_id
            for c in corpus_index.values()
        )
        pros_list = []
        if n_confirmed > 0:
            pros_list.append(f"Подтверждено {n_confirmed} из {n_total} анализируемых утверждений законопроекта.")
        if n_contradicted == 0 and has_real_evidence and has_local_evidence and v_ratio >= 0.5:
            # Только если >50% фактов проверено и есть локальные источники
            pros_list.append("Противоречий с действующим законодательством РК не обнаружено.")
        if n_confirmed > 0 and n_contradicted == 0 and has_local_evidence and v_ratio >= 0.5:
            pros_list.append("Все проверенные утверждения соответствуют нормативно-правовой базе.")
        if not pros_list:
            # Fallback: nothing confirmed, but also nothing contradicted — neutral
            if n_unverified == n_total and not has_real_evidence:
                pros_list.append("Документ не содержит явных фактических ошибок (корпус источников недостаточен для полной верификации).")
            elif n_unverified == n_total:
                pros_list.append("Документ не содержит фактических ошибок в проверяемой части (недостаточно источников для полной верификации).")
            else:
                pros_list.append(f"Проанализировано {n_total} утверждений законопроекта.")
        
        # Наполняем cons список в AnalysisJSON для рендеринга
        analysis.cons = []
        if n_contradicted > 0:
            analysis.cons.append(f"Выявлено {n_contradicted} противоречий с действующим законодательством Республики Казахстан.")
        if n_unverified_risks > 0:
            analysis.cons.append(f"Не удалось верифицировать {n_unverified_risks} критических/высоких утверждений из-за отсутствия необходимых источников в собранном корпусе.")
        if analysis.negative_space:
            analysis.cons.append(f"Выявлено {len(analysis.negative_space)} регуляторных пробелов или коллизий в законопроекте.")
        if not analysis.cons:
            analysis.cons.append("Критических коллизий или неустраненных противоречий не обнаружено.")

        confirmed_list = [v for v in analytical_verdicts if v.status == VerdictStatus.CONFIRMED]
        contradictions_list = [v for v in analytical_verdicts if v.status == VerdictStatus.CONTRADICTED]
        unverified_list = [v for v in analytical_verdicts if v.status == VerdictStatus.UNVERIFIED]
        
        rec_str = (
            f"Юридический аудит завершен. Из {n_total} выдвинутых утверждений: "
            f"{len(confirmed_list)} подтверждено действующим законодательством Республики Казахстан, "
            f"{len(contradictions_list)} опровергнуто, {len(unverified_list)} не верифицировано (отсутствуют в предоставленном корпусе)."
        )
        if contradictions_list:
            rec_str += f" КРИТИЧЕСКИ: {len(contradictions_list)} ошибок."

        analysis.stats = AnalysisStats(
            n_total=n_total,
            n_confirmed=n_confirmed,
            n_contradicted=n_contradicted,
            n_unverified=n_unverified,
            n_critical_contradicted=n_contradicted_critical,
            n_high_contradicted=n_contradicted_high,
            n_unverified_risks=n_unverified_risks,
            n_real_confirmed=n_real_confirmed,
            reliability=reliability,
            pros=pros_list,
            recommendation=rec_str,
        )

        rel_str = f"{reliability:.3f}" if reliability is not None else "None"
        logger.info(
            f"[S6/Score/v9.4] n_total={n_total} v_ratio={v_ratio:.3f} q_auth={q_auth:.3f} "
            f"q_retrieval={q_retrieval:.3f} p_conflict={p_conflict:.3f} → reliability={rel_str}"
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

    # FIX 4: Use raw claim text from DocumentClaim for BM25, not the formatted
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
        ]
        if candidate_indices:
            best_idx = max(candidate_indices, key=lambda i: raw_scores[i])
            best_raw = float(raw_scores[best_idx])
            best_cid = bm25._ids[best_idx]
            normalized = min(1.0, best_raw / bm25._max_score)
            normalized = max(0.0, normalized)
            
            if normalized >= settings.bm25_medium_threshold:
                fact.source_ids = [best_cid]
                logger.info(f"[S6/Fallback/Layer1] Verified claim '{fact.claim_id}' inside law {referenced_law_ids}: score={normalized:.3f}")
                return normalized
            else:
                logger.info(f"[S6/Fallback/Layer1] Match inside specific law was too weak ({normalized:.3f} < {settings.bm25_medium_threshold}). Rejecting.")
                return None
        else:
            # Специфичный закон был запрошен, но не найден в корпусе.
            # Блокируем глобальный поиск по другим законам во избежание галлюцинаций!
            logger.info(f"[S6/Fallback] Expected law {referenced_law_ids} is not present in the corpus. Blocking global search.")
            return None

    # --- LAYER 2: Code Family Fallback (if specific law was not found/loaded) ---
    # (If referenced_law_ids is defined but not loaded in the corpus, we try parent Codes)
    if referenced_law_ids:
        parent_codes = []
        text_lower = (claim.claim_text + " " + claim.quote).lower() if claim else fact.claim.lower()
        if "гражданск" in text_lower or " гк" in text_lower:
            parent_codes.extend(["1000-XIII", "309-II"])
        if "земельн" in text_lower or " зк" in text_lower:
            parent_codes.append("442-II")
        if "коап" in text_lower:
            parent_codes.append("235-V")
            
        parent_codes = list(set(parent_codes))
        if parent_codes:
            candidate_indices = [
                i for i, cid in enumerate(bm25._ids)
                if corpus_index[cid].law_id in parent_codes
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
            if not re.search(pattern, chunk_text):
                logger.info(f"[S6/Fallback/Layer3] Rejected Layer 3 match '{best_cid[:12]}' because it does not contain article '{article_num}'")
                return None

        fact.source_ids = [best_cid]
        logger.info(f"[S6/Fallback/Layer3] Verified claim '{fact.claim_id}' via strict corpus-wide search: score={normalized:.3f}")
        return normalized

    logger.info(f"[S6/Fallback/Layer3] Best corpus-wide match was too weak ({normalized:.3f} < {settings.bm25_fallback_threshold}). Rejecting.")
    return None


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
    claim: DocumentClaim | None = None,
) -> AuditResult:
    """
    Аудит одного факта. Без catch — исключения всплывают наверх.
    """
    # Резолвим prefix IDs → полные chunk_ids
    resolved_ids = _resolve_source_ids(fact.source_ids, corpus_index, prefix_index)

    # 1. Topology
    if not _check_topology(fact, resolved_ids, corpus_index, claim):
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
    claim: DocumentClaim | None = None,
) -> bool:
    """True если хотя бы один source_id (кроме виртуальных) существует в корпусе и соответствует law_id."""
    virtual = {"UNLINKED", "reference_data"}
    valid_ids = [sid for sid in resolved_ids if sid not in virtual and not sid.startswith("reference_")]
    if not valid_ids:
        return False

    # Extract target law IDs from claim if available
    referenced_law_ids = []
    if claim:
        referenced_law_ids = _extract_referenced_law_ids(claim)

    # Strict Cross-Domain Check: Предотвращает ложное подтверждение статей разных доменов
    text_lower = (fact.claim + " " + (claim.claim_text if claim else "")).lower()
    is_koap_claim = "коап" in text_lower or "әқбтк" in text_lower or "административ" in text_lower
    is_gk_claim = "гражданск" in text_lower or " гк" in text_lower or "азаматтық" in text_lower or "акрк" in text_lower

    found = []
    for sid in valid_ids:
        if sid in corpus_index:
            chunk = corpus_index[sid]
            chunk_law = (chunk.law_id or "").upper()
            
            # Если claim про КоАП, а чанк из Гражданского кодекса -> отсекаем (C1 Fix)
            if is_koap_claim and any(_are_law_ids_synonymous(chunk_law, gk_id) for gk_id in ["1000-XIII", "309-II", "K940001000"]):
                logger.warning(f"[S6/Cross-Domain] Rejected Civil Code chunk '{sid[:12]}' for KoAP claim '{fact.fact_id}'")
                continue
            # Если claim про ГК, а чанк из КоАП -> отсекаем
            if is_gk_claim and _are_law_ids_synonymous(chunk_law, "235-V"):
                logger.warning(f"[S6/Cross-Domain] Rejected KoAP chunk '{sid[:12]}' for Civil Code claim '{fact.fact_id}'")
                continue

            # Enforce law_id matching if claim explicitly references specific laws
            if referenced_law_ids and chunk.law_id:
                if not any(_are_law_ids_synonymous(chunk.law_id, ref_id) for ref_id in referenced_law_ids):
                    logger.warning(
                        f"[S6/Topology/Mismatch] Fact '{fact.fact_id}' (claim '{claim.claim_id}') references laws {referenced_law_ids}, "
                        f"but source chunk '{sid[:12]}' is from law '{chunk.law_id}'. Rejecting source."
                    )
                    continue
            found.append(sid)

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

        conflicts.append(
            ConflictRecord(
                record_id=f"conflict_{len(conflicts):04d}",
                conflict_type=ctype,
                claim_id=v.claim_id,
                claim_text=v.contradiction_detail or "Противоречие найдено",
                document_value=v.document_value,
                found_value=v.found_value,
                detail=v.contradiction_detail or "",
                severity=v.severity,
            )
        )

    return conflicts
