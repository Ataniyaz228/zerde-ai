"""Meta-guard: no unverified hardcoded adilet-code dict may live in the registry.

Root-cause guard. `_LEGACY_FALLBACK_CODES` was a hand-typed law_id→adilet_code
table that verify_law_registry._collect_entries did NOT check (it only collects
law_metadata + _ADILET_CODE_TO_SHORT). Because it escaped the adilet-truth
verifier, its drift went uncaught — an audit later found 9 of its 10 codes were
wrong (5× HTTP 404, 4× pointing at a different law). A wrong fallback code makes
the pipeline confidently fetch a maybe-wrong norm, the exact opposite of
CITE-OR-ABSTAIN.

So the invariant is: every static law_id↔adilet_code mapping the registry
hardcodes must be one the adilet verifier actually checks. If someone adds a new
code dict, this test fails until they either wire it into the verifier or delete
it and ingest the laws into law_metadata (the single source of truth).
"""

from __future__ import annotations

import re

import zerde.utils.law_registry as reg

# An adilet doc code: a letter + 8-10 digits, optional trailing underscore
# (e.g. "K1400000235", "Z100000261_").
_CODE_RE = re.compile(r"^[A-Z]\d{8,10}_?$")

# The ONLY static adilet-code mapping the registry is allowed to hardcode. Both
# names are the same mapping (_SHORT_TO_ADILET_CODE is the inverse of
# _ADILET_CODE_TO_SHORT), and _ADILET_CODE_TO_SHORT is what
# verify_law_registry._collect_entries fetches against adilet. Everything else
# must come from law_metadata (ingested) — never a hand-typed dict.
_ALLOWED_CODE_DICTS = {"_ADILET_CODE_TO_SHORT", "_SHORT_TO_ADILET_CODE"}


def _has_code(d: dict) -> bool:
    keys_are_codes = any(isinstance(k, str) and _CODE_RE.match(k) for k in d)
    vals_are_codes = any(isinstance(v, str) and _CODE_RE.match(v) for v in d.values())
    return keys_are_codes or vals_are_codes


def _code_bearing_dict_names() -> set[str]:
    return {
        name
        for name, val in vars(reg).items()
        if isinstance(val, dict) and val and _has_code(val)
    }


def test_no_unverified_code_dict_in_registry():
    found = _code_bearing_dict_names()
    extra = found - _ALLOWED_CODE_DICTS
    assert not extra, (
        f"law_registry hardcodes adilet-code dict(s) {sorted(extra)} that "
        f"verify_law_registry does NOT check against adilet. Either wire them into "
        f"verify_law_registry._collect_entries (prove the codes) or delete them and "
        f"ingest the laws into law_metadata. _LEGACY_FALLBACK_CODES escaped this "
        f"verifier and was 9/10 wrong — do not reintroduce that class of bug."
    )


def test_allowed_dicts_still_exist_and_bear_codes():
    # Keep the allowlist honest: if these are renamed/removed, update the verifier
    # and this guard together rather than letting the allowlist silently rot.
    for name in _ALLOWED_CODE_DICTS:
        d = getattr(reg, name, None)
        assert isinstance(d, dict) and d and _has_code(d), (
            f"{name} is in the allowlist but is missing or no longer holds codes — "
            f"reconcile with verify_law_registry."
        )


def test_legacy_fallback_codes_is_gone():
    assert not hasattr(reg, "_LEGACY_FALLBACK_CODES"), (
        "_LEGACY_FALLBACK_CODES was removed (unverified, 9/10 wrong). Coverage for a "
        "not-yet-ingested law must come from ingest into law_metadata, not a dict."
    )
