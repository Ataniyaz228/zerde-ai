"""
Tokenization helpers shared across S6 (audit BM25) and the cache's BM25
index (S5 retrieval).

IMPORTANT: tokenize_simple and tokenize_morph are DIFFERENT tokenizers with
different thresholds calibrated to their respective callers. Do NOT unify
their behavior — S6's thresholds (validation_threshold, bm25_*_threshold)
were tuned against tokenize_simple's output; cache.py's BM25 retrieval was
tuned against tokenize_morph's (pymorphy3-stemmed) output.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# tokenize_simple — moved from zerde/stages/s6_auditor.py (_tokenize, _STOP_WORDS)
# ---------------------------------------------------------------------------

# Стоп-слова для BM25 токенизации (русский + казахский + юридические)
_STOP_WORDS = frozenset([
    # --- Русские служебные слова ---
    "и", "в", "на", "с", "по", "от", "до", "за", "при", "о", "об", "из",
    "или", "а", "но", "что", "как", "это", "не", "к", "для", "то",
    "же", "бы", "ли", "со", "да", "уж", "ведь", "вот", "ну", "ж",
    # --- Английские служебные слова ---
    "the", "a", "an", "of", "in", "is", "are", "was", "were",
    # --- Казахские служебные слова (союзы, послелоги, частицы) ---
    "және", "жəне",  # и/также (оба варианта Unicode)
    "немесе",        # или
    "бойынша",       # по/согласно
    "туралы",        # о/про
    "бірақ",         # но
    "алайда",        # однако
    "сондай-ақ",     # а также
    "сонымен",       # итак/таким образом
    "сонымен қатар", # кроме того
    "яғни",          # то есть
    "мен",           # и/с (послелог)
    "үшін",          # для
    "арқылы",        # через/посредством
    "дейін",         # до
    "кейін",         # после
    "бұл",           # это/этот
    "сол",           # тот
    "осы",           # этот/данный
    "ол",            # он/она/оно
    "оның",          # его/её
    "оған",          # ему
    "олар",          # они
    "олардың",       # их
    "bar",           # есть/имеется
    "жоқ",           # нет
    "болып",         # являясь
    "болады",        # является
    "болса",         # если является
    "болған",        # бывший/являвшийся
    "керек",         # нужно/должен
    "тиіс",         # должен/обязан
    "деп",           # что/как (цитирование)
    "деген",         # называемый/сказанный
    "мынадай",       # следующий
    "қарай",         # к/в сторону
    "қарсы",         # против
    "ретінде",       # в качестве
    "тиісінше",     # соответственно
    "жағдайда",     # в случае
    "сәйкес",       # согласно/в соответствии
    "белгіленген",  # установленный
    "көзделген",    # предусмотренный
    "айқындалған",  # определённый
    "тәртіппен",    # в порядке
    "негізінде",    # на основании
    "орай",          # в связи с
    # --- Казахский юридический канцелярит ---
    "республикасы",  # республика (притяжательная форма)
    "қазақстан",     # казахстан
    "қолданысқа",   # в действие (о вступлении закона)
    "енгізіледі",   # вводится
    "жарияланған",  # опубликованный
    # --- Русский юридический канцелярит (казахстанский контекст) ---
    "настоящий", "вводится", "действие", "истечении", "официального",
    "опубликования", "республика", "казахстан",
    "внести", "изменения", "дополнения", "некоторые", "законодательные",
    "акты", "вопросам",
])


def tokenize_simple(text: str) -> list[str]:
    """
    Токенизация для S6 BM25: lowercase + удаление пунктуации + стоп-слова.
    No stemming. Moved verbatim from zerde.stages.s6_auditor._tokenize.
    """
    text = text.lower()
    # Удаляем пунктуацию (кроме дефиса в словах)
    text = re.sub(r"[^\w\s\-]", " ", text)
    tokens = text.split()
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


# ---------------------------------------------------------------------------
# tokenize_morph — moved from zerde/utils/cache.py CacheManager._tokenize_for_bm25
# (+ _stem_word / _STEM_CACHE / pymorphy3 machinery)
# ---------------------------------------------------------------------------

_STEM_CACHE: dict[str, str] = {}
_morph = None  # lazy pymorphy3.MorphAnalyzer singleton

_LEGAL_STOP_WORDS = {
    # Русские
    "закон", "кодекс", "статья", "статье", "статьи", "республики", "казахстан",
    "утратил", "силу", "вводится", "действие", "постановление", "правительства",
    "республика", "закона", "кодекса", "об", "о", "и", "в", "на", "для", "рк",
    # Казахские
    "заң", "кодексі", "бап", "баптың", "туралы", "және", "қазақстан",
    "республикасы", "заңы", "үкіметі", "заңда", "үшін", "жәнінде",
    "болады", "емес", "және", "немесе", "қорған", "рәсімі",
}

_RU_ENDINGS = [
    "ями", "ами", "ому", "ему", "ого", "его", "ыми", "ими", "ых", "их", "ею", "ою",
    "ом", "ем", "ой", "ей", "ию", "ую", "яя", "ая", "ое", "ее", "ые", "ие", "ия", "ый", "ий", "ам", "ям", "ов", "ев", "ях", "ах",
    "а", "я", "о", "е", "и", "ы", "у", "ю", "ь",
    # Казахские агглютинативные окончания
    "ға", "ге", "ғе", "да", "де", "на", "ні", "ның", "нің",
    "ды", "ді", "ты", "ті", "лар", "лер", "дар", "дер",
    "мен", "сен", "оны", "оні", "біз", "сіз",
    "ған", "ген", "ке", "қа", "ші", "шы",
]

_RU_LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"


def _stem_word(w: str) -> str:
    global _morph
    if w in _STEM_CACHE:
        return _STEM_CACHE[w]

    # Lazy load pymorphy3 analyzer
    if _morph is None:
        try:
            import pymorphy3
            _morph = pymorphy3.MorphAnalyzer()
        except ImportError:
            pass

    res = w
    # Check if contains Russian letters
    if _morph is not None and any(c in _RU_LETTERS for c in w):
        parsed = _morph.parse(w)
        if parsed:
            res = str(parsed[0].normal_form)
    else:
        if len(w) <= 4:
            res = w
        else:
            for end in _RU_ENDINGS:
                if w.endswith(end):
                    if len(w) - len(end) >= 3:
                        res = w[: -len(end)]
                        break
    _STEM_CACHE[w] = res
    return res


def tokenize_morph(text: str) -> list[str]:
    """
    Токенизация для cache.py BM25 (S5 retrieval): lowercase + пунктуация +
    юридические стоп-слова + pymorphy3-стемминг (с fallback на ручные
    окончания). Moved verbatim from
    zerde.utils.cache.CacheManager._tokenize_for_bm25.
    """
    text_lower = text.lower()
    # Удаляем пунктуацию (кроме дефиса в словах)
    text_clean = re.sub(r"[^\w\s\-]", " ", text_lower)
    words = [w.strip() for w in text_clean.split() if len(w.strip()) > 2]
    filtered = [_stem_word(w) for w in words if w not in _LEGAL_STOP_WORDS]
    return filtered if filtered else [_stem_word(w) for w in words]
