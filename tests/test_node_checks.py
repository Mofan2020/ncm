"""The checks that live in Node scripts.

``app.js`` carries logic that pytest cannot reach: the file has to be
loaded and its functions called.  The checks therefore live in Node
scripts under ``scripts/``, and this file runs them.

A script that nobody runs catches nothing -- the two scripts that were
already here were never wired up, and the bugs they would have caught
were in the code.  So they are run here, and a failure is a failure.

Node is not part of the Python dependencies, so a machine without it
skips these.  GitHub's runners have it, so CI runs them.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: The scripts and what they check.
CHECKS = (
    ("check-player-js.mjs", "the play modes of the queue"),
    ("check-lyrics-scroll.mjs", "the scrolling of the lyrics"),
    ("check-backup-js.mjs", "the backup window"),
    ("check-bili-stream-loader.mjs", "the shared Bilibili stream loader"),
    ("check-bili-hires.mjs", "the Hi-Res track of a Bilibili video"),
    ("check-bili-hires-switch.mjs", "the Hi-Res switch of the Bilibili player"),
    ("check-bili-recommend-paging.mjs", "the paging of the recommendation feed"),
    ("check-bili-default-quality.mjs", "the default quality reaching playback"),
    ("check-bili-collection-paging.mjs", "the paging of the subscribed collections"),
    ("check-bili-fav-paging.mjs", "the paging of the favourite folders"),
    ("check-bangumi-js.mjs", "the Bangumi page"),
    ("check-pixiv-viewer.mjs", "the Pixiv full-screen image viewer"),
    ("check-settings-form.mjs", "the settings form"),
)

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("script,subject", CHECKS)
def test_the_node_check_passes(script: str, subject: str) -> None:
    """Every check script has to pass."""
    node = NODE or ""
    path = SCRIPTS / script
    assert path.exists(), f"{script} is missing"
    done = subprocess.run(
        [node, str(path)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    output = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, f"{script} failed:\n{output}"



