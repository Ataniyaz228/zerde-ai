"""S7 render builds clickable adilet links from the registry's document code,
not the corpus's synthesized short-law_id URL (which 404s on adilet)."""
from __future__ import annotations

from types import SimpleNamespace

import zerde.stages.s7_render as s7


class _StubRegistry:
    def __init__(self, mapping: dict[str, str]):
        self._m = mapping

    def get_adilet_url(self, law_id: str, lang: str = "rus") -> str:
        code = self._m.get(law_id)
        return f"https://adilet.zan.kz/{lang}/docs/{code}" if code else ""


def _patch_registry(monkeypatch, mapping):
    monkeypatch.setattr(s7, "get_registry", lambda: _StubRegistry(mapping))


def test_adilet_chunk_uses_document_code(monkeypatch):
    _patch_registry(monkeypatch, {"171-VIII": "K2500000171"})
    chunk = SimpleNamespace(
        law_id="171-VIII",
        article=136,
        source_url="https://adilet.zan.kz/rus/docs/171-VIII#136",  # broken, 404s
    )
    assert s7._source_link_url(chunk) == "https://adilet.zan.kz/rus/docs/K2500000171#136"


def test_web_chunk_keeps_original_url(monkeypatch):
    _patch_registry(monkeypatch, {})
    chunk = SimpleNamespace(law_id=None, article=None, source_url="https://primeminister.kz/ru/news/1")
    assert s7._source_link_url(chunk) == "https://primeminister.kz/ru/news/1"


def test_unknown_law_falls_back_to_source_url(monkeypatch):
    _patch_registry(monkeypatch, {})  # registry has no code → ""
    chunk = SimpleNamespace(law_id="999-ZZ", article=5, source_url="https://example.test/x")
    assert s7._source_link_url(chunk) == "https://example.test/x"
