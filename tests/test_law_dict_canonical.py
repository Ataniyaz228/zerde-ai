"""Guard: кураторские keyword→law_id словари обязаны указывать на законы,
которые реестр умеет локализовать.

Корень бага «9% надёжности» — стейл-значение в захардкоженном словаре
(_TITLE_HINT_MAP["налоговый кодекс"]=120-VI вместо 214-VII): ретривал уходил в
пустоту. Эти словари — кураторские keyword-индексы (значения прогоняются через
реестр), но ничто не мешало значению устареть. Тест превращает «стейл-значение
молча ломает grounding» в «падение CI»: каждое значение обязано резолвиться в
law_id-форму, и (кроме явно ещё-не-инджестированных законов) реестр обязан уметь
найти его adilet-код. Единый источник истины — registry + law_metadata.
"""

import re

from zerde.utils.law_registry import get_registry, _STATIC_FALLBACK
from zerde.stages.s2_5_claim_extractor import _TITLE_HINT_MAP
from zerde.stages.s6_auditor import _COMMON_LAW_NAME_MAP

# Реальные законы РК, которых пока нет в корпусе (law_metadata). Допустимы как
# значения для кросс-отсылок (claim к ним не заземлится — это не стейл, а пробел
# покрытия). Любое НОВОЕ нелокализуемое значение — провал теста (опечатка/старый код).
_KNOWN_NOT_INGESTED = {"230-VIII", "255-VIII"}

_LAW_ID_RE = re.compile(r"^\d+-[IVXLCDM]+(?:-[A-Z]+)?$")


def _iter_curated_values():
    for src, mapping in (
        ("s2_5._TITLE_HINT_MAP", _TITLE_HINT_MAP),
        ("law_registry._STATIC_FALLBACK", _STATIC_FALLBACK),
    ):
        for key, val in mapping.items():
            yield src, key, val
    for key, vals in _COMMON_LAW_NAME_MAP.items():
        for val in vals:
            yield "s6._COMMON_LAW_NAME_MAP", key, val


def test_curated_law_maps_have_no_stale_or_junk_values():
    reg = get_registry()
    bad = []
    for src, key, val in _iter_curated_values():
        resolved = reg.resolve(val)
        if not _LAW_ID_RE.match(resolved):
            bad.append((src, key, val, resolved, "не law_id-форма (мусор/название/«370_»)"))
            continue
        if reg.get_adilet_code(val) is None and resolved not in _KNOWN_NOT_INGESTED:
            bad.append((src, key, val, resolved, "реестр не локализует (стейл law_id?)"))
    assert not bad, (
        "Стейл/мусорные значения в кураторских словарях law_id:\n"
        + "\n".join(
            f"  [{s}] {k!r} -> {v!r} (resolve={r}) : {why}"
            for s, k, v, r, why in bad
        )
    )


def test_title_hint_map_is_strictly_locatable():
    # Самый критичный словарь — детектор закона-мишени поправочного билля. Стейл
    # здесь = ретривал в пустоту = ложно-низкая надёжность (баг «9%»). Без исключений.
    reg = get_registry()
    for key, val in _TITLE_HINT_MAP.items():
        assert reg.get_adilet_code(val) is not None, (
            f"_TITLE_HINT_MAP[{key!r}]={val!r} не локализуется реестром "
            f"(стейл law_id? сверь с law_metadata через verify_law_registry.py)"
        )
