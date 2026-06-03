"""
Детерминированные регресс-гарды Фазы B (без LLM/сети).

C2 — S6 не должен ре-промоутить UNVERIFIED→CONFIRMED по одной лексической близости
     BM25, если (а) verifier уже понизил вердикт или (б) claim поправочный.
M3 — числовое расхождение с ТОЧНО процитированной статьёй → CONTRADICTED,
     но НЕ для поправочных claim'ов (они меняют число намеренно).
M2 — deadline-collision не должна выдумывать ложный CONTRADICTED, когда текст
     не разбит на статьи (один огромный блок).
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
from zerde.stages.s2_7_self_check import run_self_check
from zerde.stages.s6_auditor import audit_analysis

_PHRASE = "освобождение должника от уплаты расходов частного судебного исполнителя при амнистии"


def _filler(n: int) -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            chunk_id=f"filler_{i}",
            source_url="http://adilet.zan.kz/rus/docs/Zx",
            source_title="Прочее",
            content="нерелевантный наполнитель корпуса номер " + str(i),
            legal_rank=LegalRank.LAW_RK,
            law_id="999-Z",
        )
        for i in range(n)
    ]


def _analysis(verdict: ClaimVerdict) -> AnalysisJSON:
    return AnalysisJSON(
        analysis_id="t",
        source_doc_id="doc",
        plan_id="plan",
        facts=[Fact(fact_id=f"fact_{verdict.claim_id}", claim_id=verdict.claim_id,
                    claim=f"[{verdict.claim_id}]: ...", source_ids=["UNLINKED"])],
        verdicts=[verdict],
    )


# ---------------------------------------------------------------------------
# C2
# ---------------------------------------------------------------------------


def test_c2_verifier_demoted_not_re_promoted_to_confirmed():
    """Вердикт, понижённый verifier'ом (verifier_demoted=True), S6 не воскрешает в CONFIRMED."""
    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text=f"Документ закрепляет {_PHRASE} в статье 118.",
        quote=_PHRASE,
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    chunk = EvidenceChunk(
        chunk_id="real_118",
        source_url="http://adilet.zan.kz/rus/docs/Z100000261_",
        source_title="Закон 261-IV | Ст. 118",
        content=f"Статья 118. {_PHRASE}. {_PHRASE}. {_PHRASE}.",
        legal_rank=LegalRank.LAW_RK,
        law_id="261-IV",
        article="118",
    )
    verdict = ClaimVerdict(
        claim_id="claim_0001", status=VerdictStatus.UNVERIFIED, source_ids=[],
        severity=ClaimSeverity.HIGH, verifier_demoted=True,
    )
    audited = audit_analysis(_analysis(verdict), [chunk] + _filler(4),
                             ClaimExtractionResult(doc_id="doc", claims=[claim]))
    v = next(v for v in audited.verdicts if v.claim_id == "claim_0001")
    assert v.status != VerdictStatus.CONFIRMED, f"verifier-demoted ложно подтверждён: {v.status}"


def test_c2_amendment_claim_not_auto_confirmed_by_lexical_match():
    """Поправочный claim («заменить …»), лексически совпавший со статьёй, НЕ становится CONFIRMED."""
    claim = DocumentClaim(
        claim_id="claim_0002",
        claim_text=f"Заменить в статье 118 формулировку на «{_PHRASE}».",
        quote=_PHRASE,
        claim_type=ClaimType.NORMATIVE,
        severity=ClaimSeverity.HIGH,
        target_law_ids=["261-IV"],
    )
    chunk = EvidenceChunk(
        chunk_id="real_118b",
        source_url="http://adilet.zan.kz/rus/docs/Z100000261_",
        source_title="Закон 261-IV | Ст. 118",
        content=f"Статья 118. {_PHRASE}. {_PHRASE}. {_PHRASE}.",
        legal_rank=LegalRank.LAW_RK,
        law_id="261-IV",
        article="118",
    )
    verdict = ClaimVerdict(
        claim_id="claim_0002", status=VerdictStatus.UNVERIFIED, source_ids=[],
        severity=ClaimSeverity.HIGH,
    )
    audited = audit_analysis(_analysis(verdict), [chunk] + _filler(4),
                             ClaimExtractionResult(doc_id="doc", claims=[claim]))
    v = next(v for v in audited.verdicts if v.claim_id == "claim_0002")
    assert v.status != VerdictStatus.CONFIRMED, f"поправочный claim ложно подтверждён: {v.status}"


# ---------------------------------------------------------------------------
# M3
# ---------------------------------------------------------------------------


def test_m3_numeric_conflict_with_cited_article_becomes_contradicted():
    """Claim говорит 5000 МРП, ст.79 источника — 200 МРП → CONTRADICTED с реальными значениями."""
    claim = DocumentClaim(
        claim_id="claim_0003",
        claim_text="Документ устанавливает штраф 5000 МРП в статье 79.",
        quote="штраф 5000 МРП",
        claim_type=ClaimType.FINANCIAL,
        severity=ClaimSeverity.CRITICAL,
        target_law_ids=["235-V"],
    )
    chunk = EvidenceChunk(
        chunk_id="koap_79",
        source_url="http://adilet.zan.kz/rus/docs/K1400000235",
        source_title="КоАП | Ст. 79",
        content="Статья 79. Нарушение влечёт штраф 200 МРП на физических лиц.",
        legal_rank=LegalRank.CODE,
        law_id="235-V",
        article="79",
    )
    verdict = ClaimVerdict(
        claim_id="claim_0003", status=VerdictStatus.UNVERIFIED, source_ids=[],
        severity=ClaimSeverity.CRITICAL,
    )
    audited = audit_analysis(_analysis(verdict), [chunk] + _filler(4),
                             ClaimExtractionResult(doc_id="doc", claims=[claim]))
    v = next(v for v in audited.verdicts if v.claim_id == "claim_0003")
    assert v.status == VerdictStatus.CONTRADICTED, f"числовое расхождение не помечено: {v.status}"
    assert v.found_value and "200" in v.found_value
    assert v.document_value and "5000" in v.document_value


def test_m3_amendment_number_change_not_contradicted():
    """Поправка «заменить 200 на 5000» с источником 200 — НЕ противоречие (намеренная замена)."""
    claim = DocumentClaim(
        claim_id="claim_0004",
        claim_text="Заменить в статье 79 «200 МРП» на «5000 МРП».",
        quote="заменить 200 МРП на 5000 МРП",
        claim_type=ClaimType.FINANCIAL,
        severity=ClaimSeverity.CRITICAL,
        target_law_ids=["235-V"],
    )
    chunk = EvidenceChunk(
        chunk_id="koap_79b",
        source_url="http://adilet.zan.kz/rus/docs/K1400000235",
        source_title="КоАП | Ст. 79",
        content="Статья 79. Нарушение влечёт штраф 200 МРП на физических лиц.",
        legal_rank=LegalRank.CODE,
        law_id="235-V",
        article="79",
    )
    verdict = ClaimVerdict(
        claim_id="claim_0004", status=VerdictStatus.UNVERIFIED, source_ids=[],
        severity=ClaimSeverity.CRITICAL,
    )
    audited = audit_analysis(_analysis(verdict), [chunk] + _filler(4),
                             ClaimExtractionResult(doc_id="doc", claims=[claim]))
    v = next(v for v in audited.verdicts if v.claim_id == "claim_0004")
    assert v.status != VerdictStatus.CONTRADICTED, f"поправка ложно помечена ошибкой: {v.status}"


# ---------------------------------------------------------------------------
# M2
# ---------------------------------------------------------------------------


def test_m2_real_collision_in_short_structured_article_detected():
    """Контроль: в короткой статье два разных срока одного типа → коллизия выявляется."""
    text = "Статья 5.\nУведомление направляется в течение 180 дней.\nОтвет даётся в течение 60 дней."
    claims = run_self_check(text)
    assert any("колли" in c.claim_text.lower() for c in claims), "реальная коллизия не найдена"


def test_m2_no_false_collision_when_doc_not_split_into_articles():
    """Огромный неразбитый блок со сроками из 'разных статей' → НЕ выдумываем коллизию."""
    big = "Статья 1. " + ("прочий нормативный текст наполнителя. " * 400)
    big += " уведомление в течение 180 дней. " + ("ещё текст. " * 400)
    big += " другой срок в течение 60 дней."
    assert len(big) > 8000
    claims = run_self_check(big)
    assert not any("колли" in c.claim_text.lower() for c in claims), \
        "ложная коллизия в неразбитом документе"
