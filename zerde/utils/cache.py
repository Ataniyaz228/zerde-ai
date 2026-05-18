"""
ЗЕРДЕ v7.0 — SQLite Cache Manager
Кэш для чанков Адилет/Web и LLM-ответов (токен-экономия).
Ключ чанков: SHA256 контента. Ключ LLM: SHA256(model:prompt).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generator

from zerde.models import EvidenceChunk

logger = logging.getLogger(__name__)

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

    def get(self, chunk_id: str) -> EvidenceChunk | None:
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
            logger.debug(f"[Cache] HIT: {chunk_id[:12]}…")
            return chunk
        except Exception as e:
            logger.warning(f"[Cache] Failed to deserialize chunk {chunk_id[:12]}…: {e}")
            return None

    def put(self, chunk: EvidenceChunk) -> None:
        """
        Сохраняет чанк в кэш.
        Игнорирует дубликаты (INSERT OR IGNORE).

        Args:
            chunk: EvidenceChunk для сохранения.
        """
        chunk_json = chunk.model_dump_json()
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
                    datetime.utcnow().isoformat(),
                ),
            )
        logger.debug(f"[Cache] STORED: {chunk.chunk_id[:12]}…")

    def put_many(self, chunks: list[EvidenceChunk]) -> int:
        """
        Батчевое сохранение чанков.

        Returns:
            Количество реально сохранённых (не дубликатов).
        """
        stored = 0
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
                        datetime.utcnow().isoformat(),
                    ),
                )
                stored += result.rowcount

        logger.info(f"[Cache] Batch stored {stored}/{len(chunks)} chunks")
        return stored

    def has(self, chunk_id: str) -> bool:
        """Быстрая проверка наличия чанка в кэше."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM evidence_cache WHERE chunk_id = ? LIMIT 1",
                (chunk_id,),
            ).fetchone()
        return row is not None

    def stats(self) -> dict:
        """Возвращает статистику кэша."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_chunks": total,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
        }

    def clear(self) -> int:
        """Очищает весь кэш. Используй с осторожностью."""
        with self._conn() as conn:
            result = conn.execute("DELETE FROM evidence_cache")
            deleted = result.rowcount
        logger.warning(f"[Cache] CLEARED: {deleted} chunks deleted")
        return deleted


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

    def get(self, model: str, prompt: str) -> dict | None:
        """
        Возвращает кэшированный JSON-ответ или None.
        Автоматически проверяет TTL.
        """
        key = self._make_key(model, prompt)
        now_iso = datetime.utcnow().isoformat()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT response_json, expires_at FROM llm_response_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()

        if row is None:
            return None

        # Проверяем истечение TTL
        if row["expires_at"] and row["expires_at"] < now_iso:
            logger.debug(f"[LLMCache] EXPIRED: {key[:12]}…")
            self._delete(key)
            return None

        try:
            data = json.loads(row["response_json"])
            logger.info(f"[LLMCache] HIT: {key[:12]}… (model={model.split('/')[-1]})")
            return data
        except Exception as e:
            logger.warning(f"[LLMCache] Deserialize error {key[:12]}…: {e}")
            return None

    def put(
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
        now = datetime.utcnow()
        expires = (
            (now + timedelta(seconds=ttl_seconds)).isoformat()
            if ttl_seconds
            else None
        )

        with self._conn() as conn:
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

    def _delete(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM llm_response_cache WHERE cache_key = ?", (key,))

    def invalidate_expired(self) -> int:
        """Удаляет все истёкшие записи. Вызывай при старте пайплайна."""
        now_iso = datetime.utcnow().isoformat()
        with self._conn() as conn:
            result = conn.execute(
                "DELETE FROM llm_response_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
        deleted = result.rowcount
        if deleted:
            logger.info(f"[LLMCache] Purged {deleted} expired entries")
        return deleted

    def stats(self) -> dict:
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

    def clear_llm(self) -> int:
        """Очищает только LLM кэш (не трогает evidence_cache)."""
        with self._conn() as conn:
            result = conn.execute("DELETE FROM llm_response_cache")
        logger.warning(f"[LLMCache] LLM cache cleared: {result.rowcount} entries")
        return result.rowcount
