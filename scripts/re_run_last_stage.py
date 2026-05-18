"""
Утилита для перезапуска парсинга и рендеринга отчета из кэшированного JSON.
Используется, если пайплайн упал на этапе парсинга/рендеринга, чтобы не тратить дорогие токены DeepSeek.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Добавляем корневую директорию проекта в sys.path
sys.path.append(str(Path(__file__).parent.parent))

from zerde.config import get_settings
from zerde.stages.s5_analyst import _parse_analysis
from zerde.stages.s6_auditor import audit_analysis
from zerde.stages.s7_render import render_report
from zerde.stages.s1_ingest import ingest_document
from zerde.stages.s2_planner import build_query_plan
from zerde.stages.s3_gather import gather_evidence
from zerde.stages.s4_fusion import fuse_and_validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("re_run_last_stage")

async def main():
    if len(sys.argv) < 2:
        print("Использование: python scripts/re_run_last_stage.py <путь_к_исходному_pdf> [путь_к_raw_json]")
        sys.exit(1)
        
    pdf_path = sys.argv[1]
    raw_json_path = sys.argv[2] if len(sys.argv) > 2 else "last_analyst_raw_response.json"
    
    if not Path(raw_json_path).exists():
        logger.error(f"Файл с сырым ответом {raw_json_path} не найден! Нечего парсить.")
        sys.exit(1)
        
    logger.info(f"Загрузка сырого ответа Аналитика из: {raw_json_path}")
    with open(raw_json_path, "r", encoding="utf-8") as f:
        raw_json = json.load(f)
        
    logger.info("Восстановление состояния пайплайна...")
    settings = get_settings()
    
    # Запускаем дешевые этапы до Fusion, которые кэшируются
    doc_state = await ingest_document(pdf_path)
    query_plan = await build_query_plan(doc_state)
    raw_chunks = await gather_evidence(query_plan)
    fused_chunks = await fuse_and_validate(raw_chunks)
    active_chunks = [c for c in fused_chunks if not c.is_duplicate]
    
    logger.info("Парсинг JSON структуры (Этап 5)...")
    # Используем исправленный парсер
    analysis = _parse_analysis(raw_json, query_plan, settings.llm_model_analyst)
    
    logger.info("Запуск Аудитора (Этап 6)...")
    audited_analysis = audit_analysis(analysis, active_chunks)
    
    logger.info("Рендеринг отчёта (Этап 7)...")
    report_md = await render_report(audited_analysis, active_chunks)
    
    logger.info("Готово! Отчёт успешно пересобран без повторных запросов к DeepSeek.")

if __name__ == "__main__":
    asyncio.run(main())
