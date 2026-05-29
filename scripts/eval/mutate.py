"""Mutation engine (label-free eval, level 1).

Programmatically injects a KNOWN error into a clean claim, so the grounding
layer can be tested for resistance: a planted error must NOT ground.

Mutations operate at the claim level (not raw text) to target the grounding
layer precisely:
  - mutate_law: corrupt the target law_id (digit transposition). Tests whether
    resolve()/synonyms fuzzily snap a wrong law onto a real one — e.g. the
    confirmed bug resolve('253-V') -> '235-V'. Article kept intact so the
    only defect is the law.
  - mutate_article: replace the article number with an absurd one (99999),
    law kept intact. Tests "right law, impossible article".

Each returns (mutated_claim, label) or None if the claim can't be mutated
(no target law / no article).
"""

from __future__ import annotations

import re

from zerde.models import DocumentClaim
from zerde.stages.s6_auditor import _extract_article_from_claim

ABSURD_ARTICLE = "99999"


def _corrupt_law_id(law_id: str) -> str | None:
    """'235-V' -> '253-V' (swap last two digits of the numeric part).

    Falls back to +1 if the swap is a no-op (e.g. trailing equal digits).
    Returns None if there's no usable numeric part.
    """
    m = re.match(r"^\s*(\d+)(\D.*)?$", law_id.strip())
    if not m:
        return None
    num, suffix = m.group(1), m.group(2) or ""
    digits = list(num)
    if len(digits) >= 2:
        digits[-1], digits[-2] = digits[-2], digits[-1]
    new_num = "".join(digits)
    if new_num == num:
        new_num = str(int(num) + 1)
    if new_num == num:
        return None
    return new_num + suffix


def mutate_law(claim: DocumentClaim) -> tuple[DocumentClaim, str] | None:
    targets = list(getattr(claim, "target_law_ids", None) or [])
    if not targets:
        return None
    # Corrupt EVERY law reference, so the mutated claim has NO valid law. If we
    # corrupted only the first, a multi-law claim would still ground via an intact
    # law id and inflate false_grounding with a harness artifact (not a resolve bug).
    new_targets: list[str] = []
    changed: tuple[str, str] | None = None
    any_corrupted = False
    for lid in targets:
        corrupted = _corrupt_law_id(lid)
        if corrupted:
            new_targets.append(corrupted)
            any_corrupted = True
            if changed is None:
                changed = (lid, corrupted)
        else:
            new_targets.append(lid)
    if not any_corrupted:
        return None
    mutated = claim.model_copy(deep=True)
    mutated.target_law_ids = new_targets
    label = f"law:{changed[0]}->{changed[1]}"
    if len(targets) > 1:
        label += f" (+{len(targets) - 1} more)"
    return mutated, label


def mutate_article(claim: DocumentClaim) -> tuple[DocumentClaim, str] | None:
    article = _extract_article_from_claim(claim)
    if not article:
        return None
    mutated = claim.model_copy(deep=True)
    pattern = rf"\b{re.escape(article)}\b"
    mutated.claim_text = re.sub(pattern, ABSURD_ARTICLE, claim.claim_text or "", count=1)
    mutated.quote = re.sub(pattern, ABSURD_ARTICLE, claim.quote or "", count=1)
    # Confirm the mutation actually changed what the extractor sees.
    if _extract_article_from_claim(mutated) == article:
        return None
    return mutated, f"article:{article}->{ABSURD_ARTICLE}"
