"""
SQLite Cache Manager
Кэш для чанков Адилет/Web и LLM-ответов (токен-экономия).
Ключ чанков: SHA256 контента. Ключ LLM: SHA256(model:prompt).
"""

from __future__ import annotations

import os
import sys

# Crucial CPU thread limits set at the absolute top BEFORE any numpy/torch imports
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["MKL_NUM_THREADS"] = "2"
os.environ["OPENBLAS_NUM_THREADS"] = "2"
os.environ["VECLIB_MAXIMUM_THREADS"] = "2"
os.environ["NUMEXPR_NUM_THREADS"] = "2"

# Force SentenceTransformers to CPU to completely bypass GPU VRAM crashes on 6GB VRAM GPUs (like RTX 4050),
# unless ZERDE_USE_CUDA=1 is explicitly set.
if os.getenv("ZERDE_USE_CUDA") != "1":
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

_db_lock = asyncio.Lock()

from zerde.models import EvidenceChunk

logger = logging.getLogger(__name__)


def _heal_chunk_rank(chunk: EvidenceChunk) -> EvidenceChunk:
    """Dynamically re-evaluates the legal_rank of a cached chunk to prevent outdated ranks."""
    if not chunk.source_url:
        return chunk
    try:
        from zerde.models import WebTier
        from zerde.utils.legal_scorer import infer_legal_rank_from_web_content
        tier = chunk.web_tier or WebTier.TIER_2
        new_rank, conf, reason = infer_legal_rank_from_web_content(
            tier=tier,
            title=chunk.source_title or "",
            content=chunk.content or "",
            url=chunk.source_url,
        )
        if chunk.legal_rank != new_rank:
            logger.info(
                f"[Cache/healing] Healed legal_rank of chunk {chunk.chunk_id[:12]}… "
                f"from {chunk.legal_rank} to {new_rank} (URL: {chunk.source_url})"
            )
            chunk.legal_rank = new_rank
            chunk.inferred_rank = new_rank
            chunk.inferred_rank_confidence = conf
            chunk.inference_reason = reason
    except Exception as e:
        logger.warning(f"[Cache/healing] Failed to heal chunk {chunk.chunk_id[:12]}…: {e}")
    return chunk

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS evidence_cache (
    chunk_id     TEXT PRIMARY KEY,
    source_url   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_json   TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_content_hash ON evidence_cache(content_hash);

CREATE TABLE IF NOT EXISTS evidence_embeddings (
    chunk_id   TEXT PRIMARY KEY,
    embedding  BLOB NOT NULL,
    FOREIGN KEY(chunk_id) REFERENCES evidence_cache(chunk_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_response_cache (
    cache_key     TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    response_json TEXT NOT NULL,
    cached_at     TEXT NOT NULL,
    expires_at    TEXT          -- NULL = permanent
);

CREATE INDEX IF NOT EXISTS idx_llm_expires ON llm_response_cache(expires_at);
"""


class CacheManager:
    """
    SQLite-based кэш для EvidenceChunk.
    Thread-safe через WAL mode. Ключ: chunk_id (SHA256 контента).
    """

    def __init__(self, db_path: str = "zerde_cache.db") -> None:
        import os
        env_db_path = os.getenv("ZERDE_CACHE_DB")
        self.db_path = Path(env_db_path if env_db_path else db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Инициализирует БД и создаёт таблицы если не существуют."""
        with self._conn() as conn:
            conn.executescript(_CREATE_TABLE_SQL)
        logger.debug(f"[Cache] DB initialized at {self.db_path}")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager для соединения с SQLite (WAL mode)."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def get(self, chunk_id: str) -> EvidenceChunk | None:
        """
        Читает чанк из кэша по chunk_id.

        Args:
            chunk_id: SHA256 хэш контента.

        Returns:
            EvidenceChunk или None если не найден.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT chunk_json FROM evidence_cache WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()

        if row is None:
            return None

        try:
            data = json.loads(row["chunk_json"])
            chunk = EvidenceChunk.model_validate(data)
            old_rank = chunk.legal_rank
            chunk = _heal_chunk_rank(chunk)
            if chunk.legal_rank != old_rank:
                try:
                    with self._conn() as conn:
                        conn.execute(
                            "UPDATE evidence_cache SET chunk_json = ? WHERE chunk_id = ?",
                            (chunk.model_dump_json(), chunk.chunk_id),
                        )
                except Exception as ex:
                    logger.warning(f"[Cache/get] Failed to update healed rank: {ex}")
            logger.debug(f"[Cache] HIT: {chunk_id[:12]}…")
            return chunk
        except Exception as e:
            logger.warning(f"[Cache] Failed to deserialize chunk {chunk_id[:12]}…: {e}")
            return None

    async def put(self, chunk: EvidenceChunk) -> None:
        """
        Сохраняет чанк в кэш.
        Игнорирует дубликаты (INSERT OR IGNORE).

        Args:
            chunk: EvidenceChunk для сохранения.
        """
        chunk_json = chunk.model_dump_json()
        async with _db_lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence_cache
                        (chunk_id, source_url, content_hash, chunk_json, cached_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.source_url,
                        chunk.chunk_id,  # chunk_id = SHA256 = content_hash
                        chunk_json,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        logger.debug(f"[Cache] STORED: {chunk.chunk_id[:12]}…")
        self._sync_new_chunks([chunk])

    async def put_many(self, chunks: list[EvidenceChunk]) -> int:
        """
        Батчевое сохранение чанков.

        Returns:
            Количество реально сохранённых (не дубликатов).
        """
        stored = 0
        async with _db_lock:
            with self._conn() as conn:
                for chunk in chunks:
                    result = conn.execute(
                        """
                        INSERT OR IGNORE INTO evidence_cache
                            (chunk_id, source_url, content_hash, chunk_json, cached_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.chunk_id,
                            chunk.source_url,
                            chunk.chunk_id,
                            chunk.model_dump_json(),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    stored += result.rowcount

        logger.info(f"[Cache] Batch stored {stored}/{len(chunks)} chunks")
        if stored > 0:
            self._sync_new_chunks(chunks)
        return stored

    async def has(self, chunk_id: str) -> bool:
        """Быстрая проверка наличия чанка в кэше."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM evidence_cache WHERE chunk_id = ? LIMIT 1",
                (chunk_id,),
            ).fetchone()
        return row is not None

    async def stats(self) -> dict:
        """Возвращает статистику кэша."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_chunks": total,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
        }

    async def clear(self) -> int:
        """Очищает весь кэш. Используй с осторожностью."""
        async with _db_lock:
            with self._conn() as conn:
                result = conn.execute("DELETE FROM evidence_cache")
                deleted = result.rowcount
        logger.warning(f"[Cache] CLEARED: {deleted} chunks deleted")
        return deleted

    def _stem_russian_word(self, w: str) -> str:
        if len(w) <= 4:
            return w
        endings = [
            "ями", "ами", "ому", "ему", "ого", "его", "ыми", "ими", "ых", "их", "ею", "ою",
            "ом", "ем", "ой", "ей", "ию", "ую", "яя", "ая", "ое", "ее", "ые", "ие", "ия", "ый", "ий", "ам", "ям", "ов", "ев", "ях", "ах",
            "а", "я", "о", "е", "и", "ы", "у", "ю", "ь"
        ]
        for end in endings:
            if w.endswith(end):
                if len(w) - len(end) >= 3:
                    return w[:-len(end)]
        return w

    def _tokenize_for_bm25(self, text: str) -> list[str]:
        import re
        text_lower = text.lower()
        # Удаляем пунктуацию (кроме дефиса в словах)
        text_clean = re.sub(r"[^\w\s\-]", " ", text_lower)
        words = [w.strip() for w in text_clean.split() if len(w.strip()) > 2]
        LEGAL_STOP_WORDS = {
            "закон", "кодекс", "статья", "статье", "статьи", "республики", "казахстан",
            "утратил", "силу", "вводится", "действие", "постановление", "правительства",
            "республика", "закона", "кодекса", "об", "о", "и", "в", "на", "для", "рк"
        }
        filtered = [self._stem_russian_word(w) for w in words if w not in LEGAL_STOP_WORDS]
        return filtered if filtered else [self._stem_russian_word(w) for w in words]

    def _lazy_init_embeddings(self) -> None:
        """Lazy loads BGE-M3 model and pre-loads all embeddings/BM25 index in memory."""
        if getattr(self, "_embeddings_loaded", False):
            return

        import threading
        if not hasattr(self, "_init_lock"):
            self._init_lock = threading.Lock()

        with self._init_lock:
            if getattr(self, "_embeddings_loaded", False):
                return
            
            logger.info("[Cache/Vector] Initializing BGE-M3 model and pre-loading embeddings...")
            
            import numpy as np
            
            # 1. Lazy load SentenceTransformers
            try:
                import torch
                torch.set_num_threads(2)
                
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                logger.error(f"[Cache/Vector] Failed to import sentence-transformers/torch: {e}")
                raise e

            # Use GPU only if ZERDE_USE_CUDA=1 and CUDA is available
            use_cuda = os.getenv("ZERDE_USE_CUDA") == "1"
            device = "cuda" if (use_cuda and torch.cuda.is_available()) else "cpu"
            logger.info(f"[Cache/Vector] Using device: {device} (Thread limit: {torch.get_num_threads()})")
            
            # Load BGE-M3 in FP16 if on CUDA to save VRAM, else FP32 on CPU
            model_kwargs = {}
            if device == "cuda":
                model_kwargs["torch_dtype"] = torch.float16
            
            self._embed_model = SentenceTransformer(
                "BAAI/bge-m3",
                device=device,
                model_kwargs=model_kwargs if model_kwargs else None
            )
            
            # 2. Pre-load all embeddings from SQLite
            self._embeddings_keys = []
            embeddings_list = []
            
            with self._conn() as conn:
                rows = conn.execute("SELECT chunk_id, embedding FROM evidence_embeddings").fetchall()
                for row in rows:
                    self._embeddings_keys.append(row["chunk_id"])
                    vec = np.frombuffer(row["embedding"], dtype=np.float32)
                    embeddings_list.append(vec)
                    
            if embeddings_list:
                self._embeddings_matrix = np.array(embeddings_list, dtype=np.float32)
                self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)
            else:
                self._embeddings_matrix = np.empty((0, 1024), dtype=np.float32)
                self._embeddings_norms = np.empty((0,), dtype=np.float32)
                
            # 3. Pre-load and construct BM25 index for all cached chunks
            logger.info("[Cache/Vector] Constructing BM25 index for all chunks...")
            self._bm25_keys = []
            tokenized_corpus = []
            
            with self._conn() as conn:
                rows = conn.execute("SELECT chunk_id, chunk_json FROM evidence_cache").fetchall()
                for row in rows:
                    try:
                        data = json.loads(row["chunk_json"])
                        content = data.get("content", "")
                        title = data.get("source_title", "")
                        tokens = self._tokenize_for_bm25(content + " " + title)
                        if tokens:
                            self._bm25_keys.append(row["chunk_id"])
                            tokenized_corpus.append(tokens)
                    except Exception as ex:
                        pass
                        
            if tokenized_corpus:
                from rank_bm25 import BM25Okapi
                self._bm25_index = BM25Okapi(tokenized_corpus)
                self._bm25_tokens = tokenized_corpus
            else:
                self._bm25_index = None
                self._bm25_tokens = []
                
            self._embeddings_loaded = True
            logger.info(f"[Cache/Vector] Initialized successfully. Loaded {len(self._embeddings_keys)} embeddings and {len(self._bm25_keys)} BM25 documents.")

    def _sync_new_chunks(self, chunks: list[EvidenceChunk]) -> None:
        """Dynamically syncs new chunks with in-memory embeddings and BM25 index at runtime."""
        if not getattr(self, "_embeddings_loaded", False):
            return

        import numpy as np
        
        new_chunk_ids = []
        new_texts = []
        
        for c in chunks:
            if c.chunk_id not in self._embeddings_keys:
                new_chunk_ids.append(c.chunk_id)
                new_texts.append((c.content or "") + " " + (c.source_title or ""))

        if not new_chunk_ids:
            return

        logger.info(f"[Cache/Vector] Dynamically syncing {len(new_chunk_ids)} new chunks in RAM...")

        try:
            # Generate normalized BGE-M3 embeddings
            embeddings = self._embed_model.encode(
                new_texts, 
                batch_size=8, 
                show_progress_bar=False, 
                normalize_embeddings=True
            )
            embeddings = np.array(embeddings, dtype=np.float32)
            
            # Store in SQLite evidence_embeddings
            with self._conn() as conn:
                for cid, emb in zip(new_chunk_ids, embeddings):
                    conn.execute(
                        "INSERT OR REPLACE INTO evidence_embeddings (chunk_id, embedding) VALUES (?, ?)",
                        (cid, emb.tobytes()),
                    )
            
            # Append to in-memory matrix
            self._embeddings_keys.extend(new_chunk_ids)
            if self._embeddings_matrix.shape[0] == 0:
                self._embeddings_matrix = embeddings
            else:
                self._embeddings_matrix = np.vstack([self._embeddings_matrix, embeddings])
            self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)
            
            # Rebuild BM25 index
            logger.info("[Cache/Vector] Rebuilding in-memory BM25 index...")
            tokenized_corpus = []
            self._bm25_keys = []
            with self._conn() as conn:
                rows = conn.execute("SELECT chunk_id, chunk_json FROM evidence_cache").fetchall()
                for row in rows:
                    try:
                        data = json.loads(row["chunk_json"])
                        content = data.get("content", "")
                        title = data.get("source_title", "")
                        tokens = self._tokenize_for_bm25(content + " " + title)
                        if tokens:
                            self._bm25_keys.append(row["chunk_id"])
                            tokenized_corpus.append(tokens)
                    except Exception:
                        pass
            
            if tokenized_corpus:
                from rank_bm25 import BM25Okapi
                self._bm25_index = BM25Okapi(tokenized_corpus)
                self._bm25_tokens = tokenized_corpus
                
        except Exception as e:
            logger.error(f"[Cache/Vector] Failed to dynamically sync new chunks: {e}")

    def embed_chunks(self, chunks: list[EvidenceChunk]) -> None:
        """Pre-computes and stores embeddings for a list of chunks in SQLite. Optimized for RTX 4050."""
        if not chunks:
            return

        import numpy as np
        self._lazy_init_embeddings()
        
        device_type = str(getattr(self._embed_model, "device", "cpu"))
        logger.info(f"[Cache/Vector] Encoding {len(chunks)} chunks in batches of 8 using BGE-M3...")
        texts = [(c.content or "") + " " + (c.source_title or "") for c in chunks]
        
        try:
            embeddings = self._embed_model.encode(
                texts,
                batch_size=8,
                show_progress_bar=True,
                normalize_embeddings=True
            )
            embeddings = np.array(embeddings, dtype=np.float32)
            if "cuda" in device_type:
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            
            logger.info(f"[Cache/Vector] Storing {len(chunks)} embeddings in SQLite...")
            with self._conn() as conn:
                for chunk, emb in zip(chunks, embeddings):
                    conn.execute(
                        "INSERT OR REPLACE INTO evidence_embeddings (chunk_id, embedding) VALUES (?, ?)",
                        (chunk.chunk_id, emb.tobytes()),
                    )
            logger.info("[Cache/Vector] Finished storing embeddings successfully.")
            
            # If loaded, sync in-memory matrix as well
            if getattr(self, "_embeddings_loaded", False):
                self._sync_new_chunks(chunks)
        except Exception as e:
            logger.error(f"[Cache/Vector] Failed to generate/store embeddings: {e}")
            raise e

    def get_embeddings(self, chunks: list[EvidenceChunk]) -> list[list[float]]:
        """Retrieves cached embeddings from SQLite or computes them locally via BGE-M3."""
        import numpy as np
        
        # Check cache first
        chunk_ids = [c.chunk_id for c in chunks]
        embeddings_map = {}
        
        with self._conn() as conn:
            # Batch queries by 999 (SQLite limit)
            for idx in range(0, len(chunk_ids), 999):
                batch_ids = chunk_ids[idx:idx+999]
                placeholders = ",".join(["?"] * len(batch_ids))
                rows = conn.execute(
                    f"SELECT chunk_id, embedding FROM evidence_embeddings WHERE chunk_id IN ({placeholders})",
                    tuple(batch_ids)
                ).fetchall()
                for row in rows:
                    vec = np.frombuffer(row["embedding"], dtype=np.float32)
                    embeddings_map[row["chunk_id"]] = vec.tolist()
                    
        # Identify missing chunks
        missing_chunks = [c for c in chunks if c.chunk_id not in embeddings_map]
        
        if missing_chunks:
            logger.info(f"[Cache/Vector] Computing embeddings for {len(missing_chunks)} chunks using BGE-M3...")
            self._lazy_init_embeddings()
            texts = [(c.content or "") + " " + (c.source_title or "") for c in missing_chunks]
            device_type = str(getattr(self._embed_model, "device", "cpu"))
            computed = self._embed_model.encode(
                texts,
                batch_size=8,
                show_progress_bar=False,
                normalize_embeddings=True
            )
            if "cuda" in device_type:
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            
            # Store in DB and populate map
            with self._conn() as conn:
                for chunk, emb in zip(missing_chunks, computed):
                    emb_float32 = emb.astype(np.float32)
                    conn.execute(
                        "INSERT OR REPLACE INTO evidence_embeddings (chunk_id, embedding) VALUES (?, ?)",
                        (chunk.chunk_id, emb_float32.tobytes()),
                    )
                    embeddings_map[chunk.chunk_id] = emb_float32.tolist()
                    
            # Update RAM representation if initialized
            if getattr(self, "_embeddings_loaded", False):
                self._sync_new_chunks(missing_chunks)
                
        # Return in original order
        return [embeddings_map[c.chunk_id] for c in chunks]

    async def search_local(
        self,
        query_text: str,
        law_ids: list[str] | None = None,
        articles: list[str] | None = None,
        limit: int = 10,
    ) -> list[EvidenceChunk]:
        """
        Parallel Hybrid Search (SQL Strategy 0 + BM25 + BGE-M3 Semantic) 
        fused via Reciprocal Rank Fusion (RRF k=60).
        """
        import json
        import re
        import numpy as np
        
        # 1. Normalize law_ids
        normalized_law_ids = []
        if law_ids:
            try:
                from zerde.stages.s3_gather import _LAW_ID_KNOWN
            except ImportError:
                _LAW_ID_KNOWN = {}
            for lid in law_ids:
                lid_norm = lid.strip().replace("\u0406", "I").replace("\u0456", "i").upper()
                normalized_law_ids.append(lid_norm)
                known_code = _LAW_ID_KNOWN.get(lid_norm)
                if known_code:
                    normalized_law_ids.append(known_code.upper())
                if known_code and known_code.endswith("_"):
                    normalized_law_ids.append(known_code[:-1].upper())

        # Normalize articles
        normalized_articles = []
        if articles:
            for art in articles:
                art_norm = art.strip().lower()
                if art_norm:
                    normalized_articles.append(art_norm)

        # ----------------------------------------------------
        # BRANCH A: Parallel Search 1 — Exact SQL Search (Strategy 0)
        # ----------------------------------------------------
        sql_candidates = []
        with self._conn() as conn:
            if normalized_law_ids:
                if normalized_articles:
                    law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                    art_conds = " OR ".join(["json_extract(chunk_json, '$.article') = ?" for _ in normalized_articles])
                    sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) AND ({art_conds}) LIMIT ?"
                    params = [f"%{lid}%" for lid in normalized_law_ids] + list(normalized_articles) + [limit * 2]
                else:
                    law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                    sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) LIMIT ?"
                    params = [f"%{lid}%" for lid in normalized_law_ids] + [limit * 2]
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    for r in rows:
                        data = json.loads(r["chunk_json"])
                        chunk = EvidenceChunk.model_validate(data)
                        sql_candidates.append(chunk.chunk_id)
                except sqlite3.OperationalError:
                    pass

        # ----------------------------------------------------
        # BRANCH B: Parallel Search 2 — Lexical BM25 Search
        # ----------------------------------------------------
        bm25_candidates = []
        query_tokens = self._tokenize_for_bm25(query_text)
        
        # Ensure BM25 index is lazy loaded
        self._lazy_init_embeddings()
        
        if self._bm25_index and query_tokens:
            try:
                bm25_scores = self._bm25_index.get_scores(query_tokens)
                top_indices = np.argsort(bm25_scores)[::-1][:limit * 3]
                for idx in top_indices:
                    if idx < len(self._bm25_tokens) and (set(query_tokens) & set(self._bm25_tokens[idx])):
                        bm25_candidates.append(self._bm25_keys[idx])
            except Exception as e:
                logger.warning(f"[Cache/search_local] BM25 scoring failed: {e}")

        # ----------------------------------------------------
        # BRANCH C: Parallel Search 3 — Semantic Vector Search (BGE-M3)
        # ----------------------------------------------------
        semantic_candidates = []
        if self._embeddings_matrix.shape[0] > 0:
            try:
                # 1. Embed query
                query_vector = self._embed_model.encode(
                    query_text, 
                    show_progress_bar=False, 
                    normalize_embeddings=True
                ).astype(np.float32)
                
                # 2. Vectorized Cosine Similarities calculation
                similarities = np.dot(self._embeddings_matrix, query_vector)
                
                # 3. Sort indices descending
                top_indices = np.argsort(similarities)[::-1][:limit * 3]
                for idx in top_indices:
                    if similarities[idx] > 0.45:
                        semantic_candidates.append(self._embeddings_keys[idx])
            except Exception as e:
                logger.error(f"[Cache/search_local] Semantic search failed: {e}")

        # Law ID priority boost
        law_id_boost = {}
        if normalized_law_ids:
            candidate_ids = set(sql_candidates + bm25_candidates + semantic_candidates)
            for cid in candidate_ids:
                chunk_obj = await self.get(cid)
                if chunk_obj and chunk_obj.law_id:
                    cid_law = chunk_obj.law_id.strip().upper()
                    if any(lid in cid_law for lid in normalized_law_ids):
                        law_id_boost[cid] = 5.0

        # ----------------------------------------------------
        # Strict AND Boost
        # ----------------------------------------------------
        strict_and_boost = {}
        query_set = set(query_tokens)
        if query_set:
            candidate_ids = set(sql_candidates + bm25_candidates + semantic_candidates)
            for cid in candidate_ids:
                chunk_obj = await self.get(cid)
                if chunk_obj:
                    chunk_tokens = set(self._tokenize_for_bm25((chunk_obj.content or "") + " " + (chunk_obj.source_title or "")))
                    if query_set.issubset(chunk_tokens):
                        strict_and_boost[cid] = 10.0

        # ----------------------------------------------------
        # Reciprocal Rank Fusion (RRF k=60)
        # ----------------------------------------------------
        rrf_scores = {}
        k = 60
        
        # SQL Exact Search gets very high priority
        for rank, cid in enumerate(sql_candidates):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.5 / (k + rank))
            
        for rank, cid in enumerate(bm25_candidates):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
            
        for rank, cid in enumerate(semantic_candidates):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))

        # Add strict AND boost
        for cid, boost in strict_and_boost.items():
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + boost

        # Add law ID boost
        for cid, boost in law_id_boost.items():
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + boost

        # Sort combined chunk_ids by RRF score descending
        sorted_rrf = sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        
        # Load full EvidenceChunk objects for the winners
        final_chunks = []
        for cid, score in sorted_rrf:
            chunk_obj = await self.get(cid)
            if chunk_obj:
                final_chunks.append(chunk_obj)

        logger.debug(f"[Cache] Local parallel hybrid search query='{query_text}' found={len(final_chunks)} chunks via RRF")
        return final_chunks




# ---------------------------------------------------------------------------
# LLM Response Cache
# ---------------------------------------------------------------------------


class LLMCache:
    """
    Кэш LLM-ответов по SHA256(model:prompt).
    Одна SQLite БД с CacheManager (llm_response_cache таблица).

    TTL политика:
      - None   = постоянный (Planner, Claim Extractor — детерминированы)
      - 86400  = 24 часа   (Auditor — корпус может обновиться)
    """

    def __init__(self, db_path: str = "zerde_cache.db") -> None:
        self.db_path = Path(db_path)
        # Убеждаемся что таблица создана
        with self._conn() as conn:
            conn.executescript(_CREATE_TABLE_SQL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _make_key(model: str, prompt: str) -> str:
        """SHA256(model:prompt) — детерминированный ключ кэша."""
        raw = f"{model}:{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def get(self, model: str, prompt: str) -> dict | None:
        """
        Возвращает кэшированный JSON-ответ или None.
        Автоматически проверяет TTL (Lazy Deletion).
        """
        key = self._make_key(model, prompt)
        now_iso = datetime.now(UTC).isoformat()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT response_json, expires_at FROM llm_response_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()

        if row is None:
            return None

        # Проверяем истечение TTL
        if row["expires_at"] and row["expires_at"] < now_iso:
            logger.debug(f"[LLMCache] EXPIRED (Lazy Deletion): {key[:12]}…")
            # Lazy Deletion: не удаляем физически во время get, а просто отдаем None
            return None

        try:
            data = json.loads(row["response_json"])
            logger.info(f"[LLMCache] HIT: {key[:12]}… (model={model.split('/')[-1]})")
            return data
        except Exception as e:
            logger.warning(f"[LLMCache] Deserialize error {key[:12]}…: {e}")
            return None

    async def put(
        self,
        model: str,
        prompt: str,
        response: dict,
        ttl_seconds: int | None = None,
    ) -> None:
        """
        Сохраняет LLM-ответ в кэш.

        Args:
            model: ID модели (для ключа и дебага).
            prompt: Полный промпт (все сообщения сериализованы).
            response: Parsed JSON dict от LLM.
            ttl_seconds: None = постоянный, int = TTL в секундах.
        """
        key = self._make_key(model, prompt)
        now = datetime.now(UTC).replace(tzinfo=None)
        expires = (
            (now + timedelta(seconds=ttl_seconds)).isoformat()
            if ttl_seconds
            else None
        )
        now_iso = datetime.now(UTC).isoformat()

        async with _db_lock:
            with self._conn() as conn:
                # Purge expired entries under the write lock
                conn.execute(
                    "DELETE FROM llm_response_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now_iso,),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO llm_response_cache
                        (cache_key, model, response_json, cached_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        model,
                        json.dumps(response, ensure_ascii=False),
                        now.isoformat(),
                        expires,
                    ),
                )
        logger.info(
            f"[LLMCache] STORED: {key[:12]}… "
            f"model={model.split('/')[-1]} ttl={ttl_seconds or 'permanent'}"
        )

    async def _delete(self, key: str) -> None:
        async with _db_lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM llm_response_cache WHERE cache_key = ?", (key,))

    async def invalidate_expired(self) -> int:
        """Удаляет все истёкшие записи. Вызывай при старте пайплайна."""
        now_iso = datetime.now(UTC).isoformat()
        async with _db_lock:
            with self._conn() as conn:
                result = conn.execute(
                    "DELETE FROM llm_response_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now_iso,),
                )
            deleted = result.rowcount
        if deleted:
            logger.info(f"[LLMCache] Purged {deleted} expired entries")
        return deleted

    async def stats(self) -> dict:
        """Статистика LLM кэша."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM llm_response_cache").fetchone()[0]
            permanent = conn.execute(
                "SELECT COUNT(*) FROM llm_response_cache WHERE expires_at IS NULL"
            ).fetchone()[0]
        return {
            "total_llm_entries": total,
            "permanent": permanent,
            "with_ttl": total - permanent,
        }

    async def clear_llm(self) -> int:
        """Очищает только LLM кэш (не трогает evidence_cache)."""
        async with _db_lock:
            with self._conn() as conn:
                result = conn.execute("DELETE FROM llm_response_cache")
            logger.warning(f"[LLMCache] LLM cache cleared: {result.rowcount} entries")
            return result.rowcount
