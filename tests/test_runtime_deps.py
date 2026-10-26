"""Nothing may pull an imaging library into the runtime package.

Pillow arrived through the ``qrcode[pil]`` extra and cost about 16 MB of the
bundle (Pillow itself plus the OpenSSL it links against).  The QR code is now
drawn by :mod:`src.core.png` and the wallpaper resized by the platform's own
tool (:mod:`src.core.imaging`), so these three guards keep it that way.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: An import of Pillow, or of qrcode's Pillow-backed image factory.
_PILLOW_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(?:PIL\b|qrcode\.image\.pil\b|qrcode\.image\.styledpil\b)",
    re.MULTILINE,
)


def _runtime_sources() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + [ROOT / "main.py"]


def test_no_module_imports_pillow():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _runtime_sources()
        if _PILLOW_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"these modules still import Pillow: {offenders}"


def test_the_requirements_do_not_pull_pillow():
    # Comments are stripped first: the file explains *why* the extra is not
    # used, so the literal name legitimately appears in a comment.
    lines = [
        line.split("#", 1)[0].strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ]
    requirements = "\n".join(line for line in lines if line)
    assert "[pil]" not in requirements, "the qrcode[pil] extra drags Pillow in"
    assert "pillow" not in requirements.lower()


def test_the_spec_does_not_bundle_pillow():
    spec = (ROOT / "main.spec").read_text(encoding="utf-8")
    hidden = spec.split("hiddenimports = [")[1].split("]")[0]
    assert "PIL" not in hidden
    assert "qrcode.image.pil" not in hidden
    excluded = spec.split("excludes=[")[1].split("],")[0]
    # qrcode imports its Pillow drawers in a try/except, so PyInstaller would
    # collect them unless they are excluded by name.
    assert '"PIL"' in excluded
    assert '"qrcode.image.styles.moduledrawers.pil"' in excluded
