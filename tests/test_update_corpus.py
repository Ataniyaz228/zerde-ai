import json
import os
import sqlite3
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from zerde.models import EvidenceChunk, LegalRank
from zerde.utils.cache import CacheManager
from scripts.update_corpus import get_latest_adilet_version, clean_database_chunks

# Mock HTML history response for K1400000235
MOCK_HTML_HISTORY = """
<html>
<body>
<table id="ethernatable">
    <tr>
        <td class="odd">1</td>
        <td class="odd"><a href="/rus/archive/docs/K1400000235/05.07.2014">Об административных правонарушениях</a></td>
        <td class="odd">Кодекс...</td>
        <td class="odd">05.07.2014</td>
        <td class="odd">Новый</td>
    </tr>
    <tr>
        <td class="even last">212</td>
        <td class="even last"><a href="/rus/docs/K1400000235">Об административных правонарушениях</a></td>
        <td class="even last">Кодекс Республики Казахстан...</td>
        <td class="even last">12.03.2026</td>
        <td class="even last">Обновленный</td>
    </tr>
</table>
</body>
</html>
"""

@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_update_cache.db")

def test_parse_latest_adilet_version():
    with patch("urllib.request.urlopen") as mock_urlopen:
        # Mock response object
        mock_response = MagicMock()
        mock_response.read.return_value = MOCK_HTML_HISTORY.encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        latest_version = get_latest_adilet_version("K1400000235")
        assert latest_version == "2026-03-12"

@pytest.mark.asyncio
async def test_clean_database_chunks_cascade(temp_db_path):
    # Initialize cache and insert a chunk
    cache = CacheManager(temp_db_path)
    
    chunk = EvidenceChunk(
        chunk_id="chunk_to_delete",
        source_url="https://adilet.zan.kz/rus/docs/test#1",
        source_title="Закон о персональных данных",
        content="Персональные данные — сведения, относящиеся к определенному физическому лицу.",
        legal_rank=LegalRank.LAW_RK,
        law_id="87-IV",
        article="1",
        language="ru",
    )
    
    await cache.put_many([chunk])
    cache.embed_chunks([chunk])
    
    # Verify they exist
    stats = await cache.stats()
    assert stats["total_chunks"] == 1
    assert stats["total_embeddings"] == 1
    
    # Run the database cleaner from update_corpus
    clean_database_chunks(temp_db_path, "87-IV")
    
    # Verify cascade deletion cleaned both tables completely!
    conn = sqlite3.connect(temp_db_path)
    c_chunks = conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
    c_embeddings = conn.execute("SELECT COUNT(*) FROM evidence_embeddings").fetchone()[0]
    conn.close()
    
    assert c_chunks == 0
    assert c_embeddings == 0
