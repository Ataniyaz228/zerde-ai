import asyncio
import os
import sys
import hashlib
import re
from pathlib import Path
import fitz  # pymupdf
import docx  # python-docx

# Add project root to sys.path so we can import zerde modules
sys.path.append(str(Path(__file__).parent.parent))

from zerde.models import EvidenceChunk, LegalRank, WebTier
from zerde.utils.cache import CacheManager
from zerde.stages.s3_gather import _regex_split_articles, _LAW_ID_KNOWN

# Reverse lookup for known codes from s3_gather
_CODE_TO_LAW_ID = {v.upper(): k for k, v in _LAW_ID_KNOWN.items()}

def detect_law_id_from_path(file_path: Path) -> str:
    filename = file_path.name.lower()
    parent_name = file_path.parent.name.lower()
    
    # Check if this is Constitution of RK
    if "конституция" in filename or "конституция" in parent_name:
        if "k950001000" in filename:
            return "K950001000_"
        elif "k2600000000" in filename:
            return "K2600000000_"
        return "K950001000_"
        
    # Search for known full codes (like K1400000235) in filename
    for code, short_id in _CODE_TO_LAW_ID.items():
        if code.lower() in filename or code.lower().replace("_", "") in filename:
            return short_id
            
    # Fallback to extracting short ID patterns from name (e.g. 235-V)
    match = re.search(r"(\d+-[ivxldc]+)", filename, re.IGNORECASE)
    if match:
        return match.group(1).upper()
        
    # Check parent folder for code
    for code, short_id in _CODE_TO_LAW_ID.items():
        if code.lower() in parent_name or code.lower().replace("_", "") in parent_name:
            return short_id
            
    return "UNKNOWN"

def extract_pdf_text(path: Path) -> str:
    doc = fitz.open(str(path))
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    doc.close()
    return "\n".join(pages)

def extract_docx_text(path: Path) -> str:
    doc = docx.Document(str(path))
    paragraphs = []
    for para in doc.paragraphs:
        paragraphs.append(para.text)
    for table in doc.tables:
        for row in table.rows:
            row_text = [cell.text for cell in row.cells]
            paragraphs.append(" | ".join(row_text))
    return "\n".join(paragraphs)

async def ingest_all_docs():
    print("🚀 Starting local document ingestion to zerde_cache.db...")
    cache = CacheManager("zerde_cache.db")
    docs_dir = Path("docs")
    
    # Find all reference files, ignoring drafts/test bills
    exclude_files = {"draft_law.txt", "zerde_test_bill_rk_2025.docx", "проект_закона_коап_каз.docx"}
    
    all_chunks = []
    
    for path in docs_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() in exclude_files:
            print(f"Skipping test/draft file: {path.name}")
            continue
            
        suffix = path.suffix.lower()
        if suffix not in (".pdf", ".docx"):
            continue
            
        print(f"\nProcessing reference file: {path}")
        law_id = detect_law_id_from_path(path)
        print(f"Detected Law ID: {law_id}")
        
        try:
            if suffix == ".pdf":
                text = extract_pdf_text(path)
            else:
                text = extract_docx_text(path)
                
            print(f"Extracted {len(text)} characters. Splitting into articles...")
            
            articles = _regex_split_articles(text)
            if not articles:
                # If regex fails, split by paragraphs/pages to ensure we get chunks
                print("⚠️ Regex article split returned 0 articles. Falling back to paragraph chunks.")
                paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
                for idx, p in enumerate(paragraphs):
                    articles.append({
                        "article_num": f"p{idx+1}",
                        "content": p[:3000]
                    })
                    
            print(f"Generated {len(articles)} chunks/articles.")
            
            is_code = law_id.startswith("K") or "кодекс" in str(path).lower() or law_id in ("235-V", "226-V", "350-VI", "212-IV", "1000-XIII", "409-I", "442-II", "414-I")
            rank = LegalRank.CODE if is_code else LegalRank.LAW_RK
            
            for art in articles:
                content = art["content"]
                art_num = art["article_num"]
                
                chunk_id = hashlib.sha256(content.encode()).hexdigest()
                chunk = EvidenceChunk(
                    chunk_id=chunk_id,
                    source_url=f"https://adilet.zan.kz/rus/docs/{law_id}#{art_num}",
                    source_title=f"НПА {law_id} | Ст. {art_num}",
                    content=content,
                    legal_rank=rank,
                    law_id=law_id,
                    article=art_num,
                )
                all_chunks.append(chunk)
                
        except Exception as e:
            print(f"❌ Error processing {path}: {e}")
            import traceback
            traceback.print_exc()
            
    if all_chunks:
        print(f"\nStoring {len(all_chunks)} chunks to sqlite cache...")
        stored = await cache.put_many(all_chunks)
        print(f"🎉 Stored {stored} chunks in zerde_cache.db successfully!")
    else:
        print("\nNo chunks found to store!")

if __name__ == "__main__":
    asyncio.run(ingest_all_docs())
