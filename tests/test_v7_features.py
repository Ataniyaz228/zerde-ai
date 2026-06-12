"""
Tests for Zerde v7.0 features:
- Structural claim filter
- Deterministic claim deduplication
- Conflicts bridge from verdicts
- Validation status override
"""
import pytest

from zerde.models import (
    ClaimSeverity,
    ClaimType,
    ClaimVerdict,
    ConflictType,
    DocumentClaim,
    Fact,
    ValidationStatus,
    VerdictStatus,
)
from zerde.stages.s2_5_claim_extractor import _dedup_claims, _is_structural_claim
from zerde.stages.s6_auditor import _build_conflicts_from_verdicts
from zerde.stages.s7_render import _fact_icon


class TestStructuralFilter:
    def test_structural_legal_ref_no_modal(self):
        # Regex claim: "внести изменения в статью 41" — structural даже при HIGH severity
        c = DocumentClaim(
            claim_id="c1",
            claim_text="Документ утверждает: article_ref=41 (строка: ...)",
            claim_type=ClaimType.LEGAL_REF,
            severity=ClaimSeverity.HIGH,
        )
        assert _is_structural_claim(c) is True

    def test_not_structural_due_to_modal_verb(self):
        c = DocumentClaim(
            claim_id="c2",
            claim_text="Оператор обязан уведомить субъекта в течение 24 часов",
            claim_type=ClaimType.TEMPORAL,
            severity=ClaimSeverity.HIGH,
        )
        assert _is_structural_claim(c) is False

    def test_not_structural_due_to_numeric_norm(self):
        # Штраф 500 МРП — нормативное, не structural
        c = DocumentClaim(
            claim_id="c3",
            claim_text="Документ утверждает: fine_mrp=500",
            claim_type=ClaimType.FINANCIAL,
            severity=ClaimSeverity.CRITICAL,
        )
        assert _is_structural_claim(c) is False

    def test_structural_phrase(self):
        # FACTUAL/NORMATIVE теперь всегда аналитические (v9.6, шаг 2.5), поэтому
        # для проверки step-4 structural_phrases берём тип, доходящий до шага 4.
        c = DocumentClaim(
            claim_id="c4",
            claim_text="Раздел 10 присутствует в структуре документа",
            claim_type=ClaimType.TEMPORAL,   # не FACTUAL/NORMATIVE/LEGAL_*; нет модала/числовой нормы
            severity=ClaimSeverity.LOW,
        )
        assert _is_structural_claim(c) is True

    def test_factual_claim_is_analytical_v96(self):
        # Регрессионный замок на v9.6: FACTUAL никогда не структурный — идёт в аудитор.
        c = DocumentClaim(
            claim_id="c4b",
            claim_text="Статья 10 присутствует в структуре документа",
            claim_type=ClaimType.FACTUAL,
            severity=ClaimSeverity.LOW,
        )
        assert _is_structural_claim(c) is False

    def test_not_structural_modal_inside_low(self):
        # Граничный случай: LOW severity но есть модальный глагол
        c = DocumentClaim(
            claim_id="c5",
            claim_text="Оператор должен присутствовать на совещании",
            claim_type=ClaimType.FACTUAL,
            severity=ClaimSeverity.LOW,
        )
        assert _is_structural_claim(c) is False

    def test_structural_law_id_high_severity(self):
        # Regex claim с HIGH severity: "№ 94-V" — structural (просто номер закона)
        c = DocumentClaim(
            claim_id="c6",
            claim_text="Документ утверждает: law_id=94-V (строка: ...)",
            claim_type=ClaimType.LEGAL_ID,
            severity=ClaimSeverity.HIGH,
        )
        assert _is_structural_claim(c) is True


class TestClaimDedup:
    def test_dedup_identical_claims(self):
        c1 = DocumentClaim(
            claim_id="c1", claim_text="Закон № 94-V", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL, entities=["94-V"]
        )
        c2 = DocumentClaim(
            claim_id="c2", claim_text="закон 94-V", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL, entities=["94-V"]
        )
        result = _dedup_claims([c1, c2])
        assert len(result) == 1
        # quote_variants собраны (quotes пустые по умолчанию, поэтому 0)
        assert len(result[0].quote_variants) == 0

    def test_dedup_empty_list(self):
        assert _dedup_claims([]) == []

    def test_dedup_keeps_best(self):
        c1 = DocumentClaim(
            claim_id="c1", claim_text="87-IV", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL,
            deterministic_verdict="INVALID → 94-V",
        )
        c2 = DocumentClaim(
            claim_id="c2", claim_text="87-iv", claim_type=ClaimType.LEGAL_ID, severity=ClaimSeverity.CRITICAL,
        )
        result = _dedup_claims([c1, c2])
        assert len(result) == 1
        assert result[0].deterministic_verdict == "INVALID → 94-V"

    def test_no_false_dedup(self):
        c1 = DocumentClaim(claim_id="c1", claim_text="штраф 500 МРП", claim_type=ClaimType.FINANCIAL, severity=ClaimSeverity.CRITICAL)
        c2 = DocumentClaim(claim_id="c2", claim_text="штраф 1500 МРП", claim_type=ClaimType.FINANCIAL, severity=ClaimSeverity.CRITICAL)
        result = _dedup_claims([c1, c2])
        assert len(result) == 2


class TestConflictsBridge:
    def test_hierarchy_conflict(self):
        v = ClaimVerdict(
            claim_id="c1",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="Штраф превышает лимит КоАП (2000 МРП)",
            confidence="HIGH",
            severity=ClaimSeverity.HIGH,
        )
        result = _build_conflicts_from_verdicts([v])
        assert len(result) == 1
        assert result[0].conflict_type == ConflictType.HIERARCHY
        assert result[0].severity == ClaimSeverity.HIGH

    def test_temporal_conflict(self):
        v = ClaimVerdict(
            claim_id="c2",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="Срок 24 часа не соответствует 10 рабочим дням",
            confidence="MEDIUM",
        )
        result = _build_conflicts_from_verdicts([v])
        assert result[0].conflict_type == ConflictType.TEMPORAL

    def test_factual_conflict(self):
        v = ClaimVerdict(
            claim_id="c3",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="МРП 3450 вместо 3932",
            confidence="HIGH",
        )
        result = _build_conflicts_from_verdicts([v])
        assert result[0].conflict_type == ConflictType.FACTUAL

    def test_exceeds_without_hierarchy_is_factual(self):
        # "превышает" без КоАП/иерархии → FACTUAL, не HIERARCHY
        v = ClaimVerdict(
            claim_id="c3b",
            status=VerdictStatus.CONTRADICTED,
            contradiction_detail="Сумма превышает установленный лимит",
            confidence="HIGH",
        )
        result = _build_conflicts_from_verdicts([v])
        assert result[0].conflict_type == ConflictType.FACTUAL

    def test_skips_confirmed(self):
        v = ClaimVerdict(claim_id="c4", status=VerdictStatus.CONFIRMED, confidence="HIGH")
        result = _build_conflicts_from_verdicts([v])
        assert len(result) == 0

    def test_dedup_by_claim_id(self):
        v1 = ClaimVerdict(claim_id="c5", status=VerdictStatus.CONTRADICTED, contradiction_detail="err1", confidence="HIGH")
        v2 = ClaimVerdict(claim_id="c5", status=VerdictStatus.CONTRADICTED, contradiction_detail="err2", confidence="HIGH")
        result = _build_conflicts_from_verdicts([v1, v2])
        assert len(result) == 1


class TestFactIconOverride:
    def test_contradicted_is_red(self):
        f = Fact(fact_id="f1", claim_id="c1", claim="test", source_ids=["s1"], validation_status=ValidationStatus.HIGH)
        v_map = {"c1": ClaimVerdict(claim_id="c1", status=VerdictStatus.CONTRADICTED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🔴"

    def test_confirmed_is_green(self):
        f = Fact(fact_id="f2", claim_id="c2", claim="test", source_ids=["s1"], validation_status=ValidationStatus.LOW)
        v_map = {"c2": ClaimVerdict(claim_id="c2", status=VerdictStatus.CONFIRMED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🟢"

    def test_unverified_risk_is_orange(self):
        f = Fact(fact_id="f3", claim_id="c3", claim="test", source_ids=["s1"], validation_status=ValidationStatus.UNVERIFIED)
        v_map = {"c3": ClaimVerdict(claim_id="c3", status=VerdictStatus.UNVERIFIED, confidence="HIGH")}
        assert _fact_icon(f, v_map) == "🟠"

    def test_unverified_neutral_is_grey(self):
        f = Fact(fact_id="f4", claim_id="c4", claim="test", source_ids=["s1"], validation_status=ValidationStatus.UNVERIFIED)
        v_map = {"c4": ClaimVerdict(claim_id="c4", status=VerdictStatus.UNVERIFIED, confidence="LOW")}
        assert _fact_icon(f, v_map) == "⚫"


class TestClaimDedupEntityGrouping:
    def test_regex_entity_grouping(self):
        c1 = DocumentClaim(
            claim_id="c1",
            claim_text="Документ утверждает: law_id=235-VII (строка: «строка 10»)",
            quote="Цитата 1",
            claim_type=ClaimType.LEGAL_ID,
            severity=ClaimSeverity.CRITICAL,
            entities=["235-VII"],
        )
        c2 = DocumentClaim(
            claim_id="c2",
            claim_text="Документ утверждает: law_id=235-vii (строка: «строка 15»)",
            quote="Цитата 2",
            claim_type=ClaimType.LEGAL_ID,
            severity=ClaimSeverity.CRITICAL,
            entities=["235-VII"],
        )
        result = _dedup_claims([c1, c2])
        assert len(result) == 1
        assert result[0].entities == ["235-VII"]
        assert "Цитата 2" in result[0].quote_variants or "Цитата 1" in result[0].quote_variants


class TestPipelineResultAttributes:
    def test_attribute_access_and_validation(self):
        from zerde.models import DocumentFormat, DocumentState
        from zerde.pipeline import ZerdePipelineResult

        res = ZerdePipelineResult()
        # Setting a valid annotated attribute
        doc_state = DocumentState(
            doc_id="d1",
            original_path="f.txt",
            format=DocumentFormat.TXT,
            raw_text="hello",
            normalized_text="hello",
            char_count=5,
        )
        res.doc_state = doc_state
        assert res.doc_state == doc_state
        assert res["doc_state"] == doc_state

        # Accessing an undeclared attribute raises AttributeError
        with pytest.raises(AttributeError):
            _ = res.some_undeclared_attribute

        # Setting an undeclared attribute raises AttributeError
        with pytest.raises(AttributeError):
            res.some_undeclared_attribute = "value"

        # Accessing / setting private variables bypasses validation (so it falls back to dict/object logic)
        res._internal = "test"
        assert res._internal == "test"


class TestCacheConcurrency:
    @pytest.mark.asyncio
    async def test_cache_concurrency_stress(self):
        import asyncio
        import random
        from pathlib import Path

        from zerde.models import EvidenceChunk, LegalRank, WebTier
        from zerde.utils.cache import CacheManager, LLMCache

        # Setup test DB path
        db_path = "test_concurrency_cache.db"
        if Path(db_path).exists():
            try:
                Path(db_path).unlink()
            except Exception:
                pass

        cm = CacheManager(db_path=db_path)
        lc = LLMCache(db_path=db_path)

        # Pre-populate some dummy chunks & LLM keys
        chunks = [
            EvidenceChunk(
                chunk_id=f"chunk_{i}",
                source_url=f"http://example.com/{i}",
                source_title=f"Title {i}",
                content=f"Content for chunk {i}",
                content_summary=f"Summary {i}",
                legal_rank=LegalRank.LAW_RK,
                web_tier=WebTier.TIER_1,
            )
            for i in range(50)
        ]
        await cm.put_many(chunks)

        for i in range(50):
            await lc.put(model="test_model", prompt=f"prompt_{i}", response={"answer": f"response_{i}"})

        async def concurrent_reader():
            for _ in range(20):
                idx = random.randint(0, 49)
                # Concurrent read CacheManager
                chk = await cm.get(f"chunk_{idx}")
                assert chk is not None
                assert chk.chunk_id == f"chunk_{idx}"

                # Concurrent read LLMCache
                res = await lc.get(model="test_model", prompt=f"prompt_{idx}")
                assert res is not None
                assert res["answer"] == f"response_{idx}"

                # Non-existent keys
                assert await cm.get("non_existent") is None
                assert await lc.get("model", "non_existent_prompt") is None

                # Read stats
                await cm.stats()
                await lc.stats()

                await asyncio.sleep(0.001)

        async def concurrent_writer():
            for i in range(10):
                # Concurrent write CacheManager
                new_chunk = EvidenceChunk(
                    chunk_id=f"chunk_new_{i}",
                    source_url=f"http://example.com/new_{i}",
                    source_title=f"Title new {i}",
                    content=f"Content new {i}",
                    content_summary=f"Summary new {i}",
                    legal_rank=LegalRank.LAW_RK,
                    web_tier=WebTier.TIER_1,
                )
                await cm.put(new_chunk)

                # Concurrent write LLMCache
                await lc.put(model="test_model", prompt=f"prompt_new_{i}", response={"answer": f"response_new_{i}"})

                await asyncio.sleep(0.002)

        # Launch 50 readers (doing 1000 reads total) and 10 writers (doing 100 writes total)
        readers = [concurrent_reader() for _ in range(50)]
        writers = [concurrent_writer() for _ in range(10)]

        await asyncio.gather(*(readers + writers))

        # Cleanup DB
        if Path(db_path).exists():
            try:
                Path(db_path).unlink()
            except Exception:
                pass
        # Check that temporary files like db_path + "-wal" or "-shm" are also removed
        for suffix in ["-wal", "-shm"]:
            p = Path(db_path + suffix)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
