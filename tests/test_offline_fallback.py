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


# ===========================================================================
# Тесты: Резолвинг человекочитаемых названий в law_id
# ===========================================================================


class TestResolveLawName:
    """_resolve_law_name должна преобразовывать названия НПА в короткие ID."""

    def test_exact_name_to_id(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("ГК РК (Общая часть)") == "1000-XIII"
        assert _resolve_law_name("Земельный кодекс РК") == "442-II"
        assert _resolve_law_name("Бюджетный кодекс РК") == "95-IV"

    def test_full_name_to_id(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("Гражданский кодекс Республики Казахстан (Общая часть)") == "1000-XIII"
        assert _resolve_law_name("Земельный кодекс Республики Казахстан") == "442-II"

    def test_law_name_to_id(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("Закон о государственном имуществе") == "413-IV"
        assert _resolve_law_name("Закон о местном государственном управлении") == "148-II"
        assert _resolve_law_name("Закон о персональных данных") == "94-V"

    def test_substring_match(self):
        from zerde.stages.s3_gather import _resolve_law_name
        # "Закон РК о государственном имуществе" содержит "государственном имуществе" — должно матчиться
        result = _resolve_law_name("Закон РК о государственном имуществе")
        assert result == "413-IV"

    def test_short_id_passthrough(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("550-IV") == "550-IV"
        assert _resolve_law_name("94-V") == "94-V"
        assert _resolve_law_name("1000-XIII") == "1000-XIII"

    def test_adilet_code_passthrough(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("K940001000_") == "K940001000_"
        assert _resolve_law_name("Z1300000094") == "Z1300000094"

    def test_case_insensitive(self):
        from zerde.stages.s3_gather import _resolve_law_name
        assert _resolve_law_name("Гражданский Кодекс РК (Общая Часть)") == "1000-XIII"
        assert _resolve_law_name("КоАП") == "235-V"
        assert _resolve_law_name("коап") == "235-V"


# ===========================================================================
# Тесты V9.3: Вычисляемые поля, иерархический поиск и фильтрация по метаданным
# ===========================================================================

def test_computed_fields_in_analysis_json():
    """Проверяет динамическое вычисление pros и recommendation в AnalysisJSON."""
    from zerde.models import AnalysisJSON, ClaimVerdict, VerdictStatus
    
    # 1. С дефолтными пустыми значениями
    verdicts = [
        ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.CONFIRMED),
        ClaimVerdict(claim_id="claim_0002", status=VerdictStatus.UNVERIFIED),
        ClaimVerdict(claim_id="claim_0003", status=VerdictStatus.CONTRADICTED),
    ]
    analysis = AnalysisJSON(
        analysis_id="test_id",
        source_doc_id="test_doc",
        plan_id="test_plan",
        verdicts=verdicts,
    )
    
    # Должен вычисляться динамически
    assert "Подтверждено 1 из 3" in analysis.pros[0]
    assert "1 подтверждено" in analysis.recommendation
    assert "1 опровергнуто" in analysis.recommendation
    assert "1 не верифицировано" in analysis.recommendation
    
    # 2. При мутации verdicts (вручную меняем статус одного вердикта)
    verdicts[1].status = VerdictStatus.CONFIRMED
    # И свойства pros / recommendation мгновенно отражают изменения!
    assert "Подтверждено 2 из 3" in analysis.pros[0]
    assert "2 подтверждено" in analysis.recommendation
    assert "1 опровергнуто" in analysis.recommendation
    assert "0 не верифицировано" in analysis.recommendation
    
    # 3. При задании пользовательских custom значений
    analysis.custom_pros = ["Мой кастомный про"]
    analysis.custom_recommendation = "Моя кастомная рекомендация"
    
    assert analysis.pros == ["Мой кастомный про"]
    assert analysis.recommendation == "Моя кастомная рекомендация"


def test_boilerplate_stopwords_tokenization():
    """Проверяет, что юридический канцелярит фильтруется в _tokenize, а ключевые слова сохраняются."""
    from zerde.stages.s6_auditor import _tokenize
    
    # Чистый бесполезный канцелярит (без "закон" / "кодекс") должен превращаться в пустой список
    cliche_text = "Настоящий вводится в действие по истечении"
    tokens = _tokenize(cliche_text)
    assert not tokens, f"Boilerplate clichés should yield empty tokens, got {tokens}"
    
    # Смешанный текст должен сохранять содержательные слова и ключевые юридические термины (закон/кодекс)
    mixed_text = "Настоящий Закон вводится в действие по истечении шести месяцев"
    tokens_mixed = _tokenize(mixed_text)
    assert "шести" in tokens_mixed
    assert "месяцев" in tokens_mixed
    assert "закон" in tokens_mixed, "закон should be preserved for high BM25 recall"


def test_check_topology_metadata_filtering():
    """Проверяет фильтрацию по law_id в _check_topology."""
    from zerde.models import DocumentClaim, ClaimType, ClaimSeverity, Fact, ValidationStatus
    from zerde.stages.s6_auditor import _check_topology
    
    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Изменения в Гражданский кодекс статьи 207",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.CRITICAL,
        entities=["1000-XIII"],
    )
    
    fact = Fact(
        fact_id="fact_0001",
        claim_id="claim_0001",
        claim=claim.claim_text,
        source_ids=["gk_chunk_1"],
    )
    
    # 1. Корректный law_id -> Проходит проверку
    corpus_index = {
        "gk_chunk_1": EvidenceChunk(
            chunk_id="gk_chunk_1",
            source_url="http://adilet.zan.kz/rus/docs/K940001000_",
            source_title="ГК РК",
            content="Статья 207. Ответственность учредителя...",
            legal_rank=LegalRank.CODE,
            web_tier=WebTier.TIER_1,
            law_id="1000-XIII",
        )
    }
    
    assert _check_topology(fact, ["gk_chunk_1"], corpus_index, claim) is True
    
    # 2. Неверный law_id (Закон о политических партиях) -> Отклоняется
    corpus_index_mismatch = {
        "gk_chunk_1": EvidenceChunk(
            chunk_id="gk_chunk_1",
            source_url="http://adilet.zan.kz/rus/docs/Z090000122_",
            source_title="Закон о партиях",
            content="Статья 2. Настоящий закон вводится...",
            legal_rank=LegalRank.LAW_RK,
            web_tier=WebTier.TIER_1,
            law_id="Z090000122_",
        )
    }
    
    assert _check_topology(fact, ["gk_chunk_1"], corpus_index_mismatch, claim) is False


def test_v9_3_bugfixes():
    """Проверяет новые исправления v9.3: обратную совместимость кэша, парсинг law_id веб-чанков, reliability и косвенные ссылки."""
    from zerde.models import AnalysisJSON, ClaimVerdict, VerdictStatus, ClaimSeverity
    from zerde.stages.s3_gather import _extract_law_id_from_text
    from zerde.stages.s6_auditor import audit_analysis, _extract_referenced_law_ids, _COMMON_LAW_NAME_MAP
    from zerde.models import DocumentClaim, ClaimType
    
    # 1. Проверяем обратную совместимость десериализации (BUG-5)
    legacy_data = {
        "analysis_id": "test_legacy",
        "source_doc_id": "doc_123",
        "plan_id": "plan_456",
        "pros": ["Старый плюс 1", "Старый плюс 2"],
        "recommendation": "Старая общая рекомендация",
        "verdicts": [],
        "facts": []
    }
    # Должно пройти валидацию без ValidationError и перенести значения в custom_*
    model = AnalysisJSON.model_validate(legacy_data)
    assert model.custom_pros == ["Старый плюс 1", "Старый плюс 2"]
    assert model.custom_recommendation == "Старая общая рекомендация"
    assert model.pros == ["Старый плюс 1", "Старый плюс 2"]
    assert model.recommendation == "Старая общая рекомендация"
    
    # 2. Проверяем извлечение law_id из веб-результатов (BUG-4)
    assert _extract_law_id_from_text("О внесении изменений в Гражданский Кодекс РК", "нормы статьи 207...") == "1000-XIII"
    assert _extract_law_id_from_text("Нарушения по КоАП РК штрафы", "согласно статье 79...") == "235-V"
    assert _extract_law_id_from_text("Уголовный кодекс РК", "статья 207...") == "226-V"
    assert _extract_law_id_from_text("закон о персональных данных 94-V", "согласие субъекта...") == "94-V"
    
    # 3. Проверяем, что reliability = None, если 0 реальных подтверждений (BUG-1)
    analysis = AnalysisJSON(
        analysis_id="test_rel",
        source_doc_id="doc_123",
        plan_id="plan_456",
        verdicts=[
            # Только виртуальные/неподтвержденные или пустые источники
            ClaimVerdict(claim_id="claim_0001", status=VerdictStatus.CONFIRMED, source_ids=["UNLINKED"], severity=ClaimSeverity.MEDIUM),
            ClaimVerdict(claim_id="claim_0002", status=VerdictStatus.UNVERIFIED, source_ids=[], severity=ClaimSeverity.MEDIUM),
        ],
    )
    audited = audit_analysis(analysis, [])
    assert audited.overall_reliability is None, "Надежность должна быть None, если нет реальных подтверждений физическим корпусом."
    
    # 4. Проверяем расширенные косвенные ссылки (BUG-6)
    claim_tax = DocumentClaim(
        claim_id="c_tax",
        claim_text="в соответствии с налоговым законодательством",
        claim_type=ClaimType.NORMATIVE,
        severity=ClaimSeverity.HIGH
    )
    claim_budget = DocumentClaim(
        claim_id="c_budget",
        claim_text="согласно нормам бюджетного законодательства Республики Казахстан",
        claim_type=ClaimType.NORMATIVE,
        severity=ClaimSeverity.HIGH
    )
    assert "Налоговый кодекс" in _extract_referenced_law_ids(claim_tax)
    assert "Бюджетный кодекс" in _extract_referenced_law_ids(claim_budget)


@pytest.mark.asyncio
async def test_v9_4_metadata_first_search():
    """
    Проверяет, что Metadata-first поиск (Шаг 1) точно сопоставляет
    утверждение и чанк по law_id и номеру статьи, минуя BM25.
    """
    from zerde.models import DocumentClaim, ClaimType, ClaimSeverity
    from zerde.stages.s6_auditor import _exact_metadata_search, _extract_article_from_claim
    
    claim = DocumentClaim(
        claim_id="claim_m1",
        claim_text="В соответствии со статьей 44 Гражданского кодекса",
        quote="статья 44 Гражданского кодекса",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH
    )
    
    # 1. Извлечение статьи
    assert _extract_article_from_claim(claim) == "44"
    
    # 2. Прямой metadata-поиск
    chunk_1 = EvidenceChunk(
        chunk_id="chunk_m1",
        source_url="http://adilet.zan.kz/rus/docs/K940001000_",
        source_title="Гражданский кодекс",
        content="Текст статьи 44 о юридических лицах...",
        legal_rank=LegalRank.CODE,
        law_id="1000-XIII",
        article="44"
    )
    chunk_2 = EvidenceChunk(
        chunk_id="chunk_m2",
        source_url="http://adilet.zan.kz/rus/docs/K940001000_",
        source_title="Гражданский кодекс",
        content="Текст статьи 45...",
        legal_rank=LegalRank.CODE,
        law_id="1000-XIII",
        article="45"
    )
    
    corpus = {"chunk_m1": chunk_1, "chunk_m2": chunk_2}
    candidates = _exact_metadata_search(claim, corpus)
    assert candidates == ["chunk_m1"]


def test_v9_4_multi_signal_ranking():
    """
    Проверяет многофакторный LegalRank классификатор для веб-страниц.
    """
    from zerde.utils.legal_scorer import infer_legal_rank_from_web_content
    
    # 1. Точное совпадение Кодекса на Expert домене -> CODE (Rank 2)
    rank, conf, reason = infer_legal_rank_from_web_content(
        tier=WebTier.TIER_2,
        title="Гражданский кодекс Республики Казахстан (Общая часть)",
        content="Статья 1. Отношения, регулируемые гражданским законодательством",
        url="http://zakon.kz/docs/civil_code"
    )
    assert rank == LegalRank.CODE
    assert conf >= 0.8
    assert "Strict Code pattern match" in reason
    
    # 2. Блог с упоминанием «этического кодекса» -> Fallback к TIER_3 rank (MEDIA_UNKNOWN)
    rank_fp, conf_fp, reason_fp = infer_legal_rank_from_web_content(
        tier=WebTier.TIER_3,
        title="Новый этический кодекс компании",
        content="Мы приняли этический кодекс поведения для сотрудников.",
        url="https://medium.com/@user/ethics"
    )
    assert rank_fp == LegalRank.MEDIA_UNKNOWN
    assert "Fallback to WebTier TIER_3" in reason_fp


def test_v9_4_mathematical_reliability_and_stats():
    """
    Проверяет математическую модель калибрации надежности и stats_builder.
    """
    from zerde.models import AnalysisJSON, ClaimVerdict, VerdictStatus, ClaimSeverity, Fact, ValidationStatus
    from zerde.stages.s6_auditor import audit_analysis
    
    chunk = EvidenceChunk(
        chunk_id="real_c1",
        source_url="http://adilet.zan.kz/docs",
        source_title="Закон РК",
        content="Утверждение 1 Действующая норма права",
        legal_rank=LegalRank.LAW_RK,
        law_id="50-V"
    )
    dummy = [
        EvidenceChunk(
            chunk_id=f"dummy_{i}",
            source_url="http://adilet.zan.kz/docs",
            source_title="Закон РК",
            content="Совершенно другой текст и слова для наполнения базы",
            legal_rank=LegalRank.LAW_RK
        )
        for i in range(4)
    ]
    
    # Создаем тестовый анализ с:
    # - 1 CONFIRMED (CRITICAL, с реальным источником)
    # - 1 CONTRADICTED (MEDIUM)
    analysis = AnalysisJSON(
        analysis_id="test_m94",
        source_doc_id="doc_123",
        plan_id="plan_456",
        facts=[
            Fact(fact_id="f1", claim_id="claim_1", claim="Утверждение 1", source_ids=["real_c1"], confidence=1.0, validation_status=ValidationStatus.HIGH, bm25_score=0.9),
            Fact(fact_id="f2", claim_id="claim_2", claim="Утверждение 2", source_ids=["real_c1"], confidence=0.8, validation_status=ValidationStatus.LOW, bm25_score=0.3),
        ],
        verdicts=[
            ClaimVerdict(claim_id="claim_1", status=VerdictStatus.CONFIRMED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_2", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.MEDIUM),
        ]
    )
    
    audited = audit_analysis(analysis, [chunk] + dummy)
    
    # 1. Проверяем наличие stats
    assert audited.stats is not None
    assert audited.stats.n_total == 2
    assert audited.stats.n_confirmed == 1
    assert audited.stats.n_contradicted == 1
    
    # 2. Проверяем динамические свойства-делегаты
    assert "Подтверждено 1 из 2" in audited.pros[0]
    assert "1 подтверждено" in audited.recommendation
    assert "1 опровергнуто" in audited.recommendation
    
    # 3. Проверяем надежность (не должна быть None, так как есть реальный подтвержденный источник)
    assert audited.overall_reliability is not None
    assert 0.0 <= audited.overall_reliability <= 1.0


def test_v9_5_new_features():
    """
    Проверяет:
    1. Синонимичный маппинг law_id (_are_law_ids_synonymous) с регистронезависимостью.
    2. Детерминированное определение ранга по URL-паттернам Әділет.
    3. Ограничение снизу для Reliability Score (min 5% при n_real_confirmed > 0).
    4. Сохранение пробелов в датах/сроках вступления в силу.
    """
    # 1. Синонимы
    from zerde.stages.s6_auditor import _are_law_ids_synonymous
    assert _are_law_ids_synonymous("k940001000", "1000-XIII")
    assert _are_law_ids_synonymous("K940001000", "1000-xiii")
    assert _are_law_ids_synonymous("235-V", "k1400000235")
    assert _are_law_ids_synonymous("350-vi", "K2000000350")
    assert not _are_law_ids_synonymous("1000-XIII", "235-V")

    # 2. Ранг по URL-паттернам
    from zerde.utils.legal_scorer import infer_legal_rank_from_web_content
    # Кодекс (k)
    r_code, _, _ = infer_legal_rank_from_web_content(
        tier=WebTier.TIER_1, title="КоАП", content="штрафы", url="https://adilet.zan.kz/rus/docs/K1400000235"
    )
    assert r_code == LegalRank.CODE

    # Закон (z)
    r_law, _, _ = infer_legal_rank_from_web_content(
        tier=WebTier.TIER_1, title="Закон о госимуществе", content="имущество", url="https://adilet.zan.kz/rus/docs/Z1100000413"
    )
    assert r_law == LegalRank.LAW_RK

    # Приказ министерства (v)
    r_order, _, _ = infer_legal_rank_from_web_content(
        tier=WebTier.TIER_1, title="Приказ Минздрава", content="нормативы", url="https://adilet.zan.kz/rus/docs/V2400000155"
    )
    assert r_order == LegalRank.MINISTERIAL_ORDER

    # 3. Лимит Reliability 5% при тяжелых коллизиях
    from zerde.models import AnalysisJSON, ClaimVerdict, VerdictStatus, ClaimSeverity, Fact, ValidationStatus
    from zerde.stages.s6_auditor import audit_analysis
    
    chunk = EvidenceChunk(
        chunk_id="real_c1",
        source_url="http://adilet.zan.kz/docs",
        source_title="Закон РК",
        content="Норма права",
        legal_rank=LegalRank.LAW_RK,
        law_id="50-V"
    )
    
    # Конфигурируем экстремальный случай:
    # 1 подтвержденный факт, но ОЧЕНЬ много критических противоречий (p_conflict > 1.0)
    analysis = AnalysisJSON(
        analysis_id="test_m95_rel",
        source_doc_id="doc_123",
        plan_id="plan_456",
        facts=[
            Fact(fact_id="f1", claim_id="claim_1", claim="Норма права", source_ids=["real_c1"], confidence=1.0, validation_status=ValidationStatus.HIGH, bm25_score=0.9),
        ],
        verdicts=[
            ClaimVerdict(claim_id="claim_1", status=VerdictStatus.CONFIRMED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_2", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_3", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_4", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_5", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_6", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_7", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
            ClaimVerdict(claim_id="claim_8", status=VerdictStatus.CONTRADICTED, source_ids=["real_c1"], severity=ClaimSeverity.CRITICAL),
        ]
    )
    
    dummy = [
        EvidenceChunk(chunk_id=f"dummy_{i}", source_url="http://adilet.zan.kz/docs", source_title="Закон", content="Текст", legal_rank=LegalRank.LAW_RK)
        for i in range(4)
    ]
    
    audited = audit_analysis(analysis, [chunk] + dummy)
    # Должно быть строго 0.05 (5%), а не 0.0 из-за лимита надежности снизу
    assert audited.overall_reliability == 0.05

    # 4. Сохранение пробелов в датах/сроках
    from zerde.stages.s2_5_claim_extractor import _regex_extract
    claims = _regex_extract("вводится в действие по истечении шести месяцев со дня его опубликования")
    # Проверяем, что в сущностях пробелы сохранились!
    assert any(any("шести месяцев со дня" in e for e in c.entities) for c in claims)
