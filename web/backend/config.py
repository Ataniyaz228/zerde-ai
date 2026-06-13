import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_CORS = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"


class Settings:
    def __init__(self) -> None:
        self.cors_origins: list[str] = [
            o.strip() for o in os.environ.get("ZERDE_CORS_ORIGINS", _DEFAULT_CORS).split(",") if o.strip()
        ]
        self.max_upload_bytes: int = int(os.environ.get("ZERDE_MAX_UPLOAD_MB", "25")) * 1024 * 1024
        self.allowed_suffixes: set[str] = {".docx", ".pdf", ".txt"}
        self.upload_dir: Path = _PROJECT_ROOT / "data" / "raw"
        self.output_dir: Path = _PROJECT_ROOT / "output"
        self.jobs_db_path: Path = Path(__file__).resolve().parent / "jobs.db"


settings = Settings()
