"""SQLite-backed user store with scrypt password hashing.

Sync sqlite3 wrapped in asyncio.to_thread (one connection per call), mirroring
services/jobs.py. Passwords are hashed with stdlib hashlib.scrypt + a random
per-user salt — no third-party hashing dependency.
"""

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# scrypt cost parameters. n=2**15 keeps a single hash well under ~100ms on a
# modern CPU while staying expensive to brute-force.
_N, _R, _P, _DKLEN = 2**15, 8, 1, 32
# scrypt needs ~128*N*r bytes; OpenSSL's default maxmem is 32 MiB and 2**15*8
# lands exactly on it → "memory limit exceeded". Set an explicit ceiling with
# headroom so the parameters (not the default) decide.
_MAXMEM = 128 * _N * _R + 1024 * 1024


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a self-describing `scrypt$n$r$p$salt$hash` string."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored scrypt string."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n_i, r_i = int(n), int(r)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=n_i, r=r_i, p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            maxmem=128 * n_i * r_i + 1024 * 1024,
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _init_db_sync(db_path: Path | str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


async def init_db(db_path: Path | str | None = None) -> None:
    path = db_path if db_path is not None else settings.users_db_path
    await asyncio.to_thread(_init_db_sync, path)


def _create_user_sync(db_path: Path | str, username: str, password: str) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), _now()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


async def create_user(username: str, password: str, db_path: Path | str | None = None) -> int:
    """Create a user; raises sqlite3.IntegrityError if the username exists."""
    path = db_path if db_path is not None else settings.users_db_path
    return await asyncio.to_thread(_create_user_sync, path, username, password)


def _get_by_sync(db_path: Path | str, column: str, value: object) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT id, username, password_hash, created_at FROM users WHERE {column} = ?",
            (value,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_by_username(username: str, db_path: Path | str | None = None) -> dict | None:
    path = db_path if db_path is not None else settings.users_db_path
    return await asyncio.to_thread(_get_by_sync, path, "username", username)


async def get_by_id(user_id: int, db_path: Path | str | None = None) -> dict | None:
    path = db_path if db_path is not None else settings.users_db_path
    return await asyncio.to_thread(_get_by_sync, path, "id", user_id)


def _list_users_sync(db_path: Path | str) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def list_users(db_path: Path | str | None = None) -> list[dict]:
    path = db_path if db_path is not None else settings.users_db_path
    return await asyncio.to_thread(_list_users_sync, path)
