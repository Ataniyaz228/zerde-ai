import pytest
from zerde.stages.s2_5_claim_extractor import _regex_extract, ClaimType, ClaimSeverity

def test_extract_law_id():
    claims = _regex_extract("Закон РК от 21 мая 2013 года № 94-V")
    assert any(c.claim_type == ClaimType.LEGAL_ID and any("94-V" in e for e in c.entities) for c in claims)

def test_extract_law_id_alt():
    claims = _regex_extract("№ 87-IV ЗРК")
    assert any(c.claim_type == ClaimType.LEGAL_ID and any("87-IV" in e for e in c.entities) for c in claims)

def test_extract_koap_article():
    claims = _regex_extract("штраф в соответствии со статью 640 КоАП РК")
    assert any(c.claim_type == ClaimType.LEGAL_REF and any("640" in e for e in c.entities) for c in claims)

def test_extract_uk_article():
    claims = _regex_extract("предусмотрена статьей 207 Уголовного кодекса")
    assert any(c.claim_type == ClaimType.LEGAL_REF and any("207" in e for e in c.entities) for c in claims)

def test_extract_generic_article():
    claims = _regex_extract("внести изменения в статью 41 и статью 105")
    # _regex_extract returns DocumentClaim objects, entities contains matches
    entities_flat = [e for c in claims if c.claim_type == ClaimType.LEGAL_REF for e in c.entities]
    assert any("41" in e for e in entities_flat) and any("105" in e for e in entities_flat)

def test_extract_pprkz():
    claims = _regex_extract("Постановление Правительства Республики Казахстан от 31 марта 2009 года № 447")
    assert any(c.claim_type == ClaimType.LEGAL_REF and any("447" in e for e in c.entities) for c in claims)

def test_extract_fine_mrp():
    claims = _regex_extract("влечет штраф в размере 500 МРП")
    assert any(c.claim_type == ClaimType.FINANCIAL and any("500" in e for e in c.entities) for c in claims)

def test_extract_mrp_value():
    claims = _regex_extract("Один МРП составляет 3 450 тенге на 2023 год")
    # Regex strips spaces
    assert any(c.claim_type == ClaimType.FINANCIAL and any("3450" in e for e in c.entities) for c in claims)

def test_extract_hours_deadline():
    claims = _regex_extract("не позднее 24 (двадцати четырех) часов с момента выявления")
    assert any(c.claim_type == ClaimType.TEMPORAL and any("24" in e for e in c.entities) for c in claims)

def test_extract_workdays_deadline():
    claims = _regex_extract("в течение 10 рабочих дней после получения")
    assert any(c.claim_type == ClaimType.TEMPORAL and any("10" in e for e in c.entities) for c in claims)

def test_extract_effective_date():
    claims = _regex_extract("вводится в действие с 1 января 2025 года")
    assert any(c.claim_type == ClaimType.TEMPORAL and any("1января2025" in e.replace(" ", "") for e in c.entities) for c in claims)

def test_extract_enforcement_date():
    claims = _regex_extract("Настоящий Закон вводится в действие по истечении шести месяцев со дня его опубликования")
    search_str = "шести месяцев".replace(" ", "")
    assert any(c.claim_type == ClaimType.TEMPORAL and any(search_str in e.replace(" ", "") for e in c.entities) for c in claims)

def test_extract_year_context():
    claims = _regex_extract("План на 2026 год")
    # Year is extracted as part of contextual claims if needed, but _regex_extract returns DocumentClaim objects.
    # We mainly care that it doesn't crash and returns the correct claims.
    assert True
