"""
Stage 7: Renderer (Markdown "Lawyer" Format)
Вход:  AnalysisJSON (валидированный)
Выход: Markdown-отчёт

Структура отчёта:
  1. Executive Summary
  2. Нормативная база (источники по рангу)
  3. Выявленные конфликты
  4. Аналитические выводы (факты + выводы)
  5. Пробелы регулирования
  6. Нормативные оценки (pros/cons, affected parties)
  7. Рекомендации
  8. Reliability Score
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from zerde.config import get_settings
from zerde.models import (
    AnalysisJSON,
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    ConflictRecord,
    EvidenceChunk,
    Fact,
    LegalRank,
    ValidationStatus,
    VerdictStatus,
)

logger = logging.getLogger(__name__)

# Иконки статусов для Markdown (V7.0)
_STATUS_ICONS = {
    ValidationStatus.HIGH: "🟢",
    ValidationStatus.MEDIUM: "🟡",
    ValidationStatus.LOW: "🔴",
    ValidationStatus.UNVERIFIED: "⚫",
}

# V7.0: Умный выбор иконки факта с учётом вердикта
_RISK_ICONS = {
    "CONTRADICTED": "🔴",
    "UNVERIFIED_RISK": "🟠",
    "CONFIRMED": "🟢",
    "UNVERIFIED_NEUTRAL": "⚫",
}


def _fact_icon(fact: Fact, verdict_map: dict[str, ClaimVerdict]) -> str:
    """V7.0: Определяет иконку факта с учётом вердикта (override BM25-цвета)."""
    import re
    claim_id = fact.claim_id
    if not claim_id and fact.claim:
        match = re.search(r"\[(claim_\d{4})\]", fact.claim)
        if match:
            claim_id = match.group(1)

    if claim_id and claim_id in verdict_map:
        v = verdict_map[claim_id]
        if v.status == VerdictStatus.CONTRADICTED:
            return _RISK_ICONS["CONTRADICTED"]
        if v.status == VerdictStatus.UNVERIFIED:
            is_high_severity = v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
            is_high_confidence = v.confidence in ("HIGH", "MEDIUM")
            if is_high_severity or is_high_confidence:
                return _RISK_ICONS["UNVERIFIED_RISK"]
        if v.status == VerdictStatus.CONFIRMED:
            return _RISK_ICONS["CONFIRMED"]
    # Fallback на validation_status
    return _STATUS_ICONS.get(fact.validation_status, "⚫")

_RANK_LABELS = {
    LegalRank.INTERNATIONAL_TREATY: "Международный договор",
    LegalRank.CODE: "Кодекс",
    LegalRank.CONSTITUTIONAL_LAW: "Конституционный закон",
    LegalRank.LAW_RK: "Закон РК",
    LegalRank.PRESIDENTIAL_DECREE: "Указ Президента",
    LegalRank.GOVERNMENT_RESOLUTION: "Постановление Правительства",
    LegalRank.MINISTERIAL_ORDER: "Приказ министерства",
    LegalRank.SC_PROSECUTORS_CLARIFICATION: "Разъяснение ВС / Генпрокуратуры",
    LegalRank.SC_CASE_LAW: "Судебная практика ВС",
    LegalRank.EXPERT_ANALYTICS: "Эксперт / Аналитика",
    LegalRank.MEDIA_UNKNOWN: "СМИ / Неизвестный",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def render_report(
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
    output_path: str | Path | None = None,
    policy_analysis: dict | None = None,
) -> str:
    """
    Этап 7: Рендерит Markdown-отчёт формата "Lawyer".

    Args:
        analysis: Валидированный AnalysisJSON из Этапа 6.
        chunks: Корпус EvidenceChunk для источниковой секции.
        output_path: Путь для сохранения .md файла. Если None — автогенерация.

    Returns:
        Markdown-строка отчёта.
    """
    settings = get_settings()

    logger.info(f"[S7] Rendering report. analysis_id={analysis.analysis_id}")

    # Индекс чанков для быстрого доступа
    corpus_index: dict[str, EvidenceChunk] = {c.chunk_id: c for c in chunks}
    # Prefix index: префиксы chunk_id (от 8 до 32 символов) → полный chunk_id
    # LLM может возвращать обрезанные IDs разной длины
    prefix_index: dict[str, str] = {}
    for cid in corpus_index:
        for prefix_len in range(8, 33):
            pfx = cid[:prefix_len]
            if pfx not in prefix_index:
                prefix_index[pfx] = cid

    active_chunks = [c for c in chunks if not c.is_duplicate]
    conflict_chunks = [c for c in active_chunks if c.is_conflict]

    sections = [
        _render_header(analysis),
        _render_executive_summary(analysis),
        _render_policy_analysis(policy_analysis),
        _render_structural_checklist(analysis),
        _render_normative_base(active_chunks),
        _render_conflicts(conflict_chunks, analysis.conflicts),
        _render_facts_and_conclusions(analysis, corpus_index, prefix_index),
        _render_negative_space(analysis),
        _render_normative_assessments(analysis),
        _render_pros_cons(analysis),
        _render_affected_parties(analysis),
        _render_recommendation(analysis),
        _render_reliability_footer(analysis),
    ]

    report_md = "\n\n---\n\n".join(s for s in sections if s.strip())

    # Сохранение файла
    if output_path is None:
        output_dir = Path(settings.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"zerde_report_{ts}.md"

    Path(output_path).write_text(report_md, encoding="utf-8")
    logger.info(f"[S7] Report saved: {output_path}")

    return report_md


# ---------------------------------------------------------------------------
# Section Renderers
# ---------------------------------------------------------------------------


def _render_header(analysis: AnalysisJSON) -> str:
    """Заголовок отчёта."""
    return (
        f"# 🏛️ ЗЕРДЕ - Юридический Анализ\n\n"
        f"**ID анализа:** `{analysis.analysis_id}`  \n"
        f"**Дата:** {analysis.analyzed_at.strftime('%d.%m.%Y %H:%M')} UTC  \n"
        f"**Модель:** `{analysis.llm_model_used}`  \n"
    )


def _render_executive_summary(analysis: AnalysisJSON) -> str:
    """Executive Summary секция."""
    reliability_bar = _reliability_bar(analysis.overall_reliability)
    confirmed = sum(1 for v in analysis.verdicts if v.status.value == "CONFIRMED")
    contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    unverified = sum(1 for v in analysis.verdicts if v.status.value == "UNVERIFIED")
    structural_cnt = len(analysis.structural_claims)

    lines = [
        "## 📋 Executive Summary\n",
        f"{reliability_bar}\n",
        f"- **Подтверждено (✅):** {confirmed}",
        f"- **Опровергнуто (❌):** {contradicted}",
        f"- **Не проверено (⚠️):** {unverified}",
    ]
    if structural_cnt:
        lines.append(f"- **Структурных элементов:** {structural_cnt}")
    lines.extend([
        f"- **Выводов:** {len(analysis.conclusions)}",
        f"- **Пробелов регулирования:** {len(analysis.negative_space)}",
        "",
    ])
    return "\n".join(lines)


def _render_structural_checklist(analysis: AnalysisJSON) -> str:
    """V7.0: Компактный чеклист структурных claims с конкретными статьями."""
    if not analysis.structural_claims:
        return ""

    lines = ["## 📑 Состав Документа\n"]

    # Извлекаем entities (номера статей/законов) для каждого типа
    legal_ids: set[str] = set()
    legal_refs: set[str] = set()
    other_items: list[str] = []

    for c in analysis.structural_claims:
        if c.claim_type == ClaimType.LEGAL_ID and c.entities:
            legal_ids.update(c.entities)
        elif c.claim_type == ClaimType.LEGAL_REF and c.entities:
            legal_refs.update(c.entities)
        else:
            other_items.append(c.claim_text[:80])

    if legal_ids:
        ids_str = ", ".join(sorted(legal_ids))
        lines.append(f"- **Упомянутые законы:** {ids_str}")
    if legal_refs:
        refs_str = ", ".join(sorted(legal_refs))
        lines.append(f"- **Упомянутые статьи:** {refs_str}")
    if other_items:
        lines.append(f"- **Прочие структурные элементы:** {len(other_items)}")

    lines.append("")
    return "\n".join(lines)


def _render_policy_analysis(policy: dict | None) -> str:
    """Аналитическая оценка от S5.5 Policy Analyst."""
    if not policy:
        return ""

    lines = ["## 📊 Аналитическая Оценка\n"]
    lines.append("> [!NOTE]")
    lines.append("> Данная секция сгенерирована AI-аналитиком и носит рекомендательный характер.\n")

    # Предмет и цель
    if policy.get("subject"):
        lines.append(f"### 📋 Предмет и цель\n{policy['subject']}\n")

    # Регуляторный контекст
    if policy.get("regulatory_context"):
        lines.append(f"### 🏛️ Место в системе НПА\n{policy['regulatory_context']}\n")

    # Затронутые стороны
    if policy.get("affected_parties"):
        lines.append("### 👥 Круг затронутых лиц\n")
        lines.append("| Группа | Влияние | Детали |")
        lines.append("|--------|---------|--------|")
        for p in policy["affected_parties"]:
            lines.append(f"| {p.get('party', '')} | {p.get('impact', '')} | {p.get('details', '')} |")
        lines.append("")

    # Ключевые изменения
    if policy.get("key_changes"):
        lines.append("### 🔄 Ключевые изменения\n")
        sig_icons = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
        for ch in policy["key_changes"]:
            icon = sig_icons.get(ch.get("significance", ""), "⚪")
            lines.append(f"- {icon} **[{ch.get('significance', 'N/A')}]** {ch.get('change', '')}")
        lines.append("")

    # Риски
    if policy.get("risks"):
        lines.append("### ⚠️ Риски\n")
        for r in policy["risks"]:
            sev_icons = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
            icon = sev_icons.get(r.get("severity", ""), "⚪")
            mitigation = f" → *{r['mitigation']}*" if r.get("mitigation") else ""
            lines.append(f"- {icon} **[{r.get('severity', 'N/A')}]** {r.get('risk', '')}{mitigation}")
        lines.append("")

    # Сильные/слабые стороны
    if policy.get("strengths"):
        lines.append("### ✅ Сильные стороны\n")
        for s in policy["strengths"]:
            lines.append(f"- {s}")
        lines.append("")
    if policy.get("weaknesses"):
        lines.append("### ❌ Слабые стороны\n")
        for w in policy["weaknesses"]:
            lines.append(f"- {w}")
        lines.append("")

    # Рекомендация аналитика
    if policy.get("recommendation"):
        rec_icons = {
            "ПРИНЯТЬ": "🟢", "ПРИНЯТЬ С ЗАМЕЧАНИЯМИ": "🟡",
            "ОТПРАВИТЬ НА ДОРАБОТКУ": "🟠", "ОТКЛОНИТЬ": "🔴",
        }
        icon = rec_icons.get(policy["recommendation"], "⚫")
        lines.append(f"### {icon} Рекомендация аналитика: **{policy['recommendation']}**\n")
        if policy.get("recommendation_reasoning"):
            lines.append(f"{policy['recommendation_reasoning']}\n")

    return "\n".join(lines)


def _render_normative_base(active_chunks: list[EvidenceChunk]) -> str:
    """V7.0: Таблица источников, агрегированная по law_id внутри ранга."""
    if not active_chunks:
        return "## 📚 Нормативная База\n\n*Источники не найдены.*"

    # Группируем по рангу → law_id → chunks
    by_rank: dict[LegalRank, dict[str, list[EvidenceChunk]]] = {}
    for chunk in sorted(active_chunks, key=lambda c: int(c.legal_rank)):
        rank = chunk.legal_rank
        law_key = chunk.law_id or chunk.source_title
        by_rank.setdefault(rank, {}).setdefault(law_key, []).append(chunk)

    lines = ["## 📚 Нормативная База\n"]
    for rank, law_groups in by_rank.items():
        rank_label = _RANK_LABELS.get(rank, str(rank))
        lines.append(f"\n### Ранг {int(rank)}: {rank_label}")
        for law_key, law_chunks in law_groups.items():
            # Берём заголовок из первого chunk'а группы
            representative = law_chunks[0]
            law_title = representative.law_title or representative.source_title
            lines.append(f"- **{law_title}**")
            # Подпункты — статьи/ссылки
            for c in law_chunks:
                conflict_flag = " ⚠️ КОНФЛИКТ" if c.is_conflict else ""
                art_ref = f" (ст. {c.article})" if c.article else ""
                lines.append(
                    f"  - [{c.source_title}]({c.source_url}){art_ref}{conflict_flag}"
                )

    return "\n".join(lines)


def _render_conflicts(
    conflict_chunks: list[EvidenceChunk],
    bridge_conflicts: list[ConflictRecord],
) -> str:
    """V7.0: Единая секция конфликтов: S4 (chunks) + S6 (bridge)."""
    total = len(conflict_chunks) + len(bridge_conflicts)
    if not total:
        return "## ⚖️ Конфликты и Коллизии\n\n*Конфликтов не выявлено.*"

    lines = [
        f"## ⚖️ Выявленные Конфликты и Коллизии ({total})\n",
        "> [!WARNING]",
        "> Ниже перечислены юридические коллизии, требующие правовой оценки.\n",
    ]

    if conflict_chunks:
        lines.append("### Конфликты источников (S4)\n")
        for chunk in conflict_chunks:
            conflict_str = ", ".join(ct.value for ct in chunk.conflict_types)
            lines.append(f"**{chunk.source_title}** — `{chunk.chunk_id[:12]}…`")
            lines.append(f"- Тип: {conflict_str} | Ранг: {_RANK_LABELS.get(chunk.legal_rank, str(chunk.legal_rank))}")
            if chunk.conflict_with_ids:
                lines.append(f"- Конфликтует с: {', '.join(c[:12] for c in chunk.conflict_with_ids)}")
            lines.append("")

    if bridge_conflicts:
        lines.append("### Конфликты с нормами документа (S5/S6)\n")
        for rec in bridge_conflicts:
            sev_icon = "🔴" if rec.severity == ClaimSeverity.HIGH else "🟡"
            lines.append(f"#### {sev_icon} `{rec.record_id}` — {rec.conflict_type.value}")
            lines.append(f"- **Claim:** {rec.claim_text}")
            if rec.document_value and rec.found_value:
                lines.append(f"- **Документ:** {rec.document_value} → **Норма:** {rec.found_value}")
            lines.append(f"- **Детали:** {rec.detail}")
            lines.append("")

    return "\n".join(lines)


def _render_facts_and_conclusions(
    analysis: AnalysisJSON,
    corpus_index: dict[str, EvidenceChunk],
    prefix_index: dict[str, str],
) -> str:
    """Факты и выводы с источниками и статусами."""
    virtual_ids = {"UNLINKED", "reference_data"}
    lines = ["## 🔍 Факты и Выводы\n"]

    # H3 & H4 Fix: Матчим Fact и Verdict строго по claim_id, а не по индексам (zip)
    verdict_map = {v.claim_id: v for v in analysis.verdicts}

    if analysis.facts:
        lines.append("### Установленные факты\n")
        for fact in analysis.facts:
            verdict = verdict_map.get(fact.claim_id) if fact.claim_id else None
            if not verdict:
                # Fallback на дефолтный пустой вердикт, если связь не найдена
                verdict = ClaimVerdict(claim_id=fact.claim_id or "", status=VerdictStatus.UNVERIFIED, source_ids=fact.source_ids)

            # V7.0: Умная иконка с учётом вердикта (override BM25-цвета)
            icon = _fact_icon(fact, verdict_map)
            score_str = f" (BM25: {fact.bm25_score:.2f})" if fact.bm25_score is not None else ""
            lines.append(f"#### {icon} `{fact.fact_id}`{score_str}")

            if verdict.status == VerdictStatus.CONTRADICTED:
                lines.append("> [!WARNING]")
                lines.append(f"> **[ОШИБКА]** {fact.claim}\n")
            elif verdict.status == VerdictStatus.CONFIRMED:
                lines.append("> [!NOTE]")
                lines.append(f"> **[ПОДТВЕРЖДЕНО]** {fact.claim}\n")
            elif verdict.status == VerdictStatus.UNVERIFIED and verdict.confidence in ("HIGH", "MEDIUM"):
                # V7.0: Risk flag для важных непроверенных claims
                lines.append("> [!CAUTION]")
                lines.append(f"> **[⚠️ РИСК РЕТРИВАЛА]** {fact.claim}\n")
                lines.append("> *Система не смогла найти подтверждающие источники. Требуется ручная верификация.*\n")
            else:
                lines.append(f"> **[НЕ ПРОВЕРЕНО]** {fact.claim}\n")

            # Рендерим только реальные источники (не виртуальные)
            real_source_lines = []
            for sid in fact.source_ids:
                if sid in virtual_ids or sid.startswith("reference_"):
                    continue  # reference_data / UNLINKED — не показываем
                # Резолвим prefix → full ID
                full_id = prefix_index.get(sid, sid)
                chunk = corpus_index.get(full_id)
                if chunk:
                    real_source_lines.append(
                        f"- [{chunk.source_title}]({chunk.source_url}) `{full_id[:12]}…`"
                    )
                # Не нашли — просто пропускаем (не засоряем отчёт)

            if real_source_lines:
                lines.append("**Источники:**")
                lines.extend(real_source_lines)
            lines.append("")

    if analysis.conclusions:
        lines.append("\n### Выводы\n")
        for conc in analysis.conclusions:
            icon = _STATUS_ICONS.get(conc.validation_status, "⚫")
            lines.append(f"#### {icon} `{conc.conclusion_id}` [{conc.reasoning_type}]")
            lines.append(f"{conc.statement}\n")

    return "\n".join(lines)


def _render_negative_space(analysis: AnalysisJSON) -> str:
    """Пробелы регулирования."""
    if not analysis.negative_space:
        return ""

    lines = ["## 🕳️ Пробелы Регулирования\n"]
    for item in analysis.negative_space:
        gap_icons = {
            "regulatory_hole": "🔴",
            "intentional_silence": "🟡",
            "delegation_gap": "🟠",
        }
        icon = gap_icons.get(item.gap_type, "⚫")
        lines.append(f"### {icon} `{item.item_id}` — {item.gap_type}")
        lines.append(f"**Домен:** {item.affected_domain}\n")
        lines.append(f"{item.description}\n")

    return "\n".join(lines)


def _render_normative_assessments(analysis: AnalysisJSON) -> str:
    """Нормативные оценки влияния."""
    if not analysis.normative:
        return ""

    lines = ["## 📊 Нормативные Оценки\n"]
    risk_icons = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "🚨"}

    for assessment in analysis.normative:
        icon = risk_icons.get(assessment.risk_level, "⚫")
        lines.append(f"### {icon} [{assessment.risk_level}] `{assessment.assessment_id}`")
        lines.append(f"{assessment.norm_description}\n")
        if assessment.economic_impact:
            lines.append(f"- **Экономическое влияние:** {assessment.economic_impact}")
        if assessment.social_impact:
            lines.append(f"- **Социальное влияние:** {assessment.social_impact}")
        lines.append("")

    return "\n".join(lines)


def _render_pros_cons(analysis: AnalysisJSON) -> str:
    """Плюсы и минусы."""
    if not analysis.pros and not analysis.cons:
        return ""

    lines = ["## ⚖️ Плюсы и Минусы\n"]
    if analysis.pros:
        lines.append("### ✅ Плюсы")
        lines.extend(f"- {p}" for p in analysis.pros)
    if analysis.cons:
        lines.append("\n### ❌ Минусы")
        lines.extend(f"- {c}" for c in analysis.cons)

    return "\n".join(lines)


def _render_affected_parties(analysis: AnalysisJSON) -> str:
    """Затронутые стороны."""
    if not analysis.affected_parties:
        return ""

    role_icons = {
        "beneficiary": "✅",
        "obligated": "📋",
        "regulator": "🏛️",
        "third_party": "👥",
    }

    lines = ["## 👥 Затронутые Стороны\n"]
    for party in analysis.affected_parties:
        icon = role_icons.get(party.role, "👤")
        lines.append(f"- {icon} **{party.name}** ({party.role}): {party.description}")

    return "\n".join(lines)


def _render_recommendation(analysis: AnalysisJSON) -> str:
    """Рекомендации."""
    if not analysis.recommendation:
        return ""

    return (
        "## 💡 Рекомендации\n\n"
        "> [!IMPORTANT]\n"
        f"> {analysis.recommendation}"
    )


def _render_reliability_footer(analysis: AnalysisJSON) -> str:
    """Нижний колонтитул с Reliability Score."""
    score = analysis.overall_reliability
    bar = _reliability_bar(score)
    return (
        f"## 📈 Reliability Score\n\n"
        f"{bar}\n\n"
        f"*Сгенерировано Zerde AI. "
        f"Анализ носит информационный характер и не является юридической консультацией.*"
    )


def _reliability_bar(score: float | None) -> str:
    """Генерирует текстовый progress bar для reliability score."""
    if score is None:
        return "> **Reliability:** N/A (анализ не завершён)"

    filled = int(score * 10)
    bar = "█" * filled + "░" * (10 - filled)
    percent = int(score * 100)

    if score >= 0.75:
        icon = "🟢"
    elif score >= 0.50:
        icon = "🟡"
    else:
        icon = "🔴"

    return f"> **Reliability:** {icon} `{bar}` {percent}%"
