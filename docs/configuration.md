# Конфигурация

Все параметры читаются из `.env` файла через `pydantic-settings`. Конфигурация определена в `zerde/config.py`.

## Переменные окружения

### API-ключи

| Переменная | Обязательная | Описание |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | Ключ LLM-провайдера. Gemini: `AIza...`, OpenRouter: `sk-or-v1-...`, OpenAI: `sk-...` |
| `TAVILY_API_KEY` | ❌ | Ключ Tavily Search API (альтернатива DuckDuckGo) |
| `SERPER_API_KEY` | ❌ | Ключ Serper.dev Search API |
| `GOOGLE_API_KEY` | ❌ | Ключ Google CSE JSON API |
| `GOOGLE_CSE_ID` | ❌ | ID Google Custom Search Engine |
| `EMBEDDING_API_KEY` | ❌ | OpenAI ключ для embeddings (если LLM через OpenRouter) |

### LLM-провайдер

| Переменная | По умолчанию | Описание |
|---|---|---|
| `OPENAI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | Base URL провайдера |

Поддерживаемые провайдеры:
- **Gemini**: `https://generativelanguage.googleapis.com/v1beta/openai/`
- **OpenRouter**: `https://openrouter.ai/api/v1`
- **OpenAI**: `https://api.openai.com/v1`

### LLM-модели

Каждая стадия использует свою модель. Это позволяет назначить тяжёлую модель на критические стадии и лёгкую на простые:

| Переменная | По умолчанию | Стадия |
|---|---|---|
| `LLM_MODEL_PLANNER` | `gemini-3.5-flash` | S2 — Query Planner |
| `LLM_MODEL_EXTRACTOR` | `gemini-3.5-flash` | S2.5 — Claim Extractor |
| `LLM_MODEL_ANALYST` | `gemini-3.5-flash` | S5 — Auditor |
| `LLM_MODEL_POLICY_ANALYST` | `gemini-3.5-flash` | S5.5 — Policy Analyst |
| `LLM_MAX_TOKENS_PLANNER` | `8192` | Макс. токенов для Planner |
| `LLM_MAX_TOKENS_ANALYST` | `16384` | Макс. токенов для Auditor |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding (если ключ задан) |

### Надёжность LLM

| Переменная | По умолчанию | Описание |
|---|---|---|
| `ZERDE_LLM_OVERALL_TIMEOUT_S` | `240` | Жёсткий wall-clock потолок на один LLM-вызов целиком. httpx read-таймаут ловит только паузы между байтами; «капающий»/зависший провайдер иначе держал бы слот семафора и вешал S5. По таймауту вызов отменяется (tenacity ретраит как транзиент, затем fail-closed → UNVERIFIED). |
| `ZERDE_OPENROUTER_PROVIDER_ORDER` | `openai,deepinfra,fireworks` | Порядок предпочитаемых провайдеров OpenRouter (`provider.order`, `allow_fallbacks=true`). Закрепляет провайдера для воспроизводимости (одна модель у разных провайдеров даёт разный вывод даже при `temperature=0`). НЕ входит в ключ кэша. Применяется только при OpenRouter base_url. |

### Поиск

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SEARCH_PROVIDER` | `duckduckgo` | Провайдер web-поиска (`duckduckgo`, `tavily`, `google`, `serper`) |
| `WEB_MAX_RESULTS_PER_QUERY` | `10` | Макс. результатов на запрос |

### Adilet (adilet.zan.kz)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `ADILET_BASE_URL` | `https://adilet.zan.kz` | Base URL |
| `ADILET_TIMEOUT_SECONDS` | `30` | Таймаут запросов |
| `ADILET_MAX_ARTICLES_PER_LAW` | `50` | Лимит статей на закон |
| `ADILET_TLS_VERIFY` | `False` | TLS-верификация (отключена — сертификат нестабильный) |

### Fusion и BM25

| Переменная | По умолчанию | Описание |
|---|---|---|
| `COSINE_SIMILARITY_THRESHOLD` | `0.92` | Порог семантической дедупликации |
| `VALIDATION_THRESHOLD` | `0.25` | BM25 порог для статуса HIGH |
| `BM25_MEDIUM_THRESHOLD` | `0.12` | BM25 порог для статуса MEDIUM |
| `BM25_FALLBACK_THRESHOLD` | `0.20` | BM25 порог для автоматического корпусного поиска |

### Кэш и вывод

| Переменная | По умолчанию | Описание |
|---|---|---|
| `CACHE_DB_PATH` | `<project_root>/zerde_cache.db` | Путь к SQLite корпусу. **Обязательно абсолютный путь** |
| `OUTPUT_DIR` | `output` | Директория для отчётов |
| `LOG_LEVEL` | `INFO` | Уровень логирования |

### OpenRouter (опционально)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `OPENROUTER_SITE_URL` | `https://github.com/zerde` | Сайт для OpenRouter dashboard |
| `OPENROUTER_APP_NAME` | `""` | Название приложения |

## Пример .env

```env
# === LLM Provider ===
OPENAI_API_KEY=AIzaSy...your-gemini-key
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/

# === Models (по умолчанию gemini-3.5-flash для всех) ===
LLM_MODEL_PLANNER=gemini-3.5-flash
LLM_MODEL_EXTRACTOR=gemini-3.5-flash
LLM_MODEL_ANALYST=gemini-3.5-flash

# === Web Search ===
SEARCH_PROVIDER=duckduckgo

# === Cache ===
CACHE_DB_PATH=/home/user/zerde/zerde_cache.db

# === Logging ===
LOG_LEVEL=INFO
```

## Заметки

- `CACHE_DB_PATH` по умолчанию вычисляется как `<project_root>/zerde_cache.db`. Относительный путь однажды привёл к тому, что backend работал с пустой базой — используй абсолютный.
- Если используешь OpenRouter, cosine дедупликация (OpenAI embeddings) не работает без отдельного `EMBEDDING_API_KEY`. Дедупликация откатывается на SHA256.
- `ADILET_TLS_VERIFY=False` — это не баг, сертификат adilet.zan.kz регулярно не валидируется.
