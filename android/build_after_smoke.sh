#!/usr/bin/env bash
# Wait for the p4a toolchain smoke build to produce an APK, then build ours.
#
# The smoke build is what compiles hostpython3 / python3 / sdl2 / kivy into
# the package cache.  Once it lands, every recipe is cached and the real
# build is a repack.  Chaining them here means the second build starts on its
# own instead of waiting for someone to notice the first one finished.
set -uo pipefail

TEST_APK_DIR=/tmp/p4a_test/bin
REAL_LOG=/tmp/ncm_apk_build.log
DEADLINE=$(( $(date +%s) + 7200 ))   # two hours, then give up

echo "== waiting for the toolchain smoke APK =="
while :; do
    if compgen -G "$TEST_APK_DIR/*.apk" > /dev/null; then
        echo "   smoke APK found: $(ls "$TEST_APK_DIR"/*.apk | head -1)"
        break
    fi
    if [ "$(date +%s)" -gt "$DEADLINE" ]; then
        echo "   gave up waiting after two hours" >&2
        exit 1
    fi
    # Bail out early if the smoke build already died.
    if ! pgrep -f "p4a apk" > /dev/null; then
        if compgen -G "$TEST_APK_DIR/*.apk" > /dev/null; then
            break
        fi
        echo "   smoke build is gone and produced no APK -- check /tmp/p4a_build.log" >&2
        exit 1
    fi
    sleep 30
done

echo "== building the real ncm APK =="
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
{
    ./build_apk.sh 2>&1
} | tee "$REAL_LOG"
status=${PIPESTATUS[0]}
echo "== build_apk.sh exited $status =="
ls -la bin/*.apk 2>/dev/null || echo "   no APK in bin/"
exit "$status"
