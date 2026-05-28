import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, UTC, timedelta
from pathlib import Path
from typing import Generator


import numpy as np
from sentence_transformers import SentenceTransformer
import torch

from zerde.models import EvidenceChunk, LegalRank, WebTier

logger = logging.getLogger(__name__)

_db_lock = asyncio.Lock()
_STEM_CACHE = {}

# Версия LLM-кэша. Bump при изменении промптов/контракта ответа, чтобы
# инвалидировать устаревшие закэшированные ответы (входит в _make_key).
PROMPT_CACHE_VERSION = 1

# ───────────────────────────────────────────────────────────────────────────
# Fix #3: BGE-M3 Синглтон на уровне модуля
# Все инстансы CacheManager разделяют одну модель через глобальные переменные
# уровня модуля. Решает "Broken pipe" при параллельных subprocess.
# ───────────────────────────────────────────────────────────────────────────
_BGE_LOCK = threading.Lock()
_BGE_MODEL: SentenceTransformer | None = None
_BGE_DEVICE: str | None = None


def _get_bge_model() -> tuple[SentenceTransformer, str]:
    """Возвращает (model, device) — создаёт единжды на процесс."""
    global _BGE_MODEL, _BGE_DEVICE
    if _BGE_MODEL is not None:
        return _BGE_MODEL, _BGE_DEVICE  # type: ignore[return-value]
    with _BGE_LOCK:
        if _BGE_MODEL is not None:
            return _BGE_MODEL, _BGE_DEVICE  # type: ignore[return-value]
        device = "cuda" if (torch.cuda.is_available() and os.getenv("ZERDE_USE_CUDA") == "1") else "cpu"
        logger.info(f"[BGE-M3/Singleton] Инициализация BGE-M3 (device={device})...")
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else {}
        _BGE_MODEL = SentenceTransformer(
            "BAAI/bge-m3",
            device=device,
            model_kwargs=model_kwargs if model_kwargs else None
        )
        _BGE_MODEL.max_seq_length = 2048
        _BGE_DEVICE = device
        logger.info(f"[BGE-M3/Singleton] Готово на {device}")
    return _BGE_MODEL, _BGE_DEVICE  # type: ignore[return-value]

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS evidence_cache (
    chunk_id     TEXT PRIMARY KEY,
    source_url   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_json   TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_embeddings (
    chunk_id  TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
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

CREATE TABLE IF NOT EXISTS law_metadata (
    law_id      TEXT PRIMARY KEY,  -- короткий ID: "261-IV"
    adilet_code TEXT,              -- полный код Адилет: "Z100000261_"
    title_ru    TEXT,              -- заголовок на русском
    title_kz    TEXT,              -- заголовок на казахском
    chunk_count INTEGER DEFAULT 0, -- кол-во чанков в evidence_cache
    updated_at  TEXT               -- ISO timestamp последнего обновления
);
"""


class CacheManager:
    """
    SQLite-based кэш для EvidenceChunk.
    Thread-safe через WAL mode. Ключ: chunk_id (SHA256 контента).
    """

    def __init__(self, db_path: str = "zerde_cache.db") -> None:
        env_db_path = os.getenv("ZERDE_CACHE_DB")
        self.db_path = Path(env_db_path if env_db_path else db_path)
        self._shared_conn = None
        self._init_db()
        self._morph = None
        self._reranker = None
        self.deserialization_failures = 0

    def _init_db(self) -> None:
        """Инициализирует БД и создаёт таблицы если не существуют."""
        if self._shared_conn is None:
            self._shared_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._shared_conn.execute("PRAGMA journal_mode=WAL")
            self._shared_conn.row_factory = sqlite3.Row
        with self._shared_conn:
            self._shared_conn.executescript(_CREATE_TABLE_SQL)
        logger.debug(f"[Cache] DB initialized at {self.db_path}")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager для соединения с SQLite (WAL mode)."""
        if getattr(self, "_shared_conn", None) is None:
            self._shared_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._shared_conn.execute("PRAGMA journal_mode=WAL")
            self._shared_conn.row_factory = sqlite3.Row
        try:
            yield self._shared_conn
            self._shared_conn.commit()
        except Exception:
            self._shared_conn.rollback()
            raise

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
            self.deserialization_failures += 1
            logger.error(
                f"[Cache] Failed to deserialize chunk {chunk_id[:12]}… (total failures: {self.deserialization_failures}): {e}. Potential DB corruption!"
            )
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
        if stored > 0:
            logger.info(f"[Cache] Batch stored {stored} new chunks.")
            self._sync_new_chunks(chunks)
        return stored

    def upsert_law_metadata(
        self,
        law_id: str,
        adilet_code: str = "",
        title_ru: str = "",
        title_kz: str = "",
        chunk_count: int = 0,
    ) -> None:
        """
        Обновляет или вставляет запись в law_metadata.

        Args:
            law_id: Короткий ID закона ("261-IV").
            adilet_code: Полный код Адилет ("Z100000261_").
            title_ru: Заголовок на русском.
            title_kz: Заголовок на казахском.
            chunk_count: Количество чанков в evidence_cache.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO law_metadata (law_id, adilet_code, title_ru, title_kz, chunk_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(law_id) DO UPDATE SET
                    adilet_code  = COALESCE(NULLIF(excluded.adilet_code, ''), law_metadata.adilet_code),
                    title_ru     = COALESCE(NULLIF(excluded.title_ru, ''), law_metadata.title_ru),
                    title_kz     = COALESCE(NULLIF(excluded.title_kz, ''), law_metadata.title_kz),
                    chunk_count  = chunk_count + excluded.chunk_count,
                    updated_at   = excluded.updated_at
                """,
                (
                    law_id,
                    adilet_code,
                    title_ru,
                    title_kz,
                    chunk_count,
                    datetime.now(UTC).isoformat(),
                ),
            )
        logger.debug(f"[Cache] law_metadata upserted: {law_id}")

    def get_law_registry_rows(self) -> list[dict]:
        """
        Возвращает все записи из law_metadata.

        Returns:
            Список словарей {law_id, adilet_code, title_ru, title_kz, chunk_count}.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT law_id, adilet_code, title_ru, title_kz, chunk_count FROM law_metadata"
            ).fetchall()
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        """Возвращает статистику по кэшу EvidenceChunk и их эмбеддингам."""
        with self._conn() as conn:
            total_chunks = conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
            total_embeddings = conn.execute("SELECT COUNT(*) FROM evidence_embeddings").fetchone()[0]
            law_count = conn.execute("SELECT COUNT(*) FROM law_metadata").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "total_chunks": total_chunks,
            "total_embeddings": total_embeddings,
            "law_registry_entries": law_count,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
        }

    def embed_chunks(self, chunks: list[EvidenceChunk]) -> None:
        """
        Pre-computes and stores vector embeddings for EvidenceChunks in batch.
        Uses SentenceTransformer BGE-M3 model on configured hardware (GPU/CPU).
        
        Args:
            chunks: List of EvidenceChunk objects to embed.
        """
        if not chunks:
            return

        self._lazy_init_embeddings()
        import numpy as np

        chunk_ids = [c.chunk_id for c in chunks]
        # Concat content and title to maximize context retrieval representation
        texts = [(c.content or "") + " " + (c.source_title or "") for c in chunks]

        logger.info(f"[Cache/Vector] Running BGE-M3 batch embedding for {len(chunks)} chunks...")
        try:
            embeddings = self._embed_model.encode(
                texts,
                batch_size=32,
                show_progress_bar=True,
                normalize_embeddings=True
            )
            embeddings = np.array(embeddings, dtype=np.float32)

            # Store in SQLite database
            with self._conn() as conn:
                for cid, emb in zip(chunk_ids, embeddings):
                    conn.execute(
                        "INSERT OR REPLACE INTO evidence_embeddings (chunk_id, embedding) VALUES (?, ?)",
                        (cid, emb.tobytes()),
                    )
            logger.info(f"[Cache/Vector] Stored {len(chunks)} embeddings in DB.")
            
            # Sync to in-memory representation!
            new_keys = []
            new_embs = []
            for cid, emb in zip(chunk_ids, embeddings):
                if cid not in self._embeddings_keys:
                    self._embeddings_keys.append(cid)
                    new_keys.append(cid)
                    new_embs.append(emb)
            
            if new_embs:
                if self._embeddings_matrix.shape[0] == 0:
                    self._embeddings_matrix = np.array(new_embs, dtype=np.float32)
                else:
                    self._embeddings_matrix = np.vstack([self._embeddings_matrix, np.array(new_embs, dtype=np.float32)])
                self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)

            # Sync in-memory BM25 index dynamically
            for c in chunks:
                if c.chunk_id not in self._bm25_keys:
                    tokens = self._tokenize_for_bm25((c.content or "") + " " + (c.source_title or ""))
                    if tokens:
                        self._bm25_keys.append(c.chunk_id)
                        self._bm25_tokens.append(tokens)
            
            if self._bm25_tokens:
                from rank_bm25 import BM25Okapi
                self._bm25_index = BM25Okapi(self._bm25_tokens)
        except Exception as e:
            logger.error(f"[Cache/Vector] Batch embedding generation failed: {e}")
            raise

    def get_embeddings(self, chunks: list[EvidenceChunk]) -> list[np.ndarray]:
        """
        Retrieves or generates embeddings for a list of EvidenceChunks.
        Returns a list of numpy arrays, one for each chunk.
        """
        if not chunks:
            return []

        self._lazy_init_embeddings()
        
        # Ensure they are synced first
        self._sync_new_chunks(chunks)
        
        # Map each chunk_id to its index in self._embeddings_keys to get the vector from self._embeddings_matrix
        results = []
        for c in chunks:
            try:
                idx = self._embeddings_keys.index(c.chunk_id)
                results.append(self._embeddings_matrix[idx])
            except ValueError:
                # Fallback: if not found, generate on the fly
                text = (c.content or "") + " " + (c.source_title or "")
                emb = self._embed_model.encode(
                    [text], 
                    show_progress_bar=False, 
                    normalize_embeddings=True
                )[0]
                emb = np.array(emb, dtype=np.float32)
                results.append(emb)
                
        return results

    def _stem_word(self, w: str) -> str:
        if w in _STEM_CACHE:
            return _STEM_CACHE[w]

        # Lazy load pymorphy3 analyzer
        if self._morph is None:
            try:
                import pymorphy3
                self._morph = pymorphy3.MorphAnalyzer()
            except ImportError:
                pass

        res = w
        # Check if contains Russian letters
        if self._morph is not None and any(c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя" for c in w):
            parsed = self._morph.parse(w)
            if parsed:
                res = str(parsed[0].normal_form)
        else:
            if len(w) <= 4:
                res = w
            else:
                endings = [
                    "ями", "ами", "ому", "ему", "ого", "его", "ыми", "ими", "ых", "их", "ею", "ою",
                    "ом", "ем", "ой", "ей", "ию", "ую", "яя", "ая", "ое", "ее", "ые", "ие", "ия", "ый", "ий", "ам", "ям", "ов", "ев", "ях", "ах",
                    "а", "я", "о", "е", "и", "ы", "у", "ю", "ь",
                    # Казахские агглютинативные окончания
                    "ға", "ге", "ғе", "да", "де", "на", "ні", "ның", "нің",
                    "ды", "ді", "ты", "ті", "лар", "лер", "дар", "дер",
                    "мен", "сен", "оны", "оні", "біз", "сіз",
                    "ған", "ген", "ке", "қа", "ші", "шы",
                ]
                for end in endings:
                    if w.endswith(end):
                        if len(w) - len(end) >= 3:
                            res = w[:-len(end)]
                            break
        _STEM_CACHE[w] = res
        return res

    def _tokenize_for_bm25(self, text: str) -> list[str]:
        import re
        text_lower = text.lower()
        # Удаляем пунктуацию (кроме дефиса в словах)
        text_clean = re.sub(r"[^\w\s\-]", " ", text_lower)
        words = [w.strip() for w in text_clean.split() if len(w.strip()) > 2]
        LEGAL_STOP_WORDS = {
            # Русские
            "закон", "кодекс", "статья", "статье", "статьи", "республики", "казахстан",
            "утратил", "силу", "вводится", "действие", "постановление", "правительства",
            "республика", "закона", "кодекса", "об", "о", "и", "в", "на", "для", "рк",
            # Казахские
            "заң", "кодексі", "бап", "баптың", "туралы", "және", "қазақстан",
            "республикасы", "заңы", "үкіметі", "заңда", "үшін", "жәнінде",
            "болады", "емес", "және", "немесе", "қорған", "рәсімі",
        }
        filtered = [self._stem_word(w) for w in words if w not in LEGAL_STOP_WORDS]
        return filtered if filtered else [self._stem_word(w) for w in words]

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
            # Fix #3: Используем глобальный синглтон вместо создания новой модели
            self._embed_model, device = _get_bge_model()
            self._device_type = device
            
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
                batch_size=32, 
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

            # Sync in-memory BM25 index dynamically
            for c in chunks:
                tokens = self._tokenize_for_bm25((c.content or "") + " " + (c.source_title or ""))
                if tokens:
                    self._bm25_keys.append(c.chunk_id)
                    self._bm25_tokens.append(tokens)
            
            if self._bm25_tokens:
                from rank_bm25 import BM25Okapi
                self._bm25_index = BM25Okapi(self._bm25_tokens)
                
            logger.info(f"[Cache/Vector] Dynamic sync complete. New active keys count: {len(self._embeddings_keys)}")
        except Exception as e:
            logger.error(f"[Cache/Vector] Dynamic sync failed: {e}")

    def _lazy_init_reranker(self) -> None:
        """Lazy loads the BAAI/bge-reranker-v2-m3 Cross-Encoder model in memory."""
        if getattr(self, "_reranker_loaded", False):
            return

        import threading
        if not hasattr(self, "_reranker_lock"):
            self._reranker_lock = threading.Lock()

        with self._reranker_lock:
            if getattr(self, "_reranker_loaded", False):
                return

            logger.info("[Cache/Reranker] Initializing BAAI/bge-reranker-v2-m3 cross-encoder model...")
            device = "cuda" if (torch.cuda.is_available() and os.getenv("ZERDE_USE_CUDA") == "1") else "cpu"
            logger.info(f"[Cache/Reranker] Using Cross-Encoder device: {device}")

            try:
                from sentence_transformers import CrossEncoder
                model_kwargs = {}
                if device == "cuda":
                    model_kwargs["torch_dtype"] = torch.float16

                self._reranker = CrossEncoder(
                    "BAAI/bge-reranker-v2-m3",
                    device=device,
                    model_kwargs=model_kwargs if model_kwargs else None
                )
                self._reranker_loaded = True
                logger.info("[Cache/Reranker] Cross-Encoder initialized successfully!")
            except Exception as e:
                logger.error(f"[Cache/Reranker] Cross-Encoder initialization failed: {e}")
                self._reranker = None
                self._reranker_loaded = False

    async def search_local(
        self,
        query_text: str,
        law_ids: list[str] | None = None,
        articles: list[str] | None = None,
        limit: int = 10,
    ) -> list[EvidenceChunk]:
        """
        Parallel Hybrid Search (SQL Strategy 0 + BM25 + BGE-M3 Semantic) 
        fused via Weighted Linear Combination (WLC) and reranked using BAAI/bge-reranker-v2-m3.
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
                    params = [f"%{lid}%" for lid in normalized_law_ids] + list(normalized_articles) + [limit * 3]
                else:
                    law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                    sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) LIMIT ?"
                    params = [f"%{lid}%" for lid in normalized_law_ids] + [limit * 3]
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
        bm25_raw_scores = {}
        query_tokens = self._tokenize_for_bm25(query_text)

        # Ensure BM25 index is lazy loaded
        self._lazy_init_embeddings()

        if self._bm25_index and query_tokens:
            try:
                bm25_scores = self._bm25_index.get_scores(query_tokens)
                for idx, score in enumerate(bm25_scores):
                    cid = self._bm25_keys[idx]
                    if set(query_tokens) & set(self._bm25_tokens[idx]):
                        bm25_raw_scores[cid] = float(score)
                bm25_candidates = sorted(bm25_raw_scores.keys(), key=lambda k: bm25_raw_scores[k], reverse=True)[:limit * 3]
            except Exception as e:
                logger.warning(f"[Cache/search_local] BM25 scoring failed: {e}")

        # ----------------------------------------------------
        # BRANCH C: Parallel Search 3 — Semantic Vector Search (BGE-M3)
        # ----------------------------------------------------
        semantic_candidates = []
        semantic_raw_scores = {}
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

                # 3. Collect candidates above cosine threshold
                for idx, score in enumerate(similarities):
                    cid = self._embeddings_keys[idx]
                    if score >= -1.0:
                        semantic_raw_scores[cid] = float(score)
                semantic_candidates = sorted(semantic_raw_scores.keys(), key=lambda k: semantic_raw_scores[k], reverse=True)[:limit * 3]
            except Exception as e:
                logger.error(f"[Cache/search_local] Semantic search failed: {e}")

        # ----------------------------------------------------
        # WLC Score Fusion
        # ----------------------------------------------------
        candidate_ids = set(sql_candidates + bm25_candidates + semantic_candidates)
        if not candidate_ids:
            return []

        # MinMax normalize BM25 scores
        bm25_vals = list(bm25_raw_scores.values())
        if bm25_vals:
            min_bm25 = min(bm25_vals)
            max_bm25 = max(bm25_vals)
            bm25_range = max_bm25 - min_bm25
        else:
            min_bm25, bm25_range = 0.0, 1.0

        # MinMax normalize Semantic scores
        semantic_vals = list(semantic_raw_scores.values())
        if semantic_vals:
            min_sem = min(semantic_vals)
            max_sem = max(semantic_vals)
            sem_range = max_sem - min_sem
        else:
            min_sem, sem_range = 0.0, 1.0

        # Compute combined WLC score for each candidate
        wlc_scores = {}
        for cid in candidate_ids:
            # 1. SQL match score (binary 0 or 1)
            sql_score = 1.0 if cid in sql_candidates else 0.0

            # 2. BM25 score normalized
            bm25_raw = bm25_raw_scores.get(cid, 0.0)
            bm25_norm = (bm25_raw - min_bm25) / bm25_range if bm25_range > 0 else 0.0
            if bm25_raw <= 0:
                bm25_norm = 0.0

            # 3. Semantic score normalized
            sem_raw = semantic_raw_scores.get(cid, 0.0)
            sem_norm = (sem_raw - min_sem) / sem_range if sem_range > 0 else 0.0
            if sem_raw <= -1.0:
                sem_norm = 0.0

            # Weighted Linear Combination: 55% Semantic, 30% BM25, 15% SQL match (Calibrated weights)
            wlc_scores[cid] = 0.55 * sem_norm + 0.30 * bm25_norm + 0.15 * sql_score

        # Simple query language detection
        has_cyrillic = any('\u0400' <= ch <= '\u04FF' for ch in query_text)
        has_kaz_chars = any(ch in "әғқңөұүһіӘҒҚҢӨҰҮҺІ" for ch in query_text)
        detected_lang = "kk" if has_kaz_chars else ("ru" if has_cyrillic else None)

        # Load chunks and adjust WLC scores with language matching and version freshness
        adjusted_wlc = []
        for cid, score in sorted(wlc_scores.items(), key=lambda item: item[1], reverse=True)[:50]:
            chunk_obj = await self.get(cid)
            if not chunk_obj:
                continue
            
            # Language match boost
            lang_factor = 1.0
            if detected_lang and chunk_obj.language:
                if chunk_obj.language != detected_lang:
                    lang_factor = 0.85  # Slight penalty for cross-lingual matches, preserves BGE-M3 cross-lingual capability
            
            # Version freshness boost (recent version gets a slight boost)
            version_boost = 0.0
            if chunk_obj.source_version:
                try:
                    # e.g. "2026-03-11" -> parse year
                    year = int(chunk_obj.source_version.split("-")[0])
                    version_boost = max(0.0, (year - 2020) * 0.01)
                except Exception:
                    pass
                    
            adjusted_score = score * lang_factor + version_boost
            adjusted_wlc.append((chunk_obj, adjusted_score))

        # Sort adjusted candidates and take top 30
        adjusted_wlc.sort(key=lambda x: x[1], reverse=True)
        top_candidates = [chunk for chunk, _ in adjusted_wlc[:30]]

        # ----------------------------------------------------
        # Cross-Encoder Reranking (BAAI/bge-reranker-v2-m3)
        # ----------------------------------------------------
        self._lazy_init_reranker()

        if getattr(self, "_reranker", None) and top_candidates:
            try:
                logger.info(f"[Cache/search_local] Reranking {len(top_candidates)} candidates using BAAI/bge-reranker-v2-m3...")
                # Format pairs: [query, passage]
                pairs = []
                for chunk in top_candidates:
                    passage = (chunk.content or "") + " " + (chunk.source_title or "")
                    pairs.append([query_text, passage])

                # Compute scores using CrossEncoder predict
                rerank_scores = self._reranker.predict(pairs)
                if hasattr(rerank_scores, "tolist"):
                    rerank_scores = rerank_scores.tolist()
                elif isinstance(rerank_scores, float):
                    rerank_scores = [rerank_scores]

                # Combine candidates and their scores
                candidate_scored = list(zip(top_candidates, rerank_scores))
                candidate_scored.sort(key=lambda x: x[1], reverse=True)

                # Log top-3 reranker scores for debugging
                for i, (chunk, score) in enumerate(candidate_scored[:3]):
                    logger.debug(f"[Cache/Reranker] Top-{i+1} chunk={chunk.chunk_id[:12]} score={score:.4f} content={chunk.content[:100]}…")

                final_chunks = [chunk for chunk, _ in candidate_scored[:limit]]
            except Exception as e:
                logger.error(f"[Cache/search_local] Reranking failed: {e}. Falling back to WLC sorting.")
                final_chunks = top_candidates[:limit]
        else:
            final_chunks = top_candidates[:limit]

        return final_chunks


class LLMCache:
    """Кэш ответов LLM (SQLite)."""

    def __init__(self, db_path: str = "zerde_cache.db") -> None:
        self.db_path = db_path
        self._shared_conn = None
        self._init_db()

    def _init_db(self) -> None:
        if self._shared_conn is None:
            self._shared_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._shared_conn.execute("PRAGMA journal_mode=WAL")
            self._shared_conn.row_factory = sqlite3.Row
        with self._shared_conn:
            self._shared_conn.executescript(_CREATE_TABLE_SQL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        if getattr(self, "_shared_conn", None) is None:
            self._shared_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._shared_conn.execute("PRAGMA journal_mode=WAL")
            self._shared_conn.row_factory = sqlite3.Row
        try:
            yield self._shared_conn
            self._shared_conn.commit()
        except Exception:
            self._shared_conn.rollback()
            raise

    async def get(self, model: str, prompt_key: str = None, prompt: str = None) -> dict | None:
        actual_prompt = prompt_key if prompt_key is not None else prompt
        if actual_prompt is None:
            raise TypeError("get() missing 1 required positional argument: 'prompt_key'")
        cache_key = self._make_key(model, actual_prompt)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT response_json, expires_at FROM llm_response_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()

        if row is None:
            return None

        # Проверка истечения TTL
        expires_at = row["expires_at"]
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at)
            if datetime.now(UTC) > expires_dt:
                async with _db_lock:
                    with self._conn() as conn:
                        conn.execute("DELETE FROM llm_response_cache WHERE cache_key = ?", (cache_key,))
                return None

        return json.loads(row["response_json"])

    async def put(self, model: str, prompt_key: str = None, response: dict = None, ttl_seconds: int | None = None, prompt: str = None) -> None:
        actual_prompt = prompt_key if prompt_key is not None else prompt
        if actual_prompt is None:
            raise TypeError("put() missing 1 required positional argument: 'prompt_key'")
        cache_key = self._make_key(model, actual_prompt)
        response_json = json.dumps(response, ensure_ascii=False)
        cached_at = datetime.now(UTC).isoformat()
        
        expires_at = None
        if ttl_seconds is not None:
            expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

        async with _db_lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO llm_response_cache
                        (cache_key, model, response_json, cached_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cache_key, model, response_json, cached_at, expires_at),
                )

    async def invalidate_expired(self) -> None:
        now_str = datetime.now(UTC).isoformat()
        async with _db_lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM llm_response_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (now_str,))

    async def _delete(self, cache_key: str) -> None:
        async with _db_lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM llm_response_cache WHERE cache_key = ?", (cache_key,))

    @staticmethod
    def _make_key(model: str, prompt_key: str) -> str:
        # PROMPT_CACHE_VERSION включён в ключ: bump инвалидирует все старые
        # записи при изменении промптов/контракта (см. модульную константу).
        raw = f"v{PROMPT_CACHE_VERSION}:{model}:{prompt_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def stats(self) -> dict:
        """Возвращает статистику по таблице llm_response_cache."""
        with self._conn() as conn:
            total_keys = conn.execute("SELECT COUNT(*) FROM llm_response_cache").fetchone()[0]
        return {
            "total_keys": total_keys,
        }


# ---------------------------------------------------------------------------
# Dynamic Cache Healing (v9.5)
# ---------------------------------------------------------------------------


def _heal_chunk_rank(chunk: EvidenceChunk) -> EvidenceChunk:
    """
    Динамически исцеляет устаревшие (outdated) legal_rank при чтении из кэша.
    Например, КоАП РК (закон 235-V) должен быть CODE (ранг 2), но в кэше старых версий
    он мог быть сохранен как MINISTERIAL_ORDER или LAW_RK (ранг 7).
    """
    if not chunk.law_id:
        return chunk

    lid = chunk.law_id.strip().upper()
    current_rank = chunk.legal_rank

    # Кодексы РК
    if lid in ("235-V", "226-V", "350-VI", "212-IV", "1000-XIII", "409-I", "442-II", "414-I", "414-I-NEW",
               "171-VIII", "178-VIII", "360-VI-NEW", "125-VI-NEW", "375-V-NEW", "400-VI-NEW"):
        if current_rank != LegalRank.CODE:
            logger.info(f"[Cache/Healing] Healed chunk {chunk.chunk_id[:12]}… rank: {current_rank} -> CODE")
            chunk.legal_rank = LegalRank.CODE
            chunk.inferred_rank = LegalRank.CODE

    # Конституция
    elif lid in ("K950001000_", "K2600000000_"):
        if current_rank != LegalRank.CONSTITUTIONAL_LAW:
            logger.info(f"[Cache/Healing] Healed chunk {chunk.chunk_id[:12]}… rank: {current_rank} -> CONSTITUTIONAL_LAW")
            chunk.legal_rank = LegalRank.CONSTITUTIONAL_LAW
            chunk.inferred_rank = LegalRank.CONSTITUTIONAL_LAW

    return chunk
