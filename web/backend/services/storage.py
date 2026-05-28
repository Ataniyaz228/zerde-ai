import os
import glob
import re
from datetime import datetime

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "output"))


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
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read(3000)
        except OSError:
            content = ""

        reports.append({
            "id": filename,
            "filename": filename,
            "date": _parse_date(filename),
            "reliability_score": _extract_score(content),
            "status": "done",
        })

    return sorted(reports, key=lambda x: x["date"], reverse=True)


def get_report(report_id: str) -> dict | None:
    # Sanitize: report_id must be a plain filename, no path traversal
    if os.sep in report_id or "/" in report_id or ".." in report_id:
        return None

    filepath = os.path.join(OUTPUT_DIR, report_id)
    if not os.path.exists(filepath):
        return None

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    score = _extract_score(content)

    return {
        "id": report_id,
        "content": content,
        "metadata": {
            "filename": report_id,
            "date": _parse_date(report_id),
            "reliability_score": score,
        },
    }
