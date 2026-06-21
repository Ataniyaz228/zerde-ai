"""S7-translate — guard сохранности улик + fail-safe фолбэк на RU.

Без сети: cached_llm_call/make_llm_client мокаются. Проверяем главный инвариант —
перевод, потерявший улику (номер статьи / ID закона / % / дату / URL / статус-тег),
ОТБРАСЫВАЕТСЯ и возвращается оригинал RU.
"""

from __future__ import annotations

from types import SimpleNamespace

import zerde.stages.s7_translate as s7t


def _fake_settings(protocol: str = "openai", base_url: str = "", api_key: str = ""):
    """Минимальный settings-stub: фиксирует путь переводчика, не читая .env.

    translate_report_md в обоих ветках обращается только к этим полям; сам
    LLM-вызов в тестах замокан, поэтому остальные настройки не нужны.
    """
    return SimpleNamespace(
        translator_protocol=protocol,
        translator_base_url=base_url,
        translator_api_key=api_key,
        llm_model_translator="translator-model",
        llm_max_tokens_translator=16384,
    )

_RU = (
    "# Отчёт\n\n"
    "## ⚖️ Выявленные Конфликты и Коллизии (2)\n\n"
    "Норма противоречит **ст. 14** закона `253-V` (код K1400000226).\n"
    "> …подаётся в течение десяти рабочих дней…\n\n"
    "Надёжность: 92%. Дата: 15.06.2026. Источник: https://adilet.zan.kz/rus/docs/K1400000226\n\n"
    "**[ПОДТВЕРЖДЕНО]** вывод обоснован.\n"
)


def test_guard_passes_when_all_evidence_kept():
    # KZ: проза «переведена» (условно), но все улики дословно на месте.
    kz = _RU.replace("Норма противоречит", "Норма қайшы келеді").replace("вывод обоснован", "қорытынды негізді")
    assert s7t._evidence_preserved(_RU, kz) is True


def test_guard_fails_when_percent_dropped():
    kz = _RU.replace("92%", "тоқсан екі пайыз")  # процент «перевели» → улика потеряна
    assert s7t._evidence_preserved(_RU, kz) is False


def test_guard_fails_when_law_id_changed():
    kz = _RU.replace("253-V", "235-V")  # подмена ID закона — худший случай
    assert s7t._evidence_preserved(_RU, kz) is False


def test_guard_fails_when_article_or_status_dropped():
    assert s7t._evidence_preserved(_RU, _RU.replace("ст. 14", "14-бап")) is False
    assert s7t._evidence_preserved(_RU, _RU.replace("[ПОДТВЕРЖДЕНО]", "[РАСТАЛДЫ]")) is False


def test_guard_no_evidence_is_safe():
    plain = "# Заголовок\n\nПросто проза без улик.\n"
    assert s7t._evidence_preserved(plain, "# Тақырып\n\nЖай проза.\n") is True


async def test_translate_returns_kz_on_good_translation(monkeypatch):
    good_kz = _RU.replace("Норма противоречит", "Норма қайшы келеді")

    async def fake_call(**kwargs):
        return {"text": good_kz}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz", settings=_fake_settings())
    assert out == good_kz


async def test_translate_falls_back_to_ru_when_guard_fails(monkeypatch):
    bad_kz = _RU.replace("92%", "тоқсан екі пайыз")  # улика потеряна

    async def fake_call(**kwargs):
        return {"text": bad_kz}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz", settings=_fake_settings())
    assert out == _RU  # fail-safe: отдаём оригинал


async def test_translate_strips_fence_wrapped_markdown(monkeypatch):
    # Модель завернула весь документ в ```markdown … ``` вопреки инструкции.
    good_kz = _RU.replace("Норма противоречит", "Норма қайшы келеді")
    wrapped = f"```markdown\n{good_kz}\n```"

    async def fake_call(**kwargs):
        return {"text": wrapped}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz", settings=_fake_settings())
    assert out == good_kz  # fence снят, улики на месте → отдан KZ


async def test_translate_falls_back_on_empty_response(monkeypatch):
    async def fake_call(**kwargs):
        return {}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz", settings=_fake_settings())
    assert out == _RU


async def test_translate_uses_anthropic_path_when_configured(monkeypatch):
    # translator_protocol=anthropic + base_url+key → идём в Anthropic-путь,
    # НЕ в OpenAI cached_llm_call. Проверяем и выбор пути, и проброс аргументов.
    good_kz = _RU.replace("Норма противоречит", "Норма қайшы келеді")
    seen: dict = {}

    async def fake_anthropic(**kwargs):
        seen.update(kwargs)
        return {"text": good_kz}

    async def fail_openai(**kwargs):  # не должен вызваться
        raise AssertionError("OpenAI-путь не должен использоваться при anthropic")

    monkeypatch.setattr(s7t, "cached_anthropic_messages_call", fake_anthropic)
    monkeypatch.setattr(s7t, "cached_llm_call", fail_openai)

    out = await s7t.translate_report_md(
        _RU, "kz",
        settings=_fake_settings("anthropic", "https://api.openmodel.ai", "om-key"),
    )
    assert out == good_kz
    assert seen["base_url"] == "https://api.openmodel.ai"
    assert seen["api_key"] == "om-key"
    assert seen["model"] == "translator-model"


async def test_translate_falls_back_to_ru_when_anthropic_raises(monkeypatch):
    # Шлюз упал (напр. 4xx/сеть) → fail-safe на RU.
    async def boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(s7t, "cached_anthropic_messages_call", boom)
    out = await s7t.translate_report_md(
        _RU, "kz",
        settings=_fake_settings("anthropic", "https://api.openmodel.ai", "om-key"),
    )
    assert out == _RU


async def test_translate_noop_for_unsupported_lang():
    assert await s7t.translate_report_md(_RU, "en") == _RU


def test_extract_anthropic_text_drops_thinking():
    from zerde.utils.llm_client import _extract_anthropic_text

    data = {
        "content": [
            {"type": "thinking", "thinking": "рассуждаю про перевод…"},
            {"type": "text", "text": "Аударылған "},
            {"type": "text", "text": "мәтін"},
        ]
    }
    assert _extract_anthropic_text(data) == "Аударылған мәтін"
    assert _extract_anthropic_text({}) == ""
    assert _extract_anthropic_text({"content": "nope"}) == ""
