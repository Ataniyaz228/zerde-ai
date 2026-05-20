"""
Golden Dataset Evaluation Script (EDD)
Runs the ZERDE pipeline on a known reference document and calculates structured metrics.
"""

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from zerde.config import get_settings
from zerde.pipeline import run_pipeline
from zerde.models import VerdictStatus

class ExpectedIssue(BaseModel):
    issue_id: str
    claim_text_keywords: list[str]
    expected_verdict: VerdictStatus
    expected_contradiction_contains: list[str]
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

EXPECTED_ISSUES = [
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
        claim_text_keywords=["640", "КоАП"],
        expected_verdict=VerdictStatus.CONTRADICTED,
        expected_contradiction_contains=["79", "200"],
        severity="HIGH",
    ),
    ExpectedIssue(
        issue_id="uk_article_207",
        claim_text_keywords=["207", "Уголовн"],
        expected_verdict=VerdictStatus.CONTRADICTED,
        expected_contradiction_contains=["нет", "отсутствует", "исключена", "утратила силу"],
        severity="CRITICAL",
    ),
    ExpectedIssue(
        issue_id="deadline_24h",
        claim_text_keywords=["24", "часов"],
        expected_verdict=VerdictStatus.CONTRADICTED,
        expected_contradiction_contains=["дней", "день"],
        severity="HIGH",
    ),
    ExpectedIssue(
        issue_id="fines_inflated_500",
        claim_text_keywords=["500", "МРП"],
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

@dataclass
class EvalResult:
    recall: float
    precision: float
    f1: float
    false_positives: list[str]
    missed: list[str]
    unverified: list[str]
    corpus_claim_coverage: float
    latency_sec: float
    cost_usd: float

class RegressionGuard:
    max_latency_sec: float = 600
    max_cost_usd: float = 3.00
    min_recall: float = 0.50

async def evaluate():
    doc_path = "docs/ZERDE_test_bill_RK_2025.docx"
    print(f"Running evaluation on {doc_path}...")
    
    start_time = time.time()
    
    try:
        # Pipeline is expected to return a dict with "analysis" and "report_path"
        result = await run_pipeline(doc_path)
    except Exception as e:
        print(f"Pipeline failed: {e}")
        sys.exit(1)
        
    latency = time.time() - start_time
    
    analysis = result["analysis"]
    verdicts = analysis.verdicts
    
    found_issues = []
    missed = []
    unverified = []
    false_positives = []
    
    # 1. Match EXPECTED_ISSUES with verdicts
    for expected in EXPECTED_ISSUES:
        matched = False
        for verdict in verdicts:
            doc_val = verdict.document_value or ""
            detail = verdict.contradiction_detail or ""
            
            # Match keywords in document_value (ANY keyword is enough)
            if any(kw.lower() in doc_val.lower() for kw in expected.claim_text_keywords):
                if verdict.status == expected.expected_verdict:
                    if expected.expected_contradiction_contains:
                        # If required keywords exist in contradiction detail
                        if any(c_kw.lower() in detail.lower() for c_kw in expected.expected_contradiction_contains):
                            found_issues.append(expected.issue_id)
                            matched = True
                            break
                    else:
                        found_issues.append(expected.issue_id)
                        matched = True
                        break
                elif verdict.status == VerdictStatus.UNVERIFIED and expected.expected_verdict != VerdictStatus.UNVERIFIED:
                    unverified.append(expected.issue_id)
                    matched = True
                    break
        if not matched:
            missed.append(expected.issue_id)
            
    # 2. False Positives (Any CONTRADICTED that isn't mapped to an expected issue)
    all_contradicted = [v for v in verdicts if v.status == VerdictStatus.CONTRADICTED]
    false_positives_count = len(all_contradicted) - len([i for i in found_issues if i not in unverified])
    if false_positives_count < 0:
        false_positives_count = 0
    
    # 3. Calculate metrics
    total_expected = len([i for i in EXPECTED_ISSUES if i.expected_verdict == VerdictStatus.CONTRADICTED])
    found_contradicted = len([i for i in found_issues if i not in unverified])
    
    recall = found_contradicted / total_expected if total_expected > 0 else 0.0
    precision = found_contradicted / (found_contradicted + false_positives_count) if (found_contradicted + false_positives_count) > 0 else 0.0
    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    
    # 4. Corpus Claim Coverage (% of claims with at least 1 BM25 source matching)
    # The pipeline should set "source_ids" for verdicts that have relevant context
    claims_with_sources = sum(1 for v in verdicts if len(v.source_ids) > 0)
    coverage = claims_with_sources / len(verdicts) if len(verdicts) > 0 else 0.0
    
    print("=" * 40)
    print("🏅 EVALUATION RESULTS (Golden Dataset)")
    print("=" * 40)
    print(f"Recall:       {recall:.2%} ({found_contradicted}/{total_expected} critical errors found)")
    print(f"Precision:    {precision:.2%} ({found_contradicted} correct / {found_contradicted + false_positives_count} total flagged)")
    print(f"F1 Score:     {f1:.2%}")
    print(f"Coverage:     {coverage:.2%} (Claims with BM25 matches)")
    print(f"Latency:      {latency:.1f} sec")
    print("-" * 40)
    if missed:
        print(f"❌ Missed (Expected CONTRADICTED, but not flagged or keyword mismatch): {missed}")
    if unverified:
        print(f"⚠️ Unverified (Expected CONTRADICTED, but missing sources): {unverified}")
    print(f"👻 False Positives Count: {false_positives_count}")
    print("=" * 40)
    
    # 5. Regression Guards
    failed_guards = False
    if latency > RegressionGuard.max_latency_sec:
        print(f"🚨 GUARD FAILED: Latency {latency:.1f}s > {RegressionGuard.max_latency_sec}s")
        failed_guards = True
        
    if recall < RegressionGuard.min_recall:
        print(f"🚨 GUARD FAILED: Recall {recall:.2%} < {RegressionGuard.min_recall:.2%}")
        failed_guards = True
        
    if failed_guards:
        sys.exit(1)
    else:
        print("✅ All Regression Guards Passed")

if __name__ == "__main__":
    asyncio.run(evaluate())
