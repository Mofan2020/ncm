"""更新包下载的两条路：借 SDM，或者自己下。

``subprocess.run`` 是注入的（``runner``），所以这里一条真实命令都不跑。
"""
from __future__ import annotations

import json
import os

import pytest

from src.core import update_fetch


class FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """按命令行前缀给回答，并记下每一条命令。"""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(list(args))
        key = " ".join(args[:2]) if len(args) > 1 else args[0]
        answer = self.answers.get(key)
        if answer is None:
            answer = self.answers.get(args[0], FakeResult())
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _json(answer) -> str:  # noqa: ANN001
    return json.dumps(answer, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _on_macos(monkeypatch):
    """SDM 那条路只在 macOS 上走。"""
    monkeypatch.setattr(update_fetch.sys, "platform", "darwin")
    monkeypatch.setattr(update_fetch.shutil, "which", lambda name: "/usr/local/bin/sdm")
    monkeypatch.setattr(update_fetch, "POLL_SECONDS", 0.0)


def _patch_builtin(monkeypatch, written: bytes = b"x" * 100):
    """把内置下载换成「写一个文件就算下完」，并记下 verify。"""
    seen: dict[str, object] = {}

    def fake_download_asset(url, dest, progress=None, verify=True):
        seen["verify"] = verify
        seen["url"] = url
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(written)
        if progress is not None:
            progress(len(written), len(written))
        return {"success": True, "path": dest, "bytes": len(written), "message": ""}

    import src.core.updater as updater

    monkeypatch.setattr(updater, "download_asset", fake_download_asset)
    return seen


# ---------------------------------------------------------------- 能不能用


def test_sdm_is_not_used_off_macos(monkeypatch):
    monkeypatch.setattr(update_fetch.sys, "platform", "win32")

    assert update_fetch.sdm_status()["reason"] == "platform"


def test_no_binary_means_no_sdm(monkeypatch):
    monkeypatch.setattr(update_fetch.shutil, "which", lambda name: None)

    assert update_fetch.sdm_status()["available"] is False


def test_a_dead_daemon_is_reported_as_offline(monkeypatch):
    """装了但后台没起：这不是错误，只是借不到。"""
    runner = FakeRunner({"sdm ping": FakeResult(1, "", "connection refused")})

    status = update_fetch.sdm_status(runner=runner)

    assert status["available"] is False
    assert status["reason"] == "offline"


def test_a_live_daemon_answers_pong():
    runner = FakeRunner({"sdm ping": FakeResult(0, _json({"pong": True}))})

    assert update_fetch.sdm_status(runner=runner)["available"] is True


# ---------------------------------------------------------------- 借 SDM


def test_the_package_is_downloaded_with_sdm_and_moved_into_place(tmp_path, monkeypatch):
    """SDM 有它自己的下载目录，命令行里没有「下到这个路径」，所以下完要搬。"""
    _patch_builtin(monkeypatch)
    sdm_copy = tmp_path / "sdm-downloads" / "SkycMedia.zip"
    sdm_copy.parent.mkdir()
    sdm_copy.write_bytes(b"zip-bytes")
    dest = tmp_path / "update" / "SkycMedia.zip"
    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(0, _json({"task_id": "abc", "state": "queued"})),
        "sdm list": [
            FakeResult(0, _json([{"id": "abc", "state": "downloading",
                                  "bytes_done": 4, "total": 9,
                                  "speed_bytes_per_sec": 2_000_000}])),
            FakeResult(0, _json([{"id": "abc", "state": "completed",
                                  "bytes_done": 9, "total": 9,
                                  "destination": str(sdm_copy)}])),
        ],
    })
    seen: list[tuple[int, int, float]] = []

    result = update_fetch.download(
        "https://example.com/SkycMedia.zip", str(dest),
        on_progress=lambda got, total, speed: seen.append((got, total, speed)),
        runner=runner,
    )

    assert result["success"] is True
    assert result["engine"] == "sdm", "能用 SDM 就该用它"
    assert dest.read_bytes() == b"zip-bytes", "文件要搬到更新目录"
    assert not sdm_copy.exists(), "搬而不是复制：留两份只是占地方"
    assert seen and seen[0][2] == 2_000_000, "速度要原样报给界面"


def test_the_thread_count_reaches_the_command(tmp_path):
    sdm_copy = tmp_path / "a.zip"
    sdm_copy.write_bytes(b"x")
    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(0, _json({"task_id": "t1"})),
        "sdm list": FakeResult(0, _json([{"id": "t1", "state": "completed",
                                          "destination": str(sdm_copy)}])),
    })

    update_fetch.sdm_download("https://example.com/a.zip", str(tmp_path / "out" / "a.zip"),
                              threads=16, runner=runner)

    add = [call for call in runner.calls if call[:2] == ["sdm", "add"]][0]
    assert "--threads" in add
    assert add[add.index("--threads") + 1] == "16"


def test_a_failed_task_falls_back_without_cancelling_a_dead_task(tmp_path, monkeypatch):
    """任务已经失败了，取消它没有意义——它本来就不跑了。"""
    seen = _patch_builtin(monkeypatch)
    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(0, _json({"task_id": "t2"})),
        "sdm list": FakeResult(0, _json([{"id": "t2", "state": "failed",
                                          "error_message": "403"}])),
    })

    result = update_fetch.download("https://example.com/a.zip",
                                   str(tmp_path / "a.zip"), runner=runner)

    assert result["success"] is True
    assert result["engine"] == "builtin"
    assert result["fallback_reason"] == "failed"
    assert seen["verify"] is True
    assert not [call for call in runner.calls if call[:2] == ["sdm", "cancel"]]


def test_a_task_waiting_for_a_new_link_is_cancelled_before_the_fallback(tmp_path, monkeypatch):
    """``awaiting_new_link`` 不是终态：它还占着 SDM 的任务槽，退回前要掐掉。"""
    _patch_builtin(monkeypatch)
    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(0, _json({"task_id": "t4"})),
        "sdm list": FakeResult(0, _json([{"id": "t4", "state": "awaiting_new_link"}])),
    })

    result = update_fetch.download("https://example.com/a.zip",
                                   str(tmp_path / "a.zip"), runner=runner)

    assert result["engine"] == "builtin"
    assert result["fallback_reason"] == "awaiting_new_link"
    assert ["sdm", "cancel", "t4"] in runner.calls


def test_an_add_that_is_refused_falls_back(tmp_path, monkeypatch):
    _patch_builtin(monkeypatch)
    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(1, "", "unknown option --threads"),
    })

    result = update_fetch.download("https://example.com/a.zip",
                                   str(tmp_path / "a.zip"), runner=runner)

    assert result["engine"] == "builtin"
    assert result["fallback_reason"] == "add_failed"


# ------------------------------------------------------- 不借 SDM 的时候


def test_skipping_the_certificate_check_never_borrows_sdm(tmp_path, monkeypatch):
    """``sdm add`` 没有「不校验证书」这一档，硬用它就是每次连接都失败。"""
    seen = _patch_builtin(monkeypatch)
    runner = FakeRunner({"sdm ping": FakeResult(0, _json({"pong": True}))})

    result = update_fetch.download("https://example.com/a.zip", str(tmp_path / "a.zip"),
                                   verify=False, runner=runner)

    assert result["engine"] == "builtin"
    assert result["fallback_reason"] == "skip_ssl"
    assert seen["verify"] is False, "内置那条路必须真的关掉校验"
    assert not [call for call in runner.calls if call[:2] == ["sdm", "add"]]


def test_the_setting_can_turn_sdm_off_entirely(tmp_path, monkeypatch):
    _patch_builtin(monkeypatch)
    runner = FakeRunner({"sdm ping": FakeResult(0, _json({"pong": True}))})

    result = update_fetch.download("https://example.com/a.zip", str(tmp_path / "a.zip"),
                                   use_sdm=False, runner=runner)

    assert result["engine"] == "builtin"
    assert result["fallback_reason"] == "disabled"
    assert runner.calls == [], "关掉之后连 ping 都不该发"


def test_progress_reaches_the_caller_on_the_builtin_path(tmp_path, monkeypatch):
    seen: list[tuple[int, int, float]] = []

    def fake_download_asset(url, dest, progress=None, verify=True):  # noqa: ANN001, ANN201
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"x" * 10)
        for step in (5, 10):
            progress(step, 10)
        return {"success": True, "path": dest, "bytes": 10, "message": ""}

    import src.core.updater as updater

    monkeypatch.setattr(updater, "download_asset", fake_download_asset)

    result = update_fetch.download(
        "https://example.com/a.zip", str(tmp_path / "a.zip"), use_sdm=False,
        on_progress=lambda got, total, speed: seen.append((got, total, speed)))

    assert result["success"] is True
    assert [item[0] for item in seen] == [5, 10]
    assert all(item[1] == 10 for item in seen)


def test_a_stalled_task_does_not_hang_forever(tmp_path):
    """SDM 卡住不动时不能一直等——界面上就是一个永远不动的进度条。"""
    clock = {"now": 0.0}

    def fake_clock() -> float:
        clock["now"] += 60.0
        return clock["now"]

    runner = FakeRunner({
        "sdm ping": FakeResult(0, _json({"pong": True})),
        "sdm add": FakeResult(0, _json({"task_id": "t3"})),
        "sdm list": FakeResult(0, _json([{"id": "t3", "state": "downloading",
                                          "bytes_done": 0, "total": 100}])),
    })

    outcome = update_fetch.sdm_download(
        "https://example.com/a.zip", str(tmp_path / "a.zip"),
        runner=runner, clock=fake_clock, sleeper=lambda _s: None)

    assert outcome["ok"] is False
    assert outcome["reason"] == "stalled"
    assert ["sdm", "cancel", "t3"] in runner.calls
