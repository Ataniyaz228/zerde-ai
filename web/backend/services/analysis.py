import asyncio
import concurrent.futures
import logging
import os
import sys
import time
from pathlib import Path

from api.ws import manager

# ── Ensure project root is on sys.path ──────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Load .env from project root BEFORE importing zerde (pydantic-settings) ──
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=_ENV_FILE, override=False)
    except ImportError:
        # python-dotenv not installed — set vars manually
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip().strip('"').strip("'")

logger = logging.getLogger(__name__)

analysis_status: dict[str, str] = {}

# F-D2: анализы сериализуются single-worker executor'ом (max_workers=1), а не
# блокирующим Lock в потоке FastAPI-пула. Прежде queued-аплоад висел на
# Lock.acquire(), удерживая поток пула; теперь ожидание — в очереди executor'а,
# потоки пула освобождаются сразу. Один-за-раз по-прежнему нужен: общий SQLite-
# кэш (несколько CacheManager-соединений) + синглтоны BGE/reranker.
_ANALYSIS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="zerde-analysis"
)

STAGE_MAP = {
    "S1":  "extract",
    "S2":  "extract",
    "S2.5": "extract",
    "S2.7": "extract",
    "S3":  "search",
    "S4":  "search",
    "S5":  "verify",
    "S5.2": "verify",
    "S5.5": "verify",
    "S6":  "verify",
    "S7":  "report",
}

STAGE_DISPLAY = {
    "extract": "Извлечение тезисов",
    "search":  "Поиск в базе НПА",
    "verify":  "Верификация и анализ",
    "report":  "Формирование отчёта",
}


def get_analysis_status(analysis_id: str) -> dict:
    status = analysis_status.get(analysis_id, "unknown")
    return {"analysis_id": analysis_id, "status": status}





def start_analysis(analysis_id: str, file_path: str, main_loop: asyncio.AbstractEventLoop) -> None:
    """Entry point called from FastAPI BackgroundTask.

    F-D2: только ставит задачу в single-worker executor и сразу возвращается —
    поток FastAPI-пула не удерживается на время ожидания/прогона. Анализы идут
    строго по одному (executor max_workers=1); ждущие — в очереди executor'а
    (статус 'queued'), а не висят на блокирующем Lock.acquire().
    """
    analysis_status[analysis_id] = "queued"
    _ANALYSIS_EXECUTOR.submit(_run_analysis_blocking, analysis_id, file_path, main_loop)


def _run_analysis_blocking(analysis_id: str, file_path: str, main_loop: asyncio.AbstractEventLoop) -> None:
    """Исполняется на единственном worker-потоке анализатора (сериализация)."""
    analysis_status[analysis_id] = "running"
    try:
        asyncio.run(_run_analysis_async(analysis_id, file_path, main_loop))
    except Exception:
        logger.exception(f"[{analysis_id}] analysis worker crashed")
        analysis_status[analysis_id] = "error"


async def _run_analysis_async(analysis_id: str, file_path: str, main_loop: asyncio.AbstractEventLoop) -> None:
    async def _broadcast(analysis_id: str, msg: dict) -> None:
        future = asyncio.run_coroutine_threadsafe(
            manager.send_personal_message(msg, analysis_id),
            main_loop
        )
        try:
            future.result(timeout=5)
        except Exception as e:
            logger.warning(f"[WS] broadcast error: {e}")
    output_dir = _PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_stem = Path(file_path).stem[:40]
    output_path = output_dir / f"zerde_report_{safe_stem}_{ts}.md"

    # Notify frontend: pipeline steps started
    sent_stages: set[str] = set()

    async def stage_start(stage_key: str, message: str | None = None) -> None:
        if stage_key in sent_stages:
            return
        sent_stages.add(stage_key)
        await _broadcast(analysis_id, {
            "type": "stage_start",
            "stage": stage_key,
            "message": message or STAGE_DISPLAY.get(stage_key, stage_key),
        })

    async def stage_done(stage_key: str) -> None:
        await _broadcast(analysis_id, {
            "type": "stage_done",
            "stage": stage_key,
        })

    async def stage_error(stage_key: str, message: str) -> None:
        await _broadcast(analysis_id, {
            "type": "stage_error",
            "stage": stage_key,
            "message": message,
        })

    try:
        # ── Import real pipeline ──────────────────────────────────────────
        from zerde.pipeline import run_pipeline

        # Stage 1+2: extract
        await stage_start("extract", "Загрузка и извлечение тезисов")

        # F-A3: прогресс — явный колбэк из пайплайна (run_pipeline зовёт progress_cb
        # на границах стадий S3/S5/S7), вместо хрупкого парсинга текста лог-сообщений.
        stage_transitions = {
            "S3": ("extract", "search", "Поиск в базе НПА Казахстана"),
            "S5": ("search", "verify", "Верификация и анализ коллизий"),
            "S7": ("verify", "report", "Формирование отчёта"),
        }

        async def _progress(stage: str) -> None:
            t = stage_transitions.get(stage)
            if t:
                done, start, msg = t
                await stage_done(done)
                await stage_start(start, msg)

        logger.info(f"[{analysis_id}] Pipeline execution started for {file_path}")
        result = await run_pipeline(file_path, output_path, progress_cb=_progress)
        logger.info(f"[{analysis_id}] Pipeline execution completed. Calculating scores...")

        await stage_done("report")

        # Структурная надёжность — берём из объекта анализа, НЕ выскребаем
        # regex'ом из Markdown. Пишем сайдкар <report>.meta.json для storage.
        import json as _json

        from zerde.stages.s7_render import build_reliability_summary

        summary = build_reliability_summary(result.analysis)
        score = summary["reliability_pct"] or 0
        meta_path = output_path.parent / (output_path.name + ".meta.json")
        try:
            meta_path.write_text(
                _json.dumps({"report_id": output_path.name, **summary}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[Analysis] Could not write meta sidecar {meta_path}: {e}")

        analysis_status[analysis_id] = "completed"
        await _broadcast(analysis_id, {
            "type": "done",
            "report": result.report_md,
            "score": score,
            "report_id": output_path.name,
        })

    except Exception as exc:
        logger.exception(f"[Analysis] Pipeline error for {analysis_id}: {exc}")
        analysis_status[analysis_id] = "error"
        # Mark remaining stages as error
        for stage in ["extract", "search", "verify", "report"]:
            if stage not in sent_stages:
                await stage_error(stage, str(exc))
        await _broadcast(analysis_id, {
            "type": "error",
            "message": str(exc),
        })
