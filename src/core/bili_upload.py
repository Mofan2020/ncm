"""B站投稿：把本地视频传到自己空间里。

投稿走的不是主站那套 API，而是会员中心的接口（``member.bilibili.com``），
所以这个模块单独一个文件，不碰 :mod:`src.core.bilibili`。

整个流程四步，任何一步失败都会在返回值的 ``steps`` 里写清楚卡在哪一步：

1. ``GET  /x/vuweb/pre/category``   取分区列表（``tid`` 和名字）
2. ``GET  /x/vuweb/pre``             预上传，拿到上传地址和 ``complete_token``
3. ``POST <endpoint>``               把视频本体 multipart 传上去
4. ``POST /x/vuweb/add``             提交稿件，拿到 ``aid`` / ``bvid``

这几个接口变化很快，还带风控，所以实现上有三条硬规矩：

* **上传地址不写死**，一律从预上传的回答里读 —— 服务端换了地址，代码要能跟上。
* **预上传的调用方式挨个试**：带不带 ``csrf``、带不带 WBI 签名，共四种组合，
  哪一个先回答 ``code 0`` 就用哪一个。
* **任何一步都不抛异常**，只返回失败和中文原因；界面拿到的一定是字典。

写操作会真的把视频发到公开平台上，所以测试一律用假传输层跑，
**不要拿真实账号去验证**。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:  # pragma: no cover - 只是拿个 UA，缺了也能跑
    from src.core.bilibili import BILI_UA
except Exception:  # pragma: no cover
    BILI_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

log = logging.getLogger("ncm.bili.upload")

__all__ = ["BiliUploader", "MEMBER_BASE"]

#: 会员中心。投稿的每一步都在这个域上。
MEMBER_BASE = "https://member.bilibili.com"

#: 分区列表的候选路径，第一个能用的就是它。
CATEGORY_PATHS = ("/x/vuweb/pre/category", "/x/vupre/web/archive/pre")

#: 预上传的候选路径。
PRE_PATHS = ("/x/vuweb/pre", "/x/vupre/web/archive/pre")

#: 提交稿件。
ADD_PATH = "/x/vuweb/add"

#: 预上传最多试多少次：路由、方法、csrf、签名各有变化，试太多次反而会触发风控。
_MAX_PRE_ATTEMPTS = 8

#: 常见的服务端错误码 -> 中文短句。写错了要在界面上说清楚是哪里错。
ERROR_MESSAGES: dict[int, str] = {
    -101: "B站账号未登录，请先登录",
    -104: "账号被封禁，不能投稿",
    -111: "登录校验失败，请重新登录 B 站",
    -400: "参数不对，投稿被拒绝了",
    -403: "没有投稿权限",
    -352: "触发风控了，过一会儿再试",
    62002: "稿件不可见",
    62004: "文件上传失败",
    62012: "文件没通过校验，换个格式试试",
}

#: 步骤名字，界面直接显示。
STEP_CHECK = "本地检查"
STEP_PRE = "预上传"
STEP_UPLOAD = "上传文件"
STEP_COVER = "上传封面"
STEP_SUBMIT = "提交"


def _reason(code: Any, server_message: Any = "") -> str:
    """Turn a server code plus its message into one Chinese sentence."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        number = -1
    text = str(server_message or "").strip()
    if text in ("", "0", "success"):
        text = ""
    lowered = text.lower()
    if "captcha" in lowered or "验证码" in text:
        return "需要验证码，投稿被风控拦下了"
    if "频繁" in text or "风控" in text:
        return "操作太频繁，触发风控了，过一会儿再试"
    known = ERROR_MESSAGES.get(number, "")
    if known:
        return f"{known}（{text}）" if text else known
    if text:
        return text
    return f"服务端返回 {number}"


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a JSON value into a dictionary (an empty one when it is not)."""
    return value if isinstance(value, dict) else {}


def _pick_error(messages: list[str], known: list[str]) -> str:
    """Pick the message that says the most.

    A route that simply does not exist answers ``-404``; that tells the user
    nothing, so a message about a known reason (not signed in, blocked, ...)
    wins over it.
    """
    if known:
        return known[0]
    for message in messages:
        if message and message != "no route":
            return message
    if messages:
        return messages[0]
    return "拿不到投稿分区，检查网络或重新登录"


class BiliUploader:
    """Upload a local video to the signed-in Bilibili account.

    ``client`` is a :class:`~src.core.bilibili.BiliClient` (or anything with
    the same surface): ``session``, ``csrf()``, ``is_logged_in`` and
    ``_headers()``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ steps
    def _step(self, name: str, ok: bool, message: str = "") -> None:
        """Record the outcome of one step so the UI can say where it stopped."""
        self._steps.append({"name": name, "ok": bool(ok), "message": str(message or "")})

    def _fail(self, message: str) -> dict[str, Any]:
        """Build the failure answer; the caller records the failed step."""
        return {
            "success": False,
            "message": str(message or "投稿失败"),
            "aid": "",
            "bvid": "",
            "steps": list(self._steps),
        }

    def _local_fail(self, message: str) -> dict[str, Any]:
        """Fail before anything is sent: a local check did not pass."""
        self._step(STEP_CHECK, False, message)
        return self._fail(message)

    # -------------------------------------------------------------- transport
    def _session(self) -> Any:
        return getattr(self._client, "session", None) or self._client._session

    def _headers(self) -> dict[str, str]:
        """Headers for the member centre: its own Referer and Origin."""
        headers: dict[str, str] = {"User-Agent": BILI_UA}
        try:
            extra = self._client._headers()  # noqa: SLF001 - same package
        except Exception:  # pragma: no cover - the client does not raise
            extra = {}
        if isinstance(extra, dict):
            headers.update({k: v for k, v in extra.items() if v})
        headers["User-Agent"] = BILI_UA
        headers["Referer"] = "https://member.bilibili.com/"
        headers["Origin"] = "https://member.bilibili.com"
        headers.setdefault("Accept", "application/json, text/plain, */*")
        return headers

    def _csrf(self) -> str:
        try:
            return str(self._client.csrf() or "")
        except Exception:  # pragma: no cover
            return ""

    def _get_json(self, url: str, params: dict[str, Any]) -> tuple[Any, str]:
        """GET one URL; returns ``(payload, error)``."""
        try:
            response = self._session().get(url, params=params, headers=self._headers(),
                                          timeout=(10, 30))
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            return None, f"连不上服务器：{exc}"
        status = getattr(response, "status_code", 200)
        if status != 200:
            return None, f"服务器返回 HTTP {status}"
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None, "服务器没有返回内容"
        return payload, ""

    def _post_json(self, url: str, data: dict[str, Any],
                   timeout: tuple[int, int] = (10, 60)) -> tuple[Any, str]:
        """POST one form; returns ``(payload, error)``."""
        try:
            response = self._session().post(url, data=data, headers=self._headers(),
                                           timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return None, f"请求失败：{exc}"
        status = getattr(response, "status_code", 200)
        if status != 200:
            return None, f"服务器返回 HTTP {status}"
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            text = ""
            try:
                text = str(getattr(response, "text", "") or "")
            except Exception:  # pragma: no cover
                text = ""
            return (text if text else None), ""
        return payload, ""

    def _post_multipart(self, url: str, fields: dict[str, Any], file_path: str,
                        field_name: str = "file") -> tuple[Any, str]:
        """POST one file as multipart/form-data; returns ``(payload, error)``."""
        path = Path(file_path)
        try:
            handle = path.open("rb")
        except OSError as exc:
            return None, f"打不开文件：{exc}"
        try:
            with handle:
                files = {field_name: (path.name, handle, "application/octet-stream")}
                response = self._session().post(url, data=fields, files=files,
                                               headers=self._headers(),
                                               timeout=(10, 1800))
        except Exception as exc:  # noqa: BLE001
            return None, f"上传失败：{exc}"
        status = getattr(response, "status_code", 200)
        if status != 200:
            return None, f"服务器返回 HTTP {status}"
        try:
            return response.json(), ""
        except Exception:  # noqa: BLE001
            text = ""
            try:
                text = str(getattr(response, "text", "") or "")
            except Exception:  # pragma: no cover
                text = ""
            return (text if text else None), ""

    # -------------------------------------------------------------- categories
    def categories(self) -> dict[str, Any]:
        """Return the upload categories of the account.

        Answer: ``{"success", "categories": [{"tid", "name", "parent"}],
        "message"}``.
        """
        self._steps = []
        messages: list[str] = []
        known: list[str] = []
        for path in CATEGORY_PATHS:
            payload, error = self._get_json(MEMBER_BASE + path, {})
            if error:
                messages.append(error)
                continue
            if not isinstance(payload, dict):
                messages.append("服务器没有返回内容")
                continue
            if payload.get("code") != 0:
                message = _reason(payload.get("code"), payload.get("message"))
                messages.append(message)
                try:
                    if int(payload.get("code")) in ERROR_MESSAGES:
                        known.append(message)
                except (TypeError, ValueError):
                    pass
                continue
            items = self._parse_categories(payload.get("data"))
            if items:
                return {"success": True, "categories": items, "message": ""}
            messages.append("服务器没有给出分区列表")
        return {"success": False, "categories": [],
                "message": _pick_error(messages, known)}

    @staticmethod
    def _parse_categories(data: Any) -> list[dict[str, Any]]:
        """Read ``tid`` and the names out of the category answer.

        The shape moves around: the list sits under ``archive_pre``, sometimes
        under ``categories``, sometimes the data *is* the list.  Every entry
        carries ``tid`` plus a name (``tname``); the parent name is ``tybe``
        for a flat list and the entry's own name for a nested one.
        """
        container = _as_dict(data)
        raw: Any = None
        for key in ("archive_pre", "categories", "archive_pre_list", "list"):
            value = container.get(key)
            if isinstance(value, list):
                raw = value
                break
        if raw is None and isinstance(data, list):
            raw = data
        if not isinstance(raw, list):
            return []

        found: list[dict[str, Any]] = []
        seen: set[int] = set()

        def add(tid: Any, name: Any, parent: Any) -> None:
            try:
                number = int(tid)
            except (TypeError, ValueError):
                return
            if number <= 0 or number in seen:
                return
            seen.add(number)
            found.append({
                "tid": number,
                "name": str(name or "").strip() or f"分区 {number}",
                "parent": str(parent or "").strip(),
            })

        for entry in raw:
            if not isinstance(entry, dict):
                continue
            entry_name = entry.get("tname") or entry.get("name") or ""
            entry_parent = entry.get("tybe") or entry.get("parent") or ""
            if entry.get("tid") is not None:
                add(entry.get("tid"), entry_name, entry_parent)
            children = entry.get("children") or entry.get("sub") or []
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    add(child.get("tid"),
                        child.get("tname") or child.get("name") or "",
                        child.get("tybe") or entry_name)
        return found

    # ------------------------------------------------------------------ upload
    def upload(self, video_path: str, title: str, tid: int,
               desc: str = "", tags: str = "", cover_path: str = "") -> dict[str, Any]:
        """Publish a video.

        Answer: ``{"success", "message", "aid", "bvid", "steps"}``.  ``steps``
        lists every step with ``{"name", "ok", "message"}`` so the UI can point
        at the step that failed.
        """
        self._steps = []
        path = str(video_path or "").strip()
        clean_title = str(title or "").strip()
        try:
            tid_number = int(tid)
        except (TypeError, ValueError):
            tid_number = 0

        # Local checks first: a missing file must not waste a submission.
        if not path:
            return self._local_fail("没有选择视频文件")
        target = Path(path).expanduser()
        if not target.exists():
            return self._local_fail("找不到这个视频文件")
        if not target.is_file():
            return self._local_fail("这个路径不是文件")
        try:
            if target.stat().st_size <= 0:
                return self._local_fail("视频文件是空的")
        except OSError as exc:
            return self._local_fail(f"读不了这个视频文件：{exc}")
        if not clean_title:
            return self._local_fail("标题不能为空")
        if tid_number <= 0:
            return self._local_fail("没有选择分区")

        if not getattr(self._client, "is_logged_in", False):
            self._step(STEP_CHECK, False, "还没有登录 B 站，先登录再投稿")
            return self._fail(self._steps[-1]["message"])
        csrf = self._csrf()
        if not csrf:
            self._step(STEP_CHECK, False, "没拿到登录凭证，请重新登录 B 站")
            return self._fail(self._steps[-1]["message"])
        self._step(STEP_CHECK, True, "文件、标题、分区都没问题")

        # ---- 1. pre-upload ------------------------------------------------
        pre_data, pre_error = self._pre_upload({
            "type": "video",
            "cover_type": "0",
            "tid": str(tid_number),
            "title": clean_title,
            "desc": str(desc or ""),
            "tag": str(tags or ""),
        })
        if not pre_data:
            self._step(STEP_PRE, False, pre_error)
            return self._fail(pre_error)
        self._step(STEP_PRE, True, "拿到了上传参数")

        extras = _as_dict(pre_data.get("extras"))
        endpoint = self._endpoint(pre_data)
        if not endpoint:
            self._step(STEP_UPLOAD, False, "预上传没有给上传地址")
            return self._fail(self._steps[-1]["message"])
        upload_id = str(pre_data.get("upload_id") or "")
        biz_id = str(pre_data.get("biz_id") or "")
        mid = str(pre_data.get("mid") or "")
        complete_token = str(pre_data.get("complete_token") or "")
        filename = str(extras.get("bili_filename") or pre_data.get("filename") or "")

        fields: dict[str, Any] = {}
        for key, value in (("biz_id", biz_id), ("mid", mid),
                           ("complete_token", complete_token), ("upload_id", upload_id),
                           ("filename", filename)):
            if value:
                fields[key] = value
        for key, value in extras.items():
            if isinstance(value, str | int | float) and value != "" and key not in fields:
                fields[key] = value

        # ---- 2. upload the file -------------------------------------------
        payload, error = self._post_multipart(endpoint, fields, str(target), "file")
        if error:
            self._step(STEP_UPLOAD, False, error)
            return self._fail(error)
        if isinstance(payload, dict):
            if payload.get("code") not in (0, None):
                message = _reason(payload.get("code"), payload.get("message"))
                self._step(STEP_UPLOAD, False, message)
                return self._fail(message)
            data = _as_dict(payload.get("data"))
            token = str(data.get("complete_token") or "")
            if token:
                complete_token = token
        elif isinstance(payload, str) and payload.strip():
            # Some answers are the bare token.
            complete_token = payload.strip()
        self._step(STEP_UPLOAD, True, "视频传完了")

        # ---- 3. the cover (optional) --------------------------------------
        cover_value = ""
        cover_file = str(cover_path or "").strip()
        if cover_file:
            cover_value, cover_message = self._upload_cover(cover_file)
            self._step(STEP_COVER, bool(cover_value),
                       cover_message or "封面传完了")

        # ---- 4. submit ------------------------------------------------------
        form: dict[str, Any] = {
            "title": clean_title,
            "tid": str(tid_number),
            "desc": str(desc or ""),
            "tag": str(tags or ""),
            "copyright": "1",
            "source": "-1",
            "cover": cover_value,
            "no_reprint": "0",
            "open_elec": "0",
            "filename": filename,
            "up_selection_reply": "0",
            "mid": mid,
            "csrf": csrf,
            "complete_token": complete_token,
        }
        payload, error = self._post_json(MEMBER_BASE + ADD_PATH, form)
        if error:
            self._step(STEP_SUBMIT, False, error)
            return self._fail(error)
        if not isinstance(payload, dict):
            message = "服务器没有返回内容"
            self._step(STEP_SUBMIT, False, message)
            return self._fail(message)
        if payload.get("code") != 0:
            message = _reason(payload.get("code"), payload.get("message"))
            self._step(STEP_SUBMIT, False, message)
            return self._fail(message)

        data = _as_dict(payload.get("data"))
        aid = str(data.get("aid") or data.get("arc") or "")
        bvid = str(data.get("bvid") or "")
        if not bvid:
            arc = _as_dict(data.get("arc"))
            bvid = str(arc.get("bvid") or "")
        self._step(STEP_SUBMIT, True, "投稿成功")
        return {
            "success": True,
            "message": "投稿成功",
            "aid": aid,
            "bvid": bvid,
            "steps": list(self._steps),
        }

    # ---------------------------------------------------------------- helpers
    def _pre_upload(self, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Ask for the upload parameters.

        Returns ``(data, error)``.  The route, the method (GET or POST), the
        ``csrf`` and the WBI signature all move around, so several
        combinations per route are tried until one answers ``code 0`` with a
        usable ``data`` block.  The attempt count is capped: too many calls
        are exactly what trips the risk-control layer.
        """
        last = "预上传失败"
        attempts = 0
        for signed, with_csrf in ((False, True), (False, False),
                                  (True, True), (True, False)):
            for path in PRE_PATHS:
                for method in ("GET", "POST"):
                    if attempts >= _MAX_PRE_ATTEMPTS:
                        break
                    query = dict(params)
                    if with_csrf:
                        query["csrf"] = self._csrf()
                    if signed:
                        query = self._sign(query)
                        if not query:
                            continue
                    attempts += 1
                    payload, error = self._request(method, MEMBER_BASE + path, query)
                    if error:
                        last = error
                        continue
                    if not isinstance(payload, dict):
                        last = "服务器没有返回内容"
                        continue
                    if payload.get("code") != 0:
                        last = _reason(payload.get("code"), payload.get("message"))
                        continue
                    data = _as_dict(payload.get("data"))
                    if data:
                        return data, ""
                    last = "预上传没有给上传参数"
        return {}, last

    def _request(self, method: str, url: str,
                 params: dict[str, Any]) -> tuple[Any, str]:
        """One request with the method the caller wants; ``(payload, error)``."""
        if method.upper() == "POST":
            return self._post_json(url, params)
        return self._get_json(url, params)

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add the WBI signature, if the client can produce one."""
        try:
            from src.core.bilibili import sign_params

            key = str(self._client.wbi_key() or "")
            if not key:
                return {}
            return sign_params(params, key)
        except Exception:  # noqa: BLE001 - signing is optional
            return {}

    @staticmethod
    def _endpoint(data: dict[str, Any]) -> str:
        """Read the upload address out of the pre-upload answer.

        It may sit under ``endpoint`` (a string), under ``endpoint`` as a
        dictionary of named endpoints, or under ``upload_url``.
        """
        candidates: list[Any] = []
        for key in ("endpoint", "upload_url", "upload_endpoint", "url", "host"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                candidates.append(value)
        for mapping in candidates:
            for name in ("biliup", "ugcupos", "upload", "url", "endpoint", "bfs"):
                value = mapping.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in mapping.values():
                if isinstance(value, str) and value.strip().startswith("http"):
                    return value.strip()
        return ""

    def _upload_cover(self, cover_path: str) -> tuple[str, str]:
        """Upload the cover image; returns ``(address, message)``.

        The cover is optional: on failure the address is empty and the
        submission goes on without a cover.
        """
        path = Path(cover_path).expanduser()
        if not path.exists() or not path.is_file():
            return "", "找不到封面图片"
        try:
            if path.stat().st_size <= 0:
                return "", "封面图片是空的"
        except OSError as exc:
            return "", f"读不了封面图片：{exc}"

        data, error = self._pre_upload({"type": "video", "cover_type": "1"})
        if not data:
            return "", f"封面的预上传失败：{error}" if error else "封面的预上传失败"
        endpoint = self._endpoint(data)
        if not endpoint:
            return "", "封面的预上传没有给上传地址"
        extras = _as_dict(data.get("extras"))
        fields: dict[str, Any] = {}
        for key, value in (("biz_id", str(data.get("biz_id") or "")),
                           ("mid", str(data.get("mid") or "")),
                           ("complete_token", str(data.get("complete_token") or "")),
                           ("upload_id", str(data.get("upload_id") or ""))):
            if value:
                fields[key] = value
        for key, value in extras.items():
            if isinstance(value, str | int | float) and value != "" and key not in fields:
                fields[key] = value

        payload, error = self._post_multipart(endpoint, fields, str(path), "file")
        if error:
            return "", error
        if isinstance(payload, dict):
            if payload.get("code") not in (0, None):
                return "", _reason(payload.get("code"), payload.get("message"))
            answer = _as_dict(payload.get("data"))
            for key in ("url", "cover", "image", "file_url", "location", "src"):
                value = answer.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), ""
            return "", "服务器没有给封面地址"
        if isinstance(payload, str) and payload.strip():
            return payload.strip(), ""
        return "", "服务器没有给封面地址"

    # ------------------------------------------------------------------ cover
    def cover_from_frame(self, video_path: str, at_seconds: float = 1.0) -> str:
        """Grab one frame of the video and use it as the cover.

        Returns the path of the temporary JPEG, or an empty string when that
        does not work (no ``ffmpeg`` on the machine, no video, no frame).
        """
        executable = shutil.which("ffmpeg")
        if not executable:
            log.warning("没有找到 ffmpeg，封面得自己选")
            return ""
        video = Path(str(video_path or "")).expanduser()
        if not video.exists() or not video.is_file():
            log.warning("找不到视频文件 %s", video)
            return ""
        handle, name = tempfile.mkstemp(prefix="skycmedia_cover_", suffix=".jpg")
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            Path(name).unlink()
        except OSError:
            pass
        command = [
            executable, "-y",
            "-ss", str(float(at_seconds)),
            "-i", str(video),
            "-frames:v", "1",
            "-q:v", "2",
            name,
        ]
        try:
            subprocess.run(command, check=False, capture_output=True, timeout=120)
        except Exception as exc:  # noqa: BLE001
            log.warning("截帧失败：%s", exc)
            self._discard(name)
            return ""
        try:
            if not Path(name).exists() or Path(name).stat().st_size <= 0:
                self._discard(name)
                return ""
        except OSError:
            self._discard(name)
            return ""
        return name

    @staticmethod
    def _discard(path: str) -> None:
        """Remove a temporary file that did not work out."""
        try:
            Path(path).unlink()
        except OSError:
            pass
