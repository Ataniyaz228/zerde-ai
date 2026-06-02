#!/usr/bin/env python3
"""
Budget-aware CI gate for the false-confirm metric — PURE comparison, $0, no LLM.

scripts/eval/verdict_eval.py runs the REAL auditor (S5 + S6 with the LLM) and is
the only thing that costs API budget. THIS script never imports the pipeline and
never touches the network: it reads a committed BASELINE verdict report and a
FRESH verdict report (both produced by verdict_eval.py --json) and fails the run
if false_confirm regressed.

The north-star rule (CITE-OR-ABSTAIN): confidently confirming a false legal claim
is the worst error. So the gate is conservative — it FAILS when, for any law:
  - fresh false_confirm_rate  >  baseline false_confirm_rate   (a regression), OR
  - fresh false_confirm_rate  >  0                             (absolute ceiling).
The second rule means a law that is missing from the baseline can never pass with
a non-zero rate, and a baseline that was itself non-zero never licenses a regression.

Schema consumed (top-level keys of a verdict_eval.py report):
    law, canonical, false_confirm_rate, true_confirm_rate,
    planted_total, planted_confirmed, results[...]
A report may instead be {"error": "..."} — treated as a hard FAIL for that law.

Usage:
  # single file vs single file
  PYTHONPATH=. python scripts/eval/verdict_gate.py \
      --baseline scripts/eval/baseline_235-V.json --fresh data/eval/verdict_235-V.json

  # directory of per-law JSONs (verdict_eval writes one per law, e.g. verdict_235-V.json)
  PYTHONPATH=. python scripts/eval/verdict_gate.py \
      --baseline-dir scripts/eval/baselines --fresh-dir data/eval

Exit code: 0 = PASS, 1 = FAIL (false-confirm regression / ceiling breach / load error).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Absolute ceiling: the north-star metric must stay at 0 for every law, always.
MAX_ALLOWED_FALSE_CONFIRM = 0.0


def _load(path: str | Path) -> dict:
    """Load one verdict report. Raises on missing/invalid JSON."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def _law_of(report: dict, fallback: str) -> str:
    """The law short id a report is about; fall back to the filename stem."""
    return str(report.get("law") or report.get("canonical") or fallback)


def _reports_from_dir(directory: str | Path) -> dict[str, dict]:
    """Map law_id -> report for every verdict report in a directory (one per law).

    Only `verdict_*.json` / `baseline_*.json` are considered: a verdict report is
    what verdict_eval.py --json emits. Other JSON in the same dir (e.g. run_eval's
    label-free `results_*.json`) is unrelated and must not be misread as a verdict
    report — that would fail the gate on files it was never meant to grade.

    Keyed by the report's own `law` field when present, else the filename stem
    (with the "verdict_" / "baseline_" prefix stripped) so a baseline file and a
    fresh file for the same law line up regardless of naming.
    """
    out: dict[str, dict] = {}
    paths = sorted(
        set(Path(directory).glob("verdict_*.json"))
        | set(Path(directory).glob("baseline_*.json"))
    )
    for p in paths:
        stem = p.stem
        for prefix in ("verdict_", "baseline_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        try:
            rep = _load(p)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # A broken JSON in the fresh set must surface as a failure, not be skipped.
            out[stem] = {"error": f"load failed: {exc}"}
            continue
        out[_law_of(rep, stem)] = rep
    return out


def _rate(report: dict) -> float | None:
    """false_confirm_rate as a float, or None if the report is an error/malformed."""
    if "error" in report:
        return None
    val = report.get("false_confirm_rate")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def compare(baselines: dict[str, dict], fresh: dict[str, dict]) -> dict:
    """Pure comparison. Returns a structured result with per-law rows + a verdict.

    A law FAILS if its fresh false_confirm_rate is unreadable (error/malformed),
    exceeds the absolute ceiling (>0), or exceeds its baseline rate. A law present
    only in the baseline is reported but does not by itself fail the gate (it was
    simply not re-run); a law only in the fresh set is judged against the ceiling.
    """
    rows: list[dict] = []
    any_fail = False

    for law in sorted(set(baselines) | set(fresh)):
        base_rep = baselines.get(law)
        fresh_rep = fresh.get(law)

        base_rate = _rate(base_rep) if base_rep is not None else None
        fresh_rate = _rate(fresh_rep) if fresh_rep is not None else None

        reasons: list[str] = []
        if fresh_rep is None:
            # Baseline-only: not re-run this time. Informational, not a failure.
            status = "SKIP"
        elif fresh_rate is None:
            status = "FAIL"
            reasons.append("fresh report missing/invalid false_confirm_rate")
        else:
            if fresh_rate > MAX_ALLOWED_FALSE_CONFIRM:
                reasons.append(
                    f"false_confirm_rate {fresh_rate} > ceiling {MAX_ALLOWED_FALSE_CONFIRM}"
                )
            if base_rate is not None and fresh_rate > base_rate:
                reasons.append(
                    f"false_confirm_rate {fresh_rate} > baseline {base_rate} (regression)"
                )
            status = "FAIL" if reasons else "PASS"

        if status == "FAIL":
            any_fail = True

        rows.append({
            "law": law,
            "baseline_rate": base_rate,
            "fresh_rate": fresh_rate,
            "status": status,
            "reasons": reasons,
        })

    return {"rows": rows, "passed": not any_fail}


def _fmt_rate(val: float | None) -> str:
    return "  n/a" if val is None else f"{val:>5.3f}"


def print_report(result: dict) -> None:
    rows = result["rows"]
    print("\n" + "=" * 78)
    print("VERDICT GATE — false_confirm_rate: fresh vs committed baseline")
    print("-" * 78)
    print(f"  {'law':<14} {'baseline':>8} {'fresh':>8}  {'status':<6} reason")
    print("  " + "-" * 74)
    for r in rows:
        reason = "; ".join(r["reasons"])
        print(f"  {r['law']:<14} {_fmt_rate(r['baseline_rate']):>8} "
              f"{_fmt_rate(r['fresh_rate']):>8}  {r['status']:<6} {reason}")
    print("-" * 78)
    verdict = "PASS" if result["passed"] else "FAIL"
    print(f"GATE: {verdict}   (ceiling {MAX_ALLOWED_FALSE_CONFIRM}; "
          f"fail on any rise over baseline)")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", metavar="PATH", help="committed baseline verdict JSON (single law)")
    ap.add_argument("--fresh", metavar="PATH", help="fresh verdict JSON to check (single law)")
    ap.add_argument("--baseline-dir", metavar="DIR", help="dir of committed baseline per-law JSONs")
    ap.add_argument("--fresh-dir", metavar="DIR", help="dir of fresh per-law JSONs")
    args = ap.parse_args(argv)

    single = bool(args.baseline or args.fresh)
    multi = bool(args.baseline_dir or args.fresh_dir)
    if single and multi:
        ap.error("use either --baseline/--fresh OR --baseline-dir/--fresh-dir, not both")
    if not single and not multi:
        ap.error("nothing to compare: pass --baseline/--fresh or --baseline-dir/--fresh-dir")

    try:
        if multi:
            if not (args.baseline_dir and args.fresh_dir):
                ap.error("--baseline-dir and --fresh-dir must be given together")
            baselines = _reports_from_dir(args.baseline_dir)
            fresh = _reports_from_dir(args.fresh_dir)
        else:
            if not (args.baseline and args.fresh):
                ap.error("--baseline and --fresh must be given together")
            base_rep = _load(args.baseline)
            fresh_rep = _load(args.fresh)
            stem_b = Path(args.baseline).stem
            stem_f = Path(args.fresh).stem
            law = _law_of(fresh_rep, _law_of(base_rep, stem_f or stem_b))
            baselines = {law: base_rep}
            fresh = {law: fresh_rep}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR loading inputs: {exc}", file=sys.stderr)
        return 1

    if not fresh:
        print("ERROR: no fresh verdict reports to check.", file=sys.stderr)
        return 1

    result = compare(baselines, fresh)
    print_report(result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
