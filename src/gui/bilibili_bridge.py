"""Translation layer between the Bilibili client and the desktop frontend.

The core client (:mod:`src.core.bilibili`) speaks the Bilibili web API and
answers with plain lists; the frontend speaks dictionaries.  This module is
the bridge between the two: every method returns ``{"success": True, ...}``
when the call worked and ``{"success": False, "error_key": ..., ...}`` when
it did not, so :mod:`src.gui.bridge` never has to know how Bilibili works.

No request is made here -- the traffic goes through
:class:`src.core.bilibili.BiliClient`, which is injected in the tests.

Two conventions worth knowing:

* ``error_key`` is an i18n key of the existing catalogue, so the frontend can
  render it like any other bridge error.  The raw reason of the core client
  (for example ``api_error``) travels in ``reason``.
* The core client never raises and swallows transport errors into an empty
  list.  An empty ``items`` list therefore means "the endpoint answered
  nothing" -- it cannot be told apart from "the request failed".
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.bilibili import BILI_RID_NAMES, BILI_RIDS, BiliClient

__all__ = ["BilibiliBridge", "BILI_ORDER_ALIASES"]

log = logging.getLogger(__name__)

#: Sort orders the search endpoint understands, keyed by the friendly name
#: the desktop uses.  A value that is not listed here is passed through, so
#: the raw API names (``click``, ``pubdate``, ``dm`` ...) keep working.
BILI_ORDER_ALIASES: dict[str, str] = {
    "default": "totalrank",
    "rank": "totalrank",
    "totalrank": "totalrank",
    "play": "click",
    "click": "click",
    "hot": "click",
    "date": "pubdate",
    "pubdate": "pubdate",
    "new": "pubdate",
    "danmaku": "dm",
    "dm": "dm",
}

#: The core client's own failure codes, mapped onto i18n keys that exist in
#: the catalogue today.  The keys carry no placeholder: the desktop shows an
#: ``error_key`` without interpolating one, so ``error.api`` would end up on
#: the screen as a literal ``{message}``.
_AUDIO_ERROR_KEYS: dict[str, str] = {
    "invalid_bvid": "error.reason.song_not_found",
    "no_cid": "error.reason.song_not_found",
    "network_error": "error.network",
    "api_error": "error.unknown",
    "no_audio": "error.reason.no_url",
}


def _as_int(value: Any, default: int) -> int:
    """``int(value)`` that falls back to ``default`` instead of raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _order_value(order: Any) -> str:
    """The API sort order for a friendly name (``play`` -> ``click``)."""
    text = str(order or "default").strip().lower()
    return BILI_ORDER_ALIASES.get(text, text)


def _category_key(category: Any) -> str | None:
    """The partition key for a name (``music``) or a partition id (``28``).

    ``None`` means the partition is unknown.
    """
    text = str(category if category is not None else "all").strip()
    if not text:
        return "all"
    if text in BILI_RIDS:
        return text
    if text.isdigit():
        for key, rid in BILI_RIDS.items():
            if rid == int(text):
                return key
    return None


class BilibiliBridge:
    """The Bilibili capabilities the desktop frontend can call.

    ``client`` may be replaced in the tests; the default one talks to the
    real (read-only) Bilibili web API.
    """

    def __init__(self, client: BiliClient | None = None) -> None:
        self._client = client if client is not None else BiliClient()

    @property
    def client(self) -> BiliClient:
        """The underlying core client."""
        return self._client

    # ------------------------------------------------------------------ search
    def search(
        self,
        keyword: str,
        category: str = "all",
        order: str = "default",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        """Search Bilibili videos by keyword.

        ``category`` is a partition name (``music``) or a partition id
        (``28``); ``order`` is a friendly sort name (``play``, ``date``) or
        a raw API order.  The answer carries the normalised items of the
        core client.
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "error_key": "player.search_empty", "message": ""}
        category_key = _category_key(category)
        if category_key is None:
            return {
                "success": False,
                "error_key": "error.unknown",
                "message": f"unknown category: {category}",
            }
        api_order = _order_value(order)
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 30), 50))
        try:
            items = self._client.search(
                keyword,
                page=page_number,
                page_size=size,
                order=api_order,
                category=category_key,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili search failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        return {
            "success": True,
            "keyword": keyword,
            "category": category_key,
            "order": api_order,
            "page": page_number,
            "page_size": size,
            "items": items,
            "count": len(items),
        }

    # -------------------------------------------------------------- partitions
    def partitions(self) -> dict[str, Any]:
        """The Bilibili partitions, with the id and the name of each one."""
        items = [
            {
                "rid": rid,
                "name": BILI_RID_NAMES.get(rid, key),
                "key": key,
            }
            for key, rid in BILI_RIDS.items()
        ]
        return {"success": True, "partitions": items, "count": len(items)}

    # ------------------------------------------------------------------ popular
    def popular(self, rid: int = 28, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """The hot list of one partition.

        ``rid=0`` is the site wide hot list; the music partitions (``28``,
        ``29``) are served by the partition feed, because the site wide list
        ignores the partition.
        """
        partition = _as_int(rid, 28)
        if partition != 0 and partition not in set(BILI_RIDS.values()):
            return {
                "success": False,
                "error_key": "error.unknown",
                "message": f"unknown partition: {rid}",
            }
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 30), 50))
        try:
            items = self._client.popular(rid=partition, page=page_number, page_size=size)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili popular failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        return {
            "success": True,
            "rid": partition,
            "page": page_number,
            "page_size": size,
            "items": items,
            "count": len(items),
        }

    # ------------------------------------------------------------------ audio
    def audio_url(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """Resolve the audio stream of one video.

        The core client answers with a failure dictionary instead of raising,
        so the reason is translated into an i18n key and kept in ``reason``.
        """
        video_id = str(bvid or "").strip()
        if not video_id:
            return {
                "success": False,
                "error_key": _AUDIO_ERROR_KEYS["invalid_bvid"],
                "reason": "invalid_bvid",
                "message": "missing bvid",
            }
        try:
            result = self._client.audio_url(video_id, quality=_as_int(quality, 0))
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili audio url failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        if not result.get("success"):
            reason = str(result.get("error_key") or "")
            return {
                "success": False,
                "error_key": _AUDIO_ERROR_KEYS.get(reason, "error.unknown"),
                "reason": reason,
                "message": str(result.get("message") or ""),
            }
        return {
            "success": True,
            "bvid": video_id,
            "url": str(result.get("url") or ""),
            "mime": str(result.get("mime") or ""),
            "quality": _as_int(result.get("quality"), 0),
            "need_referer": bool(result.get("need_referer", True)),
        }
