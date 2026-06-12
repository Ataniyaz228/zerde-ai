"""
test_cache_key_pin.py
Замок ключа LLM-кэша и структуры auditor-промпта.

Цель: дрейф ключа _make_key / PROMPT_CACHE_VERSION = тихая инвалидация
всего LLM-кэша => verdict_eval.py перестаёт быть $0 (100% cache hits ломаются).
Если эти тесты падают — НЕ обновляй хэш вслепую: разберись, почему сдвинулся
ключ (изменился формат _make_key? PROMPT_CACHE_VERSION? состав prompt_key?),
и осознанно bump-ни версию + обнови _PINNED_KEY вместе с ней.
"""
import json

from zerde.utils.cache import PROMPT_CACHE_VERSION, LLMCache

_PINNED_MODEL = "test-model/v1"
_PINNED_MESSAGES = [
    {"role": "system", "content": "You are a legal auditor."},
    {"role": "user", "content": "Проверь статью 44 ГК РК."},
]
_PINNED_KEY = "612ef4daef0d7a6eba5ffe76249fa7f18299c67d7028bb5e6785577b7607f62c"


def test_llm_cache_key_is_pinned():
    """Замок ключа LLM-кэша. Дрейф ключа = тихая инвалидация кэша = $ на API.
    Если этот тест упал — НЕ обновляй хэш вслепую: пойми, почему сдвинулся ключ
    (PROMPT_CACHE_VERSION? формат _make_key? поля prompt_key?) и осознанно bump-ни версию."""
    prompt_key = json.dumps(
        {"messages": _PINNED_MESSAGES, "temperature": 0.0, "max_tokens": 4096},
        ensure_ascii=False, sort_keys=True,
    )
    assert LLMCache._make_key(_PINNED_MODEL, prompt_key) == _PINNED_KEY


def test_prompt_cache_version_pinned():
    assert PROMPT_CACHE_VERSION == 1  # bump осознанно; обновить _PINNED_KEY вместе с этим


def test_build_auditor_prompt_structure():
    """Структурная проверка build_auditor_prompt: id_map, отсутствие "сырых"
    плейсхолдеров шаблона и видимость метки S1 в готовом промпте."""
    from zerde.models import (
        ClaimExtractionResult,
        ClaimSeverity,
        ClaimType,
        DocumentClaim,
        EvidenceChunk,
        LegalRank,
        QueryPlan,
    )
    from zerde.prompts.auditor import build_auditor_prompt

    chunk = EvidenceChunk(
        chunk_id="chunk_aaa",
        source_url="https://adilet.zan.kz/rus/docs/K1400000235",
        source_title="КоАП РК",
        content="Статья 79. Нарушение законодательства о персональных данных.",
        legal_rank=LegalRank.CODE,
        law_id="235-V",
        article="79",
    )
    claim = DocumentClaim(
        claim_id="claim_0001",
        claim_text="Документ ссылается на статью 79 КоАП РК",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH,
    )
    claims = ClaimExtractionResult(doc_id="test_doc", claims=[claim])
    plan = QueryPlan(plan_id="plan_1", source_doc_id="test_doc")

    prompt, id_map = build_auditor_prompt(
        chunks=[chunk],
        claims=claims,
        plan=plan,
        doc_text="Текст проекта закона на русском языке.",
    )

    # 1. id_map: единственный chunk получает короткий label S1
    assert id_map == {"S1": "chunk_aaa"}

    # 2. Нет недозамещённых плейсхолдеров шаблона
    for placeholder in (
        "__DOCUMENT_EXCERPT__",
        "__CLAIM_COUNT__",
        "__CLAIMS_CHECKLIST__",
        "__DETERMINISTIC_VERDICTS__",
        "__REFERENCE_DATA__",
        "__CHUNK_COUNT__",
        "__CORPUS_TEXT__",
        "__SCHEMA__",
    ):
        assert placeholder not in prompt, f"unsubstituted placeholder: {placeholder}"

    # 3. Метка S1 видна LLM в тексте промпта (корпус доказательств)
    assert "S1" in prompt
