"""
test_s5_analyst.py
Тесты для _remap_source_ids и _validate_claim_coverage (S5 LLM Auditor, offline).
"""
import pytest

from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    DocumentClaim,
    Fact,
    QueryPlan,
    VerdictStatus,
)
from zerde.stages.s5_analyst import _remap_source_ids, _validate_claim_coverage, run_auditor
from zerde.utils.llm_client import get_manifest, reset_manifest


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


# ---------------------------------------------------------------------------
# run_auditor: fail-closed backfill on partial/total batch failure (Phase 2a,
# Change 3). 10 claims / batch_size=5 → 2 batches. offline ($0): no real LLM,
# chunks=[] so retrieval/build_auditor_prompt stay trivial.
# ---------------------------------------------------------------------------

def _make_claims(n: int) -> ClaimExtractionResult:
    return ClaimExtractionResult(doc_id="doc1", claims=[
        DocumentClaim(
            claim_id=f"claim_{i:04d}",
            claim_text=f"claim text {i}",
            claim_type=ClaimType.LEGAL_REF,
            severity=ClaimSeverity.HIGH,
        )
        for i in range(1, n + 1)
    ])


def _plan() -> QueryPlan:
    return QueryPlan(plan_id="plan_1", source_doc_id="doc1")


@pytest.fixture(autouse=True)
def _reset_manifest_s5():
    reset_manifest()
    yield
    reset_manifest()


async def test_one_failed_batch_backfills_unverified_and_counts(monkeypatch):
    """Batch 1 (claim_0001..0005) succeeds with CONFIRMED verdicts; batch 2
    (claim_0006..0010) raises RuntimeError. run_auditor must NOT raise, must
    return verdicts for all 10 claims, the 5 from the failed batch must be
    UNVERIFIED with source_ids==[], and the manifest must record the
    batch_failures==1 entry."""
    claims = _make_claims(10)
    plan = _plan()

    monkeypatch.setattr("zerde.stages.s5_analyst.make_llm_client", lambda settings: object())

    async def _fake_cached_llm_call(*, client, model, messages, **kwargs):
        prompt_text = "\n".join(m.get("content", "") for m in messages)
        if "claim_0001" in prompt_text:
            # Batch 1: 5 CONFIRMED verdicts.
            return {
                "verdicts": [
                    {"claim_id": f"claim_{i:04d}", "status": "CONFIRMED",
                     "found_value": "ok", "source_ids": [], "confidence": "HIGH"}
                    for i in range(1, 6)
                ]
            }
        # Batch 2: simulate exhausted-retries failure.
        raise RuntimeError("transport exhausted")

    monkeypatch.setattr("zerde.stages.s5_analyst.cached_llm_call", _fake_cached_llm_call)

    analysis = await run_auditor(chunks=[], plan=plan, claims=claims, doc_text="Текст документа.")

    assert len(analysis.verdicts) == 10
    by_id = {v.claim_id: v for v in analysis.verdicts}

    for i in range(1, 6):
        v = by_id[f"claim_{i:04d}"]
        assert v.status == VerdictStatus.CONFIRMED

    for i in range(6, 11):
        v = by_id[f"claim_{i:04d}"]
        assert v.status == VerdictStatus.UNVERIFIED
        assert v.source_ids == []

    manifest = get_manifest()
    batch_records = [r for r in manifest if r.batch_failures]
    assert len(batch_records) == 1
    assert batch_records[0].batch_failures == 1


async def test_all_batches_succeed_is_neutral(monkeypatch):
    """Both batches return valid verdicts → no batch_failures manifest entry,
    all 10 verdicts come from the (fake) LLM responses (none backfilled)."""
    claims = _make_claims(10)
    plan = _plan()

    monkeypatch.setattr("zerde.stages.s5_analyst.make_llm_client", lambda settings: object())

    async def _fake_cached_llm_call(*, client, model, messages, **kwargs):
        prompt_text = "\n".join(m.get("content", "") for m in messages)
        if "claim_0001" in prompt_text:
            ids = range(1, 6)
        else:
            ids = range(6, 11)
        return {
            "verdicts": [
                {"claim_id": f"claim_{i:04d}", "status": "CONFIRMED",
                 "found_value": "ok", "source_ids": [], "confidence": "HIGH"}
                for i in ids
            ]
        }

    monkeypatch.setattr("zerde.stages.s5_analyst.cached_llm_call", _fake_cached_llm_call)

    analysis = await run_auditor(chunks=[], plan=plan, claims=claims, doc_text="Текст документа.")

    assert len(analysis.verdicts) == 10
    assert all(v.status == VerdictStatus.CONFIRMED for v in analysis.verdicts)

    manifest = get_manifest()
    batch_records = [r for r in manifest if r.batch_failures]
    assert batch_records == []
