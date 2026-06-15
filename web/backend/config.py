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

        # Общий API-ключ. Если переменная не задана — аутентификация выключена
        # (локальная разработка, тесты). На проде задай ZERDE_API_KEY: тогда все
        # /api-запросы потребуют заголовок X-API-Key с этим значением.
        self.api_key: str | None = os.environ.get("ZERDE_API_KEY") or None

        # Rate-limit на запуск анализа: N запусков с одного IP за окно (секунды).
        self.analyze_rate_limit: int = int(os.environ.get("ZERDE_ANALYZE_RATE_LIMIT", "10"))
        self.analyze_rate_window_s: int = int(os.environ.get("ZERDE_ANALYZE_RATE_WINDOW_S", "3600"))


settings = Settings()
