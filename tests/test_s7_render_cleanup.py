"""S7 render cleanups: strip leaked internal source labels (S1/S5/S6/S12…) from
prose, and treat only adilet.zan.kz as authoritative (web → informational)."""
from __future__ import annotations

from types import SimpleNamespace

import zerde.stages.s7_render as s7
from zerde.models import LegalRank
from zerde.stages.s7_render import _clean_label_leak, _is_authoritative


def test_clean_label_leak_strips_internal_labels():
    assert _clean_label_leak("Конституция (S5, S6) использует формулировку") == \
        "Конституция использует формулировку"
    assert _clean_label_leak("нормы (S4, S5, S6, S12), устанавливающие") == \
        "нормы, устанавливающие"
    assert "S5" not in _clean_label_leak("(ст. 71, источник S5: 'x')")
    assert "редакции S5" not in _clean_label_leak("Конституции (в редакции S5, S6), далее")


def test_clean_label_leak_preserves_article_refs():
    # Не трогаем «ст.»/«п.» — это часть нормы, а не служебный ярлык.
    assert _clean_label_leak("(ст. 70 п. 3)") == "(ст. 70 п. 3)"
    assert _clean_label_leak("Статья 71 Конституции РК") == "Статья 71 Конституции РК"


def _chunk(rank, url):
    return SimpleNamespace(legal_rank=rank, source_url=url)


def test_is_authoritative_only_adilet():
    # Реальная норма с adilet → авторитетна.
    assert _is_authoritative(_chunk(LegalRank.LAW_RK, "https://adilet.zan.kz/rus/docs/K2500000171"))
    # Новость/агрегатор с реальным рангом → НЕ авторитетна (уйдёт в инфо-раздел).
    assert not _is_authoritative(_chunk(LegalRank.LAW_RK, "https://1tv.kz/news/36709"))
    assert not _is_authoritative(_chunk(LegalRank.LAW_RK, "https://kodeksy-kz.com/x.htm"))
    # MEDIA_UNKNOWN → НЕ авторитетна в любом случае.
    assert not _is_authoritative(_chunk(LegalRank.MEDIA_UNKNOWN, "https://adilet.zan.kz/x"))


class _TitleRegistry:
    def __init__(self, titles):
        self._t = titles

    def get_title(self, law_id, lang="ru"):
        return self._t.get(law_id, law_id)  # fallback = law_id (no real title)


def test_source_display_title_uses_registry_name(monkeypatch):
    monkeypatch.setattr(s7, "get_registry", lambda: _TitleRegistry({"K2600000000_": "Конституция Республики Казахстан"}))
    # НПА с известным названием → человеческое имя + статья (не «НПА {код}»).
    ch = SimpleNamespace(law_id="K2600000000_", article=71,
                         source_title="НПА K2600000000_ | Ст. 71", law_title=None)
    assert s7._source_display_title(ch) == "Конституция Республики Казахстан | Ст. 71"


def test_source_display_title_keeps_web_title(monkeypatch):
    monkeypatch.setattr(s7, "get_registry", lambda: _TitleRegistry({}))
    # Веб-источник (нет в реестре) → исходный заголовок страницы.
    web = SimpleNamespace(law_id=None, article=None, source_title="Новость — 1tv.kz", law_title=None)
    assert s7._source_display_title(web) == "Новость — 1tv.kz"
    # law_id есть, но названия в реестре нет → тоже не ломаем (fallback на source_title).
    unk = SimpleNamespace(law_id="999-ZZ", article=5, source_title="НПА 999-ZZ | Ст. 5", law_title=None)
    assert s7._source_display_title(unk) == "НПА 999-ZZ | Ст. 5"
