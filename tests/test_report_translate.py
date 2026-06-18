"""S7-translate — guard сохранности улик + fail-safe фолбэк на RU.

Без сети: cached_llm_call/make_llm_client мокаются. Проверяем главный инвариант —
перевод, потерявший улику (номер статьи / ID закона / % / дату / URL / статус-тег),
ОТБРАСЫВАЕТСЯ и возвращается оригинал RU.
"""

from __future__ import annotations

import zerde.stages.s7_translate as s7t

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
        return {"md": good_kz}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz")
    assert out == good_kz


async def test_translate_falls_back_to_ru_when_guard_fails(monkeypatch):
    bad_kz = _RU.replace("92%", "тоқсан екі пайыз")  # улика потеряна

    async def fake_call(**kwargs):
        return {"md": bad_kz}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz")
    assert out == _RU  # fail-safe: отдаём оригинал


async def test_translate_falls_back_on_empty_response(monkeypatch):
    async def fake_call(**kwargs):
        return {}

    monkeypatch.setattr(s7t, "make_llm_client", lambda settings=None: object())
    monkeypatch.setattr(s7t, "cached_llm_call", fake_call)

    out = await s7t.translate_report_md(_RU, "kz")
    assert out == _RU


async def test_translate_noop_for_unsupported_lang():
    assert await s7t.translate_report_md(_RU, "en") == _RU
