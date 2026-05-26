import asyncio
import os

# Limit CPU threads to 2 to prevent system lagging and IDE crashes on user's machine
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["MKL_NUM_THREADS"] = "2"
os.environ["OPENBLAS_NUM_THREADS"] = "2"
os.environ["VECLIB_MAXIMUM_THREADS"] = "2"
os.environ["NUMEXPR_NUM_THREADS"] = "2"

# Force PyTorch to use CPU to completely bypass CUDA Out Of Memory errors on 6GB VRAM GPU,
# unless ZERDE_USE_CUDA=1 is explicitly requested.
if os.getenv("ZERDE_USE_CUDA") != "1":
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

import sys
import json
import gc
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from zerde.models import EvidenceChunk
from zerde.utils.cache import CacheManager

async def embed_existing():
    print("🚀 Initializing CacheManager and loading all cached chunks...")
    cache = CacheManager("zerde_cache.db")
    
    # 1. Query all chunks
    chunks = []
    with cache._conn() as conn:
        rows = conn.execute("SELECT chunk_json FROM evidence_cache").fetchall()
        for r in rows:
            try:
                data = json.loads(r["chunk_json"])
                chunks.append(EvidenceChunk.model_validate(data))
            except Exception:
                pass
                
    print(f"Loaded {len(chunks)} chunks from database.")
    
    if not chunks:
        print("No chunks found to embed.")
        return
        
    device_name = "GPU (CUDA)" if os.getenv("ZERDE_USE_CUDA") == "1" else "CPU (limited to 2 threads)"
    print(f"\n🧠 Generating BGE-M3 vector embeddings for all chunks on {device_name}...")
    
    is_cuda = os.getenv("ZERDE_USE_CUDA") == "1"
    chunk_size = 1000 if is_cuda else 50
    sleep_time = 0.1 if is_cuda else 1.0
    
    loop = asyncio.get_running_loop()
    
    for idx in range(0, len(chunks), chunk_size):
        batch = chunks[idx : idx + chunk_size]
        print(f"Processing chunk batch {idx // chunk_size + 1}/{(len(chunks) - 1) // chunk_size + 1} (items {idx} to {idx + len(batch)})...")
        
        # Run in thread pool to prevent blocking event loop
        await loop.run_in_executor(None, cache.embed_chunks, batch)
        
        gc.collect()
        await asyncio.sleep(sleep_time) # Rest between batches (minimal for GPU, longer for CPU)
        
    print("\n🎉 All chunks successfully embedded and stored in database!")
    
    # Verify
    with cache._conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM evidence_embeddings").fetchone()[0]
        print(f"Total stored embeddings in DB: {count}")

if __name__ == "__main__":
    # Explicitly set PyTorch threads before importing
    try:
        import torch
        torch.set_num_threads(2)
    except ImportError:
        pass
    asyncio.run(embed_existing())
