"""
ЗЕРДЕ v6.2 — Stage 7: Renderer (Markdown "Lawyer" Format)
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
from datetime import datetime
from pathlib import Path

from zerde.config import get_settings
from zerde.models import AnalysisJSON, EvidenceChunk, LegalRank, ValidationStatus

logger = logging.getLogger(__name__)

# Иконки статусов для Markdown
_STATUS_ICONS = {
    ValidationStatus.HIGH: "🟢",
    ValidationStatus.MEDIUM: "🟡",
    ValidationStatus.LOW: "🔴",
    ValidationStatus.UNVERIFIED: "⚫",
}

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
    active_chunks = [c for c in chunks if not c.is_duplicate]
    conflict_chunks = [c for c in active_chunks if c.is_conflict]

    sections = [
        _render_header(analysis),
        _render_executive_summary(analysis),
        _render_normative_base(active_chunks),
        _render_conflicts(conflict_chunks),
        _render_facts_and_conclusions(analysis, corpus_index),
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
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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
        f"# 🏛️ ЗЕРДЕ v6.2 — Юридический Анализ\n\n"
        f"**ID анализа:** `{analysis.analysis_id}`  \n"
        f"**Дата:** {analysis.analyzed_at.strftime('%d.%m.%Y %H:%M')} UTC  \n"
        f"**Модель:** `{analysis.llm_model_used}`  \n"
    )


def _render_executive_summary(analysis: AnalysisJSON) -> str:
    """Executive Summary секция."""
    reliability_bar = _reliability_bar(analysis.overall_reliability)
    return (
        f"## 📋 Executive Summary\n\n"
        f"{reliability_bar}\n\n"
        f"- **Фактов подтверждено:** {analysis.validated_facts_count} / {len(analysis.facts)}\n"
        f"- **Неверифицировано:** {analysis.unverified_facts_count}\n"
        f"- **Выводов:** {len(analysis.conclusions)}\n"
        f"- **Пробелов регулирования:** {len(analysis.negative_space)}\n"
    )


def _render_normative_base(active_chunks: list[EvidenceChunk]) -> str:
    """Таблица источников, сгруппированная по LegalRank."""
    if not active_chunks:
        return "## 📚 Нормативная База\n\n*Источники не найдены.*"

    # Группируем по рангу
    by_rank: dict[LegalRank, list[EvidenceChunk]] = {}
    for chunk in sorted(active_chunks, key=lambda c: int(c.legal_rank)):
        by_rank.setdefault(chunk.legal_rank, []).append(chunk)

    lines = ["## 📚 Нормативная База\n"]
    for rank, rank_chunks in by_rank.items():
        rank_label = _RANK_LABELS.get(rank, str(rank))
        lines.append(f"\n### Ранг {int(rank)}: {rank_label}")
        for c in rank_chunks:
            conflict_flag = " ⚠️ КОНФЛИКТ" if c.is_conflict else ""
            law_ref = f" | {c.law_title} ст. {c.article}" if c.law_id else ""
            lines.append(
                f"- [{c.source_title}]({c.source_url}){law_ref}{conflict_flag}  \n"
                f"  `{c.chunk_id[:12]}…`"
            )

    return "\n".join(lines)


def _render_conflicts(conflict_chunks: list[EvidenceChunk]) -> str:
    """Секция юридических конфликтов."""
    if not conflict_chunks:
        return "## ⚖️ Конфликты\n\n*Конфликтов не выявлено.*"

    lines = [
        f"## ⚖️ Выявленные Конфликты ({len(conflict_chunks)} источников)\n",
        "> [!WARNING]",
        "> Следующие источники содержат юридические коллизии. Требуют правовой оценки.\n",
    ]

    for chunk in conflict_chunks:
        conflict_str = ", ".join(ct.value for ct in chunk.conflict_types)
        lines.append(f"### `{chunk.chunk_id[:12]}…` — {chunk.source_title}")
        lines.append(f"- **Тип конфликта:** {conflict_str}")
        lines.append(f"- **URL:** {chunk.source_url}")
        lines.append(f"- **Ранг:** {_RANK_LABELS.get(chunk.legal_rank, str(chunk.legal_rank))}")
        if chunk.conflict_with_ids:
            lines.append(f"- **Конфликтует с:** {', '.join(c[:12] for c in chunk.conflict_with_ids)}")
        lines.append("")

    return "\n".join(lines)


def _render_facts_and_conclusions(
    analysis: AnalysisJSON,
    corpus_index: dict[str, EvidenceChunk],
) -> str:
    """Факты и выводы с источниками и статусами."""
    lines = ["## 🔍 Факты и Выводы\n"]

    if analysis.facts:
        lines.append("### Установленные факты\n")
        for fact in analysis.facts:
            icon = _STATUS_ICONS.get(fact.validation_status, "⚫")
            score_str = f" (BM25: {fact.bm25_score:.2f})" if fact.bm25_score is not None else ""
            lines.append(f"#### {icon} `{fact.fact_id}`{score_str}")
            lines.append(f"{fact.claim}\n")
            if fact.source_ids:
                lines.append("**Источники:**")
                for sid in fact.source_ids:
                    chunk = corpus_index.get(sid)
                    if chunk:
                        lines.append(f"- [{chunk.source_title}]({chunk.source_url}) `{sid[:12]}…`")
                    else:
                        lines.append(f"- *(источник не найден)* `{sid[:12]}…`")
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
        f"*Сгенерировано ЗЕРДЕ v6.2 | "
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
