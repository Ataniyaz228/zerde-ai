"""Общие pytest-фикстуры и CI-гарды.

CI-гард BGE: на GitHub Actions модель BAAI/bge-m3 (~2.3 ГБ) не закеширована,
и её загрузка повесила бы прогон. Тесты, которым нужен BGE-M3 (torch/GPU-CPU)
или полный локальный корпус, помечены маркером `heavy` и деселектятся
(`-m "not heavy"`). Но маркер легко забыть, а локально модель в кеше — поэтому
пропуск не виден. Под `ZERDE_NO_BGE=1` мы подменяем загрузчик модели на бросок
исключения: тогда любой НЕ помеченный `heavy` тест, случайно дёрнувший BGE,
падает громко прямо в CI, а не качает 2.3 ГБ. Это детерминированная проверка
полноты разметки маркеров.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Regenerate golden snapshot files (tests/goldens/*.json) instead of comparing.",
    )


@pytest.fixture(autouse=True)
def _ci_block_bge(monkeypatch: pytest.MonkeyPatch) -> None:
    """При ZERDE_NO_BGE=1 запрещает реальную загрузку BGE-M3 (см. модульный docstring)."""
    if os.getenv("ZERDE_NO_BGE") != "1":
        return

    def _blocked(*_args: object, **_kwargs: object):
        raise RuntimeError(
            "BGE-M3 загрузка запрещена при ZERDE_NO_BGE=1 — этот тест трогает "
            "torch/семантику и должен быть помечен @pytest.mark.heavy"
        )

    monkeypatch.setattr("zerde.utils.cache._get_bge_model", _blocked)
