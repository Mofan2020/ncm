#!/bin/bash
# Mutation check: break each fix once and see the check go red.
#
# A green check that stays green when the fix is removed proves nothing.
# Each mutation is applied to a copy of the file, the check runs against
# the copy, and the original is put back.
set -u
cd "$(dirname "$0")/.." || exit 1

APP=src/gui/assets/app.js
BACKUP="$(mktemp -d)"
cp "$APP" "$BACKUP/app.js.orig"

restore() {
    if [ -f "$BACKUP/app.js.orig" ]; then cp "$BACKUP/app.js.orig" "$APP"; fi
}
# The original goes back whatever happens, and the scratch copy is only
# removed once it is no longer needed.
trap 'restore; rm -rf "$BACKUP"' EXIT

fail=0

mutate() {
    local name="$1" old="$2" new="$3"
    restore
    if ! grep -qF "$old" "$APP"; then
        echo "[SKIP] $name -- the pattern was not found"
        fail=1
        return
    fi
    python3 - "$APP" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding='utf-8').read()
if old not in text:
    sys.exit(2)
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
    if node scripts/check-backup-js.mjs >/dev/null 2>&1; then
        echo "[FAIL] $name -- the check stayed green"
        fail=1
    else
        echo "[PASS] $name -- the check went red"
    fi
}

echo "=== the mutations ==="

mutate "localRecordCount reads the wrong shape" \
'    const restored = data.local_restored || data;' \
'    const restored = data.local_restored || {};'

mutate "the row text is not put into an isolate" \
"    const inner = document.createElement('bdi');" \
"    const inner = document.createElement('span');"

mutate "the like button gets no label" \
"        const like = document.getElementById('btn-bili-like');
        if (like) like.textContent = t('bili.like');" \
"        const like = document.getElementById('btn-bili-like');"

mutate "the coin button gets no label" \
"    const coin = document.getElementById('btn-bili-coin');
    if (coin) coin.textContent = t('bili.coin');" \
"    const coin = document.getElementById('btn-bili-coin');"

restore

echo ""
if [ "$fail" -ne 0 ]; then
    echo "FAILED: a mutation slipped through"
    exit 1
fi
echo "every mutation was caught"
