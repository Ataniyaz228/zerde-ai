
import pytest

from zerde.models import EvidenceChunk, LegalRank
from zerde.utils.cache import CacheManager

# search_local грузит BGE-M3 (torch) — деселектится на CI (-m 'not heavy').
pytestmark = pytest.mark.heavy


@pytest.mark.asyncio
async def test_cache_search_local(tmp_path):
    db_file = tmp_path / "test_cache.db"
    manager = CacheManager(db_path=str(db_file))

    # Create dummy evidence chunks
    chunk1 = EvidenceChunk(
        chunk_id="chunk1",
        source_url="https://adilet.zan.kz/rus/docs/K1400000235",
        source_title="КоАП РК статья 235",
        content="Нарушение законодательства Республики Казахстан о персональных данных и их защите влечет штраф.",
        legal_rank=LegalRank.CODE,
        law_id="235-V",
        article="235"
    )
    chunk2 = EvidenceChunk(
        chunk_id="chunk2",
        source_url="https://adilet.zan.kz/rus/docs/K1400000226",
        source_title="УК РК статья 207",
        content="Нарушение работы информационной системы или информационно-коммуникационной сети.",
        legal_rank=LegalRank.CODE,
        law_id="226-V",
        article="207"
    )
    chunk3 = EvidenceChunk(
        chunk_id="chunk3",
        source_url="https://adilet.zan.kz/rus/docs/Z1300000094",
        source_title="Закон о персональных данных",
        content="Сбор и обработка персональных данных осуществляются с согласия субъекта.",
        legal_rank=LegalRank.LAW_RK,
        law_id="94-V",
        article="5"
    )

    await manager.put_many([chunk1, chunk2, chunk3])

    # 1. Test search with strict AND keyword matching (filtered). Since we set limit=1, it should only return chunk1.
    results = await manager.search_local("персональных данных штраф", limit=1)
    assert len(results) == 1
    assert results[0].chunk_id == "chunk1"

    # With higher limit, it fills with OR matches
    results = await manager.search_local("персональных данных штраф", limit=10)
    assert len(results) == 2
    assert results[0].chunk_id == "chunk1"
    assert results[1].chunk_id == "chunk3"

    # 2. Test search with OR keyword matching fallback (when strict AND returns nothing)
    # "работа" only matches chunk2, "согласие" only matches chunk3. Both together should trigger OR search
    results = await manager.search_local("работа согласие")
    assert len(results) == 2
    chunk_ids = {r.chunk_id for r in results}
    assert "chunk2" in chunk_ids
    assert "chunk3" in chunk_ids

    # 3. Test search with law_ids filter only
    results = await manager.search_local("какой-то текст", law_ids=["94-V"])
    assert len(results) == 1
    assert results[0].chunk_id == "chunk3"

    # 4. Test search with law_ids and matching keywords (with limit=1 to test strict match)
    results = await manager.search_local("защита персональных", law_ids=["235-V", "226-V"], limit=1)
    assert len(results) == 1
    assert results[0].chunk_id == "chunk1"

    # With higher limit, it fills with other matching laws
    results = await manager.search_local("защита персональных", law_ids=["235-V", "226-V"], limit=10)
    assert len(results) == 3
    assert results[0].chunk_id == "chunk1"

    # 5. Test search with legal stop words filtering (e.g. "закон", "кодекс")
    results = await manager.search_local("закон кодекс информационная система", limit=1)
    assert len(results) == 1
    assert results[0].chunk_id == "chunk2"


@pytest.mark.asyncio
async def test_cache_dynamic_healing(tmp_path):
    """Проверяет динамическое исправление устаревших/неверных legal_rank из кэша (Dynamic Cache Healing)."""
    db_file = tmp_path / "test_cache_healing.db"
    manager = CacheManager(db_path=str(db_file))

    # Создаем чанк с неверным legal_rank (MINISTERIAL_ORDER = Rank 7)
    # Но с URL, который по новым правилам v9.5 должен быть CODE (Rank 2)
    outdated_chunk = EvidenceChunk(
        chunk_id="chunk_healing_test",
        source_url="https://adilet.zan.kz/rus/docs/K1400000235",
        source_title="КоАП РК статья 235",
        content="Нарушение законодательства о персональных данных",
        legal_rank=LegalRank.MINISTERIAL_ORDER,  # Rank 7 (outdated cache entry)
        law_id="235-V",
        article="235"
    )

    await manager.put(outdated_chunk)

    # При загрузке чанк должен динамически исцелиться до LegalRank.CODE (Rank 2)
    healed_chunk = await manager.get("chunk_healing_test")
    assert healed_chunk is not None
    assert healed_chunk.legal_rank == LegalRank.CODE
    assert healed_chunk.inferred_rank == LegalRank.CODE

    # Также проверим, что в search_local чанки тоже исцеляются
    results = await manager.search_local("персональных данных")
    assert len(results) == 1
    assert results[0].legal_rank == LegalRank.CODE
