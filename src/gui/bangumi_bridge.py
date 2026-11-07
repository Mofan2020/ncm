"""Translation layer between the Bangumi client and the desktop frontend.

Same job as :mod:`src.gui.bilibili_bridge`: every method answers with
``{"success": True, ...}`` or ``{"success": False, "error_key": ...}`` so
:mod:`src.gui.bridge` never has to know how Bangumi works.

Two things are special about Bangumi here:

* **There is no account.**  Nothing in this module logs in, sends a cookie
  or writes to a server.  The favourites and the watch history live in
  :class:`src.core.bangumi_store.BangumiStore`, on this machine only.
* **Nothing is fetched eagerly.**  The subject list, the episode list and
  the mirrors are all asked for when the user opens them, never in advance.

The resource links are **not** crawled here.  They are rendered from the
templates the user configured in the settings, which is a pure string
operation -- no request leaves the machine for it.
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.bangumi import (
    BANGUMI_UA,
    BangumiClient,
    render_resource_links,
)
from src.core.bangumi_download import BangumiDownloader
from src.core.bangumi_store import BangumiCache, BangumiStore

__all__ = ["BangumiBridge"]

log = logging.getLogger(__name__)


class BangumiBridge:
    """Everything the frontend may ask Bangumi for."""

    def __init__(
        self,
        client: BangumiClient | None = None,
        store: BangumiStore | None = None,
        cache: BangumiCache | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._cache = cache
        self._downloader: BangumiDownloader | None = None
        self._settings: Any = None

    # ------------------------------------------------------------- wiring
    def set_settings(self, settings: Any) -> None:
        """Hand the settings over so the client can follow the config."""
        self._settings = settings

    def reload(self) -> None:
        """Throw away the client and the downloader.

        The host, the request interval and the download directory all come
        from the settings, so after the user changes them the old objects
        would keep using the old values.
        """
        self._client = None
        self._downloader = None

    def _cfg(self) -> Any:
        if self._settings is not None:
            return getattr(self._settings, "bangumi", None)
        try:
            from src.config.settings import get_settings

            return getattr(get_settings(), "bangumi", None)
        except Exception:  # noqa: BLE001 - 没有设置就按默认来
            return None

    @property
    def store(self) -> BangumiStore:
        if self._store is None:
            self._store = BangumiStore()
        return self._store

    @property
    def cache(self) -> BangumiCache:
        if self._cache is None:
            self._cache = BangumiCache()
        return self._cache

    @property
    def client(self) -> BangumiClient:
        if self._client is None:
            cfg = self._cfg()
            host = str(getattr(cfg, "api_host", "") or "api.bangumi.lat")
            interval = float(getattr(cfg, "request_interval", 1.0) or 1.0)
            verify = not bool(getattr(cfg, "skip_ssl_verify", False))
            self._client = BangumiClient(
                f"https://{host}", min_interval=interval, verify=verify)
        return self._client

    @property
    def downloader(self) -> BangumiDownloader:
        """下载器。目录跟着设置走，所以设置改了之后要重建。"""
        if self._downloader is None:
            cfg = self._cfg()
            directory = str(getattr(cfg, "download_dir", "") or "").strip()
            self._downloader = BangumiDownloader(directory or None)
        return self._downloader

    def _resource_sites(self) -> list[dict[str, Any]]:
        cfg = self._cfg()
        sites = getattr(cfg, "resource_sites", None)
        if isinstance(sites, list) and sites:
            return sites
        from src.core.bangumi import DEFAULT_RESOURCE_SITES

        return list(DEFAULT_RESOURCE_SITES)

    # -------------------------------------------------------------- 搜索
    def search(self, keyword: str, *, limit: int = 20,
               subject_type: int = 2) -> dict[str, Any]:
        """搜条目。结果缓存 10 分钟。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": True, "items": [], "keyword": keyword}
        key = f"{keyword}|{int(subject_type)}|{int(limit)}"

        def produce() -> list[dict[str, Any]]:
            return self.client.search(keyword, subject_type=subject_type,
                                      limit=limit)

        items = self.cache.remember("search", key, produce) or []
        return {"success": True, "keyword": keyword, "items": items}

    # -------------------------------------------------------------- 详情
    def subject(self, subject_id: int | str, *,
                refresh: bool = False) -> dict[str, Any]:
        """条目详情。缓存 1 小时，``refresh=True`` 绕过缓存。"""
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}
        if refresh:
            self.cache.discard("subject", sid)

        def produce() -> dict[str, Any] | None:
            return self.client.subject(sid)

        data = self.cache.remember("subject", sid, produce)
        if not data:
            return {"success": False, "error_key": "bangumi.error.not_found"}
        data = dict(data)
        data["favorite"] = self.store.is_favorite(sid)
        data["history"] = self.store.history_for(sid)
        return {"success": True, "subject": data}

    # -------------------------------------------------------------- 选集
    def episodes(self, subject_id: int | str, *,
                 episode_type: int | None = None,
                 refresh: bool = False) -> dict[str, Any]:
        """章节列表。缓存 1 小时。

        ``episode_type``：0=正片，1=片头，2=片尾，3=OVA，4=特别篇。
        """
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}
        tkey = "all" if episode_type is None else str(int(episode_type))
        ckey = f"{sid}|{tkey}"
        if refresh:
            self.cache.discard("episodes", ckey)

        def produce() -> list[dict[str, Any]]:
            return self.client.episodes(sid, episode_type=episode_type)

        items = self.cache.remember("episodes", ckey, produce) or []
        history = self.store.history_for(sid)
        return {
            "success": True,
            "subject_id": sid,
            "episodes": items,
            "history": history,
        }

    # ------------------------------------------------------ 资源站链接
    def resource_links(self, subject_id: int | str, *,
                       episode_id: int | str | None = None) -> dict[str, Any]:
        """按用户配的模板生成下载链接。

        这一步**不请求任何站点**——只是把 ``{keyword}`` 之类的占位符填上。
        真正的抓取是下载那一步的事。
        """
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}

        def produce() -> dict[str, Any] | None:
            return self.client.subject(sid)

        subject = self.cache.remember("subject", sid, produce) or {}
        episode: dict[str, Any] = {}
        if episode_id not in (None, ""):
            eps = self.cache.remember(
                "episodes", f"{sid}|all",
                lambda: self.client.episodes(sid)) or []
            for item in eps:
                if str(item.get("id")) == str(episode_id):
                    episode = item
                    break

        links = render_resource_links(
            self._resource_sites(), subject=subject, episode=episode)
        return {
            "success": True,
            "subject_id": sid,
            "links": links,
            "episode": episode,
        }

    # ------------------------------------------------------ 本地收藏
    def favorites(self) -> dict[str, Any]:
        """本地收藏。"""
        items = self.store.favorites()
        return {"success": True, "items": items, "count": len(items)}

    def set_favorite(self, subject_id: int | str, favorite: bool,
                     meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """收藏 / 取消收藏。**只写本地。**"""
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}
        if favorite and not meta:
            # 收藏时顺手记下名字和封面，列表里就不用再查一次。
            cached = self.cache.get("subject", sid)
            if isinstance(cached, dict):
                meta = {
                    "name": cached.get("name") or "",
                    "name_cn": cached.get("name_cn") or "",
                    "cover": (cached.get("images") or {}).get("common") or "",
                    "total_episodes": cached.get("total_episodes"),
                }
        state = self.store.set_favorite(sid, bool(favorite), meta)
        return {"success": True, "subject_id": sid, "favorite": state}

    def toggle_favorite(self, subject_id: int | str,
                        meta: dict[str, Any] | None = None) -> dict[str, Any]:
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}
        return self.set_favorite(sid, not self.store.is_favorite(sid), meta)

    # ------------------------------------------------------ 本地观看历史
    def history(self) -> dict[str, Any]:
        """本地观看历史。"""
        items = self.store.history()
        return {"success": True, "items": items, "count": len(items)}

    def record_history(self, subject_id: int | str, episode_id: int | str,
                       meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """记一条观看历史。**只写本地。**"""
        sid = str(subject_id or "").strip()
        eid = str(episode_id or "").strip()
        if not sid or not eid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}
        if not meta:
            eps = self.cache.remember(
                "episodes", f"{sid}|all",
                lambda: self.client.episodes(sid)) or []
            subject = self.cache.remember(
                "subject", sid, lambda: self.client.subject(sid)) or {}
            for item in eps:
                if str(item.get("id")) == eid:
                    meta = {
                        "ep": item.get("ep") or item.get("sort") or 0,
                        "name": item.get("name") or "",
                        "name_cn": item.get("name_cn") or "",
                        "cover": (subject.get("images") or {}).get("common") or "",
                    }
                    break
        ok = self.store.record_episode(sid, eid, meta)
        return {"success": ok, "subject_id": sid, "episode_id": eid}

    def clear_history(self) -> dict[str, Any]:
        """清空观看历史。收藏不动。"""
        count = self.store.clear_history()
        return {"success": True, "removed": count}

    # ------------------------------------------------------------ 镜像
    def mirrors(self, *, refresh: bool = False) -> dict[str, Any]:
        """镜像清单。

        顺序即默认顺序，第一个就是当前用的。用户可以自己加、可以调顺序。
        """
        cfg = self._cfg()
        configured = getattr(cfg, "mirrors", None)
        if isinstance(configured, list) and configured:
            if not refresh:
                return {"success": True, "mirrors": configured,
                        "source": "settings"}

        key = "all"
        if refresh:
            self.cache.discard("mirrors", key)

        def produce() -> list[dict[str, Any]]:
            fetched = BangumiClient.fetch_mirrors(
                verify=not bool(getattr(cfg, "skip_ssl_verify", False)))
            return fetched or BangumiClient.built_in_mirrors()

        mirrors = self.cache.remember("mirrors", key, produce) or []
        return {"success": True, "mirrors": mirrors, "source": "remote"}

    def built_in_mirrors(self) -> dict[str, Any]:
        """内置镜像清单。"""
        return {"success": True, "mirrors": BangumiClient.built_in_mirrors()}

    def current_host(self) -> dict[str, Any]:
        """当前用的 API 主机。"""
        cfg = self._cfg()
        return {
            "success": True,
            "api_host": str(getattr(cfg, "api_host", "") or "api.bangumi.lat"),
            "web_host": str(getattr(cfg, "web_host", "") or "bangumi.lat"),
            "user_agent": BANGUMI_UA,
        }

    # -------------------------------------------------------------- 下载
    def download_dir(self) -> dict[str, Any]:
        """下载落在哪儿。"""
        return {"success": True, "path": str(self.downloader.download_dir)}

    def download_url(self, url: str, filename: str = "") -> dict[str, Any]:
        """把一个资源链接存成文件。

        链接来自用户配的资源站模板——那些站点给出直链的时候，这个就能把
        文件拉下来。给的是网页链接的话，存下来的会是那页 HTML。
        """
        result = self.downloader.save_url(url, filename)
        if result.get("success"):
            return {"success": True, "path": result.get("path"),
                    "size": result.get("size", 0), "url": url}
        return {"success": False, "url": url, "reason": result.get("error", ""),
                "error_key": "bangumi.error.download_failed"}

    def cancel_download(self) -> dict[str, Any]:
        """取消正在下的那个。"""
        self.downloader.cancel()
        return {"success": True}

    def export_subject_text(self, subject_id: int | str,
                            filename: str = "") -> dict[str, Any]:
        """把条目资料和章节表导出成 txt。离线可用，不发请求以外的事。"""
        sid = str(subject_id or "").strip()
        if not sid:
            return {"success": False, "error_key": "bangumi.error.no_subject"}

        def produce() -> dict[str, Any] | None:
            return self.client.subject(sid)

        subject = self.cache.remember("subject", sid, produce) or {}
        if not subject:
            return {"success": False, "error_key": "bangumi.error.not_found"}
        episodes = self.cache.remember(
            "episodes", f"{sid}|all", lambda: self.client.episodes(sid)) or []
        result = self.downloader.export_subject_text(
            subject, episodes, filename=filename)
        if result.get("success"):
            return {"success": True, "path": result.get("path"),
                    "size": result.get("size", 0)}
        return {"success": False, "reason": result.get("error", ""),
                "error_key": "bangumi.error.download_failed"}
