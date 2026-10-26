#!/usr/bin/env bash
# Build the Android APK.
#
# Why a script instead of a bare ``buildozer android debug``: buildozer packs
# ``source.dir`` (this directory) only, while the business core lives in
# ``../src`` at the repository root.  Without staging it the APK ships without
# ``src/`` and dies on the first ``import src.core`` at launch.
#
# The staging step is an rsync into ``android/src``, which is gitignored: a
# symlink would work too, but a real copy is what buildozer's packer is known
# to handle, and it makes the packaged tree inspectable before the build.
#
#   ./build_apk.sh              # debug apk
#   ./build_apk.sh --release    # release aab
#
# Environment (see references/p4a-toolchain-macos.md):
#   ANDROIDSDK  ~/android-toolchain/sdk
#   JAVA_HOME   a JDK 17 (p4a refuses newer)
#   SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE
#               set when the network MITMs TLS
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

TOOLCHAIN="${ANDROID_TOOLCHAIN:-$HOME/android-toolchain}"
export ANDROIDSDK="${ANDROIDSDK:-$TOOLCHAIN/sdk}"
export ANDROID_HOME="$ANDROIDSDK"
export JAVA_HOME="${JAVA_HOME:-$TOOLCHAIN/jdk17/zulu-17.jdk/Contents/Home}"
export PATH="$JAVA_HOME/bin:$PATH"
export PYTHONFORANDROID_PREREQUISITES_INTERACTIVE=0

# The local accelerator re-signs TLS; every Python p4a shells out to needs the
# combined bundle or the recipe downloads fail with CERTIFICATE_VERIFY_FAILED.
if [ -f "$HOME/.hermes/certs/combined-ca.pem" ]; then
    export SSL_CERT_FILE="$HOME/.hermes/certs/combined-ca.pem"
    export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
    export CURL_CA_BUNDLE="$SSL_CERT_FILE"
fi

BUILDOZER="${BUILDOZER:-$TOOLCHAIN/bv/bin/buildozer}"
if [ ! -x "$BUILDOZER" ]; then
    echo "buildozer not found at $BUILDOZER" >&2
    exit 1
fi

echo "== staging the shared core into android/src =="
rm -rf "$HERE/src"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
      "$ROOT/src/" "$HERE/src/"

# Fail loudly here rather than in a crash log on someone's phone.
echo "== checking the staged tree can import the core =="
if [ ! -f "$HERE/src/core/api.py" ]; then
    echo "staged tree is missing src/core/api.py" >&2
    exit 1
fi
if [ ! -f "$HERE/src/version.py" ]; then
    echo "staged tree is missing src/version.py" >&2
    exit 1
fi
echo "   $(find "$HERE/src" -name '*.py' | wc -l | tr -d ' ') python files staged"

MODE="${1:-debug}"
case "$MODE" in
    --release|release)
        TARGET="android release"
        ;;
    *)
        TARGET="android debug"
        ;;
esac

echo "== buildozer $TARGET =="
cd "$HERE"
"$BUILDOZER" $TARGET

echo "== artifacts =="
ls -la "$HERE"/bin/*.apk "$HERE"/bin/*.aab 2>/dev/null || echo "  (nothing in bin/)"
