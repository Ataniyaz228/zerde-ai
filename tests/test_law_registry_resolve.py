"""
test_law_registry_resolve.py
Тесты law_registry.resolve()/id_variants() — единственный источник истины
для law_id (Фаза 1.1). Защищает false-grounding инварианты: digit-transposition,
неизвестные ID, base-ID strict match, отсутствие cross-law утечки в id_variants.
"""
import pytest

from zerde.utils.law_registry import get_registry


@pytest.fixture(scope="module")
def reg():
    return get_registry()


def test_no_digit_transposition_match(reg):
    # 253-V не должен ложно резолвиться в 235-V (false-grounding guard)
    assert reg.resolve("253-V") == "253-V"
    assert reg.resolve("235-V") == "235-V"


def test_unknown_id_shaped_returns_as_is(reg):
    assert reg.resolve("999-VI") == "999-VI"


def test_adilet_code_resolves_to_short(reg):
    assert reg.resolve("K1400000235") == "235-V"


def test_base_id_strict_match_with_suffix(reg):
    # base-ID ядро матчит хранимый суффиксный id (226-V → 226-V-UK), строго по ядру
    assert reg.resolve("226-V") == "226-V-UK"


def test_id_variants_no_cross_law_leak(reg):
    variants = {v.upper() for v in reg.id_variants("94-V")}
    assert "94-IV" not in variants and "94-VII" not in variants
