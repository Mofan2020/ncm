"""Single source of truth for application identity and version."""

__version__ = "3.6.0"

#: Version of the disclaimer text shown on the first start.  Bump it when the
#: text changes so the dialog is shown again after an update.
DISCLAIMER_VERSION = "1"

#: Product name used for the window title, artifact names and the config dir.
APP_NAME = "SkycMedia"

#: The name the project had before it was renamed.  The config directory
#: still carries it on every machine that used the old name, so the app
#: keeps reading and writing there instead of asking the user to log in
#: again.  See ``src.config.settings`` for how the fallback works.
LEGACY_APP_NAME = "NeteaseMusicDownloader"

#: Human readable name shown in the UI.
APP_DISPLAY_NAME = "SkycMedia"
APP_DISPLAY_NAME_EN = "SkycMedia"

#: Reverse-DNS id for the macOS bundle (never claim netease.com's namespace).
BUNDLE_ID = "com.skyc8266.skycmedia"

#: Project URLs.
HOMEPAGE = "https://github.com/Mofan2020/ncm"
AUTHOR = "Skyc8266"
LICENSE = "MIT"
COPYRIGHT = f"Copyright (c) 2026 {AUTHOR}. MIT licensed."
