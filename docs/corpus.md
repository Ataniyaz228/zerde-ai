# Правовой корпус

Zerde работает с локальным правовым корпусом — SQLite-базой `zerde_cache.db`, содержащей чанки из законов Республики Казахстан.

## Структура базы данных

### evidence_cache

Основная таблица — хранит чанки (фрагменты) законов.

```sql
CREATE TABLE evidence_cache (
    chunk_id     TEXT PRIMARY KEY,   -- SHA256 от контента
    source_url   TEXT NOT NULL,      -- URL источника (adilet.zan.kz/...)
    content_hash TEXT NOT NULL,      -- Дублирует chunk_id
    chunk_json   TEXT NOT NULL,      -- Полный EvidenceChunk в JSON
    cached_at    TEXT NOT NULL       -- ISO timestamp
);
```

`chunk_json` содержит полную модель `EvidenceChunk` (контент, law_id, article, legal_rank и т.д.).

### evidence_embeddings

Векторные представления чанков для семантического поиска.

```sql
CREATE TABLE evidence_embeddings (
    chunk_id  TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,        -- float32 numpy array (1024 dim, BGE-M3)
    FOREIGN KEY(chunk_id) REFERENCES evidence_cache(chunk_id) ON DELETE CASCADE
);
```

### law_metadata

Реестр загруженных законов.

```sql
CREATE TABLE law_metadata (
    law_id      TEXT PRIMARY KEY,   -- Короткий ID: "261-IV"
    adilet_code TEXT,               -- Полный код: "Z100000261_"
    title_ru    TEXT,               -- "О цифровом майнинге"
    title_kz    TEXT,               -- Казахский заголовок
    chunk_count INTEGER DEFAULT 0,  -- Количество чанков
    updated_at  TEXT                -- ISO timestamp
);
```

### llm_response_cache

Кэш LLM-ответов для экономии токенов.

```sql
CREATE TABLE llm_response_cache (
    cache_key     TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    response_json TEXT NOT NULL,
    cached_at     TEXT NOT NULL,
    expires_at    TEXT              -- NULL = permanent
);
```

## Текущее состояние

```bash
# Проверить статистику
python -c "
from zerde.utils.cache import CacheManager
import asyncio
cm = CacheManager()
print(asyncio.run(cm.stats()))
"
```

Типичный вывод: ~20 000 чанков, ~38 законов, ~178 МБ.

## Семантический поиск (search_local)

`CacheManager.search_local()` реализует гибридный поиск:

### Три параллельных стратегии

1. **SQL exact match** (Strategy 0) — точный поиск по `law_id` и `article` через `json_extract`
2. **BM25 лексический поиск** — с морфологической нормализацией (pymorphy3 для русского, ручные окончания для казахского)
3. **BGE-M3 семантический поиск** — косинусное сходство по 1024-мерным эмбеддингам

### WLC Fusion (Weighted Linear Combination)

Результаты трёх стратегий объединяются:

```
score = 0.55 × semantic + 0.30 × bm25 + 0.15 × sql_match
```

Затем применяется cross-encoder reranking (BAAI/bge-reranker-v2-m3) для финальной сортировки.

## Управление корпусом

### Добавление закона

```bash
PYTHONPATH=. .venv/bin/python scripts/ingest_single_law.py --law-id 550-IV --adilet-code Z1600000550
```

### Массовое обновление

```bash
PYTHONPATH=. .venv/bin/python scripts/update_corpus.py
```

Скрипт проходит по всем законам в `law_metadata`, скачивает актуальные версии с adilet.zan.kz и обновляет чанки.

### Верификация корпуса

```bash
# Проверка law_id ↔ adilet_code маппингов
PYTHONPATH=. .venv/bin/python scripts/verify_law_registry.py

# Проверка статей корпуса vs. adilet
PYTHONPATH=. .venv/bin/python scripts/verify_corpus_articles.py
```

### Исправление корпуса

```bash
# Dry-run (только показывает что исправит)
PYTHONPATH=. .venv/bin/python scripts/fix_corpus.py

# Применить (требует backup)
cp zerde_cache.db zerde_cache.db.bak
PYTHONPATH=. .venv/bin/python scripts/fix_corpus.py --apply
```

**Всегда делай бэкап** перед `--apply`:
```bash
cp zerde_cache.db zerde_cache.db.bak-$(date +%Y%m%d)
```

### Генерация эмбеддингов

```bash
PYTHONPATH=. .venv/bin/python scripts/embed_existing.py
```

Загружает BGE-M3 модель и генерирует векторные представления для всех чанков, у которых ещё нет эмбеддинга.

## Идентификация законов (law_registry.py)

`get_registry()` возвращает синглтон `LawRegistry`:

```python
from zerde.utils.law_registry import get_registry

registry = get_registry()

# Разрешение любого формата в каноничный ID
registry.resolve("550-IV")       # → "550-IV"
registry.resolve("Z1600000550")  # → "550-IV"
registry.resolve("Закон о цифровом майнинге")  # → "550-IV"

# Получить adilet-код
registry.get_adilet_code("550-IV")  # → "Z1600000550_"

# Все варианты написания для SQL-матчинга
registry.id_variants("550-IV")  # → ["550-IV", "Z1600000550", "Z1600000550_", ...]
```

## Предупреждения

- **Парсер adilet** иногда извлекает не все статьи из HTML-страницы. Поэтому `verify_corpus_articles.py` может показать «not-on-adilet» для реально существующих статей — нужна ручная проверка.
- **Не удаляй чанки автоматически** по результатам верификации — только после ручной проверки.
- **zerde_cache.db — единственный корпус**. CLI и backend смотрят в один файл (путь по умолчанию абсолютный).
