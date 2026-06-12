# Разработка

## Настройка среды

```bash
# Клонирование
git clone https://github.com/Ataniyaz228/zerde-ai.git
cd zerde-ai

# Виртуальное окружение (Python 3.12)
python3.12 -m venv .venv
source .venv/bin/activate

# Установка зависимостей
pip install -e ".[dev]"

# Настройка
cp .env.example .env
# Заполни OPENAI_API_KEY
```

## Тестирование

Тесты используют `pytest` с `pytest-asyncio` в режиме `auto`.

### Запуск всех тестов

```bash
.venv/bin/python -m pytest -q
```

Время прогона: ~80 секунд. Ожидаемый baseline: ~7-11 тестов fail (стабильно падающие тесты, проверяющие устаревшее поведение).

### Запуск одного теста

```bash
.venv/bin/python -m pytest tests/test_pipeline_no_llm.py::TestS4SpamFilter::test_no_legal_signal_is_spam
```

### Запуск с coverage

```bash
.venv/bin/python -m pytest --cov=zerde --cov-report=html
```

### Тестовые файлы

| Файл | Что тестирует |
|---|---|
| `test_pipeline_no_llm.py` | Стадии S1-S4 без LLM (моки) |
| `test_claim_extractor.py` | S2.5 извлечение утверждений |
| `test_claim_extractor_fixes.py` | Регрессии claim extractor |
| `test_cache_search.py` | search_local, BM25, семантика |
| `test_vector_rag.py` | Векторный RAG поиск |
| `test_rag_boundaries.py` | Граничные случаи RAG |
| `test_offline_fallback.py` | Работа без сети |
| `test_v7_features.py` | Функции v7 (вердикты, конфликты) |
| `test_verdict_gate.py` | Гейты вердиктов |
| `test_verifier_inflection_guard.py` | Морфология в верификаторе |
| `test_search_providers.py` | DuckDuckGo/Tavily/Serper |
| `test_law_dict_canonical.py` | Каноничность ID законов в словарях |
| `test_registry_codes_verified.py` | Валидность adilet-кодов в реестре |
| `test_adaptive_chunking.py` | Адаптивный chunking |
| `test_metadata_first_no_false_confirm.py` | False-confirm prevention |
| `test_planner_law_hints.py` | Law hints в planner |
| `test_update_corpus.py` | Скрипт update_corpus |

## Линтинг

```bash
# Ruff (быстрый линтер/форматтер)
.venv/bin/ruff check .

# MyPy (типы)
.venv/bin/mypy zerde
```

Конфигурация в `pyproject.toml`:
- `ruff`: target Python 3.12, line-length 100, правила E/F/I/N/W/UP
- `mypy`: strict mode, ignore missing imports

## Утилиты (scripts/)

### Eval-скрипты

| Скрипт | Описание |
|---|---|
| `scripts/eval/run_eval.py` | Label-free grounding eval (бесплатно, без LLM) |
| `scripts/eval/verdict_eval.py` | Verdict-level eval: false_confirm_rate (нужен LLM) |
| `scripts/eval_golden.py` | Golden-set eval |

```bash
# Grounding eval (30 сэмплов)
PYTHONPATH=. .venv/bin/python scripts/eval/run_eval.py --sample 30

# Verdict eval
PYTHONPATH=. .venv/bin/python scripts/eval/verdict_eval.py
```

### Скрипты корпуса

| Скрипт | Описание |
|---|---|
| `scripts/ingest_docs.py` | Массовая загрузка документов в корпус |
| `scripts/ingest_single_law.py` | Загрузка одного закона по ID |
| `scripts/update_corpus.py` | Обновление существующих законов с adilet |
| `scripts/fix_corpus.py` | Автоисправление корпуса (dry-run / --apply) |
| `scripts/embed_existing.py` | Генерация BGE-M3 эмбеддингов |
| `scripts/heal_unknown_law_ids.py` | Восстановление неизвестных law_id |

### Верификация

| Скрипт | Описание |
|---|---|
| `scripts/verify_law_registry.py` | law_id ↔ adilet_code проверка |
| `scripts/verify_corpus_articles.py` | Статьи корпуса vs. adilet (--fix-titles) |
| `scripts/smoke_test.py` | S1+S2.5 регрессия по data/corpus/ |

### Калибровка

| Скрипт | Описание |
|---|---|
| `scripts/calibrate_bm25.py` | Калибровка BM25 порогов |
| `scripts/calibrate_wlc.py` | Калибровка весов WLC fusion |

### Другое

| Скрипт | Описание |
|---|---|
| `scripts/scrape_mazhilis.py` | Парсинг законопроектов с сайта Мажилиса |
| `scripts/setup_ocr.sh` | Установка Tesseract OCR |

## CLI

```bash
# Запуск анализа
python main.py документ.docx

# Вывод
# ✅ Отчёт сохранён: output/zerde_report_документ_20260612_120000.md
# ⏱️  Время: 45.3с
# 📊 Фактов: 14
# 🔒 Надёжность: 0.78
```

Поддерживаемые форматы: `.docx`, `.pdf`, `.txt`.

Результат сохраняется в `output/`.

## Backend (dev mode)

```bash
bash web/backend/dev.sh
```

Скрипт запускает `uvicorn app:app` с двумя `--reload-dir`:
- `--reload-dir .` — изменения в `web/backend/`
- `--reload-dir ../../zerde` — изменения в `zerde/` (без этого модуль остаётся стейл)

## Frontend (dev mode)

```bash
cd web/frontend
npm install
npm run dev
```

Next.js на порту 3000. Проксирует API к backend на 8000.

## Правила контрибуции

1. **Не добавляй per-stage словари ID законов** — все маппинги через `law_registry.py`
2. **Перед изменением аудитора, fusion или grounding** — спроси: «может ли это повысить false-confirm rate?»
3. **Ground truth — adilet.zan.kz**, не кэш, не LLM, не статические данные
4. **Всегда бэкапь `zerde_cache.db`** перед записью
5. **Гейти изменения** eval-скриптами (`run_eval.py`, `verdict_eval.py`)
6. **Claim ID детерминированные** (`claim_NNNN`), никогда не используй LLM-сгенерированные
