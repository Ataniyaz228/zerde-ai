import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from tests.evaluation_data import GOLDEN_TEST_CASES, ExpectedIssue, GoldenTestCase
from zerde.config import get_settings
from zerde.pipeline import run_pipeline
from zerde.models import VerdictStatus

@dataclass
class CaseResult:
    case_id: str
    document_path: str
    recall: float
    precision: float
    f1: float
    coverage: float
    false_positives: int
    missed: list[str]
    unverified: list[str]
    found: list[str]
    latency_sec: float
    cost_usd: float
    success: bool
    error_msg: str = ""

async def run_evaluation():
    settings = get_settings()
    print("=" * 60)
    print("🔬 ZERDE AI — MULTI-DOCUMENT EVALUATION RUNNER")
    print("=" * 60)
    
    results = []
    start_time_all = time.time()
    
    for case in GOLDEN_TEST_CASES:
        print(f"\n► Running test case '{case.case_id}' on: {case.document_path}...")
        
        if not os.path.exists(case.document_path):
            results.append(CaseResult(
                case_id=case.case_id,
                document_path=case.document_path,
                recall=0.0, precision=0.0, f1=0.0, coverage=0.0,
                false_positives=0, missed=[], unverified=[], found=[],
                latency_sec=0.0, cost_usd=0.0, success=False,
                error_msg="Input file not found"
            ))
            continue
            
        start_time = time.time()
        try:
            pipeline_res = await run_pipeline(case.document_path)
            latency = time.time() - start_time
            analysis = pipeline_res.analysis
            verdicts = analysis.verdicts
            
            found_issues = []
            missed = []
            unverified = []
            
            for expected in case.expected_issues:
                matched = False
                for verdict in verdicts:
                    text_to_search = (verdict.document_value or "") + " " + (verdict.contradiction_detail or "")
                    if any(kw.lower() in text_to_search.lower() for kw in expected.claim_text_keywords):
                        if verdict.status == expected.expected_verdict:
                            found_issues.append(expected.issue_id)
                            matched = True
                            break
                        elif verdict.status == VerdictStatus.UNVERIFIED:
                            unverified.append(expected.issue_id)
                            matched = True
                            break
                if not matched:
                    missed.append(expected.issue_id)
                    
            all_contradicted = [v for v in verdicts if v.status == VerdictStatus.CONTRADICTED]
            false_positives_count = max(0, len(all_contradicted) - len(found_issues))
            
            recall = len(found_issues) / len(case.expected_issues) if case.expected_issues else 1.0
            precision = len(found_issues) / (len(found_issues) + false_positives_count) if (len(found_issues) + false_positives_count) > 0 else 1.0
            f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
            
            results.append(CaseResult(
                case_id=case.case_id,
                document_path=case.document_path,
                recall=recall,
                precision=precision,
                f1=f1,
                coverage=1.0,
                false_positives=false_positives_count,
                missed=missed,
                unverified=unverified,
                found=found_issues,
                latency_sec=latency,
                cost_usd=0.15,
                success=recall >= case.min_recall
            ))
            print(f"✓ Case '{case.case_id}' finished: Recall={recall:.1%} Precision={precision:.1%} FP={false_positives_count}")
        except Exception as e:
            results.append(CaseResult(
                case_id=case.case_id,
                document_path=case.document_path,
                recall=0.0, precision=0.0, f1=0.0, coverage=0.0,
                false_positives=0, missed=[], unverified=[], found=[],
                latency_sec=0.0, cost_usd=0.0, success=False,
                error_msg=str(e)
            ))
            
    _write_report(results, time.time() - start_time_all)
    sys.exit(0)

def _write_report(results: list[CaseResult], total_time: float):
    pass

if __name__ == "__main__":
    asyncio.run(run_evaluation())
