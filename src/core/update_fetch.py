"""更新包怎么下载：能借 SDM 就借 SDM，借不到自己下。

SDM（SuperDownloadManager）在本机跑着一个下载引擎，命令行是 ``sdm``。
它按分片并发下载，同一个 GitHub release 包能比单连接快好几倍，而且它自己
会重试、会换源。所以只要满足三个条件就用它：

* 在 macOS 上跑（Windows 那边 SDM 的 CLI 不在同一个位置，不走这条路）；
* ``sdm`` 在 PATH 里，而且它的后台服务是活的（``sdm ping`` 有回答）；
* **没有要求忽略 SSL 校验**。

最后一条是硬的：``sdm`` 的 ``add`` 命令没有「不校验证书」这个参数，而
「跳过 SSL 校验」恰恰是用加速器/自签证书的用户最常打开的那个开关。这种
情况下继续走 SDM，结果是每一次连接都证书失败，用户看到的却是一个下载不动
的进度条。所以宁可不借它，用自己那条带 ``verify=False`` 的路。

**任何一步出错都退回自己下**，包括 SDM 没装、服务没起、任务失败、卡住
不动。退回之前会把 SDM 那边那个任务取消掉，否则两份流量同时跑。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

__all__ = [
    "download",
    "sdm_status",
    "sdm_download",
    "DEFAULT_THREADS",
]

log = logging.getLogger(__name__)

#: SDM 的命令行入口。它在 PATH 里叫这个名字。
SDM_BIN = "sdm"

#: 分片线程数。SDM 的上限远高于这个，但更新包一般几十兆，再多没意义。
DEFAULT_THREADS = 8

#: 进度回调：``(已下载字节, 总字节, 每秒字节)``。总字节未知时是 0。
Progress = Callable[[int, int, float], None]

#: 多久看一次任务状态。
POLL_SECONDS = 0.6

#: 任务没出现在列表里超过这么久，就当 SDM 没接这个活。
START_TIMEOUT = 20.0

#: 字节数这么久没变，就当卡住了，退回自己下。
STALL_TIMEOUT = 120.0

#: 单条 sdm 命令本身的超时。
COMMAND_TIMEOUT = 20.0


def _run(args: list[str], runner: Any, timeout: float) -> tuple[int, str, str]:
    """跑一条 sdm 命令，返回 ``(退出码, stdout, stderr)``。

    ``runner`` 是注入点：测试里换成假的，不去真的起进程。
    """
    call = runner or subprocess.run
    try:
        result = call(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return (
        int(getattr(result, "returncode", 1) or 0),
        str(getattr(result, "stdout", "") or ""),
        str(getattr(result, "stderr", "") or ""),
    )


def _parse_json(text: str) -> Any:
    """``sdm`` 每次只输出一段 JSON；前面几行日志要能容忍。"""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def sdm_status(*, runner: Any = None) -> dict[str, Any]:
    """能用 SDM 吗。回答里带原因，界面上可以直接说给用户听。"""
    if sys.platform != "darwin":
        return {"available": False, "reason": "platform", "path": ""}
    path = shutil.which(SDM_BIN) or ""
    if not path:
        return {"available": False, "reason": "missing", "path": ""}
    code, out, _ = _run([SDM_BIN, "ping"], runner, COMMAND_TIMEOUT)
    payload = _parse_json(out)
    alive = code == 0 and isinstance(payload, dict) and payload.get("pong")
    if not alive:
        # 装了但后台没起：这不是错误，只是不能借。
        return {"available": False, "reason": "offline", "path": path}
    return {"available": True, "reason": "", "path": path}


def _task_by_id(entries: Any, task_id: str) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for item in entries:
        if isinstance(item, dict) and str(item.get("id") or "") == task_id:
            return item
    return None


def _cancel(task_id: str, runner: Any) -> None:
    """退回自己下之前先掐掉 SDM 那个任务，不然两份流量一起跑。"""
    if not task_id:
        return
    _run([SDM_BIN, "cancel", task_id], runner, COMMAND_TIMEOUT)


def sdm_download(
    url: str,
    dest_path: str,
    *,
    threads: int = DEFAULT_THREADS,
    on_progress: Progress | None = None,
    runner: Any = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """让 SDM 去下这个包，下完搬到 ``dest_path``。

    SDM 有自己的下载目录，命令行里没有「下到这个路径」的参数，所以流程是
    「让它按文件名下」→「等它完成」→「把文件搬过来」。搬而不是复制：这个
    文件唯一的用途就是安装更新，留两份只是占地方。
    """
    dest = os.path.abspath(str(dest_path))
    name = os.path.basename(dest) or "update.bin"
    args = [SDM_BIN, "add", str(url), "--filename", name,
            "--threads", str(max(1, int(threads or DEFAULT_THREADS)))]
    code, out, err = _run(args, runner, COMMAND_TIMEOUT)
    payload = _parse_json(out)
    task_id = str((payload or {}).get("task_id") or "") if isinstance(payload, dict) else ""
    if code != 0 or not task_id:
        return {"ok": False, "reason": "add_failed",
                "message": (err or out or "sdm 没能接下这个任务").strip()[:200]}

    started = clock()
    last_bytes = -1
    last_change = clock()
    total = 0
    while True:
        sleeper(POLL_SECONDS)
        code, out, err = _run([SDM_BIN, "list"], runner, COMMAND_TIMEOUT)
        if code != 0:
            _cancel(task_id, runner)
            return {"ok": False, "reason": "list_failed",
                    "message": (err or "sdm list 失败").strip()[:200]}
        entry = _task_by_id(_parse_json(out), task_id)
        now = clock()
        if entry is None:
            if now - started > START_TIMEOUT:
                _cancel(task_id, runner)
                return {"ok": False, "reason": "not_started",
                        "message": "sdm 没有开始这个任务"}
            continue
        state = str(entry.get("state") or "")
        received = int(entry.get("bytes_done") or 0)
        total = int(entry.get("total") or entry.get("total_bytes") or 0) or total
        speed = float(entry.get("speed_bytes_per_sec") or 0.0)
        if received != last_bytes:
            last_bytes = received
            last_change = now
        if on_progress is not None and total:
            try:
                on_progress(received, total, speed)
            except Exception:  # noqa: BLE001 - 界面回调坏了不能停下下载
                log.debug("progress callback failed", exc_info=True)
        if state == "completed":
            source = str(entry.get("destination") or "")
            try:
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                if source and os.path.abspath(source) != dest:
                    shutil.move(source, dest)
                elif not os.path.isfile(dest):
                    return {"ok": False, "reason": "missing_file",
                            "message": "sdm 说完成了，但找不到文件"}
            except OSError as exc:
                return {"ok": False, "reason": "move_failed", "message": str(exc)}
            return {"ok": True, "reason": "", "bytes": received, "total": total}
        if state in ("failed", "cancelled"):
            # 终态，没有东西可取消。
            return {"ok": False, "reason": state,
                    "message": str(entry.get("error_message") or f"任务状态是 {state}")[:200]}
        if state == "awaiting_new_link":
            # 不是终态：SDM 在等一个新链接，任务槽还占着。这种必须掐掉，
            # 不然退回自己下的时候它还挂着。
            _cancel(task_id, runner)
            return {"ok": False, "reason": state,
                    "message": "sdm 在等新的下载链接"}
        if now - last_change > STALL_TIMEOUT:
            _cancel(task_id, runner)
            return {"ok": False, "reason": "stalled",
                    "message": "sdm 长时间没有进展"}


def _builtin_download(url: str, dest: str, *, verify: bool,
                      on_progress: Progress | None) -> dict[str, Any]:
    """自己下，顺便算出速度。

    ``updater.download_asset`` 只报「下了多少」，速度得在这里按时间采出来：
    每 0.4 秒算一次，用最近这一段而不是全程平均，否则刚开始的几兆会把
    后面的读数拖住。
    """
    from src.core.updater import download_asset

    started = time.monotonic()
    last_time = [started]
    last_bytes = [0]
    speed = [0.0]

    def report(received: int, total: int) -> None:
        if on_progress is None:
            return
        now = time.monotonic()
        elapsed = now - last_time[0]
        if elapsed >= 0.4:
            delta = received - last_bytes[0]
            if delta > 0 and elapsed > 0:
                speed[0] = delta / elapsed
            last_time[0] = now
            last_bytes[0] = received
        try:
            on_progress(int(received), int(total), float(speed[0]))
        except Exception:  # noqa: BLE001
            log.debug("progress callback failed", exc_info=True)

    result = download_asset(url, dest, progress=report, verify=verify)
    return result


def download(
    url: str,
    dest_path: str,
    *,
    on_progress: Progress | None = None,
    verify: bool = True,
    use_sdm: bool = True,
    threads: int = DEFAULT_THREADS,
    runner: Any = None,
) -> dict[str, Any]:
    """下一份更新包，回答里带 ``engine``（``sdm`` 或 ``builtin``）。

    SDM 只在「可以用 + 没有要求跳过证书校验」时尝试，失败一律退回自己下。
    退回的理由放进 ``fallback_reason``，日志里也记一行——用户报「怎么这么慢」
    的时候，这行是唯一能解释当时发生了什么的东西。
    """
    dest = os.path.abspath(str(dest_path))
    reason = ""
    if use_sdm and verify:
        status = sdm_status(runner=runner)
        if status["available"]:
            outcome = sdm_download(url, dest, threads=threads,
                                   on_progress=on_progress, runner=runner)
            if outcome.get("ok"):
                log.info("update downloaded with sdm (%s bytes)", outcome.get("bytes"))
                return {
                    "success": True,
                    "path": dest,
                    "bytes": int(outcome.get("bytes") or 0),
                    "engine": "sdm",
                    "fallback_reason": "",
                    "message": "已下载",
                }
            reason = str(outcome.get("reason") or "sdm_failed")
            log.info("sdm download fell back to the built-in path (%s: %s)",
                     reason, outcome.get("message") or "")
        else:
            reason = str(status.get("reason") or "unavailable")
    elif use_sdm and not verify:
        # SDM 的 add 没有「不校验证书」这个开关，硬让它去下就是每次连接都
        # 证书失败。这条理由要说清楚，不然会被当成「SDM 又抽风了」。
        reason = "skip_ssl"
        log.info("update download skips sdm: the certificate check is off "
                 "and sdm has no such switch")
    else:
        reason = "disabled"

    result = _builtin_download(url, dest, verify=verify, on_progress=on_progress)
    result = dict(result)
    result["engine"] = "builtin"
    result["fallback_reason"] = reason
    return result
