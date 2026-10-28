[app]

# The Android bundle id follows the project rule: com.skyc8266.<project>.
package.domain = com.skyc8266
package.name = neteasemusic

title = NetEase Music
source.dir = .
source.include_exts = py,png,jpg,json,ttf,html,css,js
source.include_patterns = ncmandroid/*.py,src/*.py,src/**/*.py,web/*.html,web/*.js,web/*.css,webview/*.py,webview/**/*.py,proxytools/*.py,typing_extensions.py,yaml/*.py
source.exclude_dirs = tests,build,dist,.venv,__pycache__,tools,legacy

version = 3.5.10

# Only the pure-Python dependencies: no Pillow, no native wheels.  Every
# image job (QR code, wallpaper) is done without one.
requirements = python3,kivy,requests,qrcode

orientation = all
# The phone interface is a web page: it is served over the loopback server.
# No SDL window, so the bootstrap keeps SDL2 but the app draws nothing with it.
fullscreen = 0

# The icon is generated from the desktop assets by scripts/make_icon.py.
icon.filename = %(source.dir)s/assets/icon.png
presplash.filename = %(source.dir)s/assets/icon.png

android.api = 34
android.minapi = 24
android.ndk_api = 24

# One architecture: every 64-bit device is covered, and the APK stays small.
android.archs = arm64-v8a

# Release builds are bundles; debug builds are plain APKs.
android.release_artifact = apk
android.debug_artifact = apk

# The loopback media server and the NetEase API both need the network.
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,READ_MEDIA_AUDIO,FOREGROUND_SERVICE,WAKE_LOCK

# A foreground service keeps the music playing when the app is not visible.
android.service_names = ncmkivy.service:MusicService

# The app asks for no permission at runtime; the media permission is checked
# before the local library is read.
android.wakelock = True

# The NetEase CDN and the API are both HTTPS, but the loopback media server
# is plain HTTP on 127.0.0.1, so cleartext has to stay allowed for it.
android.allow_backup = True

android.accept_sdk_license = True
android.sdk_path = /Users/mofan/android-toolchain/sdk
android.ndk_path = /Users/mofan/android-toolchain/sdk/ndk/26.1.10909125
p4a.source_dir = /Users/mofan/android-toolchain/p4a-src

[buildozer]

log_level = 2
warn_on_root = 0
