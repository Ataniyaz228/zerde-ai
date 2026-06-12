"""
Source-id resolution, topology/cross-domain checks, source-domain filtering
and the "UNLINKED -> CONFIRMED" downgrade loophole closure.

Moved verbatim from zerde/stages/s6_auditor.py (Phase 1, Step 3).
"""

from __future__ import annotations

import logging
import re

from zerde.models import (
    AnalysisJSON,
    DocumentClaim,
    EvidenceChunk,
    Fact,
    ValidationStatus,
    VerdictStatus,
    WebTier,
)
from zerde.utils.claims import are_law_ids_synonymous as _are_law_ids_synonymous
from zerde.utils.claims import extract_referenced_law_ids as _extract_referenced_law_ids

logger = logging.getLogger(__name__)

# Виртуальные source_ids (не указывают на конкретный chunk).
VIRTUAL_SOURCES = {"UNLINKED", "reference_data"}


# ---------------------------------------------------------------------------
# Topology Check
# ---------------------------------------------------------------------------


def _resolve_source_ids(
    source_ids: list[str],
    corpus_index: dict[str, EvidenceChunk],
) -> list[str]:
    """
    Нормализует source_ids фактов/выводов.

    source_ids уже переведены в полные chunk_id в S5 (_remap_source_ids, который
    также ОТБРАСЫВАЕТ галлюцинированные метки), так что здесь это либо точное
    совпадение по corpus_index, либо виртуальный источник. Неизвестные id
    оставляем как есть — они не пройдут проверку существования/топологии ниже и
    дадут UNVERIFIED. Прежний Левенштейн-fuzzy (prefix-recovery) УДАЛЁН: для
    коротких id с дистанцией ≤3 он матчил почти любой chunk_id → источник
    ложного grounding'а (CITE-OR-ABSTAIN). Восстанавливать его нельзя.
    """
    virtual = {"UNLINKED", "reference_data"}
    resolved = []
    for sid in source_ids:
        if sid in virtual or sid.startswith("reference_"):
            resolved.append(sid)
        else:
            resolved.append(sid)  # точный full chunk_id или неизвестный — без fuzzy
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
    # V9.6: Точная проверка КоАП — только по аббревиатуре или точной фразе,
    # не по слову "административ" (слишком широко: есть в АППК, КоАП, приказах МВД и т.д.)
    is_koap_claim = (
        "коап" in text_lower
        or "әқбтк" in text_lower
        or bool(re.search(r"административн\w+\s+правонаруш", text_lower))
    )
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
# Source-domain filtering + UNLINKED-CONFIRMED downgrade loophole
# ---------------------------------------------------------------------------


def _apply_source_domain_filter(analysis: AnalysisJSON, corpus_index: dict[str, EvidenceChunk]) -> None:
    """
    source_ids уже переведены в полные chunk_id в S5 (_remap_source_ids:
    короткие метки S1.. → chunk_id), поэтому здесь достаточно точного совпадения
    по corpus_index — обрезанных hex-ID и prefix-recovery больше нет.

    Дополнительно: фильтрует web-источники без law_id, которые не являются
    официальными (TIER_1/TIER_2).
    """
    for v in analysis.verdicts:
        resolved_ids = []
        for sid in v.source_ids:
            if sid in VIRTUAL_SOURCES or sid.startswith("reference_"):
                resolved_ids.append(sid)
                continue
            full_id = sid if sid in corpus_index else None

            if full_id:
                # Apply Source Domain Filtering (excluding Wikipedia/non-KZ non-authoritative)
                chunk = corpus_index[full_id]
                if not chunk.law_id:
                    # Web source check
                    is_official = False
                    try:
                        if chunk.web_tier in (WebTier.TIER_1, WebTier.TIER_2):
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


def _downgrade_unsupported_confirmed(analysis: AnalysisJSON, corpus_index: dict[str, EvidenceChunk]) -> None:
    """
    Downgrade "UNLINKED -> HIGH" loophole:
    Any CONFIRMED LLM verdict with no real sources (i.e. only UNLINKED or empty
    source_ids, and not is_deterministic) must be downgraded to UNVERIFIED and
    confidence to LOW.
    """
    for v in analysis.verdicts:
        if v.status == VerdictStatus.CONFIRMED and not v.is_deterministic:
            has_reference = any(
                sid and (sid in VIRTUAL_SOURCES or sid.startswith("reference_"))
                for sid in v.source_ids
            )
            real_sources = [
                sid for sid in v.source_ids
                if sid and sid not in VIRTUAL_SOURCES and not sid.startswith("reference_")
                and sid in corpus_index  # Only count IDs that actually exist in corpus
            ]
            if not real_sources and not has_reference:
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
