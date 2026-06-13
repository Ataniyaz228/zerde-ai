"""
V9.4 Calibrated Legal Confidence Metric & Statistics Aggregator.

Moved verbatim from zerde/stages/s6_auditor.py (Phase 1, Step 3). Magic
numbers replaced with references into scoring_config.CONFIG. Behavior,
including the n_total<=2-all-UNVERIFIED -> None early-return path, is
UNCHANGED -- frozen and golden-tested (tests/test_s6_goldens.py).
"""

from __future__ import annotations

import logging

import numpy as np

from zerde.models import AnalysisJSON, AnalysisStats, ClaimSeverity, VerdictStatus
from zerde.stages.s6_audit.conflicts import _build_conflicts_from_verdicts
from zerde.stages.s6_audit.scoring_config import CONFIG

logger = logging.getLogger(__name__)


def _compute_reliability_and_stats(analysis: AnalysisJSON, corpus_index: dict) -> bool:
    """
    Computes overall_reliability + AnalysisStats + cons/conflicts.

    Returns True if the function took the early "insufficient data" return
    path (analysis is already fully populated and the caller should return
    immediately), False otherwise (caller continues with conflicts bridge).
    """
    if not analysis.verdicts:
        return False

    # Считаем только аналитические вердикты (structural не участвуют)
    analytical_verdicts = [
        v for v in analysis.verdicts
        if not (v.claim_id and v.claim_id.startswith("structural_"))
    ]
    n_total = len(analytical_verdicts)

    n_confirmed = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.CONFIRMED)
    n_contradicted = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.CONTRADICTED)
    n_unverified = sum(1 for v in analytical_verdicts if v.status == VerdictStatus.UNVERIFIED)

    n_contradicted_critical = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.CRITICAL
    )
    n_contradicted_high = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.HIGH
    )
    n_contradicted_medium = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.MEDIUM
    )
    n_contradicted_low = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.LOW
    )

    n_unverified_risks = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.UNVERIFIED and v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
    )

    n_real_confirmed = sum(
        1 for v in analytical_verdicts
        if v.status == VerdictStatus.CONFIRMED
        and any(sid and sid != "UNLINKED" and not sid.startswith("reference_") for sid in v.source_ids)
    )

    # V9.6: При очень малом числе аналитических claims reliability нельзя считать надёжно.
    # n_total=1-2 даёт экстремально низкие значения даже для корректных документов.
    if n_total <= 2 and n_total > 0:
        all_unverified = all(v.status == VerdictStatus.UNVERIFIED for v in analytical_verdicts)
        if all_unverified:
            # Недостаточно данных — не показываем процент (None = "N/A")
            reliability = None
            analysis.overall_reliability = reliability
            logger.info(f"[S6/Score] n_total={n_total}, все UNVERIFIED → reliability=None (insufficient data)")
            # Собираем статистику без reliability
            analysis.stats = AnalysisStats(
                n_total=n_total,
                n_confirmed=0,
                n_contradicted=0,
                n_unverified=n_total,
                n_critical_contradicted=0,
                n_high_contradicted=0,
                n_unverified_risks=sum(1 for v in analytical_verdicts if v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)),
                n_real_confirmed=0,
                reliability=None,
                pros=["Документ не содержит явных фактических ошибок (слишком мало проверяемых утверждений для полной верификации)."],
                recommendation=(
                    f"Юридический аудит завершен. Из {n_total} утверждений: 0 подтверждено, "
                    f"0 опровергнуто, {n_total} не верифицировано — корпус не содержит нужных источников."
                ),
            )
            analysis.conflicts = _build_conflicts_from_verdicts(analysis.verdicts)
            logger.info(f"[S6] Done (insufficient data path). UNVERIFIED={n_total}")
            return True

    # Безопасные дефолты: эти метрики определяются только в ветке n_total>0,
    # но используются ниже безусловно (pros_list, итоговый logger). При
    # n_total==0 (все вердикты структурные) без них был бы NameError, рушащий
    # весь аудит. Сейчас путь почти недостижим, но это мина при любом
    # изменении набора структурных claim'ов.
    v_ratio = 0.0
    q_auth = 0.0
    q_retrieval = 0.5
    p_conflict = 0.0

    if n_total > 0:
        # 1. Verification Coverage Score (V_ratio) weighted by severity
        severity_weights = {
            ClaimSeverity.CRITICAL: CONFIG.severity_weights["critical"],
            ClaimSeverity.HIGH: CONFIG.severity_weights["high"],
            ClaimSeverity.MEDIUM: CONFIG.severity_weights["medium"],
            ClaimSeverity.LOW: CONFIG.severity_weights["low"],
        }
        w_total = sum(severity_weights.get(v.severity, 1.0) for v in analytical_verdicts)

        w_confirmed = 0.0
        for v in analytical_verdicts:
            if v.status == VerdictStatus.CONFIRMED:
                real_sources = [sid for sid in v.source_ids if sid and sid != "UNLINKED" and not sid.startswith("reference_")]
                if not real_sources:
                    # Fallback for virtual reference-data verified claims
                    a_coef = CONFIG.virtual_coef
                else:
                    best_rank = 11
                    for sid in real_sources:
                        if sid in corpus_index:
                            best_rank = min(best_rank, int(corpus_index[sid].legal_rank))

                    if best_rank <= CONFIG.rank_band_3:
                        a_coef = CONFIG.a_coef_band_3
                    elif best_rank <= CONFIG.rank_band_6:
                        a_coef = CONFIG.a_coef_band_6
                    elif best_rank <= CONFIG.rank_band_9:
                        a_coef = CONFIG.a_coef_band_9
                    else:
                        a_coef = CONFIG.a_coef_band_other
                w_confirmed += severity_weights.get(v.severity, 1.0) * a_coef

        v_ratio = w_confirmed / w_total if w_total > 0 else 0.0

        # 2. Authority Quality Score (Q_auth)
        # Ranks 1-3 = 1.0, Ranks 4-6 = 0.8, Ranks 7-9 = 0.5, Ranks 10-11 = 0.2
        auth_scores = []
        for v in analytical_verdicts:
            if v.status == VerdictStatus.CONFIRMED:
                real_sources = [sid for sid in v.source_ids if sid and sid != "UNLINKED" and not sid.startswith("reference_")]
                if not real_sources:
                    # Fallback for virtual reference-data verified claims
                    auth_scores.append(CONFIG.virtual_coef)
                    continue

                # Find min (strongest) rank of its sources
                best_rank = 11
                for sid in real_sources:
                    if sid in corpus_index:
                        best_rank = min(best_rank, int(corpus_index[sid].legal_rank))

                if best_rank <= CONFIG.rank_band_3:
                    auth_scores.append(CONFIG.a_coef_band_3)
                elif best_rank <= CONFIG.rank_band_6:
                    auth_scores.append(CONFIG.a_coef_band_6)
                elif best_rank <= CONFIG.rank_band_9:
                    auth_scores.append(CONFIG.a_coef_band_9)
                else:
                    auth_scores.append(CONFIG.a_coef_band_other)
        q_auth = float(np.mean(auth_scores)) if auth_scores else 0.0

        # 3. Retrieval Quality Score (Q_retrieval)
        ret_scores = []
        fact_by_claim = {f.claim_id: f for f in analysis.facts if f.claim_id}
        for v in analytical_verdicts:
            if v.status == VerdictStatus.CONFIRMED and v.claim_id in fact_by_claim:
                f = fact_by_claim[v.claim_id]
                if f.bm25_score is not None:
                    ret_scores.append(f.bm25_score)
        q_retrieval = float(np.mean(ret_scores)) if ret_scores else 0.5
        q_retrieval = max(0.0, min(1.0, q_retrieval))

        # 4. Contradiction Penalty (P_conflict)
        # v9.5: Смягчённые веса. Нахождение противоречий показывает что анализ РАБОТАЕТ,
        # а не что он ненадёжный. Reliability = надёжность анализа, не качество документа.
        p_conflict = (
            CONFIG.p_conflict_critical * n_contradicted_critical +
            CONFIG.p_conflict_high * n_contradicted_high +
            CONFIG.p_conflict_medium * n_contradicted_medium +
            CONFIG.p_conflict_low * n_contradicted_low
        )
        p_conflict = max(0.0, min(CONFIG.p_conflict_clip, p_conflict))

        # Calculate Reliability (R)
        # R = (0.50 * V_ratio + 0.30 * Q_auth + 0.20 * Q_retrieval) * (1.0 - P_conflict)
        base_score = CONFIG.w_v_ratio * v_ratio + CONFIG.w_q_auth * q_auth + CONFIG.w_q_retrieval * q_retrieval
        reliability = base_score * (1.0 - p_conflict)

        # Если есть реально подтвержденные факты, ограничиваем надежность снизу 5%
        # (чтобы избежать 0% при наличии подтвержденного и противоречивого контента одновременно)
        if n_real_confirmed > 0:
            reliability = max(CONFIG.reliability_floor_with_real_confirmed, min(1.0, reliability))
        else:
            reliability = max(0.0, min(1.0, reliability))

    else:
        reliability = None

    analysis.overall_reliability = reliability

    # Compile and attach AnalysisStats (v9.4 Immutable Stage)

    # FIX 3 + FIX 5: Conditional pros — don't claim "Confirmed 0" as a positive.
    # FIX 5: Don't generate false positive "no contradictions" when corpus is empty.
    # corpus_index может быть пустым (0 локальных чанков) — в этом случае
    # "противоречий не обнаружено" — ложный позитив, т.к. мы ничего не проверяли.
    has_real_evidence = len(corpus_index) > 0
    has_local_evidence = any(
        c.adilet_fallback_used is not None or c.law_id
        for c in corpus_index.values()
    )
    pros_list = []
    if n_confirmed > 0:
        pros_list.append(f"Подтверждено {n_confirmed} из {n_total} анализируемых утверждений законопроекта.")
    if n_contradicted == 0 and has_real_evidence and has_local_evidence and v_ratio >= 0.5:
        # Только если >50% фактов проверено и есть локальные источники
        pros_list.append("Противоречий с действующим законодательством РК не обнаружено.")
    if n_confirmed > 0 and n_contradicted == 0 and has_local_evidence and v_ratio >= 0.5:
        pros_list.append("Все проверенные утверждения соответствуют нормативно-правовой базе.")
    if not pros_list:
        # Fallback: nothing confirmed, but also nothing contradicted — neutral
        if n_unverified == n_total and not has_real_evidence:
            pros_list.append("Документ не содержит явных фактических ошибок (корпус источников недостаточен для полной верификации).")
        elif n_unverified == n_total:
            pros_list.append("Документ не содержит фактических ошибок в проверяемой части (недостаточно источников для полной верификации).")
        else:
            pros_list.append(f"Проанализировано {n_total} утверждений законопроекта.")

    # Наполняем cons список в AnalysisJSON для рендеринга
    analysis.cons = []
    if n_contradicted > 0:
        analysis.cons.append(f"Выявлено {n_contradicted} противоречий с действующим законодательством Республики Казахстан.")
    if n_unverified_risks > 0:
        analysis.cons.append(f"Не удалось верифицировать {n_unverified_risks} критических/высоких утверждений из-за отсутствия необходимых источников в собранном корпусе.")
    if analysis.negative_space:
        analysis.cons.append(f"Выявлено {len(analysis.negative_space)} регуляторных пробелов или коллизий в законопроекте.")
    if not analysis.cons:
        analysis.cons.append("Критических коллизий или неустраненных противоречий не обнаружено.")

    confirmed_list = [v for v in analytical_verdicts if v.status == VerdictStatus.CONFIRMED]
    contradictions_list = [v for v in analytical_verdicts if v.status == VerdictStatus.CONTRADICTED]
    unverified_list = [v for v in analytical_verdicts if v.status == VerdictStatus.UNVERIFIED]

    rec_str = (
        f"Юридический аудит завершен. Из {n_total} выдвинутых утверждений: "
        f"{len(confirmed_list)} подтверждено действующим законодательством Республики Казахстан, "
        f"{len(contradictions_list)} опровергнуто, {len(unverified_list)} не верифицировано (отсутствуют в предоставленном корпусе)."
    )
    if contradictions_list:
        rec_str += f" КРИТИЧЕСКИ: {len(contradictions_list)} ошибок."

    analysis.stats = AnalysisStats(
        n_total=n_total,
        n_confirmed=n_confirmed,
        n_contradicted=n_contradicted,
        n_unverified=n_unverified,
        n_critical_contradicted=n_contradicted_critical,
        n_high_contradicted=n_contradicted_high,
        n_unverified_risks=n_unverified_risks,
        n_real_confirmed=n_real_confirmed,
        reliability=reliability,
        pros=pros_list,
        recommendation=rec_str,
    )

    rel_str = f"{reliability:.3f}" if reliability is not None else "None"
    logger.info(
        f"[S6/Score/v9.4] n_total={n_total} v_ratio={v_ratio:.3f} q_auth={q_auth:.3f} "
        f"q_retrieval={q_retrieval:.3f} p_conflict={p_conflict:.3f} → reliability={rel_str}"
    )
    return False
