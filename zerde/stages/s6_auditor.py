"""Совместимостный фасад над пакетом zerde/stages/s6_audit/.

Сам аудит (topology-check, BM25-скоринг, арифметика, расчёт надёжности) разнесён
по модулям s6_audit/. Здесь только реэкспорт публичных символов, чтобы старые
импорты (pipeline, s5_analyst, scripts/eval, tests) продолжали работать.
Новый код импортируй прямо из s6_audit.*.
"""

from __future__ import annotations

from zerde.stages.s6_audit.audit import _exact_metadata_search, audit_analysis
from zerde.stages.s6_audit.checks import (
    AuditResult,
    _audit_fact,
    _confidence_to_status,
    _score_to_status,
)
from zerde.stages.s6_audit.conflicts import _audit_conclusions, _build_conflicts_from_verdicts
from zerde.stages.s6_audit.reliability import _compute_reliability_and_stats
from zerde.stages.s6_audit.retrieval import ZerdeBM25, _build_bm25_index, _corpus_wide_bm25_search
from zerde.stages.s6_audit.source_filter import _check_topology, _resolve_source_ids
from zerde.stages.s6_audit.verdict_sync import _sync_verdicts_with_facts
from zerde.utils.claims import _COMMON_LAW_NAME_MAP
from zerde.utils.claims import are_law_ids_synonymous as _are_law_ids_synonymous
from zerde.utils.claims import extract_article_from_claim as _extract_article_from_claim
from zerde.utils.claims import extract_referenced_law_ids as _extract_referenced_law_ids
from zerde.utils.textproc import tokenize_simple as _tokenize

__all__ = [
    "audit_analysis",
    "_extract_article_from_claim",
    "_extract_referenced_law_ids",
    "_are_law_ids_synonymous",
    "_exact_metadata_search",
    "_COMMON_LAW_NAME_MAP",
    "_build_conflicts_from_verdicts",
    "ZerdeBM25",
    "_corpus_wide_bm25_search",
    "_tokenize",
    "_check_topology",
    # Additional symbols re-exported for internal/back-compat use:
    "AuditResult",
    "_audit_fact",
    "_audit_conclusions",
    "_build_bm25_index",
    "_confidence_to_status",
    "_score_to_status",
    "_resolve_source_ids",
    "_compute_reliability_and_stats",
    "_sync_verdicts_with_facts",
]
