import pytest

from scripts.ingest_docs import split_long_article
from zerde.models import EvidenceChunk, LegalRank
from zerde.utils.cache import CacheManager


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_adaptive_cache.db")

def test_split_long_article_basic():
    # Long article text > 4000 chars with clauses
    content = "Article Title\n" + "\n".join([f"{i}. Clause description text that goes on for a bit and tells us what is allowed and what is not." for i in range(1, 50)])

    chunks = split_long_article("10", content)
    assert len(chunks) > 1

    # Check that each chunk is <= 4000 characters
    for chunk in chunks:
        assert len(chunk["content"]) <= 4000
        assert chunk["article_num"].startswith("10_p")
        # Ensure overlap is present (the next chunk starts with the end of the previous one)

def test_split_long_article_fallback():
    # Long article with no clauses, just one giant paragraph
    content = "Giant paragraph text " * 300
    assert len(content) > 4000

    chunks = split_long_article("12", content)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk["content"]) <= 4000
        assert chunk["article_num"].startswith("12_p")

@pytest.mark.asyncio
async def test_language_penalization_in_search(temp_db_path):
    cache = CacheManager(temp_db_path)

    # Store Russian and Kazakh equivalents
    chunk_ru = EvidenceChunk(
        chunk_id="chunk_ru_lang",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Закон о персональных данных",
        content="Сбор персональных данных осуществляется с письменного согласия.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
        language="ru",
    )

    chunk_kk = EvidenceChunk(
        chunk_id="chunk_kk_lang",
        source_url="https://adilet.zan.kz/rus/docs/test#2",
        source_title="Дербес деректер туралы заң",
        content="Дербес деректерді жинау жазбаша келісіммен жүзеге асырылады.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
        language="kk",
    )

    await cache.put_many([chunk_ru, chunk_kk])
    cache.embed_chunks([chunk_ru, chunk_kk])

    # Search in Kazakh: "Дербес деректерді жинау" -> should rank Kazakh chunk first
    results_kk = await cache.search_local("Дербес деректерді жинау және өңдеу")
    assert len(results_kk) >= 1
    assert results_kk[0].chunk_id == "chunk_kk_lang"

    # Search in Russian: "Сбор персональных данных" -> should rank Russian chunk first
    results_ru = await cache.search_local("Сбор персональных данных с согласия")
    assert len(results_ru) >= 1
    assert results_ru[0].chunk_id == "chunk_ru_lang"

@pytest.mark.asyncio
async def test_version_freshness_boost(temp_db_path):
    cache = CacheManager(temp_db_path)

    # Identical content but different versions (2020 vs 2026)
    chunk_old = EvidenceChunk(
        chunk_id="chunk_2020",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Старая редакция закона",
        content="Уполномоченный орган осуществляет государственный контроль.",
        legal_rank=LegalRank.LAW_RK,
        law_id="LAW-1",
        article="5",
        language="ru",
        source_version="2020-01-01",
    )

    chunk_new = EvidenceChunk(
        chunk_id="chunk_2026",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Новая редакция закона",
        content="Уполномоченный орган осуществляет государственный контроль.",
        legal_rank=LegalRank.LAW_RK,
        law_id="LAW-1",
        article="5",
        language="ru",
        source_version="2026-03-11",
    )

    await cache.put_many([chunk_old, chunk_new])
    cache.embed_chunks([chunk_old, chunk_new])

    # Search should return the newer version first due to freshness boost
    results = await cache.search_local("государственный контроль уполномоченного органа")
    assert len(results) >= 1
    assert results[0].chunk_id == "chunk_2026"
