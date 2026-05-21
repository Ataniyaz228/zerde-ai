"""
Tests for Zerde v7.0 features:
- Structural claim filter
- Deterministic claim deduplication
- Conflicts bridge from verdicts
- Validation status override
"""
import pytest
from zerde.models import (
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    ConflictRecord,
    ConflictType,
    DocumentClaim,
    Fact,
    ValidationStatus,
    VerdictStatus,
)
from zerde.stages.s2_5_claim_extractor import _dedup_claims, _is_structural_claim
from zerde.stages.s6_auditor import _build_conflicts_from_verdicts
from zerde.stages.s7_render import _fact_icon


class TestStructuralFilter:
    def test_structural_legal_ref_low(self):
        c = DocumentClaim(
            claim_id="c1",
            claim_text="Документ утверждает: article_ref=5 (строка: ...)",
            claim_type=ClaimType.LEGAL_REF,
            severity=ClaimSeverity.LOW,
        )
        assert _is_structural_claim(c) is True

    def test_not_structural_due_to_modal_verb(self):
        c = DocumentClaim(
            claim_id="c2",
            claim_text="Оператор обязан уведомить субъекта в течение 24 часов",
            claim_type=ClaimType.TEMPORAL,
            severity=ClaimSeverity.HIGH,
        )
        assert _is_structural_claim(c) is False

    def test_not_structural_high_severity(self):
        c = DocumentClaim(
            claim_id="c3",
            claim_text="Документ утверждает: fine_mrp=500",
            claim_type=ClaimType.FINANCIAL,
            severity=ClaimSeverity.CRITICAL,
        )
        assert _is_structural_claim(c) is False

    def test_structural_phrase(self):
        c = DocumentClaim(
            claim_id="c4",
            claim_text="Статья 10 присутствует в структуре документа",
            claim_type=ClaimType.FACTUAL,
            severity=ClaimSeverity.LOW,
        )
        assert _is_structural_claim(c) is True


class TestClaimDedup:
    def test_dedup_identical_claims(self):
        c1 = DocumentClaim(
            claim_id="c1", claim_text="Закон № 94-V", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL, entities=["94-V"]
        )
        c2 = DocumentClaim(
            claim_id="c2", claim_text="закон 94-V", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL, entities=["94-V"]
        )
        result = _dedup_claims([c1, c2])
        assert len(result) == 1
        # quote_variants собраны (quotes пустые по умолчанию, поэтому 0)
        assert len(result[0].quote_variants) == 0

    def test_dedup_keeps_best(self):
        c1 = DocumentClaim(
            claim_id="c1", claim_text="87-IV", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL,
            deterministic_verdict="INVALID → 94-V",
        )
        c2 = DocumentClaim(
            claim_id="c2", claim_text="87-iv", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL,
        )
        result = _dedup_claims([c1, c2])
        assert len(result) == 1
        assert result[0].deterministic_verdict == "INVALID → 94-V"

    def test_no_false_dedup(self):
        c1 = DocumentClaim(claim_id="c1", claim_text="штраф 500 МРП", claim_type=ClaimType.FINANCIAL, severity=ClaimSeverity.CRITICAL)
        c2 = DocumentClaim(claim_id="c2", claim_text="штраф 1500 МРП", claim_type=ClaimType.FINANCIAL, severity=ClaimSeverity.CRITICAL)
        result = _dedup_claims([c1, c2])
        assert len(result) == 2


class TestConflictsBridge:
    def test_hierarchy_conflict(self):
        v = ClaimVerdict(
            claim_id="c1",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="Штраф превышает лимит КоАП (2000 МРП)",
            confidence="HIGH",
        )
        result = _build_conflicts_from_verdicts([v])
        assert len(result) == 1
        assert result[0].conflict_type == ConflictType.HIERARCHY
        assert result[0].severity == ClaimSeverity.HIGH

    def test_temporal_conflict(self):
        v = ClaimVerdict(
            claim_id="c2",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="Срок 24 часа не соответствует 10 рабочим дням",
            confidence="MEDIUM",
        )
        result = _build_conflicts_from_verdicts([v])
        assert result[0].conflict_type == ConflictType.TEMPORAL

    def test_factual_conflict(self):
        v = ClaimVerdict(
            claim_id="c3",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="МРП 3450 вместо 3932",
            confidence="HIGH",
        )
        result = _build_conflicts_from_verdicts([v])
        assert result[0].conflict_type == ConflictType.FACTUAL

    def test_skips_confirmed(self):
        v = ClaimVerdict(claim_id="c4", status=VerdictStatus.CONFIRMED, confidence="HIGH")
        result = _build_conflicts_from_verdicts([v])
        assert len(result) == 0

    def test_dedup_by_claim_id(self):
        v1 = ClaimVerdict(claim_id="c5", status=VerdictStatus.CONTRADICTED, contradiction_detail="err1", confidence="HIGH")
        v2 = ClaimVerdict(claim_id="c5", status=VerdictStatus.CONTRADICTED, contradiction_detail="err2", confidence="HIGH")
        result = _build_conflicts_from_verdicts([v1, v2])
        assert len(result) == 1


class TestFactIconOverride:
    def test_contradicted_is_red(self):
        f = Fact(fact_id="f1", claim_id="c1", claim="test", source_ids=["s1"], validation_status=ValidationStatus.HIGH)
        v_map = {"c1": ClaimVerdict(claim_id="c1", status=VerdictStatus.CONTRADICTED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🔴"

    def test_confirmed_is_green(self):
        f = Fact(fact_id="f2", claim_id="c2", claim="test", source_ids=["s1"], validation_status=ValidationStatus.LOW)
        v_map = {"c2": ClaimVerdict(claim_id="c2", status=VerdictStatus.CONFIRMED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🟢"

    def test_unverified_risk_is_orange(self):
        f = Fact(fact_id="f3", claim_id="c3", claim="test", source_ids=["s1"], validation_status=ValidationStatus.UNVERIFIED)
        v_map = {"c3": ClaimVerdict(claim_id="c3", status=VerdictStatus.UNVERIFIED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🟠"

    def test_unverified_neutral_is_grey(self):
        f = Fact(fact_id="f4", claim_id="c4", claim="test", source_ids=["s1"], validation_status=ValidationStatus.UNVERIFIED)
        v_map = {"c4": ClaimVerdict(claim_id="c4", status=VerdictStatus.UNVERIFIED, confidence="LOW")}
        assert _fact_icon(f, v_map) == "⚫"
