# tests/fixtures/

`law_metadata.db` — минимальный SQLite-фикстур с одной таблицей `law_metadata`
(37 строк, ~25 KB), скопированной из `zerde_cache.db` (law_id↔adilet_code↔title).
Используется для офлайн-CI без полного 170MB корпуса (CACHE_DB_PATH /
ZERDE_CACHE_DB указывают на него — таблицы evidence_cache при этом пустые,
поэтому retrieval вернёт 0 чанков, но импорт/law_registry/конфиг не падают).

Регенерация:

```bash
.venv/bin/python - <<'EOF'
import sqlite3
src = sqlite3.connect("zerde_cache.db")
create_sql = src.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='law_metadata'"
).fetchone()[0]
dst = sqlite3.connect("tests/fixtures/law_metadata.db")
dst.execute(create_sql)
rows = src.execute("SELECT * FROM law_metadata").fetchall()
dst.executemany(f"INSERT INTO law_metadata VALUES ({','.join('?'*len(rows[0]))})", rows)
dst.commit()
EOF
```
