# Архитектура Zerde AI

## Обзор

Zerde — это retrieval-grounded verification pipeline. Он не генерирует юридические заключения из знаний LLM, а проверяет утверждения входного документа против реальной правовой базы Казахстана (adilet.zan.kz) и открытых источников.

Система построена как цепочка функциональных стадий, связанных через строго типизированные Pydantic-модели. Каждая стадия — чистая (или почти чистая) асинхронная функция, не знающая о предыдущих и последующих стадиях.

## Стадии пайплайна

### S1 — Document Ingestion (`s1_ingest.py`)

Принимает файл документа и возвращает нормализованный текст.

**Вход:** путь к файлу (.docx, .pdf, .txt)

**Выход:** `DocumentState` — текст + метаданные

**Логика:**
- DOCX → python-docx прямое извлечение
- PDF → pymupdf извлечение текста → если текста мало, fallback на блочное извлечение → если и это не даёт результат, Tesseract OCR (русский + казахский)
- TXT → определение кодировки через chardet
- Нормализация: казахская транслитерация (`kz_translit.py`), удаление мусорных символов
- Результат: `DocumentState` с `normalized_text`, `char_count`, `language_detected`

---

### S2 — Query Planner (`s2_planner.py`)

LLM строит план сбора доказательств на основе текста документа.

**Вход:** `DocumentState`

**Выход:** `QueryPlan` — списки запросов к adilet и web

**Логика:**
- LLM получает текст документа и возвращает JSON с запросами
- Для каждого запроса: текст, ID законов, номера статей, даты
- Отдельные списки для русских, казахских и английских web-запросов
- Модель: `settings.llm_model_planner` (по умолчанию `gemini-3.5-flash`)
- Лимит: `settings.llm_max_tokens_planner` (8192 токенов)

---

### S2.5 — Claim Extractor (`s2_5_claim_extractor.py`)

Гибридный (regex + LLM) извлекатель утверждений из документа.

**Вход:** `DocumentState`

**Выход:** `ClaimExtractionResult` — список `DocumentClaim`

**Логика:**
1. **Regex-фаза**: извлечение ссылок на законы, статьи, суммы, сроки
2. **LLM-фаза**: извлечение сложных утверждений, которые regex не ловит
3. **Дедупликация**: удаление дублирующихся claims
4. **Детерминированные вердикты**: `reference_data.py` даёт вердикты по известным фактам без LLM (87-IV → несуществующий закон, правильный 94-V)
5. **Классификация**: каждый claim получает `severity` (CRITICAL/HIGH/MEDIUM/LOW) и `claim_type` (LEGAL_ID, LEGAL_REF, FINANCIAL, TEMPORAL, FACTUAL, NORMATIVE)

Claim ID детерминированный (`claim_0001`, `claim_0002`, ...) — LLM-сгенерированные ID никогда не используются.

---

### S2.7 — Self-Check (`s2_7_self_check.py`)

Детерминированный детектор внутренних противоречий.

**Вход:** нормализованный текст документа

**Выход:** список `DocumentClaim` с `is_structural=False`

**Логика:**
- Без LLM, чистый regex
- Ищет случаи, когда один и тот же закон/статья упоминается с разными значениями
- Пример: «штраф 500 МРП» в одном месте и «штраф 300 МРП» в другом

---

### S3 — Evidence Gathering (`s3_gather.py`)

Сбор доказательств из внешних и внутренних источников.

**Вход:** `QueryPlan`

**Выход:** список `EvidenceChunk`

**Логика:**
- **Adilet-запросы**: HTTP-запрос к adilet.zan.kz → парсинг HTML → извлечение статей по тегу `<b>Статья N. …</b>` → разбивка на чанки
  - TLS-верификация отключена (`verify=False`) — сертификат adilet нестабильный
  - Fallback-стратегии: XHR → CSS_SELECTOR → PDF_OCR
- **Web-запросы**: DuckDuckGo (по умолчанию) или Tavily/Google/Serper
- **Каждый чанк** получает `legal_rank` (от INTERNATIONAL_TREATY до MEDIA_UNKNOWN) и `web_tier` (TIER_1/2/3/BLACKLIST)

---

### S3.5 — Local RAG Injection (внутри `pipeline.py`)

Дополняет результаты S3 чанками из локального кэша.

**Логика:**
1. Прямой SQL-запрос по парам `(law_id, article)` из QueryPlan
2. Семантический поиск `search_local()` (BGE-M3 + BM25 WLC fusion)
3. Все инъецированные чанки помечаются `adilet_fallback_used=LOCAL_CACHE`

---

### S3.6 — Claim-driven Injection (внутри `pipeline.py`)

Дополняет результаты чанками по парам `(law_id, article)` из claims.

**Зачем**: Planner (S2) не видит будущих claims, поэтому S3.5 может пропустить статьи, упоминаемые только в claims.

---

### S4 — Fusion & Validation (`s4_fusion.py`)

Дедупликация и детекция конфликтов.

**Вход:** список `EvidenceChunk`

**Выход:** список `EvidenceChunk` с проставленными флагами

**Логика:**
- **SHA256 дедупликация**: точные дубликаты
- **Cosine дедупликация** (если embedding доступен): порог `settings.cosine_similarity_threshold` (0.92)
- **Фильтрация спама**: чанки без правового сигнала → `is_duplicate=True`
- **Конфликты**:
  - `HIERARCHY` — разница `legal_rank > 2` по одному домену
  - `TEMPORAL` — одинаковый `law_id+article`, разные `effective_date`
  - `FACTUAL` — расхождение чисел/дат (regex)

---

### S5 — LLM Auditor (`s5_analyst.py`)

Claim-by-claim верификация с цитатами.

**Вход:** активные чанки + `QueryPlan` + `ClaimExtractionResult` + текст документа

**Выход:** `AnalysisJSON` с заполненными `verdicts`

**Логика:**
1. **Retrieval per claim**: для каждого claim подбираются релевантные чанки:
   - Forced adilet/conflict чанки
   - Exact match по `law_id+article`
   - BM25 per-claim
2. **Чанки → LLM**: чанки показываются как короткие метки (S1, S2, ...), LLM возвращает метки в ответе
3. **Label remap**: `_remap_source_ids` конвертирует метки обратно в полные chunk_id (галлюцинированные метки дропаются)
4. **Вердикт**: CONFIRMED / CONTRADICTED / UNVERIFIED
5. Модель: `settings.llm_model_analyst` (по умолчанию `gemini-3.5-flash`)

**Надёжность вызовов** (`utils/llm_client.py`): каждый LLM-вызов обёрнут в wall-clock `asyncio.wait_for` (`ZERDE_LLM_OVERALL_TIMEOUT_S`), иначе «капающий» провайдер вешал слот семафора и весь S5. Транзиентные ошибки (вкл. таймаут) ретраятся tenacity; упавший батч fail-closed → claim'ы становятся UNVERIFIED (никогда CONFIRMED). Для OpenRouter закрепляется провайдер (`ZERDE_OPENROUTER_PROVIDER_ORDER`) ради воспроизводимости — не влияет на ключ кэша.

---

### S5.2 — Contradiction Verifier (`s5_2_verifier.py`)

Детерминированная проверка вердиктов CONTRADICTED.

**Логика:**
- Если CONTRADICTED вердикт не подкреплён цитатой из чанка → понижение до UNVERIFIED
- Защита от ложных противоречий: LLM иногда «находит» противоречие, которого нет в доказательствах

---

### S5.5 — Policy Analyst (`s5_5_analyst.py`)

LLM-анализ политических и социальных последствий.

**Вход:** текст документа + analysis + чанки

**Выход:** анализ рисков, затронутых сторон, рекомендации

---

### S6 — BM25 Deterministic Audit (`s6_auditor.py`)

Самая большая стадия (~1400 строк). Детерминированный аудит без LLM.

**Вход:** `AnalysisJSON` + чанки + claims

**Выход:** обогащённый `AnalysisJSON` с reliability score

**Логика:**
- BM25 скоринг каждого факта: HIGH/MEDIUM/LOW/UNVERIFIED по порогам
- Extraction article verification — проверка что статьи существуют в корпусе
- Арифметическая верификация (sympy) — проверка вычислений
- Topological audit — проверка иерархии ссылок
- Reliability score: штрафная модель на основе долей CONTRADICTED/UNVERIFIED

---

### S7 — Report Renderer (`s7_render.py`)

Формирование финального Markdown-отчёта.

**Вход:** `AnalysisJSON` + чанки + путь вывода + policy analysis

**Выход:** Markdown-строка + файл

**Содержание отчёта:**
- Краткая сводка
- **Целостность грунтования** — предупреждает (НЕ меняя вердиктов), когда вывод опирается на источник без подтверждённого adilet-кода или когда в корпусе конфликтуют редакции одного закона. No-op на обычных биллях.
- Таблица вердиктов (CONFIRMED/CONTRADICTED/UNVERIFIED)
- Выявленные конфликты и коллизии
- Структурный чеклист
- Policy analysis
- Reliability score и рекомендация

**Грунтование/презентация (детерминированно, без влияния на метрику):** имена и ссылки источников берутся из реестра (`adilet_code`, title), а не из синтезированного `НПА {код}`; внутренние метки источников (`S1/S5/…`) вычищаются из прозы; веб-источники не попадают в нормативную иерархию (только `adilet.zan.kz` авторитетен).

---

## Модели данных

Все обмены между стадиями через типизированные Pydantic-модели (`zerde/models.py`):

| Модель | Стадия | Описание |
|---|---|---|
| `DocumentState` | S1 → S2 | Нормализованный документ |
| `QueryPlan` | S2 → S3 | План запросов |
| `DocumentClaim` | S2.5 → S5 | Извлечённое утверждение |
| `ClaimExtractionResult` | S2.5 → Pipeline | Все утверждения |
| `EvidenceChunk` | S3 → S7 | Фрагмент доказательной базы |
| `ClaimVerdict` | S5 → S7 | Результат верификации |
| `AnalysisJSON` | S5 → S7 | Полная аналитическая структура |
| `Fact` | S5 → S6 | Утверждение с источниками |
| `ConflictRecord` | S6 → S7 | Выявленный конфликт |

## Диаграмма зависимостей

```
DocumentState ──→ QueryPlan ──→ [EvidenceChunk] ──→ AnalysisJSON ──→ Report.md
       │                              ↑                    ↑
       └──→ ClaimExtractionResult ────┘                    │
                    │                                      │
                    └──────────────────────────────────────┘
```

## Параллелизм

Пайплайн использует `asyncio.gather` для параллельного выполнения независимых стадий:

1. **S2 + S2.5 + S2.7** — Planner + Claim Extractor + Self-Check
2. **S5.5 + S6** — Policy Analyst + BM25 Audit

Стадии в каждой группе работают с deep copies данных, чтобы избежать мутации разделяемого состояния.

## Идентификация законов

`law_registry.py` — единственный источник истины для разрешения ID законов.

Порядок разрешения:
1. Точное совпадение `law_id`
2. Совпадение `adilet_code`
3. Статический маппинг `code → law_id`
4. Strict base-ID match (без fuzzy)
5. Таблица alias'ов
6. Return-as-is для ID-образного ввода
7. Title exact/substring/difflib (cutoff 0.6)

**Правило**: никогда не добавляй per-stage словари ID законов. Все маппинги — только через реестр.
