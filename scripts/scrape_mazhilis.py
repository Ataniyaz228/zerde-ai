"""
Скачивает законопроекты с mazhilis.parlam.kz через публичный API.

API endpoints (обнаружено в main.js Angular-бандла):
  - GET /api/core/draft-laws/?limit=N&offset=M  → список с пагинацией
  - GET /api/core/draft-laws/{id}/              → детальный JSON с documents[]

Сохраняет файлы в data/corpus/<id>/<filename>.docx и индекс CSV.

Запуск:
    python scripts/scrape_mazhilis.py --limit 50
    python scripts/scrape_mazhilis.py --limit 411 --offset 0   # всё
    python scripts/scrape_mazhilis.py --id 922                 # один проект
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://mazhilis.parlam.kz/api/core/draft-laws/"
USER_AGENT = "Mozilla/5.0 (zerde-corpus-scraper)"
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN = 0.4  # вежливая задержка


def _get(url: str, *, timeout: int = REQUEST_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str) -> dict:
    return json.loads(_get(url).decode("utf-8"))


def fetch_list(limit: int, offset: int) -> dict:
    url = f"{API_BASE}?limit={limit}&offset={offset}"
    return _get_json(url)


def fetch_detail(law_id: int) -> dict:
    return _get_json(f"{API_BASE}{law_id}/")


def _filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name or "document.bin"


def download_documents(detail: dict, out_dir: Path) -> list[Path]:
    """Скачивает все documents[] в out_dir/<law_id>/. Возвращает список путей."""
    law_id = detail["id"]
    law_dir = out_dir / str(law_id)
    law_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for doc in detail.get("documents", []):
        url = doc.get("document_url")
        if not url:
            continue
        # HTTP → HTTPS если возможно
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        fname = _filename_from_url(url)
        dest = law_dir / fname
        if dest.exists() and dest.stat().st_size > 0:
            paths.append(dest)
            continue
        try:
            data = _get(url)
            dest.write_bytes(data)
            paths.append(dest)
        except Exception as e:
            print(f"  [warn] failed {url}: {e}", file=sys.stderr)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="Сколько проектов скачать")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--id", type=int, default=None, help="Скачать только один проект по id")
    ap.add_argument("--out-dir", type=Path, default=Path("data/corpus"))
    ap.add_argument("--index-csv", type=Path, default=Path("data/corpus/_index.csv"))
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.index_csv.parent.mkdir(parents=True, exist_ok=True)

    if args.id is not None:
        ids_to_fetch = [args.id]
    else:
        print(f"Fetching list: limit={args.limit} offset={args.offset}")
        listing = fetch_list(args.limit, args.offset)
        total = listing.get("count", 0)
        ids_to_fetch = [item["id"] for item in listing.get("result", [])]
        print(f"  total in catalog: {total}, fetched ids: {len(ids_to_fetch)}")

    rows = []
    for i, law_id in enumerate(ids_to_fetch, 1):
        try:
            detail = fetch_detail(law_id)
        except Exception as e:
            print(f"[{i}/{len(ids_to_fetch)}] id={law_id} ERROR fetch detail: {e}", file=sys.stderr)
            rows.append({"id": law_id, "title": "", "status": "", "files": "", "error": str(e)})
            continue

        paths = download_documents(detail, args.out_dir)
        title = (detail.get("title") or "").replace("\n", " ").strip()
        print(f"[{i}/{len(ids_to_fetch)}] id={law_id} files={len(paths)}  {title[:70]}")
        rows.append({
            "id": law_id,
            "title": title,
            "status": detail.get("status"),
            "files": ";".join(str(p.relative_to(args.out_dir.parent.parent) if p.is_absolute() else p) for p in paths),
            "error": "",
        })
        time.sleep(SLEEP_BETWEEN)

    # Пишем/обновляем CSV (merge по id)
    existing: dict[int, dict] = {}
    if args.index_csv.exists():
        with args.index_csv.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    existing[int(r["id"])] = r
                except (ValueError, KeyError):
                    pass
    for r in rows:
        existing[int(r["id"])] = r

    with args.index_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "status", "files", "error"])
        writer.writeheader()
        for rid in sorted(existing):
            writer.writerow(existing[rid])

    print(f"\nIndex written: {args.index_csv} ({len(existing)} rows total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
