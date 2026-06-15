"""S7 render cleanups: strip leaked internal source labels (S1/S5/S6/S12…) from
prose, and treat only adilet.zan.kz as authoritative (web → informational)."""
from __future__ import annotations

from types import SimpleNamespace

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
