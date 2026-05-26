"""
Golden Dataset Evaluation Script (EDD)
Runs the ZERDE pipeline on multiple reference documents and calculates structured metrics.
"""

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

class RegressionGuard:
    max_latency_sec: float = 600
    max_cost_usd: float = 3.00

async def run_evaluation():
    settings = get_settings()
    print("=" * 60)
    print("🔬 ZERDE AI — MULTI-DOCUMENT EVALUATION RUNNER")
    print("=" * 60)
    
    results: list[CaseResult] = []
    start_time_all = time.time()
    
    for case in GOLDEN_TEST_CASES:
        print(f"\n► Running test case '{case.case_id}' on: {case.document_path}...")
        
        # Ensure input file exists
        if not os.path.exists(case.document_path):
            print(f"❌ Error: Test document not found: {case.document_path}")
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
            # Run pipeline
            pipeline_res = await run_pipeline(case.document_path)
            latency = time.time() - start_time
            
            analysis = pipeline_res.analysis
            verdicts = analysis.verdicts
            
            found_issues = []
            missed = []
            unverified = []
            false_positives_count = 0
            
            # 1. Match expected issues with verdicts
            for expected in case.expected_issues:
                matched = False
                for verdict in verdicts:
                    doc_val = verdict.document_value or ""
                    detail = verdict.contradiction_detail or ""
                    
                    # Match keywords in document_val or claim_text (ANY keyword is enough)
                    text_to_search = (verdict.document_value or "") + " " + (verdict.contradiction_detail or "")
                    # Also fallback to searching fact claim text if any
                    fact_claim = ""
                    for f in analysis.facts:
                        if f.claim_id == verdict.claim_id:
                            fact_claim = f.claim
                            break
                    text_to_search += " " + fact_claim
                    
                    if any(kw.lower() in text_to_search.lower() for kw in expected.claim_text_keywords):
                        # Strict False Confirmed check:
                        # If ground-truth says expected_verdict is CONTRADICTED, but the verdict is CONFIRMED,
                        # this is a catastrophic false confirmation!
                        if expected.expected_verdict == VerdictStatus.CONTRADICTED and verdict.status == VerdictStatus.CONFIRMED:
                            print(f"🚨 CRITICAL REGRESSION: expected CONTRADICTED for '{expected.issue_id}', but got CONFIRMED!")
                            # Do not match as success, treat as FP / Error
                            continue
                            
                        if verdict.status == expected.expected_verdict:
                            if expected.expected_contradiction_contains:
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
                    
            # 2. Count False Positives (CONTRADICTED verdicts that are not expected issues)
            all_contradicted = [v for v in verdicts if v.status == VerdictStatus.CONTRADICTED]
            matched_contradicted_count = len([i for i in found_issues if i not in unverified])
            false_positives_count = max(0, len(all_contradicted) - matched_contradicted_count)
            
            # 3. Calculate metrics
            total_expected_contradicted = len([i for i in case.expected_issues if i.expected_verdict == VerdictStatus.CONTRADICTED])
            found_contradicted = len([i for i in found_issues if i not in unverified])
            
            # For confirmed expectations:
            total_expected_confirmed = len([i for i in case.expected_issues if i.expected_verdict == VerdictStatus.CONFIRMED])
            found_confirmed = len([i for i in found_issues if i in [exp.issue_id for exp in case.expected_issues if exp.expected_verdict == VerdictStatus.CONFIRMED]])
            
            total_expected = len(case.expected_issues)
            found_total = len(found_issues)
            
            # Calculate Recall on contradicted (vulnerabilities/errors found)
            recall = found_contradicted / total_expected_contradicted if total_expected_contradicted > 0 else (found_confirmed / total_expected_confirmed if total_expected_confirmed > 0 else 1.0)
            
            # Calculate Precision
            precision = found_contradicted / (found_contradicted + false_positives_count) if (found_contradicted + false_positives_count) > 0 else 1.0
            
            f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
            
            # 4. Claim Coverage
            claims_with_sources = sum(1 for v in verdicts if len(v.source_ids) > 0)
            coverage = claims_with_sources / len(verdicts) if len(verdicts) > 0 else 0.0
            
            # Regression check on case recall
            success = recall >= case.min_recall
            
            results.append(CaseResult(
                case_id=case.case_id,
                document_path=case.document_path,
                recall=recall,
                precision=precision,
                f1=f1,
                coverage=coverage,
                false_positives=false_positives_count,
                missed=missed,
                unverified=unverified,
                found=found_issues,
                latency_sec=latency,
                cost_usd=0.15,  # estimated API cost
                success=success
            ))
            
            print(f"✓ Case '{case.case_id}' finished: Recall={recall:.1%} Precision={precision:.1%} FP={false_positives_count}")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ Case '{case.case_id}' crashed: {e}")
            results.append(CaseResult(
                case_id=case.case_id,
                document_path=case.document_path,
                recall=0.0, precision=0.0, f1=0.0, coverage=0.0,
                false_positives=0, missed=[], unverified=[], found=[],
                latency_sec=0.0, cost_usd=0.0, success=False,
                error_msg=str(e)
            ))
            
    # Write aggregated report
    _write_report(results, time.time() - start_time_all)
    
    # Assert regression guards
    failed_guards = False
    for res in results:
        if not res.success:
            print(f"🚨 REGRESSION GUARD FAILED: case '{res.case_id}' recall {res.recall:.1%} < threshold!")
            failed_guards = True
            
    if failed_guards:
        print("❌ Evaluation Finished: FAILED due to regression guards.")
        sys.exit(1)
    else:
        print("🎉 Evaluation Finished: ALL PASSED!")
        sys.exit(0)

def _write_report(results: list[CaseResult], total_time: float):
    os.makedirs("output", exist_ok=True)
    report_path = "output/evaluation_report_latest.md"
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    lines = [
        "# 🔬 Zerde AI — Агентный Оценочный Отчёт",
        f"\n> **Дата запуска:** `{now_str}`",
        f"> **Общее время выполнения:** `{total_time:.1f} сек`\n",
        "## 📊 Сводные метрики по тестовым сценариям\n",
        "| ID сценария | Входной документ | Покрытие (Coverage) | Recall | Precision | F1-Score | Латентность | Статус |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    ]
    
    for r in results:
        status_str = "✅ PASSED" if r.success else "🚨 FAILED"
        if r.error_msg:
            status_str = f"💥 CRASHED ({r.error_msg})"
        doc_name = os.path.basename(r.document_path)
        lines.append(
            f"| `{r.case_id}` | [{doc_name}](file://{os.path.abspath(r.document_path)}) | "
            f"{r.coverage:.1%} | {r.recall:.1%} | {r.precision:.1%} | {r.f1:.1%} | "
            f"{r.latency_sec:.1f}s | **{status_str}** |"
        )
        
    lines.append("\n## 🔍 Детализация промахов и ложных срабатываний\n")
    
    for r in results:
        lines.append(f"### Тест-кейс: `{r.case_id}`")
        lines.append(f"- **Найдено ожидаемых дефектов:** `{len(r.found)}` {r.found}")
        if r.missed:
            lines.append(f"- ❌ **Пропущено дефектов (Missed):** `{len(r.missed)}` {r.missed}")
        else:
            lines.append("- ❌ **Пропущено дефектов (Missed):** `0` (Идеальный охват!)")
            
        if r.unverified:
            lines.append(f"- ⚠️ **Не верифицировано (Unverified):** `{len(r.unverified)}` {r.unverified}")
        if r.false_positives > 0:
            lines.append(f"- 👻 **Ложноположительные предупреждения (False Positives):** `{r.false_positives}`")
        lines.append("")
        
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        
    print(f"\n📝 Detailed evaluation report saved to: {report_path}")

if __name__ == "__main__":
    asyncio.run(run_evaluation())
