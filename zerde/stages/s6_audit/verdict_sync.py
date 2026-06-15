"""
Post-fact-audit verdict synchronization: CONTRADICTED override + tiered
UNVERIFIED<->CONFIRMED sync based on fact validation_status/bm25_score.
"""

from __future__ import annotations

import logging

from zerde.models import AnalysisJSON, ValidationStatus, VerdictStatus
from zerde.stages.s6_audit.scoring_config import CONFIG

logger = logging.getLogger(__name__)


def _sync_verdicts_with_facts(analysis: AnalysisJSON) -> None:
    """
    Override validation_status для CONTRADICTED verdicts.
    Если вердикт CONTRADICTED — статус всегда LOW (красный), независимо от BM25.
    UNVERIFIED вердикты НЕ override'ятся в LOW — они остаются UNVERIFIED.

    Затем синхронизирует verdicts со статусами facts после аудита.
    """
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
                        threshold = (
                            CONFIG.tiered_upgrade_high_threshold
                            if fact.validation_status == ValidationStatus.HIGH
                            else CONFIG.tiered_upgrade_medium_threshold
                        )
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
