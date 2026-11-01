"""写 B站收藏夹：建收藏夹、往收藏夹里加视频。

读接口在 :mod:`src.core.bilibili` 里，写接口单独放这里：写操作会真的改动
账号，测试一律用假传输层跑，**不要拿真实账号去验证**。

两个接口都要 ``csrf``（就是 cookie 里的 ``bili_jct``），少了会回 ``-111``：

* ``POST /x/v3/fav/folder/add``    新建收藏夹，参数 ``title`` / ``intro`` /
  ``privacy`` / ``csrf``，新建的收藏夹 id 在 ``data.id``。
* ``POST /x/v3/fav/resource/deal`` 往收藏夹里加内容，参数 ``rid``（收藏夹
  id）、``type=2``（视频）、``add_media_ids``（形如 ``"123:2,456:2"``）、
  ``csrf``、``platform=web``。
"""
from __future__ import annotations

import logging
from typing import Any

__all__ = ["BiliFavorites"]

log = logging.getLogger(__name__)

#: 收藏夹的隐私：0 公开、1 私密。
PRIVACY_PUBLIC = 0


class BiliFavorites:
    """Write access to the favourite folders of a signed-in account."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ------------------------------------------------------------------ transport
    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST one form and return the parsed envelope (never raises)."""
        payload = dict(data)
        payload["csrf"] = self._csrf()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        try:
            headers.update(self._client.bili_headers())
        except Exception:  # pragma: no cover - the client does not raise
            pass
        try:
            response = self._client._session.post(  # noqa: SLF001 - same package
                "https://api.bilibili.com" + path,
                data=payload, headers=headers, timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili write failed (%s): %s", path, exc)
            return {"code": -1, "message": str(exc), "data": None}
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return {"code": -1, "message": "服务器没有返回内容", "data": None}
        if not isinstance(body, dict):
            return {"code": -1, "message": "服务器没有返回内容", "data": None}
        return body

    def _csrf(self) -> str:
        try:
            return str(self._client.csrf() or "")
        except Exception:  # pragma: no cover
            return ""

    # -------------------------------------------------------------------- writes
    def create_folder(self, title: str, intro: str = "",
                      privacy: int = PRIVACY_PUBLIC) -> dict[str, Any]:
        """Create a folder.  Returns ``{"success", "id", "message"}``."""
        title = str(title or "").strip()
        if not title:
            return {"success": False, "id": "", "message": "收藏夹名字是空的"}
        body = self._post("/x/v3/fav/folder/add", {
            "title": title,
            "intro": str(intro or ""),
            "privacy": int(privacy),
            "cover": "",
        })
        if body.get("code") != 0:
            return {
                "success": False,
                "id": "",
                "message": str(body.get("message") or "建收藏夹失败"),
            }
        data = body.get("data") or {}
        folder_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        return {
            "success": bool(folder_id),
            "id": folder_id,
            "message": "" if folder_id else "服务器没有返回收藏夹 id",
        }

    def add_videos(self, folder_id: str,
                   items: list[dict[str, Any]]) -> dict[str, Any]:
        """Add videos to a folder.  ``items`` carry ``bvid`` and ``aid``."""
        folder = str(folder_id or "").strip()
        if not folder:
            return {"success": False, "count": 0, "message": "收藏夹 id 是空的"}
        media_ids: list[str] = []
        seen: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("aid") or "").strip()
            if not aid:
                continue
            if aid in seen:
                continue
            seen.add(aid)
            # The second number is the content type: 2 = 视频.
            media_ids.append(f"{aid}:2")
        if not media_ids:
            return {"success": True, "count": 0, "message": ""}
        body = self._post("/x/v3/fav/resource/deal", {
            "rid": folder,
            "type": "2",
            "add_media_ids": ",".join(media_ids),
            "platform": "web",
        })
        if body.get("code") != 0:
            return {
                "success": False,
                "count": 0,
                "message": str(body.get("message") or "加收藏失败"),
            }
        return {"success": True, "count": len(media_ids), "message": ""}
