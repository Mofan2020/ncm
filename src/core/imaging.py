"""Resize the wallpaper with the platform's own image tool.

Pillow used to arrive through ``qrcode[pil]`` and took about 16 MB of the
38 MB bundle with it -- Pillow itself plus the OpenSSL it links against.  It
was only ever used to downscale the wallpaper, and both supported platforms
already ship a tool that does exactly that:

* macOS   -- ``/usr/bin/sips``, which resamples and writes JPEG
* Windows -- Windows PowerShell driving ``System.Drawing``

Using those keeps the "no native dependency in the bundle" rule that
:mod:`src.core.aes` protects for crypto: nothing here is packaged, nothing
here links into the app.  When no tool is available -- Linux, or a Windows
machine with Windows PowerShell removed -- :func:`downscale_to_jpeg` returns
``False`` and the callers keep their old behaviour: store the original file
untouched.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__all__ = ["has_backend", "target_size", "downscale_to_jpeg"]

log = logging.getLogger("ncm.imaging")

#: Resizing a 4K wallpaper takes well under a second.  The timeout only guards
#: against a wedged helper process, it is not a performance budget.
TIMEOUT = 120

#: PowerShell has no inline-script form that survives Windows' quoting rules
#: for arbitrary paths, so the script is written to a temp file and run with
#: ``-File``.  It must stay pure ASCII: Windows PowerShell reads a BOM-less
#: file in the ANSI codepage.
_POWERSHELL_SCRIPT = r"""
param([string]$Source, [string]$Target, [int]$MaxEdge, [int]$Quality)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$image = [System.Drawing.Image]::FromFile($Source)
try {
    $scale = 1.0
    $longest = [Math]::Max($image.Width, $image.Height)
    if ($longest -gt $MaxEdge) { $scale = $MaxEdge / [double]$longest }
    $width = [int][Math]::Max(1, [Math]::Round($image.Width * $scale))
    $height = [int][Math]::Max(1, [Math]::Round($image.Height * $scale))

    $bitmap = New-Object System.Drawing.Bitmap -ArgumentList $width, $height
    try {
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.Clear([System.Drawing.Color]::White)
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.DrawImage($image, 0, 0, $width, $height)
        } finally { $graphics.Dispose() }

        $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
            Where-Object { $_.MimeType -eq 'image/jpeg' } | Select-Object -First 1
        if (-not $codec) { throw 'no JPEG encoder' }
        $parameters = New-Object System.Drawing.Imaging.EncoderParameters -ArgumentList 1
        $parameters.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter `
            -ArgumentList ([System.Drawing.Imaging.Encoder]::Quality), ([long]$Quality)
        $bitmap.Save($Target, $codec, $parameters)
    } finally { $bitmap.Dispose() }
} finally { $image.Dispose() }
"""


def target_size(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """Scale ``width``x``height`` down to fit ``max_edge``, never up."""
    longest = max(width, height)
    if max_edge <= 0 or longest <= max_edge:
        return width, height
    scale = max_edge / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def _sips() -> str | None:
    if sys.platform != "darwin":
        return None
    return shutil.which("sips")


def _powershell() -> str | None:
    """Windows PowerShell (5.1), not PowerShell 7: only 5.1 ships .NET
    Framework with ``System.Drawing``."""
    if sys.platform != "win32":
        return None
    found = shutil.which("powershell")
    if found:
        return found
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    fallback = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(fallback) if fallback.is_file() else None


def has_backend() -> bool:
    """True when this machine has the image tool the platform route uses."""
    return _sips() is not None or _powershell() is not None


def _sips_size(sips: str, source: Path) -> tuple[int, int] | None:
    """Ask sips for the pixel size; ``None`` when it cannot read the file."""
    try:
        result = subprocess.run(
            [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(source)],
            capture_output=True, text=True, timeout=TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("sips cannot read %s: %s", source, exc)
        return None
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("pixelWidth", "pixelHeight"):
            try:
                values[key] = int(value.strip())
            except ValueError:
                return None
    if len(values) != 2:
        return None
    return values["pixelWidth"], values["pixelHeight"]


def _scale_with_sips(source: Path, target: Path, max_edge: int, quality: int) -> bool:
    sips = _sips()
    if sips is None:
        return False
    size = _sips_size(sips, source)
    if size is None:
        return False
    # sips -Z would happily enlarge a small image, so the target size is
    # computed here and passed as an exact ``-z`` resample.
    width, height = target_size(*size, max_edge)
    command = [
        sips,
        "-z", str(height), str(width),
        "-s", "format", "jpeg",
        "-s", "formatOptions", str(quality),
        str(source),
        "--out", str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("sips cannot scale %s: %s", source, exc)
        return False
    if result.returncode != 0:
        log.info("sips failed on %s: %s", source, (result.stderr or "").strip())
        return False
    return True


def _scale_with_powershell(source: Path, target: Path, max_edge: int, quality: int) -> bool:
    shell = _powershell()
    if shell is None:
        return False
    folder = Path(tempfile.mkdtemp(prefix="ncm-image-"))
    script = folder / "scale.ps1"
    try:
        script.write_text(_POWERSHELL_SCRIPT, encoding="utf-8")
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script),
             "-Source", str(source), "-Target", str(target),
             "-MaxEdge", str(max_edge), "-Quality", str(quality)],
            capture_output=True, text=True, timeout=TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("cannot run the image helper on %s: %s", source, exc)
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if result.returncode != 0:
        log.info("the image helper failed on %s: %s", source,
                 (result.stderr or result.stdout or "").strip())
        return False
    return True


def downscale_to_jpeg(
    source: Path, target: Path, *, max_edge: int, quality: int
) -> bool:
    """Write ``source`` into ``target`` as a JPEG of at most ``max_edge`` px.

    Returns ``False`` when this machine has no usable image tool or the tool
    failed; the callers then keep the original file, exactly as they did when
    Pillow was not installed.
    """
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    if sys.platform == "darwin":
        done = _scale_with_sips(source, target, max_edge, quality)
    elif sys.platform == "win32":
        done = _scale_with_powershell(source, target, max_edge, quality)
    else:
        done = False

    if not done:
        # A half-written target would be served as if it were the wallpaper.
        try:
            target.unlink()
        except OSError:
            pass
        return False
    try:
        if target.stat().st_size <= 0:
            target.unlink()
            return False
    except OSError:
        return False
    return True
