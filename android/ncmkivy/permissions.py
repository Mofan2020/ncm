"""Ask for the permission the local music library needs.

The manifest declares the audio permissions, but on Android 6 and later a
declared permission is not a granted one: without the runtime grant,
MediaStore answers with the files this app created itself and nothing else,
so the local music page looks empty on a phone full of music.

Android 13 renamed the permission -- ``READ_EXTERNAL_STORAGE`` stopped
covering audio and ``READ_MEDIA_AUDIO`` took over -- so which one to ask for
depends on the running version.

On anything that is not Android every call here is a no-op, which keeps the
desktop test run honest.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger("ncm.kivy.permissions")

#: Returned by ``request_audio_permission`` when nothing had to be asked.
ALREADY_GRANTED = "granted"
DENIED = "denied"
UNAVAILABLE = "unavailable"

__all__ = ["audio_permission", "check_audio_permission", "request_audio_permission",
           "ALREADY_GRANTED", "DENIED", "UNAVAILABLE"]


def _sdk_int() -> int:
    from jnius import autoclass

    return int(autoclass("android.os.Build$VERSION").SDK_INT)


def audio_permission() -> str:
    """The permission that gates the media library on this Android version."""
    if _sdk_int() >= 33:
        return "android.permission.READ_MEDIA_AUDIO"
    return "android.permission.READ_EXTERNAL_STORAGE"


def check_audio_permission() -> bool:
    """Whether the media library can be read right now."""
    try:
        from android.permissions import check_permission

        return bool(check_permission(audio_permission()))
    except Exception as exc:  # not Android, or no p4a bridge
        log.debug("no permission bridge: %s", exc)
        return False


def request_audio_permission(callback: Callable[[str], None] | None = None) -> str:
    """Ask for the audio permission.

    ``callback`` is called with ``ALREADY_GRANTED`` or ``DENIED`` once the
    answer is in -- from the Android UI thread, so whatever it touches has to
    be scheduled onto Kivy's thread.
    """
    try:
        from android.permissions import request_permissions

        if check_audio_permission():
            if callback is not None:
                callback(ALREADY_GRANTED)
            return ALREADY_GRANTED

        def _answered(permissions, grants) -> None:
            granted = any(bool(grant) for grant in (grants or []))
            log.info("audio permission %s", "granted" if granted else "denied")
            if callback is not None:
                callback(ALREADY_GRANTED if granted else DENIED)

        request_permissions([audio_permission()], _answered)
        return ALREADY_GRANTED
    except Exception as exc:
        log.info("no permission bridge: %s", exc)
        if callback is not None:
            callback(UNAVAILABLE)
        return UNAVAILABLE
