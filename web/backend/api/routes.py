import os
import uuid
import asyncio
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from services.analysis import start_analysis, get_analysis_status
from services.storage import list_reports, get_report

router = APIRouter()

UPLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "raw"))

# Лимит размера аплоада (H1): защищает от загрузки гигантских файлов в память.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
_ALLOWED_EXT = (".docx", ".pdf", ".txt")


def _safe_stored_name(analysis_id: str, raw_filename: str | None) -> str:
    """H1: имя файла → безопасное имя на диске.

    Берём ТОЛЬКО basename (отрезаем любые ../ и разделители путей), префиксуем
    analysis_id (изоляция от коллизий между пользователями). Раньше путь строился
    как os.path.join(UPLOAD_DIR, file.filename) с сырым именем → path traversal
    (`../../etc/x`) и перезапись чужого файла на лету.
    """
    base = Path(raw_filename or "").name.replace("\\", "_").strip()
    if not base or base in (".", ".."):
        base = "upload"
    return f"{analysis_id}__{base}"

class ReportMetadata(BaseModel):
    id: str
    filename: str
    date: str
    reliability_score: float

class AnalysisResponse(BaseModel):
    analysis_id: str
    status: str

@router.post("/analyze", response_model=AnalysisResponse)
async def analyze_document(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Uploads a document and starts the pipeline analysis in the background."""
    if not file.filename or not file.filename.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="Only .docx, .pdf, and .txt files are supported.")

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    analysis_id = str(uuid.uuid4())
    stored_name = _safe_stored_name(analysis_id, file.filename)
    file_path = os.path.join(UPLOAD_DIR, stored_name)

    # Потоковая запись с лимитом размера (H1): не грузим файл целиком в память
    # и обрываем (с удалением частичного файла), если превышен MAX_UPLOAD_BYTES.
    size = 0
    try:
        with open(file_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    f.close()
                    os.remove(file_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not store upload: {e}")

    main_loop = asyncio.get_running_loop()
    background_tasks.add_task(start_analysis, analysis_id, file_path, main_loop)
    
    return {"analysis_id": analysis_id, "status": "started"}

@router.get("/analyze/{analysis_id}")
async def check_analysis_status(analysis_id: str):
    """Checks the status of an ongoing analysis."""
    status = get_analysis_status(analysis_id)
    if not status:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return status

@router.get("/reports", response_model=List[ReportMetadata])
async def get_all_reports():
    """Returns a list of all generated reports."""
    reports = list_reports()
    return reports

@router.get("/reports/{report_id}")
async def get_report_content(report_id: str):
    """Returns the markdown content and metadata of a specific report."""
    report = get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
