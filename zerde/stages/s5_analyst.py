"""
Stage 5: The Auditor (Claim-based Verification)
"""

from __future__ import annotations

import asyncio
import json
import logging

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimVerdict,
    EvidenceChunk,
    Fact,
    NegativeSpaceItem,
    QueryPlan,
    VerdictStatus,
)
from zerde.prompts.auditor import build_auditor_prompt
from zerde.utils.llm_client import LLMParseError, cached_llm_call, make_llm_client

logger = logging.getLogger(__name__)


async def run_auditor(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    doc_text: str = "",
) -> AnalysisJSON:
    settings = get_settings()
    client = make_llm_client(settings)

    active = [c for c in chunks if not c.is_duplicate]
    conflict_ids = [c.chunk_id for c in active if c.is_conflict]

    # Строим auditor prompt (чеклист-режим)
    prompt = build_auditor_prompt(
        chunks=active,
        claims=claims,
        plan=plan,
        doc_text=doc_text,
    )

    system_msg = (
        "JSON. Аудитор юр. документов РК. Вердикт на каждый claim. "
        "ОШИБКИ = наивысший приоритет. Без пояснений вне JSON."
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    raw_json = await cached_llm_call(
        client=client,
        model=settings.llm_model_analyst,
        messages=messages,
        settings=settings,
        ttl_seconds=86400,
        max_tokens=settings.llm_max_tokens_analyst,
    )

    analysis = _parse_auditor_response(raw_json, plan, claims, settings.llm_model_analyst)
    _validate_claim_coverage(analysis, claims)
    return analysis


def _parse_auditor_response(
    raw: dict,
    plan: QueryPlan,
    claims: ClaimExtractionResult,
    model: str,
) -> AnalysisJSON:
    import uuid as _uuid
    analysis_id = str(_uuid.uuid4())

    verdicts: list[ClaimVerdict] = []
    claimed_ids = {c.claim_id for c in claims.claims}
    claim_map = {c.claim_id: c for c in claims.claims}

    # Детерминированные вердикты
    for claim in claims.claims:
        if claim.deterministic_verdict:
            status = claim.deterministic_status or VerdictStatus.UNVERIFIED
            is_error = status == VerdictStatus.CONTRADICTED
            verdicts.append(ClaimVerdict(
                claim_id=claim.claim_id,
                status=status,
                source_ids=["reference_data"],
                found_value=claim.deterministic_verdict,
                document_value=", ".join(claim.entities),
                contradiction_detail=claim.deterministic_verdict if is_error else None,
                confidence="HIGH",
                severity=claim.severity,
                is_deterministic=True,
            ))

    deterministic_ids = {v.claim_id for v in verdicts}
    seen_verdict_ids = set(deterministic_ids)
    for item in raw.get("verdicts", []):
        claim_id = item.get("claim_id", "")
        if not claim_id or claim_id not in claimed_ids or claim_id in seen_verdict_ids:
            continue
        seen_verdict_ids.add(claim_id)

        raw_status = str(item.get("status", "UNVERIFIED")).upper()
        if "CONTRADIC" in raw_status or "OPROVERG" in raw_status:
            status = VerdictStatus.CONTRADICTED
        elif "CONFIRM" in raw_status or "PODTVERZH" in raw_status:
            status = VerdictStatus.CONFIRMED
        else:
            status = VerdictStatus.UNVERIFIED

        orig_claim = claim_map.get(claim_id)
        claim_severity = orig_claim.severity if orig_claim else ClaimSeverity.MEDIUM

        verdicts.append(ClaimVerdict(
            claim_id=claim_id,
            status=status,
            source_ids=item.get("source_ids", []),
            found_value=item.get("found_value"),
            document_value=item.get("document_value"),
            contradiction_detail=item.get("contradiction_detail"),
            confidence=item.get("confidence", "MEDIUM"),
            severity=claim_severity,
            is_deterministic=False,
        ))

    facts = []
    for v in verdicts:
        if v.status == VerdictStatus.CONTRADICTED:
            claim_text = f"[{v.claim_id}]: документ='{v.document_value}' vs найдено='{v.found_value}'"
            confidence = 0.1
        elif v.status == VerdictStatus.CONFIRMED:
            claim_text = f"[{v.claim_id}]: '{v.found_value or v.document_value}'"
            confidence = 0.9
        else:
            claim_text = f"[{v.claim_id}]: '{v.document_value}'"
            confidence = 0.4
        facts.append(Fact(
            fact_id=f"fact_{len(facts):04d}",
            claim_id=v.claim_id,
            claim=claim_text,
            source_ids=v.source_ids if v.source_ids else ["UNLINKED"],
            confidence=confidence,
        ))

    return AnalysisJSON(
        analysis_id=analysis_id,
        source_doc_id=plan.source_doc_id,
        plan_id=plan.plan_id,
        facts=facts,
        conclusions=[],
        negative_space=[],
        normative=[],
        cons=raw.get("cons", []),
        llm_model_used=model,
        verdicts=verdicts,
    )


def _validate_claim_coverage(analysis: AnalysisJSON, claims: ClaimExtractionResult) -> None:
    verdict_ids = {v.claim_id for v in analysis.verdicts}
    missing = [c for c in claims.claims if c.claim_id not in verdict_ids]
    for claim in missing:
        analysis.verdicts.append(ClaimVerdict(
            claim_id=claim.claim_id,
            status=VerdictStatus.UNVERIFIED,
            source_ids=[],
            document_value=claim.claim_text[:100],
            confidence="LOW",
            severity=claim.severity,
        ))
