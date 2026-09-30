# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build description.

Windows -> ``dist/NeteaseMusicDownloader.exe`` (single file)
macOS   -> ``dist/NeteaseMusicDownloader.app`` (bundled, for both arm64 and x64)

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

block_cipher = None

# --------------------------------------------------------------- data files
datas = [
    ("src/gui/assets/index.html", "src/gui/assets"),
    ("src/gui/assets/style.css", "src/gui/assets"),
    ("src/gui/assets/app.js", "src/gui/assets"),
    ("src/i18n/zh_cn.json", "src/i18n"),
    ("src/i18n/en_us.json", "src/i18n"),
]
for optional in ("LICENSE", "README.md"):
    if (_SPEC_DIR / optional).exists():
        datas.append((optional, "."))

# ------------------------------------------------------------ hidden imports
hiddenimports = [
    "webview",
    "qrcode",
    "qrcode.image.pil",
    "qrcode.image.base",
    "PIL",
    "PIL.Image",
    "yaml",
    "cryptography",
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
