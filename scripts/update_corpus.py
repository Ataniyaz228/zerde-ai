import argparse
import asyncio
import datetime
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

# Disable SSL verification for development/unverified environments
ssl._create_default_https_context = ssl._create_unverified_context

# User-Agent header to prevent blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_latest_adilet_version(adilet_id: str) -> str | None:
    """
    Fetches the history page of the NPA from Adilet and extracts the latest revision date.
    Returns date string in YYYY-MM-DD format, or None if failed.
    """
    url = f"https://adilet.zan.kz/rus/docs/{adilet_id}/history"
    print(f"   Querying Adilet history: {url}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")
        
        # Split by table rows
        tr_blocks = re.findall(r'<tr.*?>.*?</tr>', html, re.DOTALL)
        if not tr_blocks:
            print("   ⚠️ No table rows found in history page!")
            return None
            
        # Traversal from last row (most recent chronological)
        for row in reversed(tr_blocks):
            if adilet_id.lower() in row.lower():
                date_match = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', row)
                if date_match:
                    day, month, year = date_match.groups()
                    rev_date = f"{year}-{month}-{day}"
                    print(f"   Found latest revision date: {rev_date}")
                    return rev_date
                    
        print("   ⚠️ Could not find a row referencing the adilet_id in history table!")
        return None
    except Exception as e:
        print(f"   ❌ Error fetching/parsing history for {adilet_id}: {e}")
        return None

def download_file(url: str, dest_path: Path) -> bool:
    """
    Downloads file from URL and saves it to dest_path.
    """
    print(f"   Downloading: {url} -> {dest_path}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as response:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(response.read())
        print(f"   ✅ Successfully downloaded: {dest_path.name}")
        return True
    except Exception as e:
        print(f"   ❌ Error downloading from {url}: {e}")
        return False

def clean_database_chunks(db_path: str, law_id: str):
    """
    Deletes all chunks of a specific law_id from SQLite.
    Cascade delete automatically removes vector embeddings from evidence_embeddings.
    """
    if not os.path.exists(db_path):
        print(f"   [DB] Database {db_path} does not exist yet. No cleanup needed.")
        return
        
    print(f"   [DB] Cleaning old RAG chunks and embeddings for Law ID: {law_id}...")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON") # Essential for ON DELETE CASCADE
        with conn:
            # Delete by exact match or pattern (case insensitive)
            res = conn.execute(
                "DELETE FROM evidence_cache WHERE json_extract(chunk_json, '$.law_id') = ?",
                (law_id,)
            )
            deleted_count = res.rowcount
            print(f"   [DB] Deleted {deleted_count} stale cache rows for {law_id}.")
        conn.close()
    except Exception as e:
        print(f"   ❌ Error cleaning database for {law_id}: {e}")

def run_ingester():
    """
    Runs the document ingestion script as a separate process to avoid memory/CUDA leaks.
    """
    print("\n🚀 Running scripts/ingest_docs.py to re-vectorize and embed the corpus...")
    env = os.environ.copy()
    env["ZERDE_USE_CUDA"] = "1"
    try:
        subprocess.run([sys.executable, "scripts/ingest_docs.py"], env=env, check=True)
        print("🎉 Reference corpus ingestion and BGE-M3 embedding generation complete!")
        return True
    except Exception as e:
        print(f"❌ Ingestion subprocess failed: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Zerde AI RAG Automated Corpus Updater")
    parser.add_argument("--dry-run", action="store_true", help="Print updates without modifying files or DB")
    parser.add_argument("--adilet-id", type=str, help="Update only a specific NPA by Adilet ID (e.g. K1400000235)")
    parser.add_argument("--force", action="store_true", help="Force redownload and re-ingest all documents even if up to date")
    args = parser.parse_args()

    manifest_path = Path("docs/manifest.json")
    if not manifest_path.exists():
        print(f"❌ Manifest file {manifest_path} not found!")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    db_path = os.getenv("ZERDE_CACHE_DB", "zerde_cache.db")
    documents = manifest.get("documents", [])
    updates_made = 0

    print(f"🔍 Starting Adilet update check for {len(documents)} documents...")

    for doc in documents:
        law_id = doc.get("law_id")
        adilet_id = doc.get("adilet_id")
        title = doc.get("title")
        current_version = doc.get("version_date")

        # Filter by specific Adilet ID if requested
        if args.adilet_id and adilet_id != args.adilet_id:
            continue

        print(f"\n─────────────────────────────────────────────────────────────────")
        print(f"Checking: {title} (Law ID: {law_id}, Adilet: {adilet_id})")
        print(f"Current local version date: {current_version}")

        latest_version = get_latest_adilet_version(adilet_id)
        if not latest_version:
            print("   ⚠️ Could not fetch latest version from Adilet. Skipping.")
            continue

        if latest_version <= current_version and not args.force:
            print(f"   ✅ Up to date! (Local: {current_version} >= Remote: {latest_version})")
            continue

        if latest_version <= current_version and args.force:
            print(f"   🔄 [Force] Re-downloading and re-ingesting anyway (Local: {current_version} == Remote: {latest_version})")
        else:
            print(f"   🔔 Update detected! (Remote: {latest_version} > Local: {current_version})")

        if args.dry_run:
            print("   [Dry Run] Skipping file download and database cleanup.")
            continue

        # Prepare new files directory and filenames
        # Format date for filename: YYYY-MM-DD -> DD-MM-YYYY
        dt = datetime.datetime.strptime(latest_version, "%Y-%m-%d")
        file_date_str = dt.strftime("%d-%m-%Y")

        # Find first existing folder name from files
        existing_file_path = None
        for lang_key in ("ru", "kk"):
            path_str = doc["files"].get(lang_key)
            if path_str:
                existing_file_path = Path(path_str)
                break

        if not existing_file_path:
            print("   ❌ Error: No existing files found in manifest for directory lookup!")
            continue

        doc_dir = existing_file_path.parent
        new_files = {}
        download_success = True

        for lang in ("ru", "kk"):
            lang_suffix = "rus" if lang == "ru" else "kaz"
            # e.g., k1400000235.12-03-2026.rus.pdf
            new_filename = f"{adilet_id.lower()}.{file_date_str}.{lang_suffix}.pdf"
            new_file_path = doc_dir / new_filename
            
            download_url = f"https://adilet.zan.kz/{lang}/docs/{adilet_id}/download"
            success = download_file(download_url, new_file_path)
            if success:
                new_files[lang] = str(new_file_path)
            else:
                download_success = False

        if not download_success:
            print("   ❌ Error: One or more downloads failed. Aborting update for this document.")
            # Cleanup any partially downloaded new files
            for p in new_files.values():
                if os.path.exists(p):
                    os.unlink(p)
            continue

        # Success! Delete the old files
        for lang, old_path_str in doc["files"].items():
            old_path = Path(old_path_str)
            if old_path.exists():
                print(f"   Removing old version file: {old_path.name}")
                try:
                    old_path.unlink()
                except Exception as e:
                    print(f"   ⚠️ Warning: Could not delete old file {old_path}: {e}")

        # Clean old chunks from SQLite cache dynamically
        clean_database_chunks(db_path, law_id)

        # Update document in manifest in-memory representation
        doc["version_date"] = latest_version
        doc["files"] = new_files
        updates_made += 1

    if updates_made > 0:
        # Save manifest
        manifest["last_updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"\n📝 Saved manifest updates to {manifest_path}.")

        # Re-run ingestion to index new files
        run_ingester()
    else:
        print("\n🎉 All reference documents are already up to date. No updates needed.")

if __name__ == "__main__":
    main()
