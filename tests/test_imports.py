"""Every module must be importable *first* -- no import cycles.

This is not pedantry: ``src.core.api`` imports ``src.auth``, so the moment a
module under ``src/auth`` imports something from ``src.core`` at module level,
importing that module first explodes and the app cannot start (while the rest of
the suite, which imports ``src.core`` first, stays green).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

MODULES = [
    "main",
    "src.auth.login",
    "src.core.api",
    "src.core.crypto",
    "src.core.downloader",
    "src.core.lyrics",
    "src.core.aes",
    "src.gui.bridge",
    "src.gui.theme",
    "src.config.settings",
    "src.i18n",
    "src.resources",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"`import {module}` failed on a clean interpreter:\n"
        f"{result.stderr.decode(errors='replace')[-1500:]}"
    )
