import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config import settings  # noqa: E402


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Redirect upload/output/jobs-db to tmp_path and stub the pipeline."""
    upload_dir = tmp_path / "raw"
    output_dir = tmp_path / "output"
    jobs_db = tmp_path / "jobs.db"
    upload_dir.mkdir()
    output_dir.mkdir()

    monkeypatch.setattr(settings, "upload_dir", upload_dir)
    monkeypatch.setattr(settings, "output_dir", output_dir)
    monkeypatch.setattr(settings, "jobs_db_path", jobs_db)
    monkeypatch.setattr(settings, "max_upload_bytes", 25 * 1024 * 1024)

    # Also redirect storage.OUTPUT_DIR so /api/reports sees the fake reports.
    import services.storage as storage
    monkeypatch.setattr(storage, "OUTPUT_DIR", str(output_dir))

    async def fake_run_pipeline(file_path, output_path):
        zerde_logger = logging.getLogger("zerde")
        zerde_logger.info("Stage 3: gathering evidence")
        zerde_logger.info("Stage 5: running auditor")
        zerde_logger.info("Stage 7: rendering report")

        Path(output_path).write_text("# Fake report\n", encoding="utf-8")

        verdicts = []
        analysis = SimpleNamespace(verdicts=verdicts, overall_reliability=0.42)
        return SimpleNamespace(analysis=analysis, report_md="# Fake report\n")

    def fake_build_reliability_summary(analysis):
        return {
            "reliability": 0.42,
            "reliability_pct": 42,
            "confirmed": 0,
            "contradicted": 0,
            "unverified": 0,
            "total": 0,
            "coverage_pct": 0,
        }

    monkeypatch.setattr(
        "zerde.pipeline.run_pipeline", fake_run_pipeline, raising=False
    )

    # services.analysis imports run_pipeline lazily inside the function, so
    # patch the module attribute it will resolve via `from zerde.pipeline import run_pipeline`.
    import zerde.pipeline as zerde_pipeline_module
    monkeypatch.setattr(zerde_pipeline_module, "run_pipeline", fake_run_pipeline)

    import zerde.stages.s7_render as s7_render_module
    monkeypatch.setattr(s7_render_module, "build_reliability_summary", fake_build_reliability_summary)

    yield SimpleNamespace(
        upload_dir=upload_dir, output_dir=output_dir, jobs_db=jobs_db
    )
