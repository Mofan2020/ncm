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
    ("check-bili-recommend-paging.mjs", "the paging of the recommendation feed"),
    ("check-bili-default-quality.mjs", "the default quality reaching playback"),
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_backup_check_catches_a_broken_fix() -> None:
    """The check has to go red when the fix is taken out.

    A check that stays green when the code it guards is broken proves
    nothing.  The script breaks each fix once and expects the check to
    notice; this test runs that script.
    """
    script = SCRIPTS / "check-backup-mutations.sh"
    if not script.exists():
        pytest.skip("the mutation script is not there")
    done = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    output = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, f"a mutation slipped through:\n{output}"
    assert "every mutation was caught" in output
