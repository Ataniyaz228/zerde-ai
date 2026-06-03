"""
CI-гард таблицы кодов планировщика (zerde.prompts.planner._PLANNER_LAW_CODE_HINTS).

Контекст: планировщик S2 подсказывает LLM «обиходное название → law_id». Для
законов, реально присутствующих в корпусе (law_metadata), эта подсказка ОБЯЗАНА
совпадать с каноном реестра. Стейл-код уводит ретривал в мёртвый ID — после чего
adilet-код не находится и грунтинг молча теряется. Классический баг: налоговый
кодекс был указан как 120-VI вместо 214-VII (другое ядро ID, base-ID не лечит).

Проверяем через resolve(code), а НЕ через resolve(name): резолв названия идёт
через difflib-fuzzy (cutoff 0.6) и нестабилен (напр. «Об оценочной деятельности»
ложно матчится на 133-VI). resolve(code) использует только точное/base-ID/alias —
надёжно.
"""

from __future__ import annotations

from zerde.prompts.planner import _PLANNER_LAW_CODE_HINTS
from zerde.utils.law_registry import get_registry

# Законы, точно присутствующие в корпусе. Значения — НЕЗАВИСИМЫЙ якорь (канонические
# law_id из law_metadata), специально не импортируемый из planner.py, чтобы тест ловил
# дрейф самой таблицы. Кодексы с тег-суффиксом указаны в каноне (-UK/-NEW); хинт может
# нести короткое ядро (226-V/414-I) — base-ID матчинг приводит обе формы к одному канону.
_CORE_CORPUS_LAWS = {
    "Налоговый кодекс РК": "214-VII",   # регресс-гард на стейл 120-VI
    "Бюджетный кодекс РК": "171-VIII",  # регресс-гард на стейл 95-IV (утратил силу)
    "КоАП РК": "235-V",
    "УК РК": "226-V-UK",
    "ГК РК (Общая часть)": "1000-XIII",
    "Земельный кодекс РК": "442-II",
    "Трудовой кодекс РК": "414-I-NEW",
}


def test_planner_core_law_hints_match_registry_canon():
    """Хинт планировщика для каждого корпусного закона резолвится в тот же канон, что и эталон."""
    reg = get_registry()
    for name, expected_canon in _CORE_CORPUS_LAWS.items():
        assert name in _PLANNER_LAW_CODE_HINTS, f"'{name}' пропал из таблицы планировщика"
        hint_code = _PLANNER_LAW_CODE_HINTS[name]
        resolved_hint = reg.resolve(hint_code)
        resolved_expected = reg.resolve(expected_canon)
        assert resolved_hint == resolved_expected, (
            f"Планировщик ведёт '{name}' в {hint_code!r} → {resolved_hint!r}, "
            f"а корпусный канон — {resolved_expected!r} (стейл-хинт уводит грунтинг в мёртвый ID)"
        )


def test_planner_tax_code_not_dead_120vi():
    """Прямой регресс-гард на конкретный исторический баг: налоговый ≠ 120-VI и имеет adilet-код."""
    reg = get_registry()
    tax_hint = _PLANNER_LAW_CODE_HINTS["Налоговый кодекс РК"]
    assert tax_hint != "120-VI", "Налоговый кодекс снова указывает на мёртвый 120-VI"
    assert reg.get_adilet_code(tax_hint) is not None, (
        f"Налоговый хинт {tax_hint!r} не имеет adilet-кода → S3 не сможет фетчить норму"
    )
