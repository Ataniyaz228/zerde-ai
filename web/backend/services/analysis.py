import os
import sys
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict

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

analysis_status: Dict[str, str] = {}

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
    """Entry point called from FastAPI BackgroundTask."""
    analysis_status[analysis_id] = "running"
    asyncio.run(_run_analysis_async(analysis_id, file_path, main_loop))


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

        # Run the actual pipeline — it's async, wrap progress around it.
        # We run it in full and send intermediate signals at natural breakpoints
        # by hooking into asyncio checkpoints.
        #
        # The pipeline calls stages sequentially; we inject progress beacons
        # by wrapping run_pipeline and listening to log records.

        class _ProgressHandler(logging.Handler):
            """Emits WS messages when pipeline logs stage completions."""
            def __init__(self):
                super().__init__()
                self._sent: set[str] = set()

            def emit(self, record: logging.LogRecord) -> None:
                msg = record.getMessage()

                def _transition(done_stage: str, start_stage: str, start_msg: str):
                    logger.info(f"[{analysis_id}] Pipeline progress: completed {done_stage}, starting {start_stage}")
                    asyncio.run_coroutine_threadsafe(
                        manager.send_personal_message({"type": "stage_done", "stage": done_stage}, analysis_id),
                        main_loop
                    )
                    asyncio.run_coroutine_threadsafe(
                        manager.send_personal_message({"type": "stage_start", "stage": start_stage, "message": start_msg}, analysis_id),
                        main_loop
                    )

                if "Stage 3" in msg and "extract" not in self._sent:
                    # S1+S2 done → trigger search start
                    self._sent.add("extract")
                    _transition("extract", "search", "Поиск в базе НПА Казахстана")
                elif "Stage 5" in msg and "search" not in self._sent:
                    self._sent.add("search")
                    _transition("search", "verify", "Верификация и анализ коллизий")
                elif "Stage 7" in msg and "verify" not in self._sent:
                    self._sent.add("verify")
                    _transition("verify", "report", "Формирование отчёта")

        handler = _ProgressHandler()
        handler.setLevel(logging.INFO)
        zerde_logger = logging.getLogger("zerde")
        zerde_logger.addHandler(handler)

        try:
            logger.info(f"[{analysis_id}] Pipeline execution started for {file_path}")
            result = await run_pipeline(file_path, output_path)
            logger.info(f"[{analysis_id}] Pipeline execution completed. Calculating scores...")
        finally:
            zerde_logger.removeHandler(handler)

        await stage_done("report")

        # Extract score from report
        import re
        score = 0
        score_match = re.search(
            r"Надёжность анализа:\*?\*?\s*(?:🔴|🟡|🟢|⚫)\s*(\d+)%",
            result.report_md or "",
        )
        if score_match:
            score = int(score_match.group(1))

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
