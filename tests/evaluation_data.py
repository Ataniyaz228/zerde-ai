from pydantic import BaseModel
from typing import Literal
from zerde.models import VerdictStatus

class ExpectedIssue(BaseModel):
    issue_id: str
    claim_text_keywords: list[str]
    expected_verdict: VerdictStatus
    expected_contradiction_contains: list[str]
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

class GoldenTestCase(BaseModel):
    case_id: str
    document_path: str
    expected_issues: list[ExpectedIssue]
    min_recall: float = 0.50

GOLDEN_TEST_CASES = [
    GoldenTestCase(
        case_id="zerde_bill_2025_ru",
        document_path="docs/ZERDE_test_bill_RK_2025.docx",
        min_recall=0.70,
        expected_issues=[
            ExpectedIssue(
                issue_id="law_number_87iv",
                claim_text_keywords=["87-IV"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=["94-V", "94-В", "94-v"],
                severity="CRITICAL",
            ),
            ExpectedIssue(
                issue_id="mrp_3450",
                claim_text_keywords=["3450", "3 450"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=[],
                severity="CRITICAL",
            ),
            ExpectedIssue(
                issue_id="koap_article_640",
                claim_text_keywords=["640"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=["79", "200"],
                severity="HIGH",
            ),
            ExpectedIssue(
                issue_id="uk_article_207",
                claim_text_keywords=["207"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=["нет", "отсутствует", "исключена", "утратила силу"],
                severity="CRITICAL",
            ),
            ExpectedIssue(
                issue_id="deadline_24h",
                claim_text_keywords=["24 часа", "24 часов"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=["дней", "день"],
                severity="HIGH",
            ),
            ExpectedIssue(
                issue_id="fines_inflated_500",
                claim_text_keywords=["500 МРП", "500-кратном"],
                expected_verdict=VerdictStatus.CONTRADICTED,
                expected_contradiction_contains=["200", "100", "меньше"],
                severity="HIGH",
            ),
            ExpectedIssue(
                issue_id="chronology_2012",
                claim_text_keywords=["2012"],
                expected_verdict=VerdictStatus.UNVERIFIED,
                expected_contradiction_contains=[],
                severity="LOW",
            )
        ]
    ),
    GoldenTestCase(
        case_id="koap_amnesty_2026_kk",
        document_path="docs/проект_закона_КоАП_каз.docx",
        min_recall=0.50,
        expected_issues=[
            ExpectedIssue(
                issue_id="koap_amnesty_consent",
                claim_text_keywords=["келісімі талап етілмейді", "согласие правонарушителя", "согласие"],
                expected_verdict=VerdictStatus.CONFIRMED,
                expected_contradiction_contains=[],
                severity="HIGH",
            ),
            ExpectedIssue(
                issue_id="koap_digital_registry",
                claim_text_keywords=["Цифрлық жүйені", "Тізілімі арқылы", "цифровой реестр"],
                expected_verdict=VerdictStatus.CONFIRMED,
                expected_contradiction_contains=[],
                severity="MEDIUM",
            ),
            ExpectedIssue(
                issue_id="koap_article_741_16",
                claim_text_keywords=["741-баптың", "16) тармақшамен", "741", "16"],
                expected_verdict=VerdictStatus.CONFIRMED,
                expected_contradiction_contains=[],
                severity="MEDIUM",
            )
        ]
    )
]
