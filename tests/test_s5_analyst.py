"""
test_s5_analyst.py
Тесты для _remap_source_ids и _validate_claim_coverage (S5 LLM Auditor, offline).
"""
from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    DocumentClaim,
    Fact,
    VerdictStatus,
)
from zerde.stages.s5_analyst import _remap_source_ids, _validate_claim_coverage


def test_remap_translates_labels_drops_hallucinated_keeps_sentinels():
    id_map = {"S1": "chunk_aaa", "S2": "chunk_bbb"}
    a = AnalysisJSON(
        analysis_id="t", source_doc_id="d", plan_id="p",
        verdicts=[ClaimVerdict(claim_id="c1", status=VerdictStatus.CONFIRMED,
                                source_ids=["S1", "S99", "reference_data", "UNLINKED", "S2"])],
        facts=[Fact(fact_id="f1", claim_id="c1", claim="x", source_ids=["S2", "S77"])],
    )
    _remap_source_ids(a, id_map)
    assert a.verdicts[0].source_ids == ["chunk_aaa", "reference_data", "UNLINKED", "chunk_bbb"]
    assert a.facts[0].source_ids == ["chunk_bbb"]  # S77 hallucinated → dropped


def test_remap_empty_and_none_safe():
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     verdicts=[ClaimVerdict(claim_id="c1", status=VerdictStatus.UNVERIFIED, source_ids=[])])
    _remap_source_ids(a, {})
    assert a.verdicts[0].source_ids == []


def test_missing_claims_backfilled_as_unverified():
    claims = ClaimExtractionResult(doc_id="d", claims=[
        DocumentClaim(claim_id="claim_0001", claim_text="A", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL),
        DocumentClaim(claim_id="claim_0002", claim_text="B", claim_type=ClaimType.FINANCIAL, severity=ClaimSeverity.HIGH),
    ])
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     verdicts=[ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.CONFIRMED)])
    _validate_claim_coverage(a, claims)
    by_id = {v.claim_id: v for v in a.verdicts}
    assert by_id["claim_0002"].status == VerdictStatus.UNVERIFIED
    assert by_id["claim_0002"].source_ids == []
    assert by_id["claim_0002"].confidence == "LOW"
    assert by_id["claim_0002"].severity == ClaimSeverity.HIGH   # severity carried
    assert by_id["claim_0001"].status == VerdictStatus.CONFIRMED  # existing untouched
