"""
test_offline_fallback.py
Тесты офлайн-поиска в кеше (без LLM/сеть)
Проверяет:
1. Отсутствие утечки предыдущего рана (нормативные документы о персданных)
2. Новая формула reliability_score (V8.0)
"""
import asyncio
from pathlib import Path

import pytest
from zerde.models import ClaimSeverity, ClaimVerdict, EvidenceChunk, LegalRank, WebTier, VerdictStatus
from zerde.stages.s6_auditor import _build_conflicts_from_verdicts
from zerde.utils.cache import CacheManager

# --- Вспомогательные функции ---


def _make_chunk(chunk_id: str, content: str, title: str = "Test") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url="http://adilet.zan.kz/test",
        source_title=title,
        content=content,
        legal_rank=LegalRank.LAW_RK,
        web_tier=WebTier.TIER_1,
    )


def _make_verdict(claim_id: str, status: VerdictStatus, severity: ClaimSeverity = ClaimSeverity.MEDIUM,
                  confidence: str = "MEDIUM") -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim_id,
        status=status,
        severity=severity,
        confidence=confidence,
    )


# ===========================================================================
# Тесты: Офлайн кеш — изоляция запросов
# ===========================================================================

TEST_DB = "/tmp/test_fallback_cache.db"


@pytest.fixture(autouse=True)
def cleanup_test_db():
    """Clean up test DB before and after each test."""
    for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
        path = Path(p)
        if path.exists():
            path.unlink()
    yield
    for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
        path = Path(p)
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_search_local_no_leakage_from_old_run():
    """
    Предыдущий ран: в кеше есть чанки о Персданных + ЭЦП.
    Текущий запрос: "Гражданский кодекс" — должен вернуть 0 результатов от старого рана.
    """
    cm = CacheManager(db_path=TEST_DB)

    # Старый ран: чанк Персыданных и ЭЦП попали в кеш
    old_chunks = [
        _make_chunk("old_001", "Закон Казахстана о персональных данных 2021 года", "Персданные"),
        _make_chunk("old_002", "Электронная цифровая подпись (ЭЦП) в документах", "ЭЦП"),
    ]
    await cm.put_many(old_chunks)

    # Запрос текущего рана о Гражданском кодексе
    result = await cm.search_local("Гражданский кодекс Статья 44")
    found_ids = [c.chunk_id for c in result]

    # Чанки Персданных/ЭЦП не должны попасть в результат
    assert "old_001" not in found_ids, "Leakage: Personal data chunk appeared in Civil Code search"
    assert "old_002" not in found_ids, "Leakage: ECP chunk appeared in Civil Code search"


@pytest.mark.asyncio
async def test_search_local_positive_hit():
    """search_local должен вернуть релевантный чанк, если он там есть."""
    cm = CacheManager(db_path=TEST_DB)

    relevant_chunk = _make_chunk(
        "gk_001",
        "Статья 44 Гражданского кодекса. Норма об объектах гражданских прав.",
        "Гражданский кодекс РК",
    )
    await cm.put(relevant_chunk)

    result = await cm.search_local("Гражданский кодекс Статья 44")
    found_ids = [c.chunk_id for c in result]
    assert "gk_001" in found_ids, "Relevant chunk not found in local search"


@pytest.mark.asyncio
async def test_search_local_excludes_pure_numeric_terms():
    """
    search_local Strategy 5 должна игнорировать чисто числовые термины,
    чтобы не вытащивать нерелевантные чанки через номера года или числа статьи.
    """
    cm = CacheManager(db_path=TEST_DB)

    # Чанк, содержащий "закон 2021" в тексте
    old_chunk = _make_chunk("law_2021", "Закон о Персданных 2021 года.")
    await cm.put(old_chunk)

    # Запрос с числом из номера статьи — не должно попасть
    result = await cm.search_local("Гражданский кодекс 44")
    found_ids = [c.chunk_id for c in result]
    # '44' и '2021' — чисто числовые термины, Strategy5 должна их игнорировать
    assert "law_2021" not in found_ids, "Numeric-only term leaked old chunk"


# ===========================================================================
# Тесты: Reliability Score V8.0
# ===========================================================================


def _compute_reliability(verdicts: list[ClaimVerdict]) -> float:
    """Копия формулы из s6_auditor для тестов."""
    analytical = [v for v in verdicts if not (v.claim_id and v.claim_id.startswith("structural_"))]
    n_total = len(analytical)
    if n_total == 0:
        return 0.05

    n_confirmed = sum(1 for v in analytical if v.status == VerdictStatus.CONFIRMED)
    n_unverified_neutral = sum(
        1 for v in analytical
        if v.status == VerdictStatus.UNVERIFIED
        and v.severity not in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
    )
    n_contradicted_critical = sum(
        1 for v in analytical
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.CRITICAL
    )
    n_contradicted_high = sum(
        1 for v in analytical
        if v.status == VerdictStatus.CONTRADICTED and v.severity == ClaimSeverity.HIGH
    )
    n_unverified_risks = sum(
        1 for v in analytical
        if v.status == VerdictStatus.UNVERIFIED
        and v.severity in (ClaimSeverity.CRITICAL, ClaimSeverity.HIGH)
    )

    ratio_score = (n_confirmed + 0.3 * n_unverified_neutral) / n_total
    penalty = (
        0.20 * n_contradicted_critical +
        0.10 * n_contradicted_high +
        0.05 * n_unverified_risks
    )
    return max(0.05, min(1.0, ratio_score * (1.0 - penalty)))


def test_reliability_zero_confirmed_gives_low_score():
    """
    Документ с 0 confirmed claims должен давать reliability < 20%.
    Раньше: пенальти не снижали 71%, теперь ratio_score=0 → reliability≈0.05.
    """
    verdicts = [
        _make_verdict("c1", VerdictStatus.UNVERIFIED, ClaimSeverity.CRITICAL),
        _make_verdict("c2", VerdictStatus.UNVERIFIED, ClaimSeverity.HIGH),
        _make_verdict("c3", VerdictStatus.UNVERIFIED, ClaimSeverity.MEDIUM),
        _make_verdict("c4", VerdictStatus.UNVERIFIED, ClaimSeverity.MEDIUM),
    ]
    score = _compute_reliability(verdicts)
    assert score < 0.20, f"Expected reliability < 0.20 with 0 confirmed, got {score:.3f}"


def test_reliability_full_confirmed_gives_high_score():
    """100% confirmed → reliability ≈ 1.0."""
    verdicts = [
        _make_verdict("c1", VerdictStatus.CONFIRMED, ClaimSeverity.CRITICAL, "HIGH"),
        _make_verdict("c2", VerdictStatus.CONFIRMED, ClaimSeverity.HIGH, "HIGH"),
        _make_verdict("c3", VerdictStatus.CONFIRMED, ClaimSeverity.MEDIUM),
    ]
    score = _compute_reliability(verdicts)
    assert score >= 0.95, f"Expected reliability >= 0.95 with all confirmed, got {score:.3f}"


def test_reliability_critical_contradiction_drops_score():
    """
    1 CRITICAL CONTRADICTED из 4 должен существенно снижать надёжность.
    """
    verdicts = [
        _make_verdict("c1", VerdictStatus.CONFIRMED, ClaimSeverity.MEDIUM),
        _make_verdict("c2", VerdictStatus.CONFIRMED, ClaimSeverity.MEDIUM),
        _make_verdict("c3", VerdictStatus.CONFIRMED, ClaimSeverity.MEDIUM),
        _make_verdict("c4", VerdictStatus.CONTRADICTED, ClaimSeverity.CRITICAL, "HIGH"),
    ]
    full_score = _compute_reliability([
        _make_verdict(f"cx{i}", VerdictStatus.CONFIRMED, ClaimSeverity.MEDIUM) for i in range(4)
    ])
    penalty_score = _compute_reliability(verdicts)
    assert penalty_score < full_score, (
        f"Critical contradiction should reduce reliability: full={full_score:.3f}, penalty={penalty_score:.3f}"
    )


def test_reliability_empty_verdicts_gives_minimum():
    """0 verdicts → минимальный score 0.05."""
    score = _compute_reliability([])
    assert score == pytest.approx(0.05)


def test_reliability_structural_verdicts_ignored():
    """
    structural_ verdicts не участвуют в расчёте reliability.
    """
    # Только структурные verdicts — должны давать 0.05 (нет аналитических)
    verdicts = [
        ClaimVerdict(claim_id="structural_c1", status=VerdictStatus.CONTRADICTED,
                     severity=ClaimSeverity.CRITICAL, confidence="HIGH"),
        ClaimVerdict(claim_id="structural_c2", status=VerdictStatus.CONFIRMED,
                     severity=ClaimSeverity.HIGH, confidence="HIGH"),
    ]
    score = _compute_reliability(verdicts)
    assert score == pytest.approx(0.05), f"Structural verdicts should be ignored, got {score:.3f}"
