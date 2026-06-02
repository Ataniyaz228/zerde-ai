"""Unit tests for scripts/eval/verdict_gate.py — the budget-aware false-confirm gate.

$0, no LLM, no network: every fixture is a synthetic verdict report (the schema
emitted by scripts/eval/verdict_eval.py --json) written to a tmp file or passed
in-memory. Run with: PYTHONPATH=. .venv/bin/python -m pytest tests/test_verdict_gate.py -q
"""

import json

import pytest

from scripts.eval.verdict_gate import MAX_ALLOWED_FALSE_CONFIRM, compare, main


def report(law: str, false_confirm: float, *, true_confirm: float = 1.0) -> dict:
    """A minimal verdict_eval-shaped report for one law."""
    return {
        "law": law,
        "canonical": law,
        "chunks_loaded": 100,
        "real_article_used": "1",
        "false_confirm_rate": false_confirm,
        "true_confirm_rate": true_confirm,
        "planted_total": 3,
        "planted_confirmed": int(round(false_confirm * 3)),
        "results": [],
    }


# --------------------------------------------------------------------------- #
# compare(): pure logic
# --------------------------------------------------------------------------- #

def test_ceiling_is_zero():
    assert MAX_ALLOWED_FALSE_CONFIRM == 0.0


def test_pass_when_equal_and_zero():
    base = {"235-V": report("235-V", 0.0)}
    fresh = {"235-V": report("235-V", 0.0)}
    res = compare(base, fresh)
    assert res["passed"] is True
    assert res["rows"][0]["status"] == "PASS"


def test_pass_when_fresh_lower_than_baseline():
    # Baseline was non-zero (legacy), fresh dropped to 0 — improvement, passes.
    base = {"235-V": report("235-V", 0.333)}
    fresh = {"235-V": report("235-V", 0.0)}
    res = compare(base, fresh)
    assert res["passed"] is True
    assert res["rows"][0]["status"] == "PASS"


def test_fail_when_false_confirm_rises_above_baseline():
    base = {"235-V": report("235-V", 0.0)}
    fresh = {"235-V": report("235-V", 0.333)}
    res = compare(base, fresh)
    assert res["passed"] is False
    row = res["rows"][0]
    assert row["status"] == "FAIL"
    assert any("regression" in r or "ceiling" in r for r in row["reasons"])


def test_fail_when_any_law_has_nonzero_even_if_at_or_below_baseline():
    # Both baseline and fresh are 0.25 — no regression, but the absolute ceiling
    # (>0) is breached, so the gate must still FAIL. The north-star is hard 0.
    base = {"214-VII": report("214-VII", 0.25)}
    fresh = {"214-VII": report("214-VII", 0.25)}
    res = compare(base, fresh)
    assert res["passed"] is False
    assert res["rows"][0]["status"] == "FAIL"


def test_fail_when_one_of_several_laws_regresses():
    base = {"235-V": report("235-V", 0.0), "214-VII": report("214-VII", 0.0)}
    fresh = {"235-V": report("235-V", 0.0), "214-VII": report("214-VII", 0.25)}
    res = compare(base, fresh)
    assert res["passed"] is False
    by_law = {r["law"]: r for r in res["rows"]}
    assert by_law["235-V"]["status"] == "PASS"
    assert by_law["214-VII"]["status"] == "FAIL"


def test_baseline_only_law_is_skip_not_fail():
    # A law in baseline but not re-run this time should not fail the gate.
    base = {"235-V": report("235-V", 0.0), "999-X": report("999-X", 0.0)}
    fresh = {"235-V": report("235-V", 0.0)}
    res = compare(base, fresh)
    assert res["passed"] is True
    by_law = {r["law"]: r for r in res["rows"]}
    assert by_law["999-X"]["status"] == "SKIP"


def test_fresh_only_law_judged_against_ceiling():
    # New law absent from baseline: 0 passes, >0 fails on the ceiling alone.
    res_ok = compare({}, {"500-VI": report("500-VI", 0.0)})
    assert res_ok["passed"] is True
    res_bad = compare({}, {"500-VI": report("500-VI", 0.1)})
    assert res_bad["passed"] is False


def test_error_report_in_fresh_fails():
    base = {"235-V": report("235-V", 0.0)}
    fresh = {"235-V": {"error": "no cached chunks for law 235-V"}}
    res = compare(base, fresh)
    assert res["passed"] is False
    assert res["rows"][0]["status"] == "FAIL"


def test_missing_rate_field_in_fresh_fails():
    base = {"235-V": report("235-V", 0.0)}
    bad = report("235-V", 0.0)
    del bad["false_confirm_rate"]
    res = compare(base, {"235-V": bad})
    assert res["passed"] is False
    assert res["rows"][0]["status"] == "FAIL"


# --------------------------------------------------------------------------- #
# main(): file/dir wiring + exit codes
# --------------------------------------------------------------------------- #

def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def test_main_single_file_pass(tmp_path):
    b = tmp_path / "baseline_235-V.json"
    f = tmp_path / "verdict_235-V.json"
    _write(b, report("235-V", 0.0))
    _write(f, report("235-V", 0.0))
    assert main(["--baseline", str(b), "--fresh", str(f)]) == 0


def test_main_single_file_fail_on_regression(tmp_path):
    b = tmp_path / "baseline_235-V.json"
    f = tmp_path / "verdict_235-V.json"
    _write(b, report("235-V", 0.0))
    _write(f, report("235-V", 0.333))
    assert main(["--baseline", str(b), "--fresh", str(f)]) == 1


def test_main_dir_mode_pass(tmp_path):
    bdir = tmp_path / "baselines"
    fdir = tmp_path / "fresh"
    bdir.mkdir()
    fdir.mkdir()
    for law in ("235-V", "214-VII"):
        _write(bdir / f"verdict_{law}.json", report(law, 0.0))
        _write(fdir / f"verdict_{law}.json", report(law, 0.0))
    assert main(["--baseline-dir", str(bdir), "--fresh-dir", str(fdir)]) == 0


def test_main_dir_mode_fail_when_one_law_breaches(tmp_path):
    bdir = tmp_path / "baselines"
    fdir = tmp_path / "fresh"
    bdir.mkdir()
    fdir.mkdir()
    _write(bdir / "verdict_235-V.json", report("235-V", 0.0))
    _write(fdir / "verdict_235-V.json", report("235-V", 0.0))
    _write(bdir / "verdict_214-VII.json", report("214-VII", 0.0))
    _write(fdir / "verdict_214-VII.json", report("214-VII", 0.25))
    assert main(["--baseline-dir", str(bdir), "--fresh-dir", str(fdir)]) == 1


def test_main_dir_mode_matches_files_by_law_field_not_filename(tmp_path):
    # Baseline uses a "baseline_" prefix, fresh uses "verdict_"; the report's own
    # `law` field must still line them up.
    bdir = tmp_path / "baselines"
    fdir = tmp_path / "fresh"
    bdir.mkdir()
    fdir.mkdir()
    _write(bdir / "baseline_235-V.json", report("235-V", 0.0))
    _write(fdir / "verdict_235-V.json", report("235-V", 0.333))
    res = main(["--baseline-dir", str(bdir), "--fresh-dir", str(fdir)])
    assert res == 1  # regression detected => the two files were paired


def test_main_corrupt_fresh_json_in_dir_fails(tmp_path):
    bdir = tmp_path / "baselines"
    fdir = tmp_path / "fresh"
    bdir.mkdir()
    fdir.mkdir()
    _write(bdir / "verdict_235-V.json", report("235-V", 0.0))
    (fdir / "verdict_235-V.json").write_text("{ not valid json", encoding="utf-8")
    assert main(["--baseline-dir", str(bdir), "--fresh-dir", str(fdir)]) == 1


def test_main_missing_single_baseline_file_returns_1(tmp_path):
    f = tmp_path / "verdict_235-V.json"
    _write(f, report("235-V", 0.0))
    assert main(["--baseline", str(tmp_path / "nope.json"), "--fresh", str(f)]) == 1


def test_main_rejects_mixing_single_and_dir(tmp_path):
    f = tmp_path / "verdict_235-V.json"
    _write(f, report("235-V", 0.0))
    with pytest.raises(SystemExit):
        main(["--fresh", str(f), "--fresh-dir", str(tmp_path)])


def test_main_requires_some_input():
    with pytest.raises(SystemExit):
        main([])
