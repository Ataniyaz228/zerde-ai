import uuid
from pathlib import Path

from api.security import rate_limit_analyze
from config import settings
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from services import jobs
from services.analysis import enqueue_analysis
from services.report_i18n import get_report_localized
from services.storage import list_reports

router = APIRouter()


class ReportMetadata(BaseModel):
    id: str
    filename: str
    date: str
    reliability_score: float
    confirmed: int = 0
    contradicted: int = 0
    unverified: int = 0
    total: int = 0
    coverage_pct: int = 0


class AnalysisResponse(BaseModel):
    analysis_id: str
    status: str


@router.post("/analyze", response_model=AnalysisResponse, dependencies=[Depends(rate_limit_analyze)])
async def analyze_document(file: UploadFile = File(...)):
    """Uploads a document and starts the pipeline analysis."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_suffixes:
        raise HTTPException(status_code=400, detail="Only .docx, .pdf, and .txt files are supported.")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    analysis_id = str(uuid.uuid4())
    stored_path = settings.upload_dir / f"{analysis_id}{suffix}"

    size = 0
    chunk_size = 1024 * 1024
    try:
        with open(stored_path, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    f.close()
                    stored_path.unlink(missing_ok=True)
                    max_mb = settings.max_upload_bytes // (1024 * 1024)
                    raise HTTPException(status_code=413, detail=f"File too large (max {max_mb}MB)")
                f.write(chunk)
    except HTTPException:
        raise

    await jobs.create_job(analysis_id, file.filename or "", str(stored_path))
    enqueue_analysis(analysis_id, str(stored_path))

    return {"analysis_id": analysis_id, "status": "queued"}


@router.get("/analyze/{analysis_id}")
async def check_analysis_status(analysis_id: str):
    """Checks the status of an ongoing analysis."""
    job = await jobs.get_job(analysis_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {
        "analysis_id": job["analysis_id"],
        "status": job["status"],
        "report_id": job["report_id"],
        "score": job["score"],
        "error": job["error"],
    }


@router.get("/reports", response_model=list[ReportMetadata])
async def get_all_reports():
    """Returns a list of all generated reports."""
    reports = list_reports()
    return reports


@router.get("/reports/{report_id}")
async def get_report_content(
    report_id: str,
    lang: str = Query("ru", pattern="^(ru|kz)$"),
):
    """Returns the markdown content and metadata of a specific report.

    lang=ru — канонический отчёт (по умолчанию). lang=kz — безопасный машинный
    перевод (кэшируется в сайдкар; при провале guard'а отдаётся RU)."""
    report = await get_report_localized(report_id, lang)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
