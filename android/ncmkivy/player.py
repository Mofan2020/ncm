"""Audio playback on Android through ``android.media.MediaPlayer``.

Kivy's own audio stack (SDL2_mixer) cannot stream an HTTP URL, and the
NetEase CDN needs a ``Referer`` header, so the playback goes through
``MediaPlayer`` instead:

* the URL comes from the shared :class:`~src.core.mediaserver.MediaServer`,
  which already proxies the CDN with the right headers and honours ``Range``
  requests -- the same ``http://127.0.0.1:<port>/m/<token>/...`` URLs the
  desktop build hands to its ``<audio>`` element;
* ``MediaPlayer`` runs off the UI thread for the slow parts, so the Kivy
  event loop never blocks.

Everything is optional: on a desktop (or when pyjnius is missing) the player
degrades to "no playback available" instead of raising, so the rest of the app
still runs.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

__all__ = ["Player", "PlayerState"]

log = logging.getLogger("ncm.kivy.player")


class PlayerState:
    """What the player is doing right now."""

    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    LOADING = "loading"
    ERROR = "error"


class Player:
    """A thin wrapper around ``android.media.MediaPlayer``.

    The public surface is deliberately small: ``play``, ``pause``, ``resume``,
    ``stop``, ``seek``, plus the position/duration accessors the UI polls.
    Callbacks are plain callables so a Kivy screen can hook them up without
    knowing anything about Android.
    """

    def __init__(self) -> None:
        self._player: Any = None
        self._state = PlayerState.IDLE
        self._current_url = ""
        self._duration_ms = 0
        self._position_ms = 0
        self._lock = threading.RLock()
        self._available = self._detect_backend()

        #: Called with the new state when it changes.
        self.on_state_changed: Callable[[str], None] | None = None
        #: Called when a track finishes and the next one should start.
        self.on_completed: Callable[[], None] | None = None
        #: Called with a message when playback fails.
        self.on_error: Callable[[str], None] | None = None

    # ------------------------------------------------------------- backend
    @staticmethod
    def _detect_backend() -> bool:
        """Whether ``android.media.MediaPlayer`` can be reached."""
        try:
            from jnius import autoclass  # noqa: F401
        except Exception:
            return False
        try:
            from android.media import MediaPlayer  # noqa: F401
        except Exception:
            return False
        return True

    @property
    def available(self) -> bool:
        return self._available

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_url(self) -> str:
        return self._current_url

    # -------------------------------------------------------------- control
    def play(self, url: str, position_ms: int = 0) -> bool:
        """Start playing ``url``.

        Returns False when no backend is available or the player refused the
        URL; the caller shows the error.
        """
        if not self._available or not url:
            self._set_state(PlayerState.ERROR)
            return False
        with self._lock:
            self._release_locked()
            try:
                from jnius import PythonJavaClass, autoclass, java_method

                MediaPlayer = autoclass("android.media.MediaPlayer")

                class _Listener(PythonJavaClass):
                    __javainterfaces__ = ["android/media/MediaPlayer$OnCompletionListener",
                                          "android/media/MediaPlayer$OnErrorListener"]

                    def __init__(self, owner):
                        super().__init__()
                        self.owner = owner

                    @java_method("(Landroid/media/MediaPlayer;)V")
                    def onCompletion(self, player):
                        self.owner._handle_completion()

                    @java_method("(ILandroid/media/MediaPlayer;)Z")
                    def onError(self, what, extra, player):
                        self.owner._handle_error(f"MediaPlayer error {what}/{extra}")
                        return True

                player = MediaPlayer()
                player.setDataSource(url)
                listener = _Listener(self)
                player.setOnCompletionListener(listener)
                player.setOnErrorListener(listener)
                player.prepareAsync()
                if position_ms > 0:
                    player.seekTo(int(position_ms))
                player.start()
                self._player = player
                self._current_url = url
                self._duration_ms = 0
                self._position_ms = int(position_ms)
                self._set_state(PlayerState.PLAYING)
                return True
            except Exception as exc:
                log.warning("cannot start playback: %s", exc)
                self._set_state(PlayerState.ERROR)
                self._emit_error(str(exc))
                return False

    def pause(self) -> bool:
        with self._lock:
            if self._player is None:
                return False
            try:
                self._player.pause()
                self._set_state(PlayerState.PAUSED)
                return True
            except Exception as exc:
                log.warning("cannot pause: %s", exc)
                return False

    def resume(self) -> bool:
        with self._lock:
            if self._player is None:
                return False
            try:
                self._player.start()
                self._set_state(PlayerState.PLAYING)
                return True
            except Exception as exc:
                log.warning("cannot resume: %s", exc)
                return False

    def stop(self) -> bool:
        with self._lock:
            self._release_locked()
            self._set_state(PlayerState.IDLE)
            return True

    def seek(self, position_ms: int) -> bool:
        with self._lock:
            if self._player is None:
                return False
            try:
                self._player.seekTo(max(0, int(position_ms)))
                self._position_ms = max(0, int(position_ms))
                return True
            except Exception as exc:
                log.warning("cannot seek: %s", exc)
                return False

    # ------------------------------------------------------------- readout
    def position_ms(self) -> int:
        """The current position, in milliseconds."""
        with self._lock:
            if self._player is None:
                return self._position_ms
            try:
                value = int(self._player.getCurrentPosition())
                self._position_ms = value
                return value
            except Exception:
                return self._position_ms

    def duration_ms(self) -> int:
        """The track length, in milliseconds (0 when unknown)."""
        with self._lock:
            if self._player is None:
                return self._duration_ms
            try:
                value = int(self._player.getDuration())
                if value > 0:
                    self._duration_ms = value
                return self._duration_ms
            except Exception:
                return self._duration_ms

    def is_playing(self) -> bool:
        return self._state == PlayerState.PLAYING

    # -------------------------------------------------------------- private
    def _release_locked(self) -> None:
        player = self._player
        self._player = None
        self._current_url = ""
        self._position_ms = 0
        if player is None:
            return
        try:
            player.reset()
            player.release()
        except Exception:  # pragma: no cover - best effort
            pass

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        if self.on_state_changed:
            try:
                self.on_state_changed(state)
            except Exception:  # pragma: no cover - UI callback
                log.debug("state callback failed", exc_info=True)

    def _handle_completion(self) -> None:
        self._set_state(PlayerState.IDLE)
        if self.on_completed:
            try:
                self.on_completed()
            except Exception:  # pragma: no cover - UI callback
                log.debug("completion callback failed", exc_info=True)

    def _handle_error(self, message: str) -> None:
        self._set_state(PlayerState.ERROR)
        self._emit_error(message)

    def _emit_error(self, message: str) -> None:
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:  # pragma: no cover - UI callback
                log.debug("error callback failed", exc_info=True)
