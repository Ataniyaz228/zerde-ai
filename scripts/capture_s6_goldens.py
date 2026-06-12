#!/usr/bin/env python
"""
Regenerate S6 audit golden snapshots (tests/goldens/s6_*.json).

Thin wrapper around `pytest tests/test_s6_goldens.py --update-goldens`.
Use this after a deliberate, reviewed change to audit_analysis()'s output —
NOT as a way to make a failing golden test pass without understanding why
the output changed.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/capture_s6_goldens.py
"""

from __future__ import annotations

import sys

import pytest

if __name__ == "__main__":
    sys.exit(
        pytest.main(["-q", "tests/test_s6_goldens.py", "--update-goldens", *sys.argv[1:]])
    )
