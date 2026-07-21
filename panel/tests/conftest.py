# -*- coding: utf-8 -*-
"""conftest.py — общие фикстуры тестов панели. Кладёт panel/ и корень движка на
sys.path (тесты запускаются как `pytest tests/` из panel/, без установки пакета)."""
import sys
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent.parent
ENGINE_ROOT = PANEL_DIR.parent

for p in (PANEL_DIR, ENGINE_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
