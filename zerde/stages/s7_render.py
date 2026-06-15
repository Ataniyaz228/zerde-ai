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
import re
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
from zerde.utils.law_registry import get_registry


def _clean_label_leak(text: str) -> str:
    """Убирает внутренние метки источников/чанков (S1, S5, S6, S12…), которые
    LLM иногда вписывает прямо в прозу вердиктов/коллизий/пробелов.

    Это служебные ярлыки чанков (рус/каз/сегменты), а не часть правовой нормы —
    в пользовательском тексте они только сбивают с толку («источник S5»,
    «в редакции S5, S6», «(S4, S5, S6)»). Чистим аккуратно, не трогая «ст.»/«п.».
    """
    if not text:
        return text
    t = text
    t = re.sub(r"\(\s*S\d+(?:\s*,\s*S\d+)*\s*\)", "", t)            # (S5) (S5, S6)
    t = re.sub(r"(?<=\()\s*S\d+(?:\s*,\s*S\d+)*\s*,\s*", "", t)     # (S4, ст… → (ст…
    t = re.sub(r"\s*,\s*S\d+(?:\s*,\s*S\d+)*\s*(?=\))", "", t)      # …, S6) → …)
    t = re.sub(r",?\s*источник[аи]?\s+S\d+(?:\s*,\s*S\d+)*\s*:?", "", t, flags=re.I)
    t = re.sub(r",?\s*в\s+ред(?:акции|\.)?\s+S\d+(?:\s*,\s*S\d+)*", "", t, flags=re.I)
    # Подчистка следов
    t = re.sub(r"\(\s*[;:,]\s*", "(", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,;:)])", r"\1", t)
    return t.strip()


def _is_authoritative(c: EvidenceChunk) -> bool:
    """Авторитетный нормативный источник = только adilet.zan.kz (ground truth).

    Веб-агрегаторы/новости (akorda, 1tv, pavlodar, kodeksy-kz, prg…) получают
    legal_rank ≠ MEDIA_UNKNOWN и иначе попадали бы в иерархию НПА как «Закон РК».
    Здесь — чисто отображение: такие источники уходят в «Дополнительные
    (информационные)», не влияя на вердикты (их решал S5 выше).
    """
    return c.legal_rank != LegalRank.MEDIA_UNKNOWN and "adilet.zan.kz" in (c.source_url or "")


def _law_name(chunk: EvidenceChunk) -> str:
    """Человеческое имя закона из реестра вместо синтезированного «НПА {код}».

    Корпус «запекает» source_title как «НПА K2600000000_ | Ст. 71» при ингесте;
    реестр (law_metadata.title_ru) знает «Конституция Республики Казахстан».
    Для веб-источников (нет law_id в реестре) оставляем исходный заголовок.
    """
    if chunk.law_id:
        title = get_registry().get_title(chunk.law_id)
        if title and title != chunk.law_id:
            return title
    return chunk.law_title or chunk.source_title or "Источник"


def _source_display_title(chunk: EvidenceChunk) -> str:
    """Подпись ссылки: «<имя закона> | Ст. N» для НПА, исходный title для веба."""
    if chunk.law_id:
        title = get_registry().get_title(chunk.law_id)
        if title and title != chunk.law_id:
            return f"{title} | Ст. {chunk.article}" if chunk.article else title
    return chunk.source_title


def _source_link_url(chunk: EvidenceChunk) -> str:
    """Кликабельный URL источника.

    Корпус хранит source_url, синтезированный из КОРОТКОГО law_id
    (`adilet.zan.kz/rus/docs/171-VIII`) — такой путь на adilet 404-ит, т.к.
    документы там адресуются по внутреннему коду (`.../docs/K2500000171`).
    Берём реальный код из реестра (adilet_code); для не-реестровых источников
    (веб) — оставляем исходный URL. Якорь #статья безвреден: на валидном
    документе он просто не сработает, но не ломает переход. См. ground-truth
    правило в CLAUDE.md — мёртвая ссылка = ложная цитата.
    """
    if chunk.law_id:
        url = get_registry().get_adilet_url(chunk.law_id)
        if url:
            return f"{url}#{chunk.article}" if chunk.article else url
    return chunk.source_url or ""

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

    # source_ids фактов уже переведены в полные chunk_id в S5 (_remap_source_ids),
    # поэтому prefix-recovery больше не нужен — берём sid как есть.
    virtual_ids = {"UNLINKED", "reference_data"}
    referenced_ids = set()
    for fact in analysis.facts:
        for sid in fact.source_ids:
            if sid in virtual_ids or sid.startswith("reference_"):
                continue
            referenced_ids.add(sid)

    # Нормативная база содержит только чанки, которые реально привязаны к фактам в отчёте или имеют конфликты.
    active_chunks = [
        c for c in chunks
        if not c.is_duplicate and (c.chunk_id in referenced_ids or c.is_conflict)
    ]
    conflict_chunks = [c for c in active_chunks if c.is_conflict]

    sections = [
        _render_header(analysis),
        _render_executive_summary(analysis),
        _render_policy_analysis(policy_analysis),
        _render_structural_checklist(analysis),
        _render_normative_base(active_chunks),
        _render_conflicts(conflict_chunks, analysis.conflicts),
        _render_facts_and_conclusions(analysis, corpus_index),
        _render_negative_space(analysis),
        _render_normative_assessments(analysis),
        _render_pros_cons(analysis),
        _render_affected_parties(analysis),
        _render_recommendation(analysis),
        _render_how_to_read(analysis),
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
    confirmed = sum(1 for v in analysis.verdicts if v.status.value == "CONFIRMED")
    contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    unverified = sum(1 for v in analysis.verdicts if v.status.value == "UNVERIFIED")
    structural_cnt = len(analysis.structural_claims)
    total = confirmed + contradicted + unverified

    # FIX 7: Compact reliability label for summary (full bar in footer)
    score = analysis.overall_reliability
    if score is not None:
        percent = int(score * 100)
        icon = "🟢" if score >= 0.75 else ("🟡" if score >= 0.50 else "🔴")
        reliability_line = f"> **Надёжность анализа:** {icon} {percent}%"
    elif total > 0 and confirmed == 0:
        reliability_line = "> **Надёжность анализа:** ⚫ Не рассчитана (ни одно утверждение не подтверждено источниками из корпуса)"
    else:
        reliability_line = "> **Надёжность анализа:** ⚫ Не рассчитана"

    lines = [
        "## 📋 Executive Summary\n",
        f"{reliability_line}\n",
        f"- **Подтверждено (✅):** {confirmed}",
        f"- **Опровергнуто (❌):** {contradicted}",
        f"- **Не проверено (⚠️):** {unverified}",
    ]
    if total > 0:
        coverage = int((confirmed + contradicted) / total * 100)
        lines.append(f"- **Покрытие верификацией:** {coverage}%")
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

    # FIX 6: Separate authoritative sources from informational ones
    authoritative = [c for c in active_chunks if _is_authoritative(c)]
    informational = [c for c in active_chunks if not _is_authoritative(c)]

    # Группируем по рангу → law_id → chunks
    by_rank: dict[LegalRank, dict[str, list[EvidenceChunk]]] = {}
    for chunk in sorted(authoritative, key=lambda c: int(c.legal_rank)):
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
            lines.append(f"- **{_law_name(representative)}**")
            # Подпункты — статьи/ссылки (дедуп: один и тот же (URL, статья) не повторяем —
            # у одной статьи бывает несколько чанков: рус/каз/сегменты).
            seen_refs: set[tuple[str, str]] = set()
            for c in law_chunks:
                key = (c.source_url or "", str(c.article or ""))
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                conflict_flag = " ⚠️ КОНФЛИКТ" if c.is_conflict else ""
                art_ref = f" (ст. {c.article})" if c.article else ""
                lines.append(
                    f"  - [{_source_display_title(c)}]({_source_link_url(c)}){art_ref}{conflict_flag}"
                )

    # Informational sources in a separate subsection (not part of normative hierarchy)
    if informational:
        lines.append("\n### 📌 Дополнительные источники (информационные)")
        lines.append("> Не являются частью нормативной иерархии РК.\n")
        for c in informational:
            lines.append(f"- [{c.source_title}]({c.source_url})")

    return "\n".join(lines)


def _render_conflicts(
    conflict_chunks: list[EvidenceChunk],
    bridge_conflicts: list[ConflictRecord],
) -> str:
    """V7.0: Единая секция конфликтов: S4 (chunks) + S6 (bridge)."""
    # FIX 5: Filter out non-authoritative sources from conflict list
    # MEDIA_UNKNOWN (rank 11) sources like Wikipedia should not appear as legal conflicts
    authoritative_conflicts = [c for c in conflict_chunks if _is_authoritative(c)]
    total = len(authoritative_conflicts) + len(bridge_conflicts)
    if not total:
        return "## ⚖️ Конфликты и Коллизии\n\n*Конфликтов не выявлено.*"

    lines = [
        f"## ⚖️ Выявленные Конфликты и Коллизии ({total})\n",
        "> [!WARNING]",
        "> Ниже перечислены юридические коллизии, требующие правовой оценки.\n",
    ]

    if authoritative_conflicts:
        lines.append("### Конфликты источников (S4)\n")
        for chunk in authoritative_conflicts:
            conflict_str = ", ".join(ct.value for ct in chunk.conflict_types)
            rank_label = _RANK_LABELS.get(chunk.legal_rank, str(chunk.legal_rank))
            lines.append(f"**{chunk.source_title}** — `{chunk.chunk_id[:12]}…`")
            lines.append(f"- Тип: {conflict_str} | Ранг: {rank_label}")
            if chunk.conflict_with_ids:
                lines.append(f"- Конфликтует с: {', '.join(c[:12] for c in chunk.conflict_with_ids)}")
            # FIX 5: Add content snippet so users can see what actually conflicts
            if chunk.content:
                snippet = chunk.content[:200].replace("\n", " ").strip()
                if len(chunk.content) > 200:
                    snippet += "…"
                lines.append(f"- Содержание: *{snippet}*")
            lines.append("")

    if bridge_conflicts:
        lines.append("### Конфликты с нормами документа (S5/S6)\n")
        for rec in bridge_conflicts:
            sev_icon = "🔴" if rec.severity == ClaimSeverity.HIGH else "🟡"
            lines.append(f"#### {sev_icon} `{rec.record_id}` — {rec.conflict_type.value}")
            lines.append(f"- **Claim:** {_clean_label_leak(rec.claim_text)}")
            if rec.document_value and rec.found_value:
                lines.append(
                    f"- **Документ:** {_clean_label_leak(rec.document_value)} "
                    f"→ **Норма:** {_clean_label_leak(rec.found_value)}"
                )
            lines.append(f"- **Детали:** {_clean_label_leak(rec.detail)}")
            lines.append("")

    return "\n".join(lines)


def _render_facts_and_conclusions(
    analysis: AnalysisJSON,
    corpus_index: dict[str, EvidenceChunk],
) -> str:
    """Факты и выводы с источниками и статусами."""
    virtual_ids = {"UNLINKED", "reference_data"}
    lines = ["## 🔍 Факты и Выводы\n"]

    # H3 & H4 Fix: Матчим Fact и Verdict строго по claim_id, а не по индексам (zip)
    verdict_map = {v.claim_id: v for v in analysis.verdicts}

    if analysis.facts:
        lines.append("### Установленные факты\n")
        # Сортируем факты по номеру claim'а (иначе номера скачут: 0069, 0116,
        # 0032…) и убираем текстовых близнецов (разные батчи дают дубли, напр.
        # 0036==0037).
        def _claim_num(f: Fact) -> int:
            m = re.search(r"(\d+)", f.claim_id or "")
            return int(m.group(1)) if m else 1_000_000
        _seen_claim_text: set[str] = set()
        ordered_facts: list[Fact] = []
        for f in sorted(analysis.facts, key=_claim_num):
            norm = (f.claim or "").strip().lower()
            if norm and norm in _seen_claim_text:
                continue
            _seen_claim_text.add(norm)
            ordered_facts.append(f)

        for fact in ordered_facts:
            verdict = verdict_map.get(fact.claim_id) if fact.claim_id else None
            if not verdict:
                # Fallback на дефолтный пустой вердикт, если связь не найдена
                verdict = ClaimVerdict(claim_id=fact.claim_id or "", status=VerdictStatus.UNVERIFIED, source_ids=fact.source_ids)

            # V7.0: Умная иконка с учётом вердикта (override BM25-цвета)
            icon = _fact_icon(fact, verdict_map)
            score_str = f" (BM25: {fact.bm25_score:.2f})" if fact.bm25_score is not None else ""
            lines.append(f"#### {icon} `{fact.fact_id}`{score_str}")

            claim_txt = _clean_label_leak(fact.claim)
            if verdict.status == VerdictStatus.CONTRADICTED:
                lines.append("> [!WARNING]")
                lines.append(f"> **[ОШИБКА]** {claim_txt}\n")
            elif verdict.status == VerdictStatus.CONFIRMED:
                lines.append("> [!NOTE]")
                lines.append(f"> **[ПОДТВЕРЖДЕНО]** {claim_txt}\n")
            elif verdict.status == VerdictStatus.UNVERIFIED and verdict.confidence in ("HIGH", "MEDIUM"):
                # V7.0: Risk flag для важных непроверенных claims
                lines.append("> [!CAUTION]")
                lines.append(f"> **[⚠️ РИСК РЕТРИВАЛА]** {claim_txt}\n")
                lines.append("> *Система не смогла найти подтверждающие источники. Требуется ручная верификация.*\n")
            else:
                lines.append(f"> **[НЕ ПРОВЕРЕНО]** {claim_txt}\n")

            # Рендерим только реальные источники (не виртуальные); дедуп по
            # (URL, статья) — у одной статьи бывает несколько чанков (рус/каз/сегменты),
            # для читателя это одна и та же ссылка.
            real_source_lines = []
            seen_fact_refs: set[tuple[str, str]] = set()
            for sid in fact.source_ids:
                if sid in virtual_ids or sid.startswith("reference_"):
                    continue  # reference_data / UNLINKED — не показываем
                full_id = sid  # уже полный chunk_id (ремап в S5)
                chunk = corpus_index.get(full_id)
                if chunk:
                    ref_key = (chunk.source_url or "", str(chunk.article or ""))
                    if ref_key in seen_fact_refs:
                        continue
                    seen_fact_refs.add(ref_key)
                    art_ref = f" (ст. {chunk.article})" if chunk.article else ""
                    real_source_lines.append(
                        f"- [{_source_display_title(chunk)}]({_source_link_url(chunk)}){art_ref}"
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
        lines.append(f"{_clean_label_leak(item.description)}\n")

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


def build_reliability_summary(analysis: AnalysisJSON) -> dict:
    """Структурная сводка надёжности — единый источник числа для UI/хранилища.

    Заменяет выскребание процента regex'ом из готового Markdown. Счётчики
    берутся из `analysis.verdicts` (та же логика, что в executive summary).
    """
    confirmed = sum(1 for v in analysis.verdicts if v.status.value == "CONFIRMED")
    contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    unverified = sum(1 for v in analysis.verdicts if v.status.value == "UNVERIFIED")
    total = confirmed + contradicted + unverified
    score = analysis.overall_reliability
    return {
        "reliability": score,  # float 0..1 | None
        "reliability_pct": int(score * 100) if score is not None else None,
        "confirmed": confirmed,
        "contradicted": contradicted,
        "unverified": unverified,
        "total": total,
        "coverage_pct": int((confirmed + contradicted) / total * 100) if total else 0,
    }


def _render_how_to_read(analysis: AnalysisJSON) -> str:
    """
    Объяснение что значит Reliability Score, почему он бывает низким,
    и на что смотреть пользователю. Цель — не отпугнуть низкими %.
    """
    # Считаем из verdicts (как executive summary), а не из facts: у facts поле
    # status не нормализовано, из-за чего блок раньше всегда печатал 0/0/0.
    summary = build_reliability_summary(analysis)
    n_total = summary["total"]
    confirmed = summary["confirmed"]
    unverified = summary["unverified"]
    contradicted = summary["contradicted"]

    return (
        "## ℹ️ Как читать этот отчёт\n\n"
        "> **Reliability — это не «оценка качества законопроекта».**\n"
        "> Это доля утверждений, для которых система нашла подтверждение "
        "в действующих НПА (КоАП, ГК, законы РК и т.д.).\n\n"
        f"**По вашему документу:** {n_total} проверяемых утверждений → "
        f"✅ {confirmed} подтверждено, ⚠️ {unverified} не проверено, "
        f"❌ {contradicted} опровергнуто.\n\n"
        "### Почему % бывает невысоким — это нормально\n\n"
        "- **Поправочный закон** вводит новые нормы (например, «дополняется "
        "новой статьёй 889-1»). Их по определению **нет** в действующих "
        "НПА — поэтому такие утверждения помечаются ⚠️ *не проверено*. "
        "Это не ошибка системы, а правильное поведение: нельзя «подтвердить» "
        "норму, которой ещё не существует.\n"
        "- **Оригинальный законопроект** (с нуля, не поправка) — почти весь "
        "состоит из новых норм. Ожидаемый Reliability 30–55%.\n"
        "- **Поправки к существующим статьям** (61, 63, 741 и т.д.) — "
        "система должна найти эти статьи в корпусе и подтвердить, что "
        "правка к ним вносится корректно. Ожидаемый Reliability 65–80%.\n\n"
        "### На что смотреть в первую очередь\n\n"
        "1. **❌ Опровергнуто** — система нашла прямое противоречие "
        "действующему закону. Самый важный сигнал.\n"
        "2. **⚠️ Не проверено + риск ретривала** — система не смогла найти "
        "источник, при этом утверждение похоже на ссылку на существующую "
        "норму. Стоит проверить вручную.\n"
        "3. **✅ Подтверждено с BM25 ≥ 0.5** — высокая текстовая близость к "
        "найденному источнику. Это сильнее всего, когда источник — ссылка на "
        "**adilet.zan.kz** (первоисточник). Если источник — новостной/"
        "агрегаторный сайт (akorda.kz, kodeksy-kz.com и т.п.), это лишь "
        "косвенное подтверждение существования нормы, а не её действующей "
        "редакции — проверяйте по adilet.\n\n"
        "### Что Reliability ≠\n\n"
        "- ❌ Не показатель «нужен ли этот закон».\n"
        "- ❌ Не оценка юридической техники проекта.\n"
        "- ❌ Не приговор о конституционности.\n\n"
        "Для **политико-правовой оценки** смотрите секции *Резюме*, "
        "*Затронутые стороны*, *Риски и предложения* — они генерируются "
        "отдельной моделью-аналитиком и не зависят от Reliability.\n"
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
        return "> **Reliability:** N/A — недостаточно данных для расчёта (корпус не содержит подтверждающих источников)"

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
