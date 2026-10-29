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
# p4a looks for the ``cython`` executable on PATH, not the Python module, so
# the build venv's bin has to be on PATH -- otherwise it stops with
# "Cython (cython) not found, please install it".
export PATH="$(dirname "$BUILDOZER"):$PATH"
# buildozer installs p4a's dependencies with ``pip install --user`` unless it
# can see a virtualenv or conda environment.  A venv's own python says
# "User site-packages are not visible in this virtualenv" and the install
# fails, so the environment has to be declared.
export VIRTUAL_ENV="$(dirname "$BUILDOZER")"
# A conda environment in the caller's shell is just as confusing: buildozer
# then tries to install p4a's dependencies with ``conda install``, which
# fails on this machine.  The venv above is the only environment this
# build may use.
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_EXE CONDA_PYTHON_EXE

# PyPI is unreachable from this network (a request to pypi.org/simple times
# out), and the host-python build pip-installs a few pure-Python helpers into
# it.  A mirror is reachable, so point pip at one.  This is scoped to this
# script's environment -- no global pip config is touched.
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
export PIP_DISABLE_PIP_VERSION_CHECK=1
# Note: the build deliberately keeps pip's build isolation ON.  The one
# recipe that cannot survive it (the ``android`` package, whose pyproject
# declares only setuptools while its setup.py imports Cython) is handled in
# p4a's PyProjectRecipe instead, which passes --no-isolation for that
# recipe alone -- turning isolation off globally makes kivy fail with
# unmet dependencies, because kivy pins exact cython/setuptools versions.

echo "== staging the shared core into android/src =="
rm -rf "$HERE/src"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
      "$ROOT/src/" "$HERE/src/"

# The launcher icon: the desktop app already has a 1024px master, so copy it
# instead of regenerating it (the generator needs Pillow, which is a
# development dependency only).
mkdir -p "$HERE/assets"
if [ ! -f "$HERE/assets/icon.png" ]; then
    if [ -f "$ROOT/assets/icon.png" ]; then
        cp "$ROOT/assets/icon.png" "$HERE/assets/icon.png"
        echo "   icon.png copied from assets/"
    else
        echo "no source icon at assets/icon.png" >&2
        exit 1
    fi
fi

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

# pywebview's Android backend talks to the Java WebView through the classes
# under com/pywebview/.  They ship as a jar in the upstream repository and
# are not in the PyPI package, so the build has to carry it explicitly.  A
# missing jar produces an APK that dies on launch with a ClassNotFoundError,
# which is exactly the kind of failure nobody can debug from a phone.
echo "== checking the pywebview Java classes are present =="
JAR="$HERE/libs/pywebview-android.jar"
if [ ! -f "$JAR" ]; then
    echo "missing $JAR: pywebview's Android backend will not find its Java classes" >&2
    echo "  fetch it from the upstream repository:" >&2
    echo "    webview/lib/pywebview-android.jar" >&2
    exit 1
fi
# The listing is taken once and grepped from the file: a pipeline into
# ``grep -q`` breaks under ``set -o pipefail``, because grep exits as soon as
# it matches, unzip gets a SIGPIPE and the pipeline then reports failure.
LISTING="$(unzip -l "$JAR")"
for CLASS in PyWebViewClient PyWebChromeClient PyJavascriptInterface \
             EventCallbackWrapper JsApiCallbackWrapper WebViewRequestInterceptor; do
    if ! grep -q "com/pywebview/$CLASS.class" <<< "$LISTING"; then
        echo "$JAR does not contain com/pywebview/$CLASS.class" >&2
        exit 1
    fi
done
echo "   $(grep -c 'com/pywebview/.*\.class' <<< "$LISTING") java classes"

# The launch screen: p4a stretches the presplash over the whole screen, so a
# square icon becomes a full-screen logo.  A tall image is what keeps the
# logo small.
if [ ! -f "$HERE/assets/presplash.png" ]; then
    echo "missing $HERE/assets/presplash.png" >&2
    echo "  generate it with: .venv/bin/python scripts/make_presplash.py" >&2
    exit 1
fi
echo "   launch screen: $(file -b "$HERE/assets/presplash.png")"

MODE="${1:-debug}"
case "$MODE" in
    --release|release)
        TARGET="android release"
        ;;
    *)
        TARGET="android debug"
        ;;
esac

# A release build is signed with the key described in android/keystore.properties.
# That file is not in the repository -- the passwords must not be committed --
# so a release build without it stops here instead of producing an unsigned
# package.  p4a takes the signing material from the environment.
if [ "$TARGET" = "android release" ]; then
    PROPS="$HERE/keystore.properties"
    if [ ! -f "$PROPS" ]; then
        echo "no $PROPS: cannot sign a release build" >&2
        exit 1
    fi
    _prop() { sed -n "s/^$1=//p" "$PROPS"; }
    export P4A_RELEASE_KEYSTORE="$(_prop store.file)"
    export P4A_RELEASE_KEYSTORE_PASSWD="$(_prop store.password)"
    export P4A_RELEASE_KEYALIAS="$(_prop key.alias)"
    export P4A_RELEASE_KEYALIAS_PASSWD="$(_prop key.password)"
    if [ ! -f "$P4A_RELEASE_KEYSTORE" ]; then
        echo "the keystore $P4A_RELEASE_KEYSTORE does not exist" >&2
        exit 1
    fi
    echo "== signing with $(basename "$P4A_RELEASE_KEYSTORE") (alias $P4A_RELEASE_KEYALIAS) =="
fi

# The downloads go through a local accelerator that drops connections now
# and then ("Remote end closed connection without response").  The packages
# that did finish are cached, so a retry picks up where it stopped.
#
# The version comes from src/version.py, not from the spec: the spec's copy
# drifts (the APK was still called 3.5.1 while the app reported 3.5.4, and
# the manifest's versionName is what the system shows).  buildozer takes the
# app version from the spec and has no command line option for it --
# "buildozer --version" prints buildozer's own version and exits.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/../src/version.py")"
if [ -z "$VERSION" ]; then
    echo "cannot read the version from src/version.py" >&2
    exit 1
fi
SPEC="$HERE/buildozer.spec"
sed -E "s/^version = .*/version = $VERSION/" "$SPEC" > "$SPEC.tmp"
mv "$SPEC.tmp" "$SPEC"
echo "== buildozer $TARGET (version $VERSION) =="
cd "$HERE"
# The toolchain lives outside the repository, and a rebuilt one comes back
# unpatched: the android recipe then dies with "No module named 'Cython'".
# Applying the patch is idempotent, so it happens on every build.
if [ -f "$HERE/tools/patch_p4a.py" ]; then
    "$(dirname "$BUILDOZER")/python" "$HERE/tools/patch_p4a.py" \
        --buildozer "$BUILDOZER" || exit 1
fi
# Start from an empty bin/.  A build made from an older tree leaves an APK
# whose file name carries that older version, and it sits next to the real
# one looking perfectly plausible.  Only the build that runs now may decide
# what bin/ contains.
rm -f "$HERE"/bin/*.apk "$HERE"/bin/*.aab
attempt=1
max_attempts=3
while :; do
    echo "== buildozer $TARGET (attempt $attempt/$max_attempts) =="
    if "$BUILDOZER" $TARGET; then
        break
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "buildozer failed $max_attempts times" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    echo "-- retrying in 30s --"
    sleep 30
done

echo "== artifacts =="
# buildozer can exit 0 without building anything (a mistyped flag prints its
# version and stops), so the artifact has to be there before the build counts
# as done.
if [ "$TARGET" = "android release" ]; then
    # The release package is named after the artifact type, not necessarily
    # after the version, so any package in bin/ counts here.
    if ! ls "$HERE"/bin/*.apk >/dev/null 2>&1; then
        echo "buildozer finished without producing an APK" >&2
        ls -la "$HERE"/bin/ >&2 || true
        exit 1
    fi
elif ! ls "$HERE"/bin/*"$VERSION"*.apk >/dev/null 2>&1; then
    echo "buildozer finished without producing an APK for version $VERSION" >&2
    ls -la "$HERE"/bin/ >&2 || true
    exit 1
fi
ls -la "$HERE"/bin/*.apk 2>/dev/null || echo "  (no apk in bin/)"
ls -la "$HERE"/bin/*.aab 2>/dev/null || true
