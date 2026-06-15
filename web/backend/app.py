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
    await jobs.init_db()
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

# Include routers
app.include_router(api_router, prefix="/api")
app.include_router(ws_router, prefix="/ws")

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
