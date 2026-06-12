"""
Тесты без LLM-вызовов.
Покрытие:
  - Stage 1: Ingest (DOCX парсинг)
  - Stage 3: _normalize_law_id_to_adilet_urls
  - Stage 4: ContentSpamFilter + SHA256 dedup
  - Stage 5: _rank_by_relevance (BM25 ранкер)
  - Utils: kz_translit, legal_scorer
"""


import pytest

# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------
from zerde.models import (
    AdiletFallbackStrategy,
    DocumentFormat,
    EvidenceChunk,
    LegalRank,
    WebTier,
)


def _make_chunk(
    content: str = "Тестовый контент",
    source_url: str = "https://adilet.zan.kz/rus/docs/Z1300000094",
    legal_rank: LegalRank = LegalRank.LAW_RK,
    adilet_fallback: AdiletFallbackStrategy | None = None,
    is_conflict: bool = False,
    law_id: str | None = None,
) -> EvidenceChunk:
    """Фабрика чанков для тестов."""
    import hashlib
    chunk_id = hashlib.sha256(content.encode()).hexdigest()
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_url=source_url,
        source_title="Test",
        content=content,
        legal_rank=legal_rank,
        adilet_fallback_used=adilet_fallback,
        is_conflict=is_conflict,
        law_id=law_id,
    )


# ===========================================================================
# Stage 1: Document Ingestion
# ===========================================================================

class TestS1Ingest:
    """Тесты для s1_ingest.py."""

    def test_detect_format_docx(self):
        from pathlib import Path

        from zerde.stages.s1_ingest import _detect_format
        assert _detect_format(Path("test.docx")) == DocumentFormat.DOCX

    def test_detect_format_pdf(self):
        from pathlib import Path

        from zerde.stages.s1_ingest import _detect_format
        assert _detect_format(Path("test.pdf")) == DocumentFormat.PDF

    def test_detect_format_txt(self):
        from pathlib import Path

        from zerde.stages.s1_ingest import _detect_format
        assert _detect_format(Path("test.txt")) == DocumentFormat.TXT

    def test_detect_format_unsupported(self):
        from pathlib import Path

        from zerde.stages.s1_ingest import _detect_format
        with pytest.raises(ValueError, match="Unsupported format"):
            _detect_format(Path("test.xlsx"))

    def test_heuristic_lang_russian(self):
        from zerde.stages.s1_ingest import _heuristic_lang
        result = _heuristic_lang("Настоящий Закон регулирует общественные отношения")
        assert result == "ru"

    def test_heuristic_lang_english(self):
        from zerde.stages.s1_ingest import _heuristic_lang
        result = _heuristic_lang("This law regulates public relations in the field of data protection")
        assert result == "en"

    @pytest.mark.asyncio
    async def test_ingest_docx_file(self):
        """Интеграционный тест: парсинг тестового DOCX."""
        from pathlib import Path
        docx_path = Path("docs/ZERDE_test_bill_RK_2025.docx")
        if not docx_path.exists():
            pytest.skip("Тестовый DOCX не найден")

        from zerde.stages.s1_ingest import ingest_document
        state = await ingest_document(docx_path)

        assert state.doc_id  # SHA256 не пустой
        assert state.format == DocumentFormat.DOCX
        assert state.word_count > 100  # Документ не пустой
        assert state.language_detected in ("ru", "kk", "mixed")
        assert "персональных данных" in state.normalized_text.lower()


# ===========================================================================
# Stage 3: Law ID Normalization
# ===========================================================================

class TestS3LawIdNormalization:
    """Тесты для _normalize_law_id_to_adilet_urls."""

    def test_known_law_94v(self):
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        urls = _normalize_law_id_to_adilet_urls("94-V", "https://adilet.zan.kz")
        assert "https://adilet.zan.kz/rus/docs/Z1300000094" in urls
        assert urls[0] == "https://adilet.zan.kz/rus/docs/Z1300000094"  # первый

    def test_removed_87iv_mapping_no_longer_fabricates(self):
        """87-IV больше НЕ маппится на 94-V (хардкод удалён); нет adilet-кода → нет URL."""
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        from zerde.utils.law_registry import get_registry
        assert get_registry().resolve("87-IV") == "87-IV"   # резолвит в себя, не в 94-V
        assert _normalize_law_id_to_adilet_urls("87-IV", "https://adilet.zan.kz") == []

    def test_known_koap_235v(self):
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        urls = _normalize_law_id_to_adilet_urls("235-V", "https://adilet.zan.kz")
        assert urls[0] == "https://adilet.zan.kz/rus/docs/K1400000235"

    def test_known_law_370ii(self):
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        urls = _normalize_law_id_to_adilet_urls("370-II", "https://adilet.zan.kz")
        assert urls[0] == "https://adilet.zan.kz/rus/docs/Z030000370_"

    def test_unknown_short_id_yields_no_fabricated_urls(self):
        """Неизвестный short ID НЕ должен фабриковать adilet URL (false-grounding)."""
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        urls = _normalize_law_id_to_adilet_urls("999-VI", "https://adilet.zan.kz")
        assert urls == []  # registry не знает → не угадываем URL

    def test_already_adilet_format(self):
        """ID в формате Adilet должен проходить как есть."""
        from zerde.stages.s3_gather import _normalize_law_id_to_adilet_urls
        urls = _normalize_law_id_to_adilet_urls("Z1300000094", "https://adilet.zan.kz")
        assert "https://adilet.zan.kz/rus/docs/Z1300000094" in urls


# ===========================================================================
# Stage 4: ContentSpamFilter
# ===========================================================================

class TestS4SpamFilter:
    """Тесты для ContentSpamFilter."""

    def test_adilet_chunk_never_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Короткий",  # < 400 символов, но Adilet
            adilet_fallback=AdiletFallbackStrategy.CSS_SELECTOR,
        )
        assert _is_spam(chunk) is False

    def test_egov_url_is_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Длинный контент " * 100,
            source_url="https://egov.kz/cms/ru/news/some_news",
        )
        assert _is_spam(chunk) is True

    def test_press_news_is_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Длинный контент про государственные услуги " * 30,
            source_url="https://www.gov.kz/memleket/entities/abay/press/news/details/123?lang=ru",
        )
        assert _is_spam(chunk) is True

    def test_egov_mobile_content_is_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Теперь вход в приложение eGov Mobile доступен только через биометрию. " * 10,
            source_url="https://some-random-site.kz/article",
        )
        assert _is_spam(chunk) is True

    def test_short_web_chunk_is_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Короткий текст без смысла",
            source_url="https://some-site.kz/page",
        )
        assert _is_spam(chunk) is True  # < 400 символов

    def test_no_legal_signal_is_spam(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content="Казахстан активно развивает цифровую экономику, "
                    "привлекая инвестиции из различных стран мира. "
                    "Это позволяет создавать новые рабочие места. " * 15,
            source_url="https://some-analytics.kz/article",
        )
        assert _is_spam(chunk) is True  # Нет юридических сигналов

    def test_legal_content_passes(self):
        from zerde.stages.s4_fusion import _is_spam
        chunk = _make_chunk(
            content=(
                "Согласно статье 26 Закона РК «О персональных данных и их защите», "
                "оператор обязан обеспечить хранение баз данных на серверах, "
                "расположенных на территории Республики Казахстан. "
                "Нарушение данного требования влечет штраф в размере 500 МРП "
                "для субъектов малого предпринимательства в соответствии с КоАП РК. " * 3
            ),
            source_url="https://adilet.zan.kz/rus/docs/Z1300000094",
        )
        assert _is_spam(chunk) is False

    def test_apply_spam_filter_reduces_chunks(self):
        from zerde.stages.s4_fusion import _apply_spam_filter

        chunks = [
            # Полезный — Adilet
            _make_chunk(
                content="Статья 1 Закона РК " * 50,
                adilet_fallback=AdiletFallbackStrategy.CSS_SELECTOR,
            ),
            # Спам — egov новость
            _make_chunk(
                content="eGov Mobile обновление " * 50,
                source_url="https://egov.kz/cms/ru/news/update",
            ),
            # Спам — короткий
            _make_chunk(content="Коротко", source_url="https://test.kz"),
            # Полезный — юр. контент
            _make_chunk(
                content="Статья 9 КоАП РК предусматривает штраф 500 МРП за нарушение " * 10,
                source_url="https://zakon.kz/article/123",
            ),
        ]

        filtered = _apply_spam_filter(chunks)
        assert len(filtered) == 2  # Adilet + юр. контент


# ===========================================================================
# Stage 4: SHA256 Dedup
# ===========================================================================

class TestS4Dedup:
    """Тесты для SHA256 дедупликации."""

    def test_exact_duplicates_marked(self):
        from zerde.stages.s4_fusion import _dedup_by_hash

        content = "Одинаковый контент для дедупликации " * 20
        c1 = _make_chunk(content=content, source_url="https://a.kz")
        c2 = _make_chunk(content=content, source_url="https://b.kz")
        c3 = _make_chunk(content="Уникальный контент " * 20, source_url="https://c.kz")

        result = _dedup_by_hash([c1, c2, c3])
        assert result[0].is_duplicate is False
        assert result[1].is_duplicate is True  # дубль c1
        assert result[2].is_duplicate is False


# ===========================================================================
# Stage 5: BM25 Corpus Ranker
# ===========================================================================

# ===========================================================================
# Utils: KZ Translit
# ===========================================================================

class TestKZTranslit:
    """Тесты для kz_translit."""

    def test_normalize_preserves_cyrillic(self):
        from zerde.utils.kz_translit import normalize_kz_text
        text = "Закон Республики Казахстан"
        assert normalize_kz_text(text) == text

    def test_normalize_handles_input(self):
        from zerde.utils.kz_translit import normalize_kz_text
        text = "Закон   Республики    Казахстан"
        result = normalize_kz_text(text)
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# Utils: Legal Scorer
# ===========================================================================

class TestLegalScorer:
    """Тесты для classify_web_tier и infer_legal_rank_from_tier."""

    def test_adilet_is_tier1(self):
        from zerde.utils.legal_scorer import classify_web_tier
        assert classify_web_tier("https://adilet.zan.kz/rus/docs/Z1300000094") == WebTier.TIER_1

    def test_gov_kz_is_tier1(self):
        from zerde.utils.legal_scorer import classify_web_tier
        assert classify_web_tier("https://www.gov.kz/memleket/entities/...") == WebTier.TIER_1

    def test_zakon_kz_is_tier2(self):
        from zerde.utils.legal_scorer import classify_web_tier
        assert classify_web_tier("https://zakon.kz/article/123") == WebTier.TIER_2

    def test_medium_is_tier3(self):
        from zerde.utils.legal_scorer import classify_web_tier
        assert classify_web_tier("https://medium.com/@user/article") == WebTier.TIER_3

    def test_forum_is_blacklist(self):
        from zerde.utils.legal_scorer import classify_web_tier
        assert classify_web_tier("https://forum.example.com/thread") == WebTier.BLACKLIST

    def test_tier1_to_rank(self):
        from zerde.utils.legal_scorer import infer_legal_rank_from_tier
        assert infer_legal_rank_from_tier(WebTier.TIER_1) == LegalRank.MINISTERIAL_ORDER

    def test_tier3_to_rank(self):
        from zerde.utils.legal_scorer import infer_legal_rank_from_tier
        assert infer_legal_rank_from_tier(WebTier.TIER_3) == LegalRank.MEDIA_UNKNOWN


# ===========================================================================
# Models: EvidenceChunk
# ===========================================================================

class TestModels:
    """Тесты для моделей данных."""

    def test_evidence_chunk_is_authoritative(self):
        chunk = _make_chunk(legal_rank=LegalRank.LAW_RK)
        assert chunk.is_authoritative is True

    def test_evidence_chunk_not_authoritative_for_expert(self):
        chunk = _make_chunk(legal_rank=LegalRank.EXPERT_ANALYTICS)
        assert chunk.is_authoritative is False

    def test_chunk_id_computed(self):
        chunk = _make_chunk(content="Test content for hash")
        assert len(chunk.chunk_id) == 64  # SHA256 hex


# ===========================================================================
# Stage 2.7 & Stage 6 Specific Fixes
# ===========================================================================

class TestStageFixes:
    """Тесты для исправленных логических багов в Stage 2.7 и Stage 6."""

    def test_unlinked_fact_auditor(self):
        from zerde.models import AnalysisJSON, Fact, ValidationStatus
        from zerde.stages.s6_auditor import audit_analysis

        fact = Fact(
            fact_id="fact_unlinked",
            claim="Какое-то неподтвержденное утверждение",
            source_ids=["UNLINKED"],
            confidence=0.95
        )
        analysis = AnalysisJSON(
            analysis_id="test_analysis",
            source_doc_id="test_doc",
            plan_id="test_plan",
            facts=[fact]
        )

        # Вызов аудитора без чанков в корпусе
        result = audit_analysis(analysis, [])
        assert result.facts[0].validation_status == ValidationStatus.UNVERIFIED
        assert result.facts[0].bm25_score == 0.0

    def test_sub_article_splitting(self):
        from zerde.stages.s2_7_self_check import _detect_deadline_collisions

        # Статья с дефисом, без точки в конце заголовка, содержащая противоречие по срокам
        doc_text = """
Статья 79-1
Внутренний регламент устанавливает, что ответ на запрос предоставляется в течение 10 рабочих дней.

Статья 79-1
Несмотря на предыдущие положения, срок ответа составляет 20 рабочих дней.
"""
        collisions = _detect_deadline_collisions(doc_text)
        assert len(collisions) == 1
        assert "79-1" in collisions[0].claim_text
        assert "10 vs 20" in collisions[0].claim_text

    def test_sub_article_splitting_dot_and_newline(self):
        from zerde.stages.s2_7_self_check import _detect_deadline_collisions

        # Статьи с десятичной точкой в номере
        doc_text = """
Статья 10.1.
Срок рассмотрения составляет 5 дней.

Статья 10.1
Внезапно срок рассмотрения составляет 15 дней.
"""
        collisions = _detect_deadline_collisions(doc_text)
        assert len(collisions) == 1
        assert "10.1" in collisions[0].claim_text
        assert "5 vs 15" in collisions[0].claim_text

    def test_global_chrono_anomalies(self):
        from zerde.stages.s2_7_self_check import _detect_chronological_anomalies

        # Тест проверяет, что если расстояние больше 500 символов, аномалия НЕ детектируется (C1 Fix)
        # А если расстояние меньше 500 символов — аномалия детектируется.
        padding_far = " " * 1200
        doc_text_far = f"Закон принят 21 мая 2026 года.{padding_far}Настоящий Закон вступает в силу с 1 мая 2025 года."
        anomalies_far = _detect_chronological_anomalies(doc_text_far)
        assert len(anomalies_far) == 0  # Слишком далеко, проигнорировано

        padding_near = " " * 100
        doc_text_near = f"Закон принят 21 мая 2026 года.{padding_near}Настоящий Закон вступает в силу с 1 мая 2025 года."
        anomalies_near = _detect_chronological_anomalies(doc_text_near)
        assert len(anomalies_near) == 1
        assert "21.05.2026" in anomalies_near[0].claim_text
        assert "01.05.2025" in anomalies_near[0].claim_text

