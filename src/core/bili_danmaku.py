"""B站弹幕：读、发、筛。

读弹幕走 ``/x/v2/dm/web/seg.so``，它返回的是 **protobuf** 而不是 JSON，所以这里
手写了一个极简的 protobuf 读取器（只处理 varint 和 length-delimited 两种 wire
type），不引入任何依赖。分段数量和用户的弹幕屏蔽设置走
``/x/v2/dm/web/view``（这个是 JSON）。

发弹幕走 ``/x/v2/dm/post``，表单 POST，必须带 ``csrf``（cookie 里的 ``bili_jct``）
和 ``buvid3``。B站的弹幕和评论接口有 gaia 风控，被拦下时返回 ``-352`` 或
``-401``：**不要重试**，换时间或换方式。

写接口会真的改动别人的视频，测试一律用假传输层跑，**不要拿真实账号去验证**。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

__all__ = ["BiliDanmaku", "mask_allows"]

log = logging.getLogger(__name__)

BILI_API_BASE = "https://api.bilibili.com"

#: 分段弹幕的接口，一次拿 6 分钟。
DM_SEG_PATH = "/x/v2/dm/web/seg.so"
#: 分段数量和弹幕屏蔽设置。
DM_VIEW_PATH = "/x/v2/dm/web/view"
#: 发弹幕。
DM_POST_PATH = "/x/v2/dm/post"

#: 弹幕模式：1 滚动、2 高级、3 特殊、4 底部、5 顶部、6 逆向、7 高级、8 代码、9 BAS。
DM_MODES: dict[int, str] = {
    1: "scroll",
    2: "advanced",
    3: "special",
    4: "bottom",
    5: "top",
    6: "reverse",
    7: "advanced",
    8: "code",
    9: "bas",
}

#: ``DmElem`` 的字段号（``bili.community.dm.v1``）。
DM_ELEM_FIELDS: dict[int, str] = {
    1: "id",
    2: "progress",
    3: "mode",
    4: "fontsize",
    5: "color",
    6: "midHash",
    7: "content",
    8: "ctime",
    9: "weight",
    10: "action",
    11: "pool",
    12: "idStr",
    13: "attr",
}

#: 32 位有符号整数的字段，varint 读出来是无符号的，要还原符号。
_DM_INT32_FIELDS = frozenset({"progress", "mode", "fontsize", "weight", "pool", "attr"})

#: 服务端错误码 -> 中文短句。
_ERROR_MESSAGES: dict[int, str] = {
    -101: "账号未登录",
    -111: "登录状态已失效，请重新登录",
    -400: "请求参数不对",
    -401: "被 B 站的风控拦住了，换个时间或改用其它方式",
    -352: "被 B 站的风控拦住了，换个时间或改用其它方式",
    12002: "弹幕内容被 B 站审核拦截，换个说法",
    12006: "弹幕发送太频繁了",
    12009: "该视频不允许发弹幕",
}


# ---------------------------------------------------------------------------
# 极简 protobuf 读取器
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """读一个 varint，返回 ``(值, 新位置)``。

    varint 是 7 位一组、**字节序低位在前**：每个字节的最低位是续位标志，
    其余 7 位拼进结果，低位组在前。
    """
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 越界")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def _skip_field(buf: bytes, pos: int, wire_type: int) -> int:
    """跳过一个当前用不到的字段，返回新位置。"""
    if wire_type == 0:
        _, pos = _read_varint(buf, pos)
        return pos
    if wire_type == 1:  # 64 位定长
        return pos + 8
    if wire_type == 2:  # 长度前缀
        length, pos = _read_varint(buf, pos)
        return pos + length
    if wire_type == 5:  # 32 位定长
        return pos + 4
    raise ValueError(f"不支持的 wire type: {wire_type}")


def _parse_dm_elem(buf: bytes) -> dict[str, Any]:
    """解析一条 ``DmElem``，键名用 :data:`DM_ELEM_FIELDS` 里的字段名。"""
    out: dict[str, Any] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field = key >> 3
        wire_type = key & 0x07
        name = DM_ELEM_FIELDS.get(field)
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
            if name is None:
                continue
            if name in _DM_INT32_FIELDS and value >= 1 << 31:
                value -= 1 << 32
            out[name] = value
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            payload = buf[pos:pos + length]
            pos += length
            if name is None:
                continue
            if name in ("midHash", "content", "action", "idStr"):
                out[name] = payload.decode("utf-8", "replace")
            else:
                out[name] = payload
        else:
            pos = _skip_field(buf, pos, wire_type)
    return out


def _parse_dm_elems(buf: bytes) -> list[dict]:
    """解析一整段 protobuf，返回 ``DmElem`` 列表。

    顶层消息是 ``repeated DmElem elems = 1;``，也就是顶层 field 1 重复出现，
    每个是一个 length-delimited 的子消息。
    """
    elems: list[dict] = []
    pos = 0
    try:
        while pos < len(buf):
            key, pos = _read_varint(buf, pos)
            field = key >> 3
            wire_type = key & 0x07
            if field == 1 and wire_type == 2:
                length, pos = _read_varint(buf, pos)
                payload = buf[pos:pos + length]
                pos += length
                elems.append(_parse_dm_elem(payload))
            else:
                pos = _skip_field(buf, pos, wire_type)
    except ValueError as exc:
        log.warning("弹幕 protobuf 解析中断: %s", exc)
    return elems


def _normalize(elem: dict[str, Any]) -> dict[str, Any]:
    """把一条 ``DmElem`` 变成前端能直接用的形状。"""
    elem_id = str(elem.get("idStr") or elem.get("id") or "")
    return {
        "id": elem_id,
        "time": round(int(elem.get("progress") or 0) / 1000.0, 3),
        "mode": int(elem.get("mode") or 1),
        "size": int(elem.get("fontsize") or 25),
        "color": int(elem.get("color") or 0) & 0xFFFFFF,
        "text": str(elem.get("content") or ""),
        "uid_hash": str(elem.get("midHash") or ""),
        "time_stamp": int(elem.get("ctime") or 0),
        "pool": int(elem.get("pool") or 0),
        "action": str(elem.get("action") or ""),
    }


def mask_allows(mask: dict, item: dict) -> bool:
    """按 ``dm_mask`` 判断一条弹幕会不会被用户屏蔽。

    ``dm_mask`` 的结构各个版本不太一样，含义也不完全确定，所以这里只认几种
    **明确的** 规则（某一项被列成被屏蔽的取值），其余一律放行 —— 宁可多显示
    一条，也不要把弹幕误杀。
    """
    if not isinstance(mask, dict) or not mask:
        return True
    if not isinstance(item, dict):
        return True
    for key, field in (("color", "color"), ("size", "size"), ("fontsize", "size"),
                       ("mode", "mode")):
        blocked = mask.get(key)
        if isinstance(blocked, list | tuple | set):
            try:
                values = {int(value) for value in blocked}
            except (TypeError, ValueError):
                continue
            try:
                if int(item.get(field) or 0) in values:
                    return False
            except (TypeError, ValueError):
                continue
    return True


class BiliDanmaku:
    """读、发、筛 B站弹幕。"""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._buvid3 = ""

    # ------------------------------------------------------------------ 读
    def _get_bytes(self, path: str, params: dict[str, Any]) -> bytes | None:
        """GET 一个接口并返回原始字节（弹幕接口返回的是 protobuf）。"""
        try:
            response = self._client.session.get(
                BILI_API_BASE + path,
                params=params,
                headers=self._client._headers(),  # noqa: SLF001 - same package
                timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili danmaku request failed: %s (%s)", path, exc)
            return None
        status = getattr(response, "status_code", 200)
        if status != 200:
            log.warning("bilibili danmaku request %s answered HTTP %s", path, status)
            return None
        content = getattr(response, "content", None)
        if content is None:
            try:
                content = response.content
            except Exception:  # noqa: BLE001
                content = b""
        return bytes(content or b"")

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """GET 一个返回 JSON 的接口。"""
        try:
            response = self._client.session.get(
                BILI_API_BASE + path,
                params=params,
                headers=self._client._headers(),  # noqa: SLF001 - same package
                timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili danmaku request failed: %s (%s)", path, exc)
            return None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return None
        return body if isinstance(body, dict) else None

    def _dm_view(self, cid: int) -> tuple[int, dict]:
        """取分段数量和用户的弹幕屏蔽设置。"""
        body = self._get_json(DM_VIEW_PATH, {"oid": int(cid)})
        if not body or body.get("code") != 0:
            return 0, {}
        data = body.get("data")
        if not isinstance(data, dict):
            return 0, {}
        try:
            segments = int(data.get("segs") or 0)
        except (TypeError, ValueError):
            segments = 0
        mask = data.get("dm_mask")
        return segments, (mask if isinstance(mask, dict) else {})

    def _resolve_cid(self, bvid: str, cid: int) -> int:
        if cid:
            return int(cid)
        try:
            video = self._client.video(bvid)
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili video lookup failed: %s (%s)", bvid, exc)
            return 0
        if not isinstance(video, dict):
            return 0
        try:
            return int(video.get("cid") or 0)
        except (TypeError, ValueError):
            return 0

    def danmaku(self, bvid: str, cid: int = 0, segments: int = 0) -> dict[str, Any]:
        """读取一个视频的弹幕。

        返回 ``{"success", "items", "count", "mask", "message"}``；``items`` 里
        每条是 ``{"id", "time", "mode", "size", "color", "text", "uid_hash",
        "time_stamp", "pool", "action"}``，``time`` 是秒。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return {"success": False, "items": [], "count": 0, "mask": {},
                    "message": "视频 id 是空的"}
        cid = self._resolve_cid(bvid, cid)
        if not cid:
            return {"success": False, "items": [], "count": 0, "mask": {},
                    "message": "拿不到这个视频的分 P 信息"}

        mask: dict = {}
        if not segments:
            segments, mask = self._dm_view(cid)
        try:
            segments = int(segments)
        except (TypeError, ValueError):
            segments = 0
        if segments <= 0:
            segments = 1

        items: list[dict] = []
        failed = 0
        for index in range(segments):
            raw = self._get_bytes(DM_SEG_PATH, {
                "type": 1,
                "oid": cid,
                "segment_index": index,
            })
            if raw is None:
                failed += 1
                continue
            for elem in _parse_dm_elems(raw):
                items.append(_normalize(elem))

        if failed == segments:
            # 每一段都取不到：多半是网络或签名出了问题，不是「没有弹幕」。
            return {"success": False, "items": [], "count": 0, "mask": mask,
                    "message": "弹幕读取失败，请稍后再试"}

        if not items and not mask:
            # 一条都没有时再问一次屏蔽设置，前端要用它画筛选器。
            _, mask = self._dm_view(cid)

        items.sort(key=lambda entry: (entry["time"], entry["time_stamp"]))
        return {
            "success": True,
            "items": items,
            "count": len(items),
            "mask": mask,
            "message": "" if items else "这个视频没有弹幕",
        }

    # ------------------------------------------------------------------ 发
    def _ensure_buvid3(self) -> str:
        """拿到 ``buvid3``：先看 cookie，没有就去换一次。"""
        if self._buvid3:
            return self._buvid3
        try:
            cookies = self._client.cookie_dict()
        except Exception:  # noqa: BLE001
            cookies = {}
        value = str((cookies or {}).get("buvid3") or "")
        if not value:
            try:
                self._client._ensure_buvid()  # noqa: SLF001 - same package
            except Exception as exc:  # noqa: BLE001
                log.warning("bilibili buvid fetch failed: %s", exc)
            try:
                cookies = self._client.cookie_dict()
            except Exception:  # noqa: BLE001
                cookies = {}
            value = str((cookies or {}).get("buvid3") or "")
        self._buvid3 = value
        return value

    def _csrf(self) -> str:
        try:
            return str(self._client.csrf() or "")
        except Exception:  # noqa: BLE001
            return ""

    def send(self, bvid: str, text: str, cid: int = 0, color: int = 16777215,
             size: int = 25, mode: int = 1, position: float = 0.0,
             at: str = "") -> dict[str, Any]:
        """发一条弹幕。

        ``at`` 是被 @ 的人的 ``cid``（也可以写成 ``"cid,名字"``），它会拼成
        ``at=<cid>,<name>`` 放在弹幕文字前面。

        返回 ``{"success", "message", "dm_id"}``。被风控拦下（``-352`` /
        ``-401``）时直接失败，不会重试。
        """
        bvid = str(bvid or "").strip()
        text = str(text or "").strip()
        if not bvid:
            return {"success": False, "message": "视频 id 是空的", "dm_id": ""}
        if not text:
            return {"success": False, "message": "弹幕内容是空的", "dm_id": ""}
        cid = self._resolve_cid(bvid, cid)
        if not cid:
            return {"success": False, "message": "拿不到这个视频的分 P 信息",
                    "dm_id": ""}

        message = text
        at = str(at or "").strip()
        if at:
            if "," in at:
                at_cid, _, at_name = at.partition(",")
            else:
                at_cid, at_name = at, ""
            prefix = f"at={at_cid.strip()},{at_name.strip()}"
            message = f"{prefix} {text}"

        csrf = self._csrf()
        payload = {
            "type": 1,
            "oid": int(cid),
            "bvid": bvid,
            "message": message,
            "color": int(color),
            "fontsize": int(size),
            "mode": int(mode),
            "pool": 0,
            "rnd": int(time.time() * 1000) ^ random.randint(0, 999),
            "ct": int(time.time() * 1000),
            "csrf": csrf,
            "buvid3": self._ensure_buvid3(),
            "position": float(position),
        }

        headers = dict(self._client._headers())  # noqa: SLF001 - same package
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            cookie = self._client.cookie_header()
        except Exception:  # noqa: BLE001
            cookie = ""
        if cookie:
            headers["Cookie"] = cookie

        try:
            response = self._client.session.post(
                BILI_API_BASE + DM_POST_PATH,
                data=payload,
                headers=headers,
                timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili danmaku post failed: %s", exc)
            return {"success": False, "message": "发送失败，请稍后再试", "dm_id": ""}

        self._remember_buvid(response)

        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            return {"success": False, "message": "B 站没有返回内容", "dm_id": ""}

        code = int(body.get("code") or 0)
        if code != 0:
            return {"success": False, "message": self._error_text(code, body),
                    "dm_id": ""}
        data = body.get("data")
        dm_id = ""
        if isinstance(data, dict):
            dm_id = str(data.get("dmid") or data.get("dm_id") or data.get("id") or "")
        if not dm_id:
            dm_id = str(body.get("dmid") or body.get("dm_id") or "")
        return {"success": True, "message": "弹幕已发送", "dm_id": dm_id}

    def _remember_buvid(self, response: Any) -> None:
        """发弹幕的响应里可能带新的 ``buvid``，记下来下次用。"""
        headers = getattr(response, "headers", None)
        if not headers:
            return
        try:
            value = headers.get("buvid") or headers.get("Buvid")
        except Exception:  # noqa: BLE001
            return
        if value:
            self._buvid3 = str(value)

    @staticmethod
    def _error_text(code: int, body: dict[str, Any]) -> str:
        """把服务端的错误码/错误信息翻成一句中文。"""
        known = _ERROR_MESSAGES.get(int(code))
        if known:
            return known
        text = str(body.get("message") or "").strip()
        if text:
            return f"发送失败：{text}"
        return f"发送失败（B 站返回 {code}）"

    # ------------------------------------------------------------------ 筛
    @staticmethod
    def filter_items(items: list[dict], *, modes=None, keywords=None,
                     min_time=None, max_time=None, colors=None) -> list[dict]:
        """按条件过滤弹幕，顺序不变。

        * ``modes``：只保留这些模式（1 滚动、4 底部、5 顶部、6 逆向、7 高级、
          8 代码、9 BAS）。
        * ``keywords``：只保留文字里含这些词的（不区分大小写，命中任意一个即可）。
        * ``min_time`` / ``max_time``：秒。
        * ``colors``：只保留这些颜色值。
        """
        result = list(items or [])
        if modes:
            wanted_modes = {int(mode) for mode in modes}
            result = [item for item in result
                      if int(item.get("mode") or 0) in wanted_modes]
        if keywords:
            words = [str(word).strip().lower() for word in keywords]
            words = [word for word in words if word]
            if words:
                result = [item for item in result
                          if any(word in str(item.get("text") or "").lower()
                                 for word in words)]
        if min_time is not None:
            limit = float(min_time)
            result = [item for item in result if float(item.get("time") or 0) >= limit]
        if max_time is not None:
            limit = float(max_time)
            result = [item for item in result if float(item.get("time") or 0) <= limit]
        if colors:
            wanted_colors = {int(color) for color in colors}
            result = [item for item in result
                      if int(item.get("color") or 0) in wanted_colors]
        return result

    def visible(self, bvid: str, cid: int = 0, **kwargs: Any) -> list[dict]:
        """读出弹幕并按 :meth:`filter_items` 的条件过滤。"""
        payload = self.danmaku(bvid, cid=cid)
        if not payload.get("success"):
            return []
        return self.filter_items(payload.get("items") or [], **kwargs)
