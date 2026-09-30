# -*- mode: python ; coding: utf-8 -*-
import sys

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/gui/assets/index.html', 'src/gui/assets'),
        ('src/gui/assets/style.css', 'src/gui/assets'),
        ('src/gui/assets/app.js', 'src/gui/assets'),
        ('src/i18n/zh_cn.json', 'src/i18n'),
        ('src/i18n/en_us.json', 'src/i18n'),
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.winforms',
        'webview.platforms.cef',
        'webview.platforms.cocoa',
        'qrcode',
        'qrcode.image.pil',
        'yaml',
        'cryptography',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == 'win32':
    # Windows: single .exe file
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='NeteaseMusicDownloader',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    # macOS: .app bundle
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='NeteaseMusicDownloader',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
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
        upx=True,
        upx_exclude=[],
        name='NeteaseMusicDownloader',
    )

    app = BUNDLE(
        coll,
        name='NeteaseMusicDownloader.app',
        icon=None,
        bundle_identifier='com.netease.music.downloader',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSBackgroundOnly': False,
            'CFBundleShortVersionString': '2.0.0',
            'CFBundleVersion': '2.0.0',
            'NSRequiresAquaSystemAppearance': False,
        },
    )