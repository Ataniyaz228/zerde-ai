"""
Frozen scoring constants for S6's reliability/audit math (Phase 1, Step 3).

Every magic number previously inlined in zerde/stages/s6_auditor.py's
audit_analysis / _audit_fact / reliability computation lives here. This is a
pure data extraction -- values are unchanged; only their location moved.
Import CONFIG (a module-level singleton) rather than instantiating
ScoringConfig yourself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoringConfig:
    # --- Reliability formula weights (V9.4) ---
    # R = (w_v_ratio * V_ratio + w_q_auth * Q_auth + w_q_retrieval * Q_retrieval) * (1 - P_conflict)
    w_v_ratio: float = 0.50
    w_q_auth: float = 0.30
    w_q_retrieval: float = 0.20

    # --- Authority / a_coef rank bands (legal_rank -> coefficient) ---
    # Ranks 1-3 -> 1.0, 4-6 -> 0.8, 7-9 -> 0.5, 10-11 -> 0.2
    rank_band_3: int = 3
    rank_band_6: int = 6
    rank_band_9: int = 9
    a_coef_band_3: float = 1.0
    a_coef_band_6: float = 0.8
    a_coef_band_9: float = 0.5
    a_coef_band_other: float = 0.2
    # Fallback a_coef / auth score for virtual (reference-data) confirmed verdicts
    virtual_coef: float = 0.5

    # --- Severity weights for V_ratio / contradiction counting ---
    severity_weights: dict = field(default_factory=lambda: {
        "critical": 3.0,
        "high": 2.0,
        "medium": 1.0,
        "low": 0.5,
    })

    # --- Contradiction penalty (P_conflict) severity weights ---
    p_conflict_critical: float = 0.08
    p_conflict_high: float = 0.05
    p_conflict_medium: float = 0.03
    p_conflict_low: float = 0.01
    p_conflict_clip: float = 0.85

    # --- Reliability floor when at least one real CONFIRMED exists ---
    reliability_floor_with_real_confirmed: float = 0.05

    # --- Tiered BM25 upgrade thresholds (UNVERIFIED -> CONFIRMED) ---
    tiered_upgrade_high_threshold: float = 0.4
    tiered_upgrade_medium_threshold: float = 0.6

    # --- _audit_fact arithmetic-mismatch penalty multiplier ---
    arithmetic_penalty_multiplier: float = 0.5

    # --- _audit_fact article-boost minimum (forces at least MEDIUM) ---
    article_boost_min_medium: float = 0.35


CONFIG = ScoringConfig()
