"""
SQLite Cache Manager
Кэш для чанков Адилет/Web и LLM-ответов (токен-экономия).
Ключ чанков: SHA256 контента. Ключ LLM: SHA256(model:prompt).
"""

from __future__ import annotations

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
        self.db_path = Path(db_path)
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

    async def search_local(
        self,
        query_text: str,
        law_ids: list[str] | None = None,
        articles: list[str] | None = None,
        limit: int = 10,
    ) -> list[EvidenceChunk]:
        """
        Ищет чанки в локальном кэше по ключевым словам в content и/или по law_ids.
        Полезно как оффлайн-fallback при ошибках сети/лимитах API.
        """
        import json
        import re
        import sqlite3

        # 1. Нормализация law_ids
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
                # Также добавим без суффикса _ для надежности
                if known_code and known_code.endswith("_"):
                    normalized_law_ids.append(known_code[:-1].upper())

        # Нормализация articles
        normalized_articles = []
        if articles:
            for art in articles:
                art_norm = art.strip().lower()
                if art_norm:
                    normalized_articles.append(art_norm)

        # Вспомогательная функция для стемминга русских слов
        def stem_russian_word(w: str) -> str:
            if len(w) <= 4:
                return w
            # Сортируем окончания по убыванию длины для корректного сопоставления
            endings = [
                "ями", "ами", "ому", "ему", "ого", "его", "ыми", "ими", "ых", "их", "ею", "ою",
                "ом", "ем", "ой", "ей", "ию", "ую", "яя", "ая", "ое", "ее", "ые", "ие", "ый", "ий", "ам", "ям", "ов", "ев", "ях", "ах",
                "а", "я", "о", "е", "и", "ы", "у", "ю", "ь"
            ]
            for end in endings:
                if w.endswith(end):
                    # Убедимся, что после отсечения основы остается хотя бы 3 символа
                    if len(w) - len(end) >= 3:
                        return w[:-len(end)]
            return w

        # 2. Выделение слов
        words = [w.strip().lower() for w in re.split(r'\s+', query_text) if len(w.strip()) > 2]
        LEGAL_STOP_WORDS = {
            "закон", "кодекс", "статья", "статье", "статьи", "республики", "казахстан",
            "утратил", "силу", "вводится", "действие", "постановление", "правительства",
            "республика", "закона", "кодекса", "об", "о", "и", "в", "на", "для", "рк"
        }
        filtered_words = [w for w in words if w not in LEGAL_STOP_WORDS]
        if not filtered_words:
            filtered_words = words

        # Применяем стемминг к ключевым словам
        stemmed_words = [stem_russian_word(w) for w in filtered_words]

        chunks = []
        seen_chunk_ids = set()

        def add_rows(rows_to_add):
            added_count = 0
            for r in rows_to_add:
                try:
                    data = json.loads(r["chunk_json"])
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
                            logger.warning(f"[Cache/search_local] Failed to update healed rank: {ex}")
                    if chunk.chunk_id not in seen_chunk_ids:
                        chunks.append(chunk)
                        seen_chunk_ids.add(chunk.chunk_id)
                        added_count += 1
                except Exception as e:
                    logger.warning(f"[Cache/search_local] Failed to parse row: {e}")
            return added_count

        with self._conn() as conn:
            # СТРАТЕГИЯ 0: Точный поиск по law_ids и конкретным номерам статей (если запрошены)
            if normalized_law_ids and normalized_articles:
                law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                art_conds = " OR ".join(["json_extract(chunk_json, '$.article') = ?" for _ in normalized_articles])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) AND ({art_conds}) LIMIT ?"
                
                params = []
                for lid in normalized_law_ids:
                    params.append(f"%{lid}%")
                for art in normalized_articles:
                    params.append(art)
                params.append(limit)
                
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            if len(chunks) < limit and not normalized_law_ids and normalized_articles:
                art_conds = " OR ".join(["json_extract(chunk_json, '$.article') = ?" for _ in normalized_articles])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE ({art_conds}) LIMIT ?"
                params = list(normalized_articles) + [limit - len(chunks)]
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            # СТРАТЕГИЯ 1: Поиск по law_ids + ВСЕМ отфильтрованным словам (AND)
            if normalized_law_ids and stemmed_words:
                law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                word_conds = " AND ".join(["(json_extract(chunk_json, '$.content') LIKE ? OR json_extract(chunk_json, '$.source_title') LIKE ?)" for _ in stemmed_words])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) AND ({word_conds}) LIMIT ?"
                
                params = []
                for lid in normalized_law_ids:
                    params.append(f"%{lid}%")
                for w in stemmed_words:
                    params.extend([f"%{w}%", f"%{w}%"])
                params.append(limit)
                
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            # СТРАТЕГИЯ 2: Поиск по law_ids + ХОТЯ БЫ ОДНОМУ слову (OR)
            if len(chunks) < limit and normalized_law_ids and stemmed_words:
                law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                word_conds = " OR ".join(["(json_extract(chunk_json, '$.content') LIKE ? OR json_extract(chunk_json, '$.source_title') LIKE ?)" for _ in stemmed_words[:3]])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) AND ({word_conds}) LIMIT ?"
                
                params = []
                for lid in normalized_law_ids:
                    params.append(f"%{lid}%")
                for w in stemmed_words[:3]:
                    params.extend([f"%{w}%", f"%{w}%"])
                params.append(limit - len(chunks))
                
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            # СТРАТЕГИЯ 3: Только по law_ids (если слова не совпали)
            if len(chunks) < limit and normalized_law_ids:
                law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) LIMIT ?"
                params = [f"%{lid}%" for lid in normalized_law_ids] + [limit - len(chunks)]
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            # СТРАТЕГИЯ 4: Поиск по всем отфильтрованным словам (AND) без привязки к law_ids
            if len(chunks) < limit and stemmed_words:
                word_conds = " AND ".join(["(json_extract(chunk_json, '$.content') LIKE ? OR json_extract(chunk_json, '$.source_title') LIKE ?)" for _ in stemmed_words])
                sql = f"SELECT chunk_json FROM evidence_cache WHERE {word_conds} LIMIT ?"
                
                params = []
                for w in stemmed_words:
                    params.extend([f"%{w}%", f"%{w}%"])
                params.append(limit - len(chunks))
                
                try:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError:
                    pass

            # СТРАТЕГИЯ 5: Поиск по любым словам (OR), исключая чисто числовые короткие запросы для исключения утечек
            if len(chunks) < limit and stemmed_words:
                safe_words = [w for w in stemmed_words[:3] if not w.isdigit() or len(w) > 3]
                if safe_words:
                    word_conds = " OR ".join(["(json_extract(chunk_json, '$.content') LIKE ? OR json_extract(chunk_json, '$.source_title') LIKE ?)" for _ in safe_words])
                    sql = f"SELECT chunk_json FROM evidence_cache WHERE {word_conds} LIMIT ?"
                    
                    params = []
                    for w in safe_words:
                        params.extend([f"%{w}%", f"%{w}%"])
                    params.append(limit - len(chunks))
                    
                    try:
                        rows = conn.execute(sql, tuple(params)).fetchall()
                        add_rows(rows)
                    except sqlite3.OperationalError:
                        pass

        logger.debug(f"[Cache] Local search query='{query_text}' law_ids={law_ids} found={len(chunks)} chunks")
        return chunks




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
