"""
Heal UNKNOWN law_ids in the evidence cache by analyzing chunk content.
Also finds and fixes misidentified law_ids.
"""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.append(str(Path(__file__).parent.parent))

from zerde.models import EvidenceChunk, LegalRank
# Inline to avoid importing s3_gather which needs ddgs
_LAW_ID_KNOWN = {
    "94-V": "Z1300000094", "87-IV": "Z1300000094", "418-V": "Z1500000418",
    "370-II": "Z030000370_", "550-IV": "Z1300000550", "274-IV": "Z100000274_",
    "11-VI": "Z1600000011", "239-VII": "Z2500000239", "401-II": "Z0300000401",
    "73-V": "Z1300000073", "223-VIII": "Z1700000223", "240-IV": "Z1100000240",
    "148-II": "Z010000148_", "368-II": "Z030000368_", "95-IV": "Z080000095_",
    "138-IV": "Z060000138_", "413-IV": "Z1100000413", "414-IV": "Z1100000414",
    "235-V": "K1400000235", "226-V": "K1400000226", "231-V": "K1400000231",
    "377-V": "K1500000377", "214-VII": "K2500000214", "414-I-NEW": "K1500000414",
    "350-VI": "K2000000350", "212-IV": "K070000212_", "1000-XIII": "K940001000_",
    "409-I": "K990000409_", "442-II": "K030000442_", "414-I": "K150000414_",
    "226-V-UK": "K1400000226", "152-VII": "Z2200000152",
    "400-VI": "K210000400_", "375-V": "K1500000375_", "481-II": "K030000481_",
    "360-VI": "K200000360_", "125-VI": "K170000125_",
}


# Extended content-based law_id detection
_CONTENT_SIGNATURES = {
    # Кодексы
    "гражданский кодекс": ("1000-XIII", LegalRank.CODE),
    "гражданского кодекса": ("1000-XIII", LegalRank.CODE),
    "азаматтық кодексі": ("1000-XIII", LegalRank.CODE),  # каз
    "кодекс об административных правонарушениях": ("235-V", LegalRank.CODE),
    "әкімшілік құқық бұзушылық": ("235-V", LegalRank.CODE),  # каз КоАП
    "уголовный кодекс": ("226-V", LegalRank.CODE),
    "қылмыстық кодекс": ("226-V", LegalRank.CODE),  # каз УК
    "уголовно-процессуальный кодекс": ("231-V", LegalRank.CODE),
    "қылмыстық-процестік кодекс": ("231-V", LegalRank.CODE),  # каз УПК
    "гражданский процессуальный кодекс": ("377-V", LegalRank.CODE),
    "азаматтық процестік кодекс": ("377-V", LegalRank.CODE),  # каз ГПК
    "трудовой кодекс": ("414-I", LegalRank.CODE),
    "еңбек кодексі": ("414-I", LegalRank.CODE),  # каз ТК
    "налоговый кодекс": ("214-VII", LegalRank.CODE),
    "салық кодексі": ("214-VII", LegalRank.CODE),  # каз НК
    "кодекс о здоровье": ("360-VI", LegalRank.CODE),
    "халық денсаулығы": ("360-VI", LegalRank.CODE),  # каз
    "земельный кодекс": ("442-II", LegalRank.CODE),
    "жер кодексі": ("442-II", LegalRank.CODE),  # каз
    "предпринимательский кодекс": ("375-V", LegalRank.CODE),
    "кәсіпкерлік кодексі": ("375-V", LegalRank.CODE),  # каз
    "экологический кодекс": ("400-VI", LegalRank.CODE),
    "экологиялық кодекс": ("400-VI", LegalRank.CODE),  # каз
    "административный процедурно-процессуальный": ("350-VI", LegalRank.CODE),
    "әкімшілік рәсімдік-процестік": ("350-VI", LegalRank.CODE),  # каз АППК
    "водный кодекс": ("481-II", LegalRank.CODE),
    "су кодексі": ("481-II", LegalRank.CODE),  # каз
    "бюджетный кодекс": ("95-IV", LegalRank.CODE),
    "бюджет кодексі": ("95-IV", LegalRank.CODE),  # каз

    # Конституция
    "конституция республики казахстан": ("K950001000_", LegalRank.CONSTITUTIONAL_LAW),
    "қазақстан республикасының конституция": ("K950001000_", LegalRank.CONSTITUTIONAL_LAW),

    # Ключевые законы
    "о государственном имуществе": ("413-IV", LegalRank.LAW_RK),
    "мемлекеттік мүлік туралы": ("413-IV", LegalRank.LAW_RK),  # каз
    "о персональных данных": ("94-V", LegalRank.LAW_RK),
    "дербес деректер": ("94-V", LegalRank.LAW_RK),  # каз
    "о защите прав потребителей": ("274-IV", LegalRank.LAW_RK),
    "тұтынушылардың құқықтарын": ("274-IV", LegalRank.LAW_RK),  # каз
    "о платежах и платежных системах": ("11-VI", LegalRank.LAW_RK),
    "төлемдер және төлем жүйелері": ("11-VI", LegalRank.LAW_RK),  # каз
    "о республиканском бюджете": ("239-VII", LegalRank.LAW_RK),
    "республикалық бюджет туралы": ("239-VII", LegalRank.LAW_RK),  # каз
    "о частных судебных исполнителях": ("152-VII", LegalRank.LAW_RK),
    "жеке сот орындаушылары": ("152-VII", LegalRank.LAW_RK),  # каз
}

# Regex patterns for short IDs in content
_SHORT_ID_PATTERN = re.compile(r"№\s*(\d+-[IVXLCD]+)", re.IGNORECASE)
_ADILET_CODE_PATTERN = re.compile(r"\b([KZP]\d{9,10}_?)\b")


def detect_law_from_content(content: str, source_title: str = "") -> tuple[str | None, LegalRank | None]:
    """Detect law_id and legal_rank from chunk content."""
    combined = (source_title + " " + content[:2000]).lower()

    # 1. Check content signatures (longest first for specificity)
    sorted_sigs = sorted(_CONTENT_SIGNATURES.keys(), key=len, reverse=True)
    for sig in sorted_sigs:
        if sig in combined:
            return _CONTENT_SIGNATURES[sig]

    # 2. Check for short ID pattern like "№ 94-V"
    match = _SHORT_ID_PATTERN.search(content[:1000])
    if match:
        short_id = match.group(1).upper()
        if short_id in _LAW_ID_KNOWN:
            return short_id, LegalRank.LAW_RK
        return short_id, LegalRank.LAW_RK

    # 3. Check for Adilet code pattern like K1400000235
    match = _ADILET_CODE_PATTERN.search(content[:1000])
    if match:
        code = match.group(1)
        # Reverse lookup
        for short_id, adilet_code in _LAW_ID_KNOWN.items():
            if adilet_code.rstrip("_") == code.rstrip("_"):
                rank = LegalRank.CODE if code.startswith("K") else LegalRank.LAW_RK
                return short_id, rank
        return code, LegalRank.LAW_RK

    return None, None


def heal_db():
    db_path = "zerde_cache.db"
    print(f"🔧 Healing law_ids in {db_path}...")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load all UNKNOWN and None law_id chunks
    rows = conn.execute(
        "SELECT chunk_id, chunk_json FROM evidence_cache "
        "WHERE json_extract(chunk_json, '$.law_id') = 'UNKNOWN' "
        "   OR json_extract(chunk_json, '$.law_id') IS NULL"
    ).fetchall()

    print(f"Found {len(rows)} chunks to heal.")

    healed = 0
    failed = 0
    stats = Counter()

    for row in rows:
        data = json.loads(row["chunk_json"])
        content = data.get("content", "")
        title = data.get("source_title", "")

        new_law_id, new_rank = detect_law_from_content(content, title)

        if new_law_id:
            data["law_id"] = new_law_id
            if new_rank:
                data["legal_rank"] = new_rank.value
                data["inferred_rank"] = new_rank.value

            # Update source_url to include actual law_id
            old_url = data.get("source_url", "")
            if "UNKNOWN" in old_url:
                adilet_code = _LAW_ID_KNOWN.get(new_law_id, new_law_id)
                art = data.get("article", "")
                data["source_url"] = f"https://adilet.zan.kz/rus/docs/{adilet_code}#{art}"
                data["source_title"] = f"НПА {new_law_id} | Ст. {art}"

            conn.execute(
                "UPDATE evidence_cache SET chunk_json = ? WHERE chunk_id = ?",
                (json.dumps(data, ensure_ascii=False), row["chunk_id"]),
            )
            healed += 1
            stats[new_law_id] += 1
        else:
            failed += 1

    conn.commit()
    conn.close()

    print(f"\n✅ Healed: {healed}")
    print(f"❌ Could not identify: {failed}")
    print(f"\n--- Healed law_id distribution ---")
    for lid, count in stats.most_common(30):
        print(f"  {lid}: {count}")


if __name__ == "__main__":
    heal_db()
