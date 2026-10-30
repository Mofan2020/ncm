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

# The Java side and the bundled JavaScript have to agree on the bridge
# signature.  Upstream changed ``PyJavascriptInterface.call`` after 6.2.1
# (it grew a fourth ``token`` argument) while the bundled JavaScript still
# calls ``window.external.call(funcName, params, id)`` with three.  Android
# resolves ``@JavascriptInterface`` methods by exact parameter count, so the
# mismatch does not raise: every bridge call throws ``Method not found`` and
# the app looks alive but does nothing -- the login dialog says "method
# error" and the home page stays empty, with nothing in the Python log.
#
# ``javap`` reads the method straight out of the class file, so the count
# checked here is the count the phone will see.
if ! command -v javap >/dev/null 2>&1; then
    echo "   javap not found: cannot verify the bridge signature" >&2
    exit 1
fi
JAR_CALLS="$(javap -classpath "$JAR" com.pywebview.PyJavascriptInterface \
    | grep -c 'public void call(java.lang.String' || true)"
if [ "$JAR_CALLS" != "1" ]; then
    echo "$JAR does not declare exactly one call() method" >&2
    exit 1
fi
# The bundled JavaScript calls external.call with three arguments; the Java
# method has to take three too.
JAR_CALL_PARAMS="$(javap -classpath "$JAR" com.pywebview.PyJavascriptInterface \
    | grep 'public void call(' \
    | sed 's/.*call(//; s/).*//' | awk -F',' '{print NF}')"
# Count the top-level arguments of the call: the argument list holds nested
# calls of its own (``pywebview.stringify(params)``), so a plain comma count
# would stop at the first closing parenthesis.
JS_CALL_PARAMS="$(grep -o 'window\.external\.call(.*' \
    "$HERE/webview/js/api.js" | head -1 \
    | awk '{
        line = $0
        depth = 0
        commas = 0
        for (i = 1; i <= length(line); i++) {
            c = substr(line, i, 1)
            if (c == "(") depth++
            else if (c == ")") { depth--; if (depth == 0) { print commas + 1; exit } }
            else if (c == "," && depth == 1) commas++
        }
    }')"
if [ -z "$JS_CALL_PARAMS" ]; then
    echo "cannot find window.external.call in $HERE/webview/js/api.js" >&2
    exit 1
fi
if [ "$JAR_CALL_PARAMS" != "$JS_CALL_PARAMS" ]; then
    echo "the jar and the bundled JavaScript disagree on the bridge signature:" >&2
    echo "  jar:     call($JAR_CALL_PARAMS parameters)" >&2
    echo "  api.js:  external.call($JS_CALL_PARAMS arguments)" >&2
    echo "  take the jar from the tag that matches the bundled pywebview" >&2
    exit 1
fi
echo "   bridge signature: jar call() takes $JAR_CALL_PARAMS parameters, api.js passes $JS_CALL_PARAMS"

# The launch screen: p4a stretches the presplash over the whole screen, so a
# square icon becomes a full-screen logo.  A tall image is what keeps the
# logo small.
if [ ! -f "$HERE/assets/presplash.png" ]; then
    echo "missing $HERE/assets/presplash.png" >&2
    echo "  generate it with: .venv/bin/python scripts/make_presplash.py" >&2
    exit 1
fi
echo "   launch screen: $(file -b "$HERE/assets/presplash.png")"

# The network security config has to be valid XML before it is handed to
# AAPT: a double dash inside a comment fails the resource compile with a
# parse error that names the file but not the offending line.
if command -v xmllint >/dev/null 2>&1; then
    if ! xmllint --noout "$HERE/res/xml/network_security_config.xml"; then
        echo "res/xml/network_security_config.xml is not valid XML" >&2
        exit 1
    fi
fi

# pywebview's own imports have to be satisfied inside the bundle: the pure
# Python ones are vendored next to it, and jnius comes from the p4a recipe.
# A missing one kills the app on launch with nothing but a ModuleNotFoundError
# in logcat, so they are checked here instead.
echo "== checking the vendored python dependencies =="
for MOD in proxy_tools bottle typing_extensions; do
    if [ ! -f "$HERE/$MOD.py" ]; then
        echo "missing $HERE/$MOD.py: pywebview imports it at startup" >&2
        exit 1
    fi
done
# proxy_tools is a module, not a package: vendoring it as a directory is the
# mistake that cost a build cycle.
if [ -d "$HERE/proxytools" ]; then
    echo "$HERE/proxytools is a directory but pywebview imports 'proxy_tools'" >&2
    exit 1
fi
echo "   proxy_tools.py bottle.py typing_extensions.py"

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
