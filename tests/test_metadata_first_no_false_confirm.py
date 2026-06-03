"""
Регресс-гард C1: metadata-first в S6 не должен производить ложный CONFIRMED.

Совпадение (law_id, article) между claim и чанком корпуса — это привязка к
ПЕРВОИСТОЧНИКУ, а НЕ доказательство, что текст статьи подтверждает СУТЬ claim.
Раньше metadata-first жёстко ставил bm25=1.0 + HIGH, и sync-блок апгрейдил
UNVERIFIED→CONFIRMED только по факту совпадения номера статьи (для поправочных
биллей, ссылающихся на существующие статьи, — массовый false-confirm).

После фикса статус metadata-first считается по реальному BM25 claim↔чанк:
несвязанное содержание → низкий score → НЕ CONFIRMED (CITE-OR-ABSTAIN).
"""

from __future__ import annotations

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
)
from zerde.stages.s6_auditor import audit_analysis


def _dummy_chunks(n: int) -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            chunk_id=f"dummy_{i}",
            source_url="http://adilet.zan.kz/rus/docs/Zdummy",
            source_title="Прочий закон",
            content="Произвольный наполнитель корпуса без отношения к проверяемым утверждениям номер "
            + str(i),
            legal_rank=LegalRank.LAW_RK,
            law_id="999-Z",
        )
        for i in range(n)
    ]


def test_metadata_match_unrelated_content_stays_unverified():
    """Claim ссылается на ст.47 закона 261-IV; в корпусе есть ст.47, но про ДРУГОЕ → не CONFIRMED."""
    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Документ вводит лицензирование импорта алкогольной продукции в статье 47.",
        quote="лицензирование импорта алкогольной продукции",
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    # Чанк с тем же (law_id, article), но совершенно про другое (наследование земли).
    chunk = EvidenceChunk(
        chunk_id="real_47",
        source_url="http://adilet.zan.kz/rus/docs/Z100000261_",
        source_title="Закон 261-IV | Ст. 47",
        content="Статья 47. Наследование земельных участков сельскохозяйственного назначения "
        "осуществляется в порядке, установленном гражданским законодательством.",
        legal_rank=LegalRank.LAW_RK,
        law_id="261-IV",
        article="47",
    )
    corpus = [chunk] + _dummy_chunks(4)

    analysis = AnalysisJSON(
        analysis_id="t1",
        source_doc_id="doc",
        plan_id="plan",
        facts=[
            Fact(fact_id="fact_claim_0001", claim_id="claim_0001", claim="[claim_0001]: ...", source_ids=["UNLINKED"]),
        ],
        verdicts=[
            ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.HIGH),
        ],
    )
    claims = ClaimExtractionResult(doc_id="doc", claims=[claim])

    audited = audit_analysis(analysis, corpus, claims)

    v = next(v for v in audited.verdicts if v.claim_id == "claim_0001")
    # Главное свойство: НЕ должно стать CONFIRMED только из-за совпадения номера статьи.
    assert v.status != VerdictStatus.CONFIRMED, (
        f"metadata-match по (закон, статья) с несвязанным содержанием ложно подтверждён: {v.status}"
    )


def test_metadata_match_supporting_content_can_confirm():
    """Позитивный контроль: если текст статьи реально про claim — CONFIRMED допустим."""
    phrase = "освобождение должника от уплаты расходов частного судебного исполнителя при амнистии"
    claim = DocumentClaim(
        claim_id="claim_0002",
        claim_text=f"Документ закрепляет {phrase} в статье 118.",
        quote=phrase,
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    chunk = EvidenceChunk(
        chunk_id="real_118",
        source_url="http://adilet.zan.kz/rus/docs/Z100000261_",
        source_title="Закон 261-IV | Ст. 118",
        content=f"Статья 118. {phrase}. {phrase}.",
        legal_rank=LegalRank.LAW_RK,
        law_id="261-IV",
        article="118",
    )
    corpus = [chunk] + _dummy_chunks(4)

    analysis = AnalysisJSON(
        analysis_id="t2",
        source_doc_id="doc",
        plan_id="plan",
        facts=[
            Fact(fact_id="fact_claim_0002", claim_id="claim_0002", claim="[claim_0002]: ...", source_ids=["UNLINKED"]),
        ],
        verdicts=[
            ClaimVerdict(claim_id="claim_0002", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.HIGH),
        ],
    )
    claims = ClaimExtractionResult(doc_id="doc", claims=[claim])

    audited = audit_analysis(analysis, corpus, claims)

    fact = next(f for f in audited.facts if f.claim_id == "claim_0002")
    # Содержание совпадает с claim → BM25 высокий, источник привязан.
    assert fact.bm25_score is not None and fact.bm25_score > 0.0
    assert "real_118" in fact.source_ids
