import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from api.ws import manager
from config import settings
from services import jobs

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

STAGE_DISPLAY = {
    "extract": "Извлечение тезисов",
    "search":  "Поиск в базе НПА",
    "verify":  "Верификация и анализ",
    "report":  "Формирование отчёта",
}

_CAPACITY = asyncio.Semaphore(1)


async def _emit(analysis_id: str, event: dict) -> None:
    await jobs.append_event(analysis_id, event)
    await manager.broadcast(event, analysis_id)


def enqueue_analysis(analysis_id: str, file_path: str) -> asyncio.Task:
    """Schedule the analysis pipeline run on the current event loop.

    Returns the created Task so tests can await it deterministically.
    """
    return asyncio.create_task(_run_analysis(analysis_id, file_path))


class _ProgressHandler(logging.Handler):
    """Emits WS messages when pipeline logs stage completions.

    Handler.emit may be called from any thread, so emits are marshalled onto
    the owning event loop via call_soon_threadsafe.
    """

    def __init__(self, analysis_id: str, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._analysis_id = analysis_id
        self._loop = loop
        self._sent: set[str] = set()

    def _schedule(self, event: dict) -> None:
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(_emit(self._analysis_id, event))
        )

    def _transition(self, done_stage: str, start_stage: str, start_msg: str) -> None:
        logger.info(
            f"[{self._analysis_id}] Pipeline progress: completed {done_stage}, starting {start_stage}"
        )
        self._schedule({"type": "stage_done", "stage": done_stage})
        self._schedule({"type": "stage_start", "stage": start_stage, "message": start_msg})

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()

        if "Stage 3" in msg and "extract" not in self._sent:
            self._sent.add("extract")
            self._transition("extract", "search", "Поиск в базе НПА Казахстана")
        elif "Stage 5" in msg and "search" not in self._sent:
            self._sent.add("search")
            self._transition("search", "verify", "Верификация и анализ коллизий")
        elif "Stage 7" in msg and "verify" not in self._sent:
            self._sent.add("verify")
            self._transition("verify", "report", "Формирование отчёта")


async def _run_analysis(analysis_id: str, file_path: str) -> None:
    async with _CAPACITY:
        await jobs.set_status(analysis_id, "running")

        sent_stages: set[str] = {"extract"}
        await _emit(analysis_id, {
            "type": "stage_start",
            "stage": "extract",
            "message": "Загрузка и извлечение тезисов",
        })

        output_dir = settings.output_dir
        output_dir.mkdir(exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_stem = Path(file_path).stem[:40]
        output_path = output_dir / f"zerde_report_{safe_stem}_{ts}.md"

        loop = asyncio.get_running_loop()
        handler = _ProgressHandler(analysis_id, loop)
        handler.setLevel(logging.INFO)
        zerde_logger = logging.getLogger("zerde")
        zerde_logger.addHandler(handler)

        try:
            from zerde.pipeline import run_pipeline

            logger.info(f"[{analysis_id}] Pipeline execution started for {file_path}")
            result = await run_pipeline(file_path, output_path)
            logger.info(f"[{analysis_id}] Pipeline execution completed. Calculating scores...")

            sent_stages |= handler._sent
            await _emit(analysis_id, {"type": "stage_done", "stage": "report"})

            # Структурная надёжность — берём из объекта анализа, НЕ выскребаем
            # regex'ом из готового Markdown. Пишем сайдкар <report>.meta.json
            # для storage.
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

            await jobs.set_status(analysis_id, "completed", report_id=output_path.name, score=score)
            await _emit(analysis_id, {
                "type": "done",
                "report": result.report_md,
                "score": score,
                "report_id": output_path.name,
            })

        except Exception as exc:
            logger.exception(f"[Analysis] Pipeline error for {analysis_id}: {exc}")
            await jobs.set_status(analysis_id, "error", error=str(exc))
            sent_stages |= handler._sent
            for stage in ["extract", "search", "verify", "report"]:
                if stage not in sent_stages:
                    await _emit(analysis_id, {"type": "stage_error", "stage": stage, "message": str(exc)})
            await _emit(analysis_id, {"type": "error", "message": str(exc)})
        finally:
            zerde_logger.removeHandler(handler)
