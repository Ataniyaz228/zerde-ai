import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router as api_router
from api.ws import router as ws_router
import uvicorn
import logging
import sys

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("zerde_backend.log", encoding="utf-8"),
        ],
    )

setup_logging()

app = FastAPI(
    title="Zerde AI API",
    description="Backend API for Zerde AI Legal Analysis Pipeline",
    version="1.0.0",
)

# Configure CORS for local frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router, prefix="/api")
app.include_router(ws_router, prefix="/ws")


def _validate_corpus_or_die() -> dict:
    """Fail-loud проверка грунтинг-корпуса на старте.

    Раньше бэкенд молча обслуживал запросы на пустой/теневой БД (см. аудит C3:
    web/backend/zerde_cache.db с 0 законов), и весь грунтинг тихо схлопывался.
    Теперь резолвим тот же путь, что использует пайплайн, и если law_metadata
    пуст — падаем громко (override: ZERDE_ALLOW_EMPTY_CACHE=1).
    """
    import sqlite3
    log = logging.getLogger("zerde.backend")
    try:
        from zerde.config import get_settings  # project root уже в sys.path (api.routes)
        db_path = get_settings().cache_db_path
        conn = sqlite3.connect(db_path)
        try:
            laws = conn.execute("SELECT COUNT(*) FROM law_metadata").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        log.error(f"[startup] Не удалось проверить корпус: {e}")
        return {"ok": False, "error": str(e)}

    if laws == 0:
        banner = (
            "\n" + "=" * 72 + "\n"
            f"[startup] ПУСТОЙ КОРПУС: law_metadata=0 в {db_path}\n"
            "Грунтинг будет молча давать одни UNVERIFIED. Заполните корпус\n"
            "(scripts/ingest_*.py) или задайте CACHE_DB_PATH на рабочую БД.\n"
            "Обойти проверку: ZERDE_ALLOW_EMPTY_CACHE=1\n"
            + "=" * 72
        )
        log.critical(banner)
        if os.getenv("ZERDE_ALLOW_EMPTY_CACHE") != "1":
            raise RuntimeError(
                f"Пустой грунтинг-корпус ({db_path}); запуск прерван. "
                "Установите ZERDE_ALLOW_EMPTY_CACHE=1 чтобы всё равно запустить."
            )
    else:
        log.info(f"[startup] Корпус ок: {laws} законов, {chunks} чанков ({db_path})")
    return {"ok": laws > 0, "laws": laws, "chunks": chunks, "db_path": str(db_path)}


_CORPUS_STATUS = _validate_corpus_or_die()


@app.get("/health")
async def health_check():
    return {"status": "ok" if _CORPUS_STATUS.get("ok") else "degraded", "corpus": _CORPUS_STATUS}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
