"""
test_real_bill_offline.py
Сквозной (но офлайн, без LLM) прогон S1 + S2.5-regex по синтетическому
тексту законопроекта (tests/fixtures/sample_bill_ru.txt).
"""
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_bill_ru.txt"


@pytest.mark.asyncio
async def test_s1_ingest_txt_bill():
    from zerde.models import DocumentFormat
    from zerde.stages.s1_ingest import ingest_document

    state = await ingest_document(FIXTURE)
    assert state.format == DocumentFormat.TXT
    assert state.doc_id
    assert state.char_count > 10
    assert "персональных данных" in state.normalized_text.lower()


def test_s2_5_regex_only_extraction():
    from zerde.models import ClaimType
    from zerde.stages.s2_5_claim_extractor import _dedup_claims, _regex_extract

    text = FIXTURE.read_text(encoding="utf-8")
    claims = _regex_extract(text)            # regex-only path — НЕ extract_claims (тот зовёт LLM)
    types = {c.claim_type for c in claims}
    assert ClaimType.LEGAL_ID in types       # 94-V пойман
    assert ClaimType.FINANCIAL in types      # 500 МРП пойман
    assert len(_dedup_claims(claims)) <= len(claims)
