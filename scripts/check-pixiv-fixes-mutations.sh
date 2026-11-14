#!/bin/bash
# Mutation check: break each of the three fixes once and see the tests go red.
#
# A green test that stays green when the fix is removed proves nothing.
# Every mutation touches a copy that is put back whatever happens -- the
# restore lives in the EXIT trap, so a killed run still leaves the tree clean.
set -u
cd "$(dirname "$0")/.." || exit 1

CORE=src/core/pixiv.py
PBRIDGE=src/gui/pixiv_bridge.py
APPJS=src/gui/assets/app.js
PIXJS=src/gui/assets/pixiv.js

BACKUP="$(mktemp -d)"
cp "$CORE" "$BACKUP/core.orig"
cp "$PBRIDGE" "$BACKUP/pbridge.orig"
cp "$APPJS" "$BACKUP/appjs.orig"
cp "$PIXJS" "$BACKUP/pixjs.orig"

restore() {
    [ -f "$BACKUP/core.orig" ] && cp "$BACKUP/core.orig" "$CORE"
    [ -f "$BACKUP/pbridge.orig" ] && cp "$BACKUP/pbridge.orig" "$PBRIDGE"
    [ -f "$BACKUP/appjs.orig" ] && cp "$BACKUP/appjs.orig" "$APPJS"
    [ -f "$BACKUP/pixjs.orig" ] && cp "$BACKUP/pixjs.orig" "$PIXJS"
    # The bytecode cache keys on (mtime seconds, size). A mutation that keeps
    # both can leave the old .pyc in place, and the test would run the code
    # that is no longer on disk.
    find src tests -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
}
trap 'restore; rm -rf "$BACKUP"' EXIT

fail=0

mutate() {
    local name="$1" file="$2" old="$3" new="$4" testfile="$5"
    restore
    if ! grep -qF "$old" "$file"; then
        echo "[SKIP] $name -- the pattern was not found"
        fail=1
        return
    fi
    python3 - "$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding='utf-8').read()
if old not in text:
    sys.exit(2)
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
    find src tests -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
    if .venv/bin/python -m pytest "$testfile" -q >/dev/null 2>&1; then
        echo "[FAIL] $name -- the test stayed green"
        fail=1
    else
        echo "[PASS] $name -- the test went red"
    fi
}

echo "=== the mutations ==="

mutate "the following route asks for rest=all" \
    "$CORE" \
    '                  "rest": "show", "lang": "zh"}' \
    '                  "rest": "all", "lang": "zh"}' \
    tests/test_pixiv_following_session.py

mutate "a dead session is not noticed" \
    "$CORE" \
    '                self.drop_expired_cookie()
                error = "登录已失效，请重新登录。"' \
    '                error = "登录已失效，请重新登录。"' \
    tests/test_pixiv_following_session.py

mutate "the follow flags are read the wrong way" \
    "$CORE" \
    '                "followed": bool(item.get("following") or item.get("isFollowed")),' \
    '                "followed": bool(item.get("isFollowed")),' \
    tests/test_pixiv_following_session.py

mutate "more does not drop what is already on screen" \
    "$PBRIDGE" \
    '        fresh = [work for work in works if str(work.get("id") or "") not in seen]' \
    '        fresh = list(works)' \
    tests/test_pixiv_discover_more.py

mutate "the settings modal forgets the pixiv panel" \
    "$APPJS" \
    "    for (const category of ['download', 'playback', 'ui', 'auth', 'bili', 'bangumi', 'pixiv']) {" \
    "    for (const category of ['download', 'playback', 'ui', 'auth', 'bili', 'bangumi']) {" \
    tests/test_pixiv_discover_more.py

mutate "more replaces the list instead of appending" \
    "$PIXJS" \
    '            pixivState.discover = pixivState.discover.concat(works);' \
    '            pixivState.discover = works;' \
    tests/test_pixiv_discover_more.py

restore

echo ""
if [ "$fail" -ne 0 ]; then
    echo "FAILED: a mutation slipped through"
    exit 1
fi
echo "every mutation was caught"
