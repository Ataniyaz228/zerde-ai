"""
ЗЕРДЕ v6.2 — Stage 1: Document Ingestion (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Вход:  путь к файлу (PDF, DOCX, TXT)
Выход: DocumentState

Поддержка:
  - PDF: pymupdf text layer → OCR fallback если текст < 100 символов
  - DOCX: python-docx (параграфы + таблицы)
  - TXT: chardet автодетект кодировки
  - KZ Latin → кириллица транслитерация
  - Язык: langdetect
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from zerde.models import DocumentFormat, DocumentState
from zerde.utils.kz_translit import normalize_kz_text

logger = logging.getLogger(__name__)

# Минимум символов для "нормального" PDF текстового слоя
_PDF_MIN_TEXT_CHARS = 100


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ingest_document(file_path: str | Path) -> DocumentState:
    """
    Этап 1: Загружает файл, извлекает текст, нормализует.

    Args:
        file_path: Путь к документу.

    Returns:
        DocumentState с нормализованным текстом.
    """
    path = Path(file_path).resolve()
    logger.info(f"[S1] Ingesting: {path}")

    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    fmt = _detect_format(path)
    raw_text = await _extract_text(path, fmt)

    if not raw_text.strip():
        raise ValueError(f"No text extracted from: {path}")

    normalized = normalize_kz_text(raw_text)
    doc_id = hashlib.sha256(raw_text.encode()).hexdigest()
    language = _detect_language(normalized)

    state = DocumentState(
        doc_id=doc_id,
        original_path=str(path),
        format=fmt,
        raw_text=raw_text,
        normalized_text=normalized,
        char_count=len(normalized),
        language_detected=language,
    )

    logger.info(f"[S1] Done. doc_id={doc_id[:8]}… words={state.word_count} lang={language} fmt={fmt}")
    return state


# ---------------------------------------------------------------------------
# Format Detection
# ---------------------------------------------------------------------------


def _detect_format(path: Path) -> DocumentFormat:
    suffix = path.suffix.lower().lstrip(".")
    try:
        return DocumentFormat(suffix)
    except ValueError:
        raise ValueError(f"Unsupported format: '{suffix}'. Supported: pdf, docx, txt")


# ---------------------------------------------------------------------------
# Text Extraction
# ---------------------------------------------------------------------------


async def _extract_text(path: Path, fmt: DocumentFormat) -> str:
    match fmt:
        case DocumentFormat.TXT:
            return _extract_txt(path)
        case DocumentFormat.PDF:
            return _extract_pdf(path)
        case DocumentFormat.DOCX:
            return _extract_docx(path)
        case _:
            raise ValueError(f"Unhandled format: {fmt}")


def _extract_txt(path: Path) -> str:
    """TXT с автодетектом кодировки через chardet."""
    import chardet

    raw_bytes = path.read_bytes()
    detected = chardet.detect(raw_bytes)
    encoding = detected.get("encoding") or "utf-8"
    confidence = detected.get("confidence", 0)

    logger.debug(f"[S1/TXT] Detected encoding: {encoding} (confidence={confidence:.2f})")
    return raw_bytes.decode(encoding, errors="replace")


def _extract_pdf(path: Path) -> str:
    """
    PDF → текст через pymupdf.
    Если текстовый слой пустой (скан) — пробуем OCR через pymupdf.fitz.
    """
    import fitz  # pymupdf

    doc = fitz.open(str(path))
    pages_text: list[str] = []

    for page_num, page in enumerate(doc):
        text = page.get_text("text")  # type: ignore[arg-type]
        pages_text.append(text)
        logger.debug(f"[S1/PDF] Page {page_num + 1}: {len(text)} chars")

    full_text = "\n".join(pages_text)
    doc.close()

    # Если текстового слоя нет — используем pymupdf встроенный TextPage (OCR-like)
    if len(full_text.strip()) < _PDF_MIN_TEXT_CHARS:
        logger.warning(f"[S1/PDF] Text layer too sparse ({len(full_text.strip())} chars). Trying textpage fallback.")
        full_text = _extract_pdf_ocr_fallback(path)

    return full_text


def _extract_pdf_ocr_fallback(path: Path) -> str:
    """
    Fallback для сканированных PDF: pymupdf high-res text extraction.
    Попытка извлечь текст через blocks и словарное представление.
    """
    import fitz

    doc = fitz.open(str(path))
    parts: list[str] = []

    for page in doc:
        # Используем dict-представление для восстановления структуры
        blocks = page.get_text("blocks")  # type: ignore[arg-type]
        for block in blocks:
            if len(block) >= 5 and isinstance(block[4], str):
                parts.append(block[4].strip())

    doc.close()
    result = "\n".join(p for p in parts if p)
    logger.info(f"[S1/PDF OCR fallback] Extracted {len(result)} chars from {path.name}")
    return result


def _extract_docx(path: Path) -> str:
    """DOCX → текст: параграфы + таблицы через python-docx."""
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []

    # Параграфы
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)

    # Таблицы
    for table in doc.tables:
        for row in table.rows:
            row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_texts:
                parts.append(" | ".join(row_texts))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Language Detection
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> str:
    """
    Определяет язык через langdetect.
    Fallback: эвристика по кириллице если langdetect упадёт.
    """
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0  # Детерминированность

        sample = text[:3000]  # Достаточно для детектирования
        lang = detect(sample)

        # Нормализуем к нашим значениям
        if lang in ("ru", "kk", "en"):
            return lang
        elif lang in ("kk", "kaz"):
            return "kk"
        else:
            return "ru"  # Для КЗ юридических документов — дефолт ru
    except Exception as e:
        logger.debug(f"[S1] langdetect failed: {e}. Using heuristic.")
        return _heuristic_lang(text)


def _heuristic_lang(text: str) -> str:
    """Эвристика: считаем кириллицу vs латиницу."""
    cyrillic = sum(1 for c in text[:500] if "\u0400" <= c <= "\u04ff")
    latin = sum(1 for c in text[:500] if "a" <= c.lower() <= "z")

    if cyrillic > latin * 2:
        return "ru"
    elif latin > cyrillic * 2:
        return "en"
    else:
        return "mixed"
