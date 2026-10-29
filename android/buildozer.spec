[app]

# The Android bundle id follows the project rule: com.skyc8266.<project>.
package.domain = com.skyc8266
package.name = neteasemusic

title = NetEase Music
source.dir = .
source.include_exts = py,png,jpg,json,ttf,html,css,js
source.include_patterns = ncmandroid/*.py,src/*.py,src/**/*.py,web/*.html,web/*.js,web/*.css,webview/*.py,webview/**/*.py,proxy_tools.py,bottle.py,typing_extensions.py,yaml/*.py
source.exclude_dirs = tests,build,dist,.venv,__pycache__,tools,legacy

version = 3.5.11

# Only the pure-Python dependencies: no Pillow, no native wheels.  Every
# image job (QR code, wallpaper) is done without one.
#
# android and pyjnius are what webview.platforms.android imports: the
# bridge between the Python code and the Java WebView is jnius, and
# android.activity/android.runnable are the pieces it leans on.
requirements = python3,kivy,requests,qrcode,android,pyjnius

orientation = all
# The phone interface is a web page: it is served over the loopback server.
# No SDL window, so the bootstrap keeps SDL2 but the app draws nothing with it.
fullscreen = 0

# The icon is generated from the desktop assets by scripts/make_icon.py.
icon.filename = %(source.dir)s/assets/icon.png

# The launch screen is its own image, not the icon: p4a shows the presplash
# in a full-screen ImageView with ScaleType.FIT_CENTER, so the square icon
# would be scaled up to the screen's width and fill it.  A tall image is
# scaled down instead, so the logo stays a logo.
presplash.filename = %(source.dir)s/assets/presplash.png
# The colour behind the image, for the moment before it is drawn and for
# the bars on screens whose aspect ratio does not match.
android.presplash_color = #EC4141

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

# pywebview's Android backend needs the Java classes under com/pywebview/
# (PyWebViewClient, PyWebChromeClient, PyJavascriptInterface and the
# callback wrappers).  They live in a jar in the upstream repository and
# are not part of the PyPI package, so without this the app dies on start
# with a ClassNotFoundError the moment the bridge is created.
android.add_jars = libs/pywebview-android.jar

# The app asks for no permission at runtime; the media permission is checked
# before the local library is read.
android.wakelock = True

android.allow_backup = True

# The interface is served over the loopback media server as plain HTTP on
# 127.0.0.1, and Android has blocked cleartext traffic by default since API
# 28.  The exemption for loopback addresses only exists from Android 17
# (API 37) on, so the address has to be allowed explicitly -- and only that
# address: the API and the CDN stay HTTPS.
android.add_resources = res/xml/network_security_config.xml:xml/network_security_config.xml
android.extra_manifest_application_arguments = manifest_application.xml

android.accept_sdk_license = True
android.sdk_path = /Users/mofan/android-toolchain/sdk
android.ndk_path = /Users/mofan/android-toolchain/sdk/ndk/26.1.10909125
p4a.source_dir = /Users/mofan/android-toolchain/p4a-src

[buildozer]

log_level = 2
warn_on_root = 0
