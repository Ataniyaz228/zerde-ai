"""Shared pytest configuration."""

from __future__ import annotations


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Regenerate golden snapshot files (tests/goldens/*.json) instead of comparing.",
    )
