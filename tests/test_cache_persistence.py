"""The cache must survive a restart and must not hide a changed song.

These run the app in **separate processes** against a shared config
directory, because that is the real scenario: the user closes the app and
opens it again tomorrow.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = r'''
import json, os, pathlib, sys
home = pathlib.Path(sys.argv[1]) / "home"
home.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(home)
os.environ["XDG_CONFIG_HOME"] = str(home / ".config")

from src.core import api as api_module
api_module._api_instance = None
api = api_module.NeteaseAPI()

calls = []
def fake_eapi(path, data=None, use_cache=True):
    calls.append(path)
    if "song/detail" in path:
        ids = [i["id"] for i in json.loads(data["c"])]
        return {"songs": [{"id": i, "name": "song%s" % i} for i in ids]}
    if "lyric" in path:
        return {"code": 200, "lrc": {"lyric": "[00:01.00] hi"}}
    return {"code": 200}
api._eapi = fake_eapi
api._legacy = lambda *a, **k: None

songs = api.get_songs_detail([str(i) for i in range(1, 51)])
api.get_lyrics("123")
from src.core.metacache import get_meta_cache
print(json.dumps({"calls": calls, "songs": len(songs),
                  "cache_dir": str(get_meta_cache().root)}))
'''


def _run(home: Path, tmp: Path) -> dict:
    script = tmp / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    result = subprocess.run(
        [sys.executable, str(script), str(home)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_cache_survives_a_restart(tmp_path):
    """The second launch must not ask the server for the same songs."""
    home = tmp_path / "shared"
    home.mkdir()

    first = _run(home, tmp_path)
    assert first["calls"], "the first launch has to fetch something"

    second = _run(home, tmp_path)
    assert second["calls"] == [], (
        f"the second launch still hit the network: {second['calls']}")
    # The songs still came back -- from the cache.
    assert second["songs"] == first["songs"]


def test_the_third_launch_is_also_silent(tmp_path):
    home = tmp_path / "shared"
    home.mkdir()
    _run(home, tmp_path)
    _run(home, tmp_path)
    third = _run(home, tmp_path)
    assert third["calls"] == []


def test_a_fresh_config_directory_fetches_again(tmp_path):
    """A different install (or a cleared cache) starts from scratch."""
    first = _run(tmp_path / "one", tmp_path)
    second = _run(tmp_path / "two", tmp_path)
    assert first["calls"] and second["calls"]


def test_the_cleared_cache_fetches_again(tmp_path):
    """The force-refresh path really does bring the data back."""
    import shutil

    home = tmp_path / "shared"
    home.mkdir()
    first = _run(home, tmp_path)
    assert first["calls"]

    # Wipe the cache the way the force-refresh button does.  The path comes
    # from the probe, because it resolves the config dir against *its* home.
    cache_dir = Path(first["cache_dir"])
    assert cache_dir.is_dir(), f"the cache was not written to {cache_dir}"
    shutil.rmtree(cache_dir, ignore_errors=True)

    third = _run(home, tmp_path)
    assert third["calls"], "after clearing, the app has to fetch again"
