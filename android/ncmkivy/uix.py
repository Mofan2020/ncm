"""Dialogs and the QR-code view.

The QR code is drawn from the module matrix that the ``qrcode`` library
produces, not from a PNG: rendering a PNG needs Pillow, which this build
does not ship.  The matrix is a list of rows of booleans, so drawing it is
a matter of putting a rectangle on every dark module.
"""
from __future__ import annotations

import logging
from typing import Any

from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput

from ncmkivy.widgets import ThemedLabel

__all__ = ["QrCodeView", "PhoneLoginDialog"]


def copy_to_clipboard(text: str) -> bool:
    """Put the text on the system clipboard.

    The app has no window of its own on Android, so the clipboard service of
    the running activity is the only way.  Off Android there is no bridge and
    this reports failure instead of raising.
    """
    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        ClipData = autoclass("android.content.ClipData")
        activity = PythonActivity.mActivity
        manager = activity.getSystemService("clipboard")
        manager.setPrimaryClip(ClipData.newPlainText("ncm", text))
        return True
    except Exception as exc:  # not Android, or no activity yet
        log.info("clipboard write failed: %s", exc)
        return False


log = logging.getLogger("ncm.kivy.uix")


class QrCodeView(BoxLayout):
    """Draws a QR code from its module matrix."""

    def __init__(self, url: str = "", size: int = 260, **kwargs: Any) -> None:
        super().__init__(orientation="vertical", padding=dp(12),
                         spacing=dp(8), **kwargs)
        self.size_hint = (None, None)
        self.width = dp(size + 24)
        self.height = dp(size + 24)

        self.canvas_widget = BoxLayout(size_hint=(1, 1))
        self.add_widget(self.canvas_widget)

        self.label = ThemedLabel(text="请用网易云音乐 App 扫码", role="body_small",
                                 color_name="on_surface_variant", halign="center")
        self.add_widget(self.label)

        self._size = size
        self.url = url
        self._draw(url)

    def _draw(self, url: str) -> None:
        """Render the QR code, or a placeholder when it cannot be made."""
        matrix = _qr_matrix(url)
        self._matrix = matrix
        self.canvas_widget.bind(pos=self._redraw, size=self._redraw)
        self._redraw()

    def _redraw(self, *_args: Any) -> None:
        """Draw the matrix at the widget's current size."""
        matrix = getattr(self, "_matrix", None)
        self.canvas_widget.canvas.clear()
        width = self.canvas_widget.width
        height = self.canvas_widget.height
        if not matrix or width <= 0 or height <= 0:
            return
        count = len(matrix)
        cell = min(width, height) / count
        offset_x = self.canvas_widget.x + (width - cell * count) / 2
        offset_y = self.canvas_widget.y + (height - cell * count) / 2
        with self.canvas_widget.canvas:
            Color(0, 0, 0, 1)
            for row_index, row in enumerate(matrix):
                for col_index, filled in enumerate(row):
                    if not filled:
                        continue
                    Rectangle(pos=(offset_x + col_index * cell,
                                   offset_y + (count - row_index - 1) * cell),
                              size=(cell, cell))


def _qr_matrix(url: str) -> list[list[bool]]:
    """The QR module matrix for ``url``.

    ``qrcode`` can produce the matrix without any image backend, so this
    works without Pillow.
    """
    if not url:
        return []
    try:
        import qrcode

        qr = qrcode.QRCode(version=None,
                           error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=1, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        return [list(row) for row in qr.get_matrix()]
    except Exception:
        return []


def _qr_cell(col: int, row: int, count: int) -> Rectangle:
    """A placeholder rectangle; the real one is drawn by :meth:`_redraw`."""
    return Rectangle(pos=(0, 0), size=(0, 0))


class PhoneLoginDialog(Popup):
    """Sign in with a phone number and an SMS code."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        self.app = app
        content = BoxLayout(orientation="vertical", padding=dp(16),
                            spacing=dp(12))
        super().__init__(title="手机号登录", content=content,
                         size_hint=(None, None), size=(dp(340), dp(320)),
                         **kwargs)

        self.phone = TextInput(hint_text="手机号", multiline=False,
                               size_hint_y=None, height=dp(48))
        self.code = TextInput(hint_text="验证码", multiline=False,
                              size_hint_y=None, height=dp(48))
        content.add_widget(self.phone)
        content.add_widget(self.code)

        self.status = ThemedLabel(text="", role="body_small",
                                  color_name="on_surface_variant")
        content.add_widget(self.status)

        from ncmkivy.widgets import _TextButton

        content.add_widget(_TextButton(text="发送验证码", on_press=self._send_code))
        content.add_widget(_TextButton(text="登录", on_press=self._login))

    def _send_code(self) -> None:
        phone = self.phone.text.strip()
        if not phone:
            self.status.text = "请输入手机号"
            return
        result = self.app.controller.login.send_phone_code(phone)
        self.status.text = str(result.get("message") or "已发送")

    def _login(self) -> None:
        phone = self.phone.text.strip()
        code = self.code.text.strip()
        if not phone or not code:
            self.status.text = "请输入手机号和验证码"
            return
        result = self.app.controller.login_phone(phone, code)
        if result.get("success"):
            self.dismiss()
        else:
            self.status.text = str(result.get("message") or "登录失败")
