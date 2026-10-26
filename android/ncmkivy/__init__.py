"""The Kivy layer of the Android build.

``controller`` is the boundary to the shared business core, ``theme`` holds
the Material 3 tokens and the size buckets, ``widgets`` the reusable
controls, ``screens`` the pages, and ``player`` the Android audio backend.
"""
from __future__ import annotations

__all__ = ["app", "controller", "player", "screens", "theme", "uix", "widgets"]
