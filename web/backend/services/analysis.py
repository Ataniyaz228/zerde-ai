import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from api.ws import manager
from config import settings
from services import jobs

if TYPE_CHECKING:
    # zerde.pipeline pulls in torch/sentence-transformers at module level, so
    # it must stay a lazy import at runtime (see run_pipeline import below).
    # This import is type-checking-only and never executes.
    from zerde.pipeline import ProgressEvent

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


def translate_progress(ev: "ProgressEvent", prev_stage: str) -> tuple[list[dict], str]:
    """Translate a pipeline ProgressEvent into frozen WS event dicts.

    Closes the previous stage (stage_done) and opens the new one
    (stage_start) when the stage changes. Pure/offline — no I/O, no loop.

    Returns (events_to_emit, new_prev_stage).
    """
    if ev.stage == prev_stage:
        return [], prev_stage
    events = [
        {"type": "stage_done", "stage": prev_stage},
        {"type": "stage_start", "stage": ev.stage, "message": ev.message},
    ]
    return events, ev.stage


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

        prev_stage = "extract"
        event_queue: asyncio.Queue[dict] = asyncio.Queue()

        def on_progress(ev: "ProgressEvent") -> None:
            nonlocal prev_stage
            events, new_prev = translate_progress(ev, prev_stage)
            for event in events:
                event_queue.put_nowait(event)
            if events:
                sent_stages.add(prev_stage)
                logger.info(
                    f"[{analysis_id}] Pipeline progress: completed {prev_stage}, starting {new_prev}"
                )
            prev_stage = new_prev

        async def _drain_progress() -> None:
            """Sequentially emit queued progress events (no concurrent _emit calls)."""
            while True:
                event = await event_queue.get()
                try:
                    await _emit(analysis_id, event)
                except Exception:
                    logger.exception(f"[{analysis_id}] Failed to emit progress event {event}")
                finally:
                    event_queue.task_done()

        drainer = asyncio.create_task(_drain_progress())

        try:
            from zerde.pipeline import run_pipeline

            logger.info(f"[{analysis_id}] Pipeline execution started for {file_path}")
            result = await run_pipeline(file_path, output_path, progress=on_progress)
            logger.info(f"[{analysis_id}] Pipeline execution completed. Calculating scores...")

            # Wait for all progress events emitted during run_pipeline to be
            # flushed (ordered, one at a time) before the final stage_done.
            await event_queue.join()

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
                "confirmed": summary["confirmed"],
                "contradicted": summary["contradicted"],
                "unverified": summary["unverified"],
                "coverage_pct": summary["coverage_pct"],
            })

        except Exception as exc:
            logger.exception(f"[Analysis] Pipeline error for {analysis_id}: {exc}")
            await jobs.set_status(analysis_id, "error", error=str(exc))

            # Flush any progress events queued before the failure — keeps emit
            # ordering consistent with the success path.
            await event_queue.join()

            for stage in ["extract", "search", "verify", "report"]:
                if stage not in sent_stages:
                    await _emit(analysis_id, {"type": "stage_error", "stage": stage, "message": str(exc)})
            await _emit(analysis_id, {"type": "error", "message": str(exc)})
        finally:
            drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drainer
