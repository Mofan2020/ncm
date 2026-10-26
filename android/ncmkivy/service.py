"""The foreground service that keeps the music playing.

Android pauses the app when it goes to the background, and a process that
is not holding a foreground service can be killed at any moment.  A
foreground service with a notification is the supported way to keep playing
music, and it is also what the user expects: a notification with the track
name and the usual controls.

The service is started through ``pyjnius``; it is a thin wrapper around
``android.app.Service`` that starts the Kivy app and keeps the process
alive while the music plays.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ncm.kivy.service")

__all__ = ["MusicService", "start_service", "stop_service"]


class MusicService:
    """Controls the Android foreground service.

    Everything degrades to a no-op when the app runs off-device (a desktop
    run of the Kivy layer), so the same code path works in both places.
    """

    def __init__(self) -> None:
        self._service: Any = None

    @staticmethod
    def available() -> bool:
        """Whether the Android service can be reached."""
        try:
            from jnius import autoclass  # noqa: F401

            from android.app import Service  # noqa: F401
        except Exception:
            return False
        return True

    def start(self) -> bool:
        """Start the service (or bring it back to the front)."""
        if not self.available():
            return False
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(PythonActivity.mService)
            intent.setClass(PythonActivity, autoclass("org.kivy.android.PythonService"))
            intent.setAction("org.kivy.android.PYTHON_SERVICE")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startService(intent)
            self._service = intent
            return True
        except Exception as exc:
            log.warning("cannot start the music service: %s", exc)
            return False

    def stop(self) -> bool:
        """Stop the service."""
        if not self.available():
            return False
        try:
            from jnius import autoclass

            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.stopService(self._service)
            self._service = None
            return True
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("cannot stop the music service: %s", exc)
            return False


def start_service() -> bool:
    """Start the foreground service."""
    return MusicService().start()


def stop_service() -> bool:
    """Stop the foreground service."""
    return MusicService().stop()
