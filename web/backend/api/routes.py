import os
import uuid
import asyncio
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from services.analysis import start_analysis, get_analysis_status
from services.storage import list_reports, get_report

router = APIRouter()

UPLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "raw"))

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
    if not file.filename.endswith((".docx", ".pdf", ".txt")):
        raise HTTPException(status_code=400, detail="Only .docx, .pdf, and .txt files are supported.")
    
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    
    analysis_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
        
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
