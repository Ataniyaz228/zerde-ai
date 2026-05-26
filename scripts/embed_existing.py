import os
import sys
import asyncio
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from zerde.utils.cache import CacheManager
from zerde.models import EvidenceChunk

async def embed_existing_cache():
    """
    Pre-computes and stores BGE-M3 vector embeddings for all ingested chunks in the DB.
    Ensures zero runtime embedding overhead on query execution.
    """
    db_path = "zerde_cache.db"
    print(f"🧠 Checking SQLite Cache database: {db_path}...")
    
    if not os.path.exists(db_path):
        print(f"❌ Error: Database file {db_path} not found. Please run ingest_docs.py first.")
        return
        
    cache = CacheManager(db_path)
    
    # Load all chunks that don't have embeddings yet
    all_chunks = []
    with cache._conn() as conn:
        rows = conn.execute("SELECT chunk_json FROM evidence_cache").fetchall()
        print(f"Found {len(rows)} total reference chunks in cache database.")
        
        # Check how many already have embeddings
        emb_rows = conn.execute("SELECT chunk_id FROM evidence_embeddings").fetchall()
        embedded_ids = {r["chunk_id"] for r in emb_rows}
        print(f"Found {len(embedded_ids)} pre-existing embeddings in database.")
        
        import json
        for r in rows:
            try:
                data = json.loads(r["chunk_json"])
                chunk = EvidenceChunk.model_validate(data)
                if chunk.chunk_id not in embedded_ids:
                    all_chunks.append(chunk)
            except Exception as e:
                print(f"⚠️ Error parsing chunk json: {e}")
                
    if not all_chunks:
        print("🎉 All reference chunks are already fully embedded! Nothing to do.")
        return
        
    print(f"🧠 Generating BGE-M3 vector embeddings for {len(all_chunks)} remaining chunks...")
    
    # SentenceTransformer BGE-M3 uses ZERDE_USE_CUDA=1 or CUDA execution if available
    # Run embed_chunks (it lazy-inits SentenceTransformer model and pre-computes embeddings)
    try:
        cache.embed_chunks(all_chunks)
        print("🎉 Successfully generated and stored BGE-M3 vector embeddings in SQLite!")
    except Exception as e:
        print(f"❌ Error generating embeddings: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(embed_existing_cache())
