from zerde.stages.s2_5_claim_extractor import (
    ClaimSeverity,
    ClaimType,
    DocumentClaim,
    _is_structural_claim,
)


def test_article_118_amendment_is_not_structural():
    # Утверждение о внесении процессуального изменения (изменение ст. 118)
    claim = DocumentClaim(
        claim_id="claim_0003",
        claim_text="Документ утверждает: пункт 6 статьи 118 после слов «статьи 9» дополнить словами «, подпункта 5-3) пункта 1 статьи 47».",
        quote="пункт 6 статьи 118 после слов «статьи 9» дополнить словами «, подпункта 5-3) пункта 1 статьи 47»;",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )

    # С новым процессуальным фиксом это утверждение должно классифицироваться как АНАЛИТИЧЕСКОЕ (не структурное)
    assert _is_structural_claim(claim) is False

def test_kazakh_amendment_is_not_structural():
    # Утверждение на казахском языке о внесении поправки
    claim = DocumentClaim(
        claim_id="claim_0004",
        claim_text="118-баптың 6-тармағы «9-баптың» деген сөздерден кейін «, 47-баптың 1-тармағының 5-3) тармақшасының» деген сөздермен толықтырылсын",
        quote="толықтырылсын",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )

    assert _is_structural_claim(claim) is False
