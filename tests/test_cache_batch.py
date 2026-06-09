"""F-B3: батч-чтение _get_many должно давать те же чанки, что и N×get()
(тот же deserialize + heal-ранг), плюс корректно пропускать отсутствующие id.
Этот путь (search_local) не покрывается verdict_eval/run_eval — прямой
эквивалентный тест на реальном кэше.
"""
import pytest

from zerde.config import get_settings
from zerde.utils.cache import CacheManager


async def test_get_many_matches_single_get():
    cm = CacheManager(get_settings().cache_db_path)
    with cm._conn() as conn:
        ids = [r["chunk_id"] for r in conn.execute(
            "SELECT chunk_id FROM evidence_cache LIMIT 7"
        ).fetchall()]
    if not ids:
        pytest.skip("empty cache corpus")

    many = await cm._get_many(ids + ["___nonexistent___"])

    # отсутствующий id не появляется
    assert "___nonexistent___" not in many
    assert await cm.get("___nonexistent___") is None

    # для каждого реального id батч == одиночный get (включая heal-ранг)
    for cid in ids:
        single = await cm.get(cid)
        assert single is not None
        assert cid in many
        assert many[cid].model_dump_json() == single.model_dump_json()


async def test_get_many_empty():
    cm = CacheManager(get_settings().cache_db_path)
    assert await cm._get_many([]) == {}
