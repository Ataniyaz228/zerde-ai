"""
Conclusions audit + CONTRADICTED-verdicts -> ConflictRecord bridge.

Moved verbatim from zerde/stages/s6_auditor.py (Phase 1, Step 3).
"""

from __future__ import annotations

from zerde.models import (
    AnalysisJSON,
    ClaimVerdict,
    ConflictRecord,
    ConflictType,
    EvidenceChunk,
    ValidationStatus,
    VerdictStatus,
)
from zerde.stages.s6_audit.source_filter import _resolve_source_ids

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
        resolved = _resolve_source_ids(conclusion.source_ids, corpus_index)
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
