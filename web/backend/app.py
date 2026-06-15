from dotenv import load_dotenv

load_dotenv()

import logging  # noqa: E402
import sys  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

# Log file lives at the project root — OUTSIDE the uvicorn --reload-dir watch
# (web/backend + zerde). Writing it inside the watched tree created a feedback
# loop: watchfiles' own "change detected" INFO lines were logged to the file,
# which watchfiles then detected, logging another line, ~once per second.
_LOG_PATH = Path(__file__).resolve().parents[2] / "zerde_backend.log"

import uvicorn  # noqa: E402
from api.routes import router as api_router  # noqa: E402
from api.ws import router as ws_router  # noqa: E402
from config import settings  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from services import jobs  # noqa: E402


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        ],
    )
    # Quiet the reload watcher: its per-change INFO spam is noise in dev and was
    # the source of the self-feeding log loop. Real reloads still print.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger(__name__)
    await jobs.init_db()
    # Рестарт убивает запущенный анализ без терминального события — помечаем
    # такие 'running'-задачи ошибкой, чтобы переподключившийся клиент не висел.
    n = await jobs.reconcile_interrupted()
    if n:
        log.info(f"[startup] Reconciled {n} interrupted running job(s) → error")
    # Задачи, принятые до рестарта, но не стартовавшие ('queued'), дозапускаем —
    # их работа иначе потерялась бы молча.
    from services.analysis import enqueue_analysis
    resumed = 0
    for job in await jobs.list_queued():
        if Path(job["stored_path"]).exists():
            enqueue_analysis(job["analysis_id"], job["stored_path"])
            resumed += 1
        else:
            await jobs.set_status(job["analysis_id"], "error", error="Загруженный файл недоступен после рестарта.")
    if resumed:
        log.info(f"[startup] Resumed {resumed} queued job(s)")
    yield


app = FastAPI(
    title="Zerde AI API",
    description="Backend API for Zerde AI Legal Analysis Pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

# Configure CORS for local frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Все /api-роуты за API-ключом (включается только если задан ZERDE_API_KEY).
from api.security import require_api_key  # noqa: E402
from fastapi import Depends  # noqa: E402

app.include_router(api_router, prefix="/api", dependencies=[Depends(require_api_key)])
app.include_router(ws_router, prefix="/ws")


@app.get("/health")
async def health_check():
    """Liveness: процесс жив и отвечает."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness_check():
    """Readiness: корпус-БД на месте и открывается. Иначе анализ работать не будет."""
    import sqlite3

    from zerde.config import get_settings as get_zerde_settings

    db_path = Path(get_zerde_settings().cache_db_path)
    if not db_path.exists():
        return JSONResponse(status_code=503, content={"status": "not ready", "reason": "corpus db missing"})
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM law_metadata LIMIT 1")
        conn.close()
    except sqlite3.Error as e:
        return JSONResponse(status_code=503, content={"status": "not ready", "reason": str(e)})
    return {"status": "ready"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
