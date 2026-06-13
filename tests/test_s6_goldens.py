"""
Golden snapshot tests for zerde.stages.s6_auditor.audit_analysis.

These tests freeze the CURRENT, real output of audit_analysis() on a set of
hand-built, corpus-independent fixtures. They exist so Phase 1's refactor
(extracting shared utils + decomposing s6_auditor into a package) can be
proven BEHAVIOR-NEUTRAL: re-run this file before and after each step and the
goldens must compare byte-identical.

Run with --update-goldens to regenerate tests/goldens/s6_<fixture>.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    DocumentClaim,
    EvidenceChunk,
    Fact,
    LegalRank,
    VerdictStatus,
    WebTier,
)
from zerde.stages.s6_auditor import audit_analysis

GOLDENS_DIR = Path(__file__).parent / "goldens"


# ---------------------------------------------------------------------------
# Extraction helper — pulls only the fields the golden schema cares about
# ---------------------------------------------------------------------------


def _extract(analysis: AnalysisJSON) -> dict:
    return {
        "overall_reliability": analysis.overall_reliability,
        "verdicts": [
            {
                "claim_id": v.claim_id,
                "status": v.status.value,
                "confidence": v.confidence,
                "source_ids": v.source_ids,
            }
            for v in analysis.verdicts
        ],
        "facts": [
            {
                "fact_id": f.fact_id,
                "claim_id": f.claim_id,
                "validation_status": f.validation_status.value,
                "bm25_score": f.bm25_score,
                "source_ids": f.source_ids,
            }
            for f in analysis.facts
        ],
        "stats": {
            "n_total": analysis.stats.n_total,
            "n_confirmed": analysis.stats.n_confirmed,
            "n_contradicted": analysis.stats.n_contradicted,
            "n_unverified": analysis.stats.n_unverified,
            "n_critical_contradicted": analysis.stats.n_critical_contradicted,
            "n_high_contradicted": analysis.stats.n_high_contradicted,
            "n_unverified_risks": analysis.stats.n_unverified_risks,
            "n_real_confirmed": analysis.stats.n_real_confirmed,
            "reliability": analysis.stats.reliability,
        } if analysis.stats else None,
        "conflicts": [
            {
                "record_id": c.record_id,
                "conflict_type": c.conflict_type.value,
                "claim_id": c.claim_id,
                "document_value": c.document_value,
                "found_value": c.found_value,
                "severity": c.severity.value,
            }
            for c in analysis.conflicts
        ],
    }


def _compare_or_update(name: str, actual: dict, update_goldens: bool) -> None:
    path = GOLDENS_DIR / f"s6_{name}.json"
    serialized = json.dumps(actual, sort_keys=True, ensure_ascii=False, indent=2)
    if update_goldens or not path.exists():
        GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized + "\n", encoding="utf-8")
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, f"Golden mismatch for {name} (run with --update-goldens to refresh)"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _chunk(
    chunk_id: str,
    content: str,
    law_id: str | None = None,
    article: str | None = None,
    legal_rank: LegalRank = LegalRank.LAW_RK,
    web_tier: WebTier | None = None,
    source_url: str = "http://adilet.zan.kz/rus/docs/Zdummy",
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=source_url,
        source_title=f"Источник {chunk_id}",
        content=content,
        legal_rank=legal_rank,
        law_id=law_id,
        article=article,
        web_tier=web_tier,
    )


# --- f1: deterministic CONFIRMED via reference_data (virtual passthrough) ---
# Covers: B1 (UNLINKED/reference_* passthrough), B9 (only-virtual sources →
# confidence-based status), B17/B18 (reliability math, virtual a_coef=0.5),
# B21 (_audit_conclusions).


def _build_f1() -> AnalysisJSON:
    return AnalysisJSON(
        analysis_id="f1",
        source_doc_id="doc1",
        plan_id="plan1",
        facts=[
            Fact(
                fact_id="fact_claim_0001",
                claim_id="claim_0001",
                claim="[claim_0001]: Закон 94-V действителен",
                source_ids=["reference_data"],
                confidence=0.9,
            ),
        ],
        verdicts=[
            ClaimVerdict(
                claim_id="claim_0001",
                status=VerdictStatus.CONFIRMED,
                source_ids=["reference_data"],
                severity=ClaimSeverity.MEDIUM,
                is_deterministic=True,
                confidence="HIGH",
            ),
        ],
        conclusions=[],
    )


# --- f2: adilet metadata-first match, arithmetic OK, real chunk resolution ---
# Covers: B2 (real chunk resolves), B5 (metadata-first exact match + arithmetic
# ok), B6 is covered separately via f2b below, B10 (_audit_fact normal path
# for claim_0003), B14 (tiered upgrade UNVERIFIED->CONFIRMED), B17/B18 (rank
# bands), B3 (web chunk filtered for non-official source).


def _build_f2() -> tuple[AnalysisJSON, list[EvidenceChunk], ClaimExtractionResult]:
    phrase = "штраф в размере 20 МРП за нарушение правил пожарной безопасности"
    chunk_47 = _chunk(
        "chunk_47",
        f"Статья 47. {phrase}.",
        law_id="261-IV",
        article="47",
        legal_rank=LegalRank.LAW_RK,
    )
    # Non-official web chunk with no law_id -> filtered out of verdict 2 source_ids
    web_chunk = _chunk(
        "web_chunk_1",
        "Случайная веб-страница без правового значения",
        law_id=None,
        web_tier=None,
        source_url="https://example.com/blog/post",
    )
    # Chunk to be found via normal _audit_fact BM25 path for claim_0003
    chunk_other = _chunk(
        "chunk_other",
        f"Статья 50. Также упоминается {phrase} в ином контексте применения штрафов пожарной безопасности.",
        law_id="261-IV",
        article="50",
        legal_rank=LegalRank.LAW_RK,
    )
    corpus = [chunk_47, web_chunk, chunk_other]

    claim1 = DocumentClaim(
        claim_id="claim_0001",
        claim_text=f"Документ устанавливает {phrase} в статье 47.",
        quote=phrase,
        claim_type=ClaimType.FINANCIAL,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    claim2 = DocumentClaim(
        claim_id="claim_0002",
        claim_text="Иное утверждение со ссылкой на веб-источник без правовой силы.",
        quote="",
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.MEDIUM,
    )
    claim3 = DocumentClaim(
        claim_id="claim_0003",
        claim_text=f"Также применяется {phrase} согласно иным нормам.",
        quote=phrase,
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.MEDIUM,
        target_law_ids=["261-IV"],
    )

    analysis = AnalysisJSON(
        analysis_id="f2",
        source_doc_id="doc2",
        plan_id="plan2",
        facts=[
            Fact(
                fact_id="fact_claim_0001",
                claim_id="claim_0001",
                claim=f"[claim_0001]: {phrase}",
                source_ids=["UNLINKED"],
            ),
            Fact(
                fact_id="fact_claim_0002",
                claim_id="claim_0002",
                claim="[claim_0002]: иное утверждение",
                source_ids=["web_chunk_1"],
            ),
            Fact(
                fact_id="fact_claim_0003",
                claim_id="claim_0003",
                claim=f"[claim_0003]: {phrase}",
                source_ids=["chunk_other"],
            ),
        ],
        verdicts=[
            ClaimVerdict(
                claim_id="claim_0001",
                status=VerdictStatus.UNVERIFIED,
                source_ids=[],
                severity=ClaimSeverity.HIGH,
            ),
            ClaimVerdict(
                claim_id="claim_0002",
                status=VerdictStatus.CONFIRMED,
                source_ids=["web_chunk_1"],
                severity=ClaimSeverity.MEDIUM,
                confidence="MEDIUM",
            ),
            ClaimVerdict(
                claim_id="claim_0003",
                status=VerdictStatus.UNVERIFIED,
                source_ids=["chunk_other"],
                severity=ClaimSeverity.MEDIUM,
            ),
        ],
        conclusions=[],
    )
    claims = ClaimExtractionResult(doc_id="doc2", claims=[claim1, claim2, claim3])
    return analysis, corpus, claims


# --- f3: CONTRADICTED verdicts + conflicts at varying severities ---
# Covers: B4 (CONFIRMED non-deterministic, no real sources -> downgrade),
# B12 (CONTRADICTED -> fact LOW, bm25=0), B19 (p_conflict severity weights),
# B22 (_build_conflicts_from_verdicts: HIERARCHY/TEMPORAL/FACTUAL).


def _build_f3() -> AnalysisJSON:
    return AnalysisJSON(
        analysis_id="f3",
        source_doc_id="doc3",
        plan_id="plan3",
        facts=[
            Fact(
                fact_id="fact_claim_0001",
                claim_id="claim_0001",
                claim="[claim_0001]: ложно подтверждённое утверждение",
                source_ids=["UNLINKED"],
                confidence=0.9,
            ),
            Fact(
                fact_id="fact_claim_0002",
                claim_id="claim_0002",
                claim="[claim_0002]: противоречие критической важности (КоАП иерархия)",
                source_ids=["UNLINKED"],
            ),
            Fact(
                fact_id="fact_claim_0003",
                claim_id="claim_0003",
                claim="[claim_0003]: противоречие сроков высокой важности",
                source_ids=["UNLINKED"],
            ),
            Fact(
                fact_id="fact_claim_0004",
                claim_id="claim_0004",
                claim="[claim_0004]: фактическое противоречие средней важности",
                source_ids=["UNLINKED"],
            ),
        ],
        verdicts=[
            # B4: CONFIRMED, non-deterministic, only UNLINKED -> downgrade to UNVERIFIED
            ClaimVerdict(
                claim_id="claim_0001",
                status=VerdictStatus.CONFIRMED,
                source_ids=["UNLINKED"],
                severity=ClaimSeverity.MEDIUM,
                is_deterministic=False,
                confidence="HIGH",
            ),
            # B12 + HIERARCHY conflict (CRITICAL)
            ClaimVerdict(
                claim_id="claim_0002",
                status=VerdictStatus.CONTRADICTED,
                source_ids=["UNLINKED"],
                severity=ClaimSeverity.CRITICAL,
                contradiction_detail="Конфликт с иерархией КоАП: подзаконный акт противоречит кодексу.",
                document_value="штраф 10 МРП",
                found_value="штраф 20 МРП",
            ),
            # B12 + TEMPORAL conflict (HIGH)
            ClaimVerdict(
                claim_id="claim_0003",
                status=VerdictStatus.CONTRADICTED,
                source_ids=["UNLINKED"],
                severity=ClaimSeverity.HIGH,
                contradiction_detail="Срок уведомления указан 10 дней, в источнике 30 дней.",
                document_value="10 дней",
                found_value="30 дней",
            ),
            # B12 + FACTUAL conflict (MEDIUM)
            ClaimVerdict(
                claim_id="claim_0004",
                status=VerdictStatus.CONTRADICTED,
                source_ids=["UNLINKED"],
                severity=ClaimSeverity.MEDIUM,
                contradiction_detail="Указано иное числовое значение, не соответствующее источнику.",
                document_value="100",
                found_value="200",
            ),
        ],
        conclusions=[],
    )


# --- f4: UNLINKED fallback recovery via corpus-wide BM25 + downgrade ---
# Covers: B3 (filtered web chunk -- not central here, mostly f2), B7 (UNLINKED
# corpus-wide BM25 recovers), B8 (recovery fails -> UNVERIFIED), B13 (fact
# UNVERIFIED/LOW + verdict CONFIRMED -> downgrade).


def _build_f4() -> tuple[AnalysisJSON, list[EvidenceChunk], ClaimExtractionResult]:
    phrase_long = (
        "лицензирование деятельности по экспорту нефтепродуктов осуществляется уполномоченным "
        "органом в порядке установленном правительством республики казахстан"
    )
    chunk_match = _chunk(
        "chunk_match",
        f"Статья 12. {phrase_long} в соответствии с настоящим законом.",
        law_id="261-IV",
        article="12",
        legal_rank=LegalRank.LAW_RK,
    )
    chunk_unrelated = _chunk(
        "chunk_unrelated",
        "Совершенно не связанный текст про регистрацию транспортных средств.",
        law_id="999-Z",
        article="1",
        legal_rank=LegalRank.LAW_RK,
    )
    corpus = [chunk_match, chunk_unrelated]

    claim1 = DocumentClaim(
        claim_id="claim_0001",
        claim_text=f"Документ регулирует {phrase_long}.",
        quote=phrase_long,
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    claim2 = DocumentClaim(
        claim_id="claim_0002",
        claim_text="Совершенно посторонний текст без какой-либо связи с корпусом вообще никак.",
        quote="",
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.LOW,
    )

    analysis = AnalysisJSON(
        analysis_id="f4",
        source_doc_id="doc4",
        plan_id="plan4",
        facts=[
            Fact(
                fact_id="fact_claim_0001",
                claim_id="claim_0001",
                claim=f"[claim_0001]: {phrase_long}",
                source_ids=["UNLINKED"],
            ),
            Fact(
                fact_id="fact_claim_0002",
                claim_id="claim_0002",
                claim="[claim_0002]: посторонний текст без связи",
                source_ids=["UNLINKED"],
            ),
        ],
        verdicts=[
            # B7: should recover via Layer 1 (law-specific) corpus-wide BM25
            ClaimVerdict(
                claim_id="claim_0001",
                status=VerdictStatus.UNVERIFIED,
                source_ids=[],
                severity=ClaimSeverity.HIGH,
            ),
            # B8 + B13: no recovery -> UNVERIFIED, and CONFIRMED verdict downgraded
            ClaimVerdict(
                claim_id="claim_0002",
                status=VerdictStatus.CONFIRMED,
                source_ids=["UNLINKED"],
                severity=ClaimSeverity.LOW,
                is_deterministic=True,  # avoid B4 downgrade path; exercise B13 sync instead
                confidence="HIGH",
            ),
        ],
        conclusions=[],
    )
    claims = ClaimExtractionResult(doc_id="doc4", claims=[claim1, claim2])
    return analysis, corpus, claims


# --- f5: insufficient-data reliability (B15 -> None) and frozen mixed case (B20 -> 0.375) ---


def _build_f5_insufficient() -> AnalysisJSON:
    return AnalysisJSON(
        analysis_id="f5a",
        source_doc_id="doc5a",
        plan_id="plan5a",
        facts=[],
        verdicts=[
            ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.MEDIUM),
            ClaimVerdict(claim_id="claim_0002", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.MEDIUM),
        ],
        conclusions=[],
    )


def _build_f5b_frozen_mixed() -> AnalysisJSON:
    return AnalysisJSON(
        analysis_id="f5b",
        source_doc_id="doc5b",
        plan_id="plan5b",
        facts=[],
        verdicts=[
            ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.CONFIRMED, source_ids=["UNLINKED"], severity=ClaimSeverity.MEDIUM),
            ClaimVerdict(claim_id="claim_0002", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.MEDIUM),
        ],
        conclusions=[],
    )


# ---------------------------------------------------------------------------
# conftest hook
# ---------------------------------------------------------------------------


@pytest.fixture
def update_goldens(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-goldens", default=False))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_f1_deterministic_confirmed(update_goldens: bool) -> None:
    analysis = _build_f1()
    audited = audit_analysis(analysis, [], None)
    _compare_or_update("f1_deterministic_confirmed", _extract(audited), update_goldens)


def test_f2_adilet_confirmed_arith(update_goldens: bool) -> None:
    analysis, corpus, claims = _build_f2()
    audited = audit_analysis(analysis, corpus, claims)
    _compare_or_update("f2_adilet_confirmed_arith", _extract(audited), update_goldens)


def test_f3_contradicted_conflicts(update_goldens: bool) -> None:
    analysis = _build_f3()
    audited = audit_analysis(analysis, [], None)
    _compare_or_update("f3_contradicted_conflicts", _extract(audited), update_goldens)


def test_f4_unlinked_fallback(update_goldens: bool) -> None:
    analysis, corpus, claims = _build_f4()
    audited = audit_analysis(analysis, corpus, claims)
    _compare_or_update("f4_unlinked_fallback", _extract(audited), update_goldens)


def test_f5a_insufficient_data(update_goldens: bool) -> None:
    analysis = _build_f5_insufficient()
    audited = audit_analysis(analysis, [], None)
    _compare_or_update("f5a_insufficient_data", _extract(audited), update_goldens)


def test_f5b_frozen_mixed(update_goldens: bool) -> None:
    analysis = _build_f5b_frozen_mixed()
    audited = audit_analysis(analysis, [], None)
    _compare_or_update("f5b_frozen_mixed", _extract(audited), update_goldens)


# ---------------------------------------------------------------------------
# Explicit reliability-math assertions (freeze branches B15/B16/B17/B20)
# ---------------------------------------------------------------------------


class TestReliabilityMath:
    def test_b15_n_total_le_2_all_unverified_is_none(self) -> None:
        """B15: n_total<=2 and all UNVERIFIED -> reliability=None."""
        analysis = _build_f5_insufficient()
        audited = audit_analysis(analysis, [], None)
        assert audited.overall_reliability is None
        assert audited.stats.reliability is None
        assert audited.stats.n_total == 2
        assert audited.stats.n_unverified == 2

    def test_b20_frozen_mixed_confirmed_unlinked_is_0_375(self) -> None:
        """B20: frozen v9.6 behavior — mixed CONFIRMED(UNLINKED)+UNVERIFIED -> 0.375.

        Frozen AS-IS per tests/test_offline_fallback.py::test_v9_3_bugfixes.
        Do not relitigate; n_real_confirmed=0 floors at max(0.0, base_score).
        """
        analysis = _build_f5b_frozen_mixed()
        audited = audit_analysis(analysis, [], None)
        assert audited.overall_reliability == pytest.approx(0.375)

    def test_b17_full_math_f2(self) -> None:
        """B17/B18: full V9.4 reliability formula runs for >2 analytical verdicts."""
        analysis, corpus, claims = _build_f2()
        audited = audit_analysis(analysis, corpus, claims)
        assert audited.overall_reliability is not None
        assert 0.0 <= audited.overall_reliability <= 1.0
        assert audited.stats.n_total == 3
