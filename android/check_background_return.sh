#!/usr/bin/env bash
# Check what happens when the app goes to the background and comes back.
#
# The symptom this guards against: the app comes back to a blank window.
# A blank window and a window with nothing in it look the same in a
# screenshot, but they are not: the first one never drew a frame, and
# the file size tells them apart.  A screen with the app on it is a few
# hundred kilobytes of PNG, a blank one is about 25 KB.
#
# Usage:
#   ./check_background_return.sh [apk]
#
# With no argument the debug APK in ./bin is used.  The script installs
# it, starts the app, takes it to the background, brings it back and
# prints the sizes of the two screenshots.
#
# It needs adb (the platform-tools from the Android SDK) and a running
# emulator or a device.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
APK="${1:-$HERE/bin/neteasemusic-3.5.17-arm64-v8a-debug.apk}"
PACKAGE="com.skyc8266.neteasemusic"
ACTIVITY="$PACKAGE/org.kivy.android.PythonActivity"
SHOTS="$(mktemp -d)"

if [ ! -f "$APK" ]; then
    echo "APK not found: $APK" >&2
    exit 1
fi

adb() {
    if [ -n "${ANDROID_PLATFORM_TOOLS:-}" ]; then
        "$ANDROID_PLATFORM_TOOLS/adb" "$@"
    else
        command adb "$@"
    fi
}

shoot() {
    local name="$1"
    adb shell screencap -p "/sdcard/$name.png" >/dev/null 2>&1
    adb pull "/sdcard/$name.png" "$SHOTS/$name.png" >/dev/null 2>&1
    stat -f%z "$SHOTS/$name.png" 2>/dev/null || echo 0
}

if ! adb get-state >/dev/null 2>&1; then
    echo "no device or emulator" >&2
    exit 1
fi

echo "installing $APK"
adb install -r "$APK" >/dev/null 2>&1 || {
    echo "install failed" >&2
    exit 1
}

adb shell am force-stop "$PACKAGE" >/dev/null 2>&1
adb logcat -c >/dev/null 2>&1

echo "starting the app"
adb shell am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \
    -n "$ACTIVITY" >/dev/null 2>&1
sleep 22

first=$(shoot first)
echo "first start:  ${first} bytes"

echo "going to the background"
adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1
sleep 7

echo "coming back"
adb shell am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \
    -n "$ACTIVITY" >/dev/null 2>&1
sleep 14

second=$(shoot second)
pid=$(adb shell pidof "$PACKAGE" 2>/dev/null | tr -d '\r')
echo "after return:  ${second} bytes   (pid ${pid:-none})"

echo
echo "screenshots in $SHOTS"

echo
echo "what the log says"
adb logcat -d -v time 2>/dev/null | grep -iE \
    "onDestroy\(\)|onStart\(\)|onCreate running|webview resumed|going away|called on thread" \
    | grep -v Launcher | tail -12

# A blank screen is about 25 KB, a drawn one is much bigger.  80 KB is
# the line: below it nothing was drawn.
if [ "$second" -lt 80000 ]; then
    echo
    echo "FAIL: the app came back blank (${second} bytes)"
    exit 1
fi

echo
echo "OK: the app came back with a drawn screen (${second} bytes)"
