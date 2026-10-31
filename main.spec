# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build description.

Windows -> ``dist/SkycMedia.exe`` (single file)
macOS   -> ``dist/SkycMedia.app`` (bundled, for both arm64 and x64)

Build with::

    pyinstaller --clean --noconfirm main.spec

Only the GUI backend of the target platform is bundled (no cefpython3, no Qt),
so the builds stay small and cannot fall back to a backend the user lacks.
"""
import sys
from pathlib import Path

# ---------------------------------------------------------------- version info
# Imported by exec (not import) so the spec works no matter how PyInstaller
# happens to set sys.path.
_SPEC_DIR = Path(SPECPATH)  # noqa: F821 - provided by PyInstaller
_version_ns: dict = {}
exec((_SPEC_DIR / "src" / "version.py").read_text(encoding="utf-8"), _version_ns)
APP_NAME = _version_ns["APP_NAME"]
APP_DISPLAY_NAME = _version_ns["APP_DISPLAY_NAME"]
VERSION = _version_ns["__version__"]
BUNDLE_ID = _version_ns["BUNDLE_ID"]
COPYRIGHT = _version_ns["COPYRIGHT"]

# The archive name must match src/resources.py, which unpacks it at startup.
# Parse just the constant instead of exec'ing the module: resources.py imports
# shutil, which touches _winapi on Windows and would make the spec depend on
# platform-specific stdlib internals.
import re as _re

RESOURCES_ARCHIVE = _re.search(
    r'^RESOURCES_ARCHIVE\s*=\s*"([^"]+)"',
    (_SPEC_DIR / "src" / "resources.py").read_text(encoding="utf-8"),
    _re.MULTILINE,
).group(1)

block_cipher = None

# --------------------------------------------------------------- data files
# Assets are listed as whole directories rather than file by file: listing each
# file is a drift generator, and desktop-lyrics.html / .js were silently missing
# from the bundle for exactly that reason.
datas = [
    ("src/gui/assets", "src/gui/assets"),
    ("src/i18n", "src/i18n"),
]

if sys.platform == "win32":
    # Windows: pack the assets into one zip that is unpacked next to the .exe at
    # startup (src/resources.ensure_resources).  WebView2 renders its "file may
    # have been deleted or moved" page when the onefile temp directory is gone or
    # quarantined by a security product, so the files must not be served from
    # _MEIPASS there.  macOS is untouched: it keeps a normal onedir .app.
    import shutil
    import zipfile

    _staging = _SPEC_DIR / "build" / "ncm-resources"
    shutil.rmtree(_staging, ignore_errors=True)

    def _pack(entry: str) -> None:
        """Mirror ``entry`` into the staging dir, skipping build droppings."""
        for _f in sorted((_SPEC_DIR / entry).rglob("*")):
            if not _f.is_file() or "__pycache__" in _f.parts:
                continue
            _target = _staging / _f.relative_to(_SPEC_DIR)
            _target.parent.mkdir(parents=True, exist_ok=True)
            _target.write_bytes(_f.read_bytes())

    for _entry in ("src/gui/assets", "src/i18n"):
        _pack(_entry)

    _archive = _SPEC_DIR / RESOURCES_ARCHIVE
    with zipfile.ZipFile(_archive, "w", zipfile.ZIP_DEFLATED) as _zf:
        for _f in sorted(_staging.rglob("*")):
            if _f.is_file():
                _zf.write(_f, _f.relative_to(_staging).as_posix())
    datas = [(str(_archive), ".")]
    shutil.rmtree(_staging, ignore_errors=True)
else:
    for optional in ("LICENSE", "README.md"):
        if (_SPEC_DIR / optional).exists():
            datas.append((optional, "."))

# ------------------------------------------------------------ hidden imports
hiddenimports = [
    "webview",
    "qrcode",
    "qrcode.image.base",
    "yaml",
]
if sys.platform == "win32":
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium"]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "webview.platforms.qt"]

a = Analysis(
    ["main.py"],
    pathex=[str(_SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # never ship the other GUI backends
        "cefpython3",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi",
        "tkinter",
        # Pillow is not a runtime dependency: the QR code is drawn by
        # src/core/png.py and the wallpaper is resized by the platform's own
        # tool (src/core/imaging.py).  qrcode still tries to import its Pillow
        # drawers -- in a try/except ImportError -- so the package has to be
        # excluded explicitly instead of merely being absent from the
        # environment.  Pillow is also what dragged libcrypto/libssl in.
        "PIL", "PIL.Image",
        "qrcode.image.pil", "qrcode.image.styledpil",
        "qrcode.image.styles.colormasks",
        "qrcode.image.styles.moduledrawers.pil",
        # pywebview only imports cryptography inside its optional HTTPS-server
        # path (webview.start(ssl=True)), which this app never uses.  Leaving it
        # in dragged libssl/libcrypto into the bundle, and on the Intel macOS
        # runner the stale copy shadowed the one the extension needed:
        # "Symbol not found: _SSL_get0_group_name".  Our own AES-128-ECB is pure
        # Python (src/core/aes.py), so nothing here needs OpenSSL.
        "cryptography",
        "pytest", "ruff", "setuptools", "pip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=["vcruntime140.dll", "python3*.dll"],
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(_SPEC_DIR / "assets" / "icon.ico"),
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX breaks Mach-O binaries / code signatures
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,   # pywebview drives its own NSApplication
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )

    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(_SPEC_DIR / "assets" / "icon.icns"),
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleName": APP_DISPLAY_NAME,
            "CFBundleDisplayName": APP_DISPLAY_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHumanReadableCopyright": COPYRIGHT,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSBackgroundOnly": False,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.music",
            "NSPrincipalClass": "NSApplication",
        },
    )
