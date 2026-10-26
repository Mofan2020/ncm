"""The login QR code is drawn without an imaging library.

Pillow used to render it; now :mod:`src.core.png` writes the PNG straight
from the QR module matrix.  These tests read that PNG back apart, so they
check the actual pixels rather than "some bytes came out".
"""
from __future__ import annotations

import logging
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
URL = "https://music.163.com/login?codekey=0123456789abcdef"


def _decode_png(png: bytes) -> tuple[int, int, bytes]:
    """Return ``(width, height, greyscale pixels)`` of an 8-bit grey PNG."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "missing the PNG signature"
    offset = 8
    width = height = 0
    compressed = bytearray()
    while offset < len(png):
        length = int.from_bytes(png[offset:offset + 4], "big")
        tag = png[offset + 4:offset + 8]
        payload = png[offset + 8:offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            depth, colour, _, _, interlace = payload[8:13]
            assert (depth, colour, interlace) == (8, 0, 0), "expected 8-bit greyscale"
        elif tag == b"IDAT":
            compressed += payload
        elif tag == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = width + 1                      # one filter byte per scanline
    assert len(raw) == height * stride
    pixels = b"".join(raw[row * stride + 1:(row + 1) * stride] for row in range(height))
    return width, height, pixels


def _manager():
    """A LoginManager with no session, no cookie and no network."""
    from src.auth.login import LoginManager

    manager = object.__new__(LoginManager)
    manager.logger = logging.getLogger("test.qrcode")
    return manager


def _matrix(url: str) -> list[list[bool]]:
    import qrcode

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    code.add_data(url)
    code.make(fit=True)
    return code.get_matrix()


def test_the_login_qr_is_drawn_from_the_module_matrix():
    png = _manager().get_qrcode_image(URL)
    assert png, "no image was produced"
    width, height, pixels = _decode_png(png)
    assert width == height

    matrix = _matrix(URL)
    assert width % len(matrix) == 0, "modules must be an even number of pixels"
    scale = width // len(matrix)

    # Every module is a solid square of the same size, and dark modules are
    # dark.  This is what makes the code scanable.
    for row, modules in enumerate(matrix):
        for column, dark in enumerate(modules):
            x = column * scale
            y = row * scale
            value = pixels[y * width + x]
            assert value == (0 if dark else 255), (row, column, value)
            assert set(pixels[y * width + x:y * width + x + scale]) == {value}


def test_the_image_is_about_the_requested_size():
    png = _manager().get_qrcode_image(URL, size=256)
    width, _height, _pixels = _decode_png(png)
    assert 200 <= width <= 320, width


def test_a_broken_url_does_not_raise():
    """An empty answer from the server reaches this function too."""
    image = _manager().get_qrcode_image("")
    assert image is None or image.startswith(b"\x89PNG")


def test_the_qr_renders_with_pillow_blocked():
    """The whole point: no imaging library is needed at runtime."""
    script = r"""
import logging
import sys


class _BlockPillow:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "PIL":
            raise ImportError("PIL must not be imported")
        return None


sys.meta_path.insert(0, _BlockPillow())

from src.auth.login import LoginManager

manager = object.__new__(LoginManager)
manager.logger = logging.getLogger("test")
image = manager.get_qrcode_image("https://music.163.com/login?codekey=x")
assert image and image.startswith(b"\x89PNG"), image
assert "PIL" not in sys.modules
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_encoder_rejects_an_empty_matrix():
    from src.core.png import render_matrix

    with pytest.raises(ValueError):
        render_matrix([])


def test_the_qr_code_decodes_back_to_the_url(tmp_path):
    """It has to be a real QR code, not merely a plausible pattern.

    macOS decodes it with CoreImage -- the same detector the camera-style
    QR scanners use -- and must get the URL back.
    """
    try:
        import Quartz
        from Foundation import NSURL
    except ImportError:
        pytest.skip("CoreImage is only reachable on macOS")

    path = tmp_path / "qr.png"
    path.write_bytes(_manager().get_qrcode_image(URL))
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    image = Quartz.CIImage.imageWithCGImage_(
        Quartz.CGImageSourceCreateImageAtIndex(source, 0, None))
    detector = Quartz.CIDetector.detectorOfType_context_options_(
        Quartz.CIDetectorTypeQRCode, None, None)
    decoded = [feature.messageString() for feature in detector.featuresInImage_(image) or []]
    assert decoded == [URL]
