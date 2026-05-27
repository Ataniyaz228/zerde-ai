import pytest
from zerde.models import (
    EvidenceChunk,
    LegalRank,
    DocumentClaim,
    ClaimType,
    ClaimSeverity,
    AnalysisJSON,
    Fact,
    ClaimVerdict,
    VerdictStatus,
    ValidationStatus,
    ClaimExtractionResult,
)
from zerde.stages.s6_auditor import (
    _extract_referenced_law_ids,
    _corpus_wide_bm25_search,
    audit_analysis,
    ZerdeBM25,
)

def test_extract_referenced_law_ids_synonyms():
    # 1. Проверяем, что aliases Закона об исполнительном производстве распознаются и маппятся на 261-IV
    claim_ru = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Закон РК об исполнительном производстве и статусе судебных исполнителей дополнен статьей 10-5.",
        quote="исполнительном производстве",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )
    law_ids = _extract_referenced_law_ids(claim_ru)
    assert "261-IV" in law_ids

    # 2. Проверяем то же самое на казахском языке
    claim_kk = DocumentClaim(
        claim_id="claim_0002",
        claim_text="«Сот орындаушылары және атқарушылық іс жүргізу туралы» ҚР Заңы",
        quote="атқарушылық іс жүргізу",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )
    law_ids_kk = _extract_referenced_law_ids(claim_kk)
    assert "261-IV" in law_ids_kk


def test_corpus_wide_bm25_search_blocks_wrong_law():
    # Создаем корпус, содержащий ТОЛЬКО комментарий к КоАП (без Закона о ЧСИ 261-IV)
    koap_commentary_chunk = EvidenceChunk(
        chunk_id="koap_commentary_001",
        source_url="https://prg.kz/document/?doc_id=33722527",
        source_title="Научно-практический комментарий к Кодексу РК об административных правонарушениях",
        content="Статья 63. Освобождение от административной ответственности и взыскания на основании акта амнистии.",
        legal_rank=LegalRank.EXPERT_ANALYTICS,
        law_id="235-V",  # КоАП
        article="63"
    )

    corpus_index = {koap_commentary_chunk.chunk_id: koap_commentary_chunk}
    bm25 = ZerdeBM25(corpus_index)

    # Утверждение явно ссылается на Закон об исполнительном производстве (261-IV)
    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Закон РК об исполнительном производстве дополнен подпунктом 3) пункта 1 статьи 10-5 (освобождение от ответственности).",
        quote="Закон РК об исполнительном производстве",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )

    fact = Fact(
        fact_id="fact_0001",
        claim_id="claim_0001",
        claim="Документ утверждает изменение в Закон РК об исполнительном производстве",
        source_ids=["UNLINKED"]
    )

    # Так как закон 261-IV отсутствует в корпусе, поиск должен быть ЗАБЛОКИРОВАН и вернуть None,
    # даже если комментарий КоАП семантически похож на слова "освобождение от ответственности"
    score = _corpus_wide_bm25_search(fact, bm25, corpus_index, claim)
    assert score is None
    assert fact.source_ids == ["UNLINKED"]


def test_sync_verdict_guard():
    # Симулируем ситуацию, когда оригинальный вердикт LLM был UNVERIFIED,
    # но BM25-fallback в Stage 6 выдал какой-то семантический шум (score > 0.40)
    
    koap_commentary_chunk = EvidenceChunk(
        chunk_id="koap_commentary_001",
        source_url="https://prg.kz/document/?doc_id=33722527",
        source_title="Научно-практический комментарий к Кодексу РК об административных правонарушениях",
        content="Статья 63. Освобождение от административной ответственности и взыскания на основании акта амнистии.",
        legal_rank=LegalRank.EXPERT_ANALYTICS,
        law_id="235-V",
        article="63"
    )

    # Делаем вид, что Stage 6 восстановил связь (сделал fallback с score=0.60)
    fact = Fact(
        fact_id="fact_0001",
        claim_id="claim_0001",
        claim="[claim_0001]: 'Закон РК об исполнительном производстве'",
        source_ids=["koap_commentary_001"],
        confidence=0.4,
        validation_status=ValidationStatus.HIGH,  # Симулируем HIGH статус от BM25-поиска
        bm25_score=0.60  # Но это fallback поиск (score < 1.0)
    )

    # Оригинальный вердикт модели — строго UNVERIFIED
    verdict = ClaimVerdict(
        claim_id="claim_0001",
        status=VerdictStatus.UNVERIFIED,
        source_ids=[],
        confidence="LOW",
        severity=ClaimSeverity.HIGH
    )

    analysis = AnalysisJSON(
        analysis_id="test_analysis_id",
        source_doc_id="test_doc",
        plan_id="test_plan",
        facts=[fact],
        verdicts=[verdict]
    )

    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Закон РК об исполнительном производстве дополнен подпунктом 3) пункта 1 статьи 10-5.",
        quote="Закон РК об исполнительном производстве",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )
    
    claims = ClaimExtractionResult(
        doc_id="test_doc",
        claims=[claim]
    )

    # Запускаем аудит
    audited = audit_analysis(analysis, [koap_commentary_chunk], claims)

    # Защитный барьер (Sync Verdict Guard) должен БЛОКИРОВАТЬ апгрейд до CONFIRMED.
    # Вердикт должен остаться строго UNVERIFIED!
    assert audited.verdicts[0].status == VerdictStatus.UNVERIFIED
    assert audited.verdicts[0].confidence == "LOW"
