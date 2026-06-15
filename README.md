# Zerde AI — Правовой аудит законопроектов Казахстана

Zerde — это автоматизированный пайплайн проверки юридической корректности казахстанских законопроектов (НПА РК). Система принимает документ, извлекает из него утверждения, ищет доказательства в правовой базе adilet.zan.kz и открытых источниках, и формирует аудиторский отчёт с вердиктами по каждому утверждению.

## Принцип работы

Документ проходит через 12-этапный пайплайн:

```
┌──────────────────────────────────────────────────────────────┐
│  Входной документ (.docx / .pdf / .txt)                      │
└──────────────┬───────────────────────────────────────────────┘
               ▼
         ┌─────────┐
         │ S1      │ Извлечение текста (PDF → OCR → текст)
         │ Ingest  │
         └────┬────┘
              ▼
    ┌─────────────────────┐
    │ S2 + S2.5 + S2.7    │ Параллельно:
    │ Planner + Claims +  │ • LLM строит план запросов
    │ Self-Check          │ • Regex + LLM извлекают утверждения
    └─────────┬───────────┘ • Детектор внутренних противоречий
              ▼
    ┌─────────────────────┐
    │ S3 + S3.5 + S3.6    │ Сбор доказательств:
    │ Evidence Gathering  │ • Adilet (парсинг статей)
    │ + Local RAG         │ • Web-поиск (DuckDuckGo/Tavily)
    └─────────┬───────────┘ • Локальный RAG из корпуса
              ▼
         ┌─────────┐
         │ S4      │ Дедупликация, фильтрация спама,
         │ Fusion  │ детекция юридических конфликтов
         └────┬────┘
              ▼
    ┌─────────────────────┐
    │ S5 + S5.2           │ LLM-аудитор: claim-by-claim
    │ Auditor + Verifier  │ верификация с цитатами
    └─────────┬───────────┘
              ▼
    ┌─────────────────────┐
    │ S5.5 + S6           │ Параллельно:
    │ Policy + BM25 Audit │ • LLM policy analyst
    └─────────┬───────────┘ • Детерминированный BM25 аудит
              ▼
         ┌─────────┐
         │ S7      │ Markdown-отчёт с вердиктами,
         │ Render  │ конфликтами и reliability score
         └─────────┘
```

## Быстрый старт

### Требования

- Python 3.12+
- API-ключ LLM-провайдера (Gemini, OpenRouter или OpenAI)
- ~4 ГБ свободного места (torch + модели)

### Установка

```bash
git clone https://github.com/Ataniyaz228/zerde-ai.git
cd zerde-ai

python -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

### Настройка

Скопируй `.env.example` в `.env` и заполни ключи:

```bash
cp .env.example .env
# Минимум — OPENAI_API_KEY
```

### Запуск через CLI

```bash
python main.py документ.docx
```

Результат: `output/zerde_report_*.md` — полный аудиторский отчёт.

### Запуск через Web-интерфейс

```bash
# Терминал 1 — Backend (FastAPI)
bash web/backend/dev.sh

# Терминал 2 — Frontend (Next.js)
cd web/frontend && npm run dev
```

Открой http://localhost:3000, загрузи документ, дождись отчёта.

### Docker Compose

```bash
docker compose build
docker compose up
# → http://localhost:3000
```

## Структура проекта

```
zerde/
├── main.py                    # CLI точка входа
├── zerde/
│   ├── config.py              # Конфигурация (pydantic-settings)
│   ├── models.py              # Pydantic-модели (контракты между стадиями)
│   ├── pipeline.py            # Оркестратор пайплайна
│   ├── reference_data.py      # Детерминированные проверки без LLM
│   ├── stages/
│   │   ├── s1_ingest.py       # Извлечение текста
│   │   ├── s2_planner.py      # LLM-планировщик запросов
│   │   ├── s2_5_claim_extractor.py  # Извлечение утверждений
│   │   ├── s2_7_self_check.py # Внутренние противоречия
│   │   ├── s3_gather.py       # Сбор доказательств
│   │   ├── s4_fusion.py       # Дедупликация и конфликты
│   │   ├── s5_analyst.py      # LLM-аудитор
│   │   ├── s5_5_analyst.py    # Policy analyst
│   │   ├── s5_2_verifier.py   # Верификатор противоречий
│   │   ├── s6_auditor.py      # BM25 детерминированный аудит
│   │   └── s7_render.py       # Рендеринг Markdown-отчёта
│   └── utils/
│       ├── cache.py           # SQLite кэш + BGE-M3 семантический поиск
│       ├── law_registry.py    # Реестр казахстанских законов
│       ├── llm_client.py      # OpenAI-совместимый LLM-клиент
│       ├── legal_scorer.py    # Скоринг юридических источников
│       └── kz_translit.py     # Казахская транслитерация
├── web/
│   ├── backend/               # FastAPI + WebSocket
│   └── frontend/              # Next.js UI
├── scripts/                   # Утилиты (корпус, eval, калибровка)
├── tests/                     # pytest-asyncio тесты
├── data/                      # Входные документы
└── output/                    # Сгенерированные отчёты
```

## Документация

| Документ | Описание |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Архитектура пайплайна и стадии |
| [docs/configuration.md](docs/configuration.md) | Переменные окружения и настройка |
| [docs/api.md](docs/api.md) | REST API и WebSocket протокол |
| [docs/corpus.md](docs/corpus.md) | Управление правовым корпусом |
| [docs/development.md](docs/development.md) | Разработка, тесты, скрипты |
| [docs/deployment.md](docs/deployment.md) | Docker, деплой, продакшн |

## Ключевой принцип

**CITE-OR-ABSTAIN**: система никогда не подтверждает утверждение без цитаты из источника. Отсутствие доказательств → UNVERIFIED, не CONFIRMED. Худшая ошибка — ложное подтверждение неверной юридической нормы.

## Лицензия

Проприетарный код. Все права защищены.
