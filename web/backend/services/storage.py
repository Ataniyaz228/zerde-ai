import glob
import json
import os
import re
from datetime import datetime

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "output"))


def _read_meta(filepath: str) -> dict:
    """Load the `<report>.meta.json` sidecar (verdict counts, reliability).

    Returns {} when the sidecar is missing (old reports predate it).
    """
    try:
        with open(filepath + ".meta.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _read_score(filepath: str, content: str, meta: dict | None = None) -> int:
    """Reliability score for a report.

    Prefers the structural sidecar `<report>.meta.json` written by the
    pipeline; falls back to regex-scraping the Markdown for old reports
    that predate the sidecar.
    """
    meta = _read_meta(filepath) if meta is None else meta
    pct = meta.get("reliability_pct")
    if pct is not None:
        return int(pct)
    return _extract_score(content)


def _verdict_counts(meta: dict) -> dict:
    """Verdict breakdown for the UI dashboard, with safe defaults."""
    return {
        "confirmed": int(meta.get("confirmed") or 0),
        "contradicted": int(meta.get("contradicted") or 0),
        "unverified": int(meta.get("unverified") or 0),
        "total": int(meta.get("total") or 0),
        "coverage_pct": int(meta.get("coverage_pct") or 0),
    }


def _parse_date(filename: str) -> str:
    """Parse date from filename and return ISO-8601 string."""
    match = re.search(r"(\d{8})_(\d{6})", filename)
    if not match:
        return datetime.now().isoformat()
    d, t = match.group(1), match.group(2)
    try:
        dt = datetime(
            int(d[:4]), int(d[4:6]), int(d[6:8]),
            int(t[:2]), int(t[2:4]), int(t[4:6]),
        )
        return dt.isoformat()
    except ValueError:
        return datetime.now().isoformat()


def _extract_score(content: str) -> int:
    # Matches: > **Надёжность анализа:** 🔴 5%
    score_match = re.search(
        r"Надёжность анализа:\*\*?\s*(?:🔴|🟡|🟢)?\s*(\d+)%",
        content,
    )
    if score_match:
        return int(score_match.group(1))
    # Simpler fallback
    score_match2 = re.search(r"Надёжность[^\d]*(\d+)%", content)
    if score_match2:
        return int(score_match2.group(1))
    return 0


def list_reports() -> list[dict]:
    reports = []
    if not os.path.exists(OUTPUT_DIR):
        return reports

    for filepath in glob.glob(os.path.join(OUTPUT_DIR, "*.md")):
        filename = os.path.basename(filepath)
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read(3000)
        except OSError:
            content = ""

        meta = _read_meta(filepath)
        reports.append({
            "id": filename,
            "filename": filename,
            "date": _parse_date(filename),
            "reliability_score": _read_score(filepath, content, meta),
            "status": "done",
            **_verdict_counts(meta),
        })

    return sorted(reports, key=lambda x: x["date"], reverse=True)


def get_report(report_id: str) -> dict | None:
    # Sanitize: report_id must be a plain filename, no path traversal
    if os.sep in report_id or "/" in report_id or ".." in report_id:
        return None

    filepath = os.path.join(OUTPUT_DIR, report_id)
    if not os.path.exists(filepath):
        return None

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    meta = _read_meta(filepath)
    score = _read_score(filepath, content, meta)

    return {
        "id": report_id,
        "content": content,
        "metadata": {
            "filename": report_id,
            "date": _parse_date(report_id),
            "reliability_score": score,
            **_verdict_counts(meta),
        },
    }
