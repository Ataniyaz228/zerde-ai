# Zerde Backend — Python 3.12 + system deps for OCR/PDF
# Build:  docker compose build backend
# Size:   ~6 GB — torch + sentence-transformers are still CORE deps in pyproject,
#         so `pip install -e .` pulls them in. Phase 4 of the audit moves semantic
#         search to a `[semantic]` extra; until then the image cannot shed torch.

FROM python:3.12-slim AS base

# System deps: tesseract for OCR fallback, poppler for PDF rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-kaz \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (cached layer) ──────────────────────────────────────────────
COPY pyproject.toml README.md ./
# Install the project in editable mode so zerde package is importable.
# We copy the minimal source first for layer caching, then the rest.
COPY zerde/__init__.py zerde/__init__.py
COPY main.py ./

# Installs everything in pyproject, INCLUDING torch + sentence-transformers
# (~2-3 GB). These are core deps today; once Phase 4 carves out a `[semantic]`
# extra, drop them here and pull back only via `pip install -e ".[semantic]"`.
#
# BuildKit cache mount: wheels (torch!) persist in a build cache across builds,
# so editing pyproject re-runs the install but does NOT re-download torch.
RUN --mount=type=cache,target=/root/.cache/pip pip install -e .

# ── Backend-specific deps ───────────────────────────────────────────────────
COPY web/backend/requirements.txt /tmp/backend-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/backend-requirements.txt && rm /tmp/backend-requirements.txt

# ── Application code ────────────────────────────────────────────────────────
COPY zerde/ zerde/
COPY web/backend/ web/backend/

# ── Directories expected at runtime ─────────────────────────────────────────
RUN mkdir -p /app/output /app/data/raw /app/uploads

EXPOSE 8000

# Default: production mode (no --reload)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "web/backend"]
