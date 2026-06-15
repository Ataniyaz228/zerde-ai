"""
Регрессия для S5.2-верификатора: ложные CONTRADICTED у поправочных биллей.

Контекст: законопроект «О внесении изменений» формулирует изменения как
«заменить A на B» / «изменить порядок перечисления на …». Аудитор-LLM раньше
помечал такие claim'ы CONTRADICTED, потому что действующая редакция содержит A,
а не B — хотя это и есть смысл поправки, а не противоречие. Детерминированный
guard понижает заведомо-ложные случаи: фраза та же с точностью до падежа/порядка
слов или document — лексическое подмножество нормы (при том же наборе чисел и
одинаковой полярности отрицания).
"""

import pytest

from zerde.models import AnalysisJSON, ClaimVerdict, VerdictStatus
from zerde.stages.s5_2_verifier import (
    _same_phrase_modulo_inflection as same,
)
from zerde.stages.s5_2_verifier import (
    verify_contradictions,
)


# --- что ОБЯЗАНО считаться мнимым противоречием (понижаем) -----------------
@pytest.mark.parametrize("a,b", [
    # переупорядочивание перечня (claim_0018, claim_0017 из налогового билля)
    ("столица и город республиканского значения",
     "городов республиканского значения и столицы"),
    ("столица, область, город республиканского значения",
     "акиматов областей, городов республиканского значения и столицы"),
    # падежные варианты одного термина
    ("во всенародном референдуме", "всенародного референдума"),
    ("Председателя Курултая", "председатель курултай"),
])
def test_inflection_or_reorder_is_not_contradiction(a, b):
    assert same(a, b) is True


# --- что НЕЛЬЗЯ глушить: реальные расхождения -------------------------------
@pytest.mark.parametrize("a,b", [
    ("республиканский референдум", "всенародный референдум"),   # разные термины
    ("штраф до 2000 МРП", "штраф до 5000 МРП"),                 # разные числа
    ("норма не применяется к резидентам", "норма применяется к резидентам"),  # отрицание
    ("курение и алкоголь запрещены", "курение запрещено"),      # document ⊄ норма
])
def test_real_difference_stays_contradiction(a, b):
    assert same(a, b) is False


async def test_verifier_demotes_reorder_contradiction():
    v = ClaimVerdict(
        claim_id="c_reorder",
        status=VerdictStatus.CONTRADICTED,
        source_ids=["chunkA"],
        document_value="столица, область, город республиканского значения",
        found_value="акиматов областей, городов республиканского значения и столицы",
        is_deterministic=False,
    )
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     facts=[], conclusions=[], verdicts=[v])
    out = await verify_contradictions(a, [])
    assert out.verdicts[0].status == VerdictStatus.UNVERIFIED


async def test_verifier_demotes_absence_based_amendment_contradiction():
    # Поправка «заменить A», но в показанном фрагменте статьи A не найдено / иной абзац.
    # Чанки на уровне статьи → недоказуемо → UNVERIFIED (а не CONTRADICTED).
    v = ClaimVerdict(
        claim_id="c_absence",
        status=VerdictStatus.CONTRADICTED,
        source_ids=["chunkA"],
        document_value="Документ предписывает заменить «Парламента» на «Курултая» в абзаце пятом ст.670",
        found_value="абзац пятый гласит: «иностранцам, направляющимся с гуманитарной помощью…»",
        contradiction_detail="в указанных абзацах отсутствует изменяемый текст; поправка неверно идентифицирует местоположение",
        is_deterministic=False,
    )
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     facts=[], conclusions=[], verdicts=[v])
    out = await verify_contradictions(a, [])
    assert out.verdicts[0].status == VerdictStatus.UNVERIFIED


async def test_verifier_demotes_replace_of_existing_text():
    # «заменить «тенге» на «теңге»», а в норме сейчас «тенге» → дозаменная форма,
    # это не противоречие.
    v = ClaimVerdict(
        claim_id="c_tenge",
        status=VerdictStatus.CONTRADICTED,
        source_ids=["chunkA"],
        document_value="Проект предписывает заменить слово «тенге» на «теңге» по всему тексту",
        found_value="в тексте Налогового кодекса используется слово тенге (например, 200 000 тг.)",
        contradiction_detail="в корпусе используется «тенге», что противоречит замене на «теңге»",
        is_deterministic=False,
    )
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     facts=[], conclusions=[], verdicts=[v])
    out = await verify_contradictions(a, [])
    assert out.verdicts[0].status == VerdictStatus.UNVERIFIED


async def test_verifier_keeps_genuine_misquote_numbers():
    # Поправка ссылается на «2000 тенге», а в норме 3000 — A в норме НЕ найден →
    # это возможный misquote, guard 0.7 НЕ должен глушить (демоция тут только от
    # отсутствия валидного источника, и без пометки про «дозаменную форму»).
    from zerde.stages.s5_2_verifier import _amendment_targets_existing_text as tgt
    assert tgt("заменить «2000 тенге» на «5000 тенге»", "статья устанавливает 3000 тенге") is False


async def test_verifier_keeps_real_contradiction_unrelated_to_guard():
    # Разные числа — guard не трогает; при отсутствии валидных источников
    # срабатывает базовое правило (нет источника → UNVERIFIED), но НЕ из-за guard.
    v = ClaimVerdict(
        claim_id="c_num",
        status=VerdictStatus.CONTRADICTED,
        source_ids=[],
        document_value="штраф до 2000 МРП",
        found_value="штраф до 5000 МРП",
        is_deterministic=False,
    )
    a = AnalysisJSON(analysis_id="t", source_doc_id="d", plan_id="p",
                     facts=[], conclusions=[], verdicts=[v])
    out = await verify_contradictions(a, [])
    # guard не должен был «съесть» это как мнимое — понижение тут от Правила 1
    assert out.verdicts[0].status == VerdictStatus.UNVERIFIED
    assert "грамматическое" not in (out.verdicts[0].contradiction_detail or "")
