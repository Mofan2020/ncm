"""Locate bundled data files in both source and frozen (PyInstaller) layouts.

``Path(__file__)`` is useless inside a frozen app: the modules live in the PYZ
archive while the HTML/CSS/JSON assets are unpacked next to the executable, so
every resource lookup goes through :func:`resource_path`.
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["bundle_root", "resource_path", "is_frozen"]


def is_frozen() -> bool:
    """Whether we are running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that holds the application's data files."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Return the absolute path of a bundled resource."""
    return bundle_root().joinpath(*parts)
