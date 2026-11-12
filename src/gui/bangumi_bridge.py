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
from src.core.bangumi_download import BangumiDownloader, BangumiDownloadQueue
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
        self._queue: BangumiDownloadQueue | None = None
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
        self._queue = None

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

    @property
    def queue(self) -> BangumiDownloadQueue:
        """下载队列。跟着下载器走——换了目录，队列也换。"""
        if self._queue is None:
            self._queue = BangumiDownloadQueue(self.downloader)
        return self._queue

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

    # ------------------------------------------------------------ 批量下载
    def enqueue_download(self, urls: list[dict[str, Any]],
                         *, start: bool = True) -> dict[str, Any]:
        """把一批资源链接放进下载队列。

        每个元素是 ``{"url": ..., "filename": ..., "label": ...}``。
        队列一次只下一个——资源站是第三方站点，不能轰炸。
        """
        items = urls if isinstance(urls, list) else []
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            result = self.queue.enqueue(
                str(item.get("url") or ""),
                str(item.get("filename") or ""),
                str(item.get("label") or ""))
            if result.get("success"):
                added += 1
        if start and added:
            self.queue.start()
        return {"success": True, "added": added, "skipped": len(items) - added}

    def download_queue_status(self) -> dict[str, Any]:
        """队列的状态。"""
        return self.queue.status()

    def clear_download_queue(self) -> dict[str, Any]:
        """清掉已经结束的任务。"""
        return {"success": True, "removed": self.queue.clear()}

    def cancel_download_queue(self) -> dict[str, Any]:
        """停掉整个队列：不再取新任务，当前那个也取消。"""
        return {"success": True, "cancelled": self.queue.cancel()}


    # ------------------------------------------------- 番剧规则播放（Kazumi）

    def _rule_cache_root(self):
        """规则缓存放在配置目录下，跟账号数据分开。"""
        from pathlib import Path

        from src.config.settings import Settings

        return Path(Settings._config_dir()) / "bangumi_rules"

    def _source_fetcher(self, *, override: dict | None = None):
        """按设置造一个取页器。

        跳过校验、自动镜像、请求间隔都从设置读；``override`` 给单次调用覆盖
        （界面上改完设置还没保存就点试用的场景）。
        """
        from src.core.bangumi_source import Fetcher

        cfg = self._cfg()
        override = override or {}

        def pick(name: str, default):
            if name in override and override[name] is not None:
                return override[name]
            value = getattr(cfg, name, default) if cfg is not None else default
            return default if value is None else value

        return Fetcher(
            skip_ssl_verify=bool(pick("skip_ssl_verify", False)),
            auto_mirror=bool(pick("auto_mirror", False)),
            interval=float(pick("request_interval", 0.0) or 0.0),
        )

    def _rule_library(self, *, override: dict | None = None):
        from src.core.bangumi_rules import RuleCache, RuleLibrary

        cfg = self._cfg()
        repos = list(getattr(cfg, "rule_repos", []) or []) if cfg is not None else []
        return RuleLibrary(RuleCache(self._rule_cache_root()), self._source_fetcher(override=override), repos)

    def _active_repo(self, repo: str = "") -> str:
        from src.core.bangumi_rules import KAZUMI_RULES_REPO, normalise_repo_url

        chosen = normalise_repo_url(repo)
        if chosen:
            return chosen
        cfg = self._cfg()
        repos = list(getattr(cfg, "rule_repos", []) or []) if cfg is not None else []
        return normalise_repo_url(repos[0]) if repos else KAZUMI_RULES_REPO

    def _active_rule_name(self, name: str = "") -> str:
        if name:
            return str(name).strip()
        cfg = self._cfg()
        return str(getattr(cfg, "active_rule", "") or "").strip() if cfg is not None else ""

    def _load_rule(self, *, rule_name: str = "", repo: str = "", override: dict | None = None):
        from src.core.bangumi_rules import RuleError

        repo_url = self._active_repo(repo)
        name = self._active_rule_name(rule_name)
        if not name:
            raise RuleError("没有选规则：先去「播放源」里挑一条")
        return self._rule_library(override=override).load_rule(repo_url, name)

    # ------------------------------------------------------------------ 设置

    def source_settings(self) -> dict[str, Any]:
        """规则播放相关的设置，给界面回显。"""
        from src.core.bangumi_rules import KAZUMI_RULES_REPO

        cfg = self._cfg()
        if cfg is None:
            return {"success": False, "error_key": "error.unknown"}
        repos = list(getattr(cfg, "rule_repos", []) or []) or [KAZUMI_RULES_REPO]
        return {
            "success": True,
            "enabled": bool(getattr(cfg, "source_enabled", False)),
            "repos": repos,
            "active_rule": str(getattr(cfg, "active_rule", "") or ""),
            "auto_mirror": bool(getattr(cfg, "auto_mirror", False)),
            "skip_ssl_verify": bool(getattr(cfg, "skip_ssl_verify", False)),
            "stream_patterns": list(getattr(cfg, "stream_patterns", []) or []),
            "default_repo": KAZUMI_RULES_REPO,
        }

    def save_source_settings(self, values: dict | None = None) -> dict[str, Any]:
        """存规则播放的设置。

        只认这里列出的键——界面传什么就写什么的话，一个拼错的键会静默写进
        YAML，下次读出来还在。
        """
        values = values or {}
        if self._settings is None:
            return {"success": False, "error_key": "error.unknown"}

        allowed = {"source_enabled", "auto_mirror", "skip_ssl_verify", "active_rule", "rule_repos"}
        applied: dict[str, Any] = {}
        for key in allowed:
            if key not in values:
                continue
            applied[key] = values[key]

        try:
            self._settings.update_bangumi(**applied)
        except Exception as exc:  # noqa: BLE001 - 保存失败要说出来
            log.warning("bangumi source settings not saved: %s", exc)
            return {"success": False, "error_key": "error.unknown"}

        cfg = self._cfg()
        # 证书校验这个开关同时要作用到媒体服务器那头
        try:
            from src.core.mediaserver import get_media_server

            get_media_server().set_bangumi_ssl_verify(
                not bool(getattr(cfg, "skip_ssl_verify", False)))
        except Exception:  # noqa: BLE001 - 服务器还没起也没关系
            pass

        return self.source_settings()

    # ------------------------------------------------------------------ 规则

    def list_rules(self, repo: str = "", refresh: bool = False) -> dict[str, Any]:
        """列出仓库里的规则。

        ``refresh`` 为真时忽略缓存重新拉。拉失败但磁盘上有旧的就用旧的，
        并且告诉界面这是旧的——比显示"没有规则"强。
        """
        from src.core.bangumi_rules import RuleError

        repo_url = self._active_repo(repo)
        try:
            library = self._rule_library()
            if refresh:
                # 让缓存过期：写一个空索引不划算，直接清掉这个仓库的索引文件
                try:
                    path = library.cache._path(repo_url, "index")
                    path.unlink()
                except OSError:
                    pass
            items, source = library.list_rules(repo_url)
        except RuleError as exc:
            return {"success": False, "error_key": "bili.error_network", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error_key": "error.network", "detail": str(exc)}

        return {
            "success": True,
            "repo": repo_url,
            "items": items,
            "source": source,
            "count": len(items),
        }

    def rule_detail(self, name: str, repo: str = "") -> dict[str, Any]:
        """一条规则的详情，含每个 XPath 能不能编译。"""
        from src.core.bangumi_rules import RuleError

        try:
            rule = self._load_rule(rule_name=name, repo=repo)
        except RuleError as exc:
            return {"success": False, "error_key": "error.unknown", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error_key": "error.network", "detail": str(exc)}

        return {
            "success": True,
            "rule": rule.to_dict(),
            "usable": rule.usable,
            "problems": rule.problems,
        }

    # ------------------------------------------------------------------ 搜索

    def rule_search(self, keyword: str, rule_name: str = "", repo: str = "") -> dict[str, Any]:
        """按规则搜番。"""
        from src.core.bangumi_rules import RuleError
        from src.core.bangumi_source import FetchError, search

        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "error_key": "bangumi.rule_keyword_empty"}

        try:
            rule = self._load_rule(rule_name=rule_name, repo=repo)
            hits = search(rule, keyword, self._source_fetcher())
        except RuleError as exc:
            return {"success": False, "error_key": "bangumi.rule_unusable", "detail": str(exc)}
        except FetchError as exc:
            return {"success": False, "error_key": "error.network", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error_key": "error.network", "detail": str(exc)}

        return {
            "success": True,
            "rule": rule.name,
            "keyword": keyword,
            "items": [hit.to_dict() for hit in hits],
        }

    def rule_chapters(self, url: str, rule_name: str = "", repo: str = "") -> dict[str, Any]:
        """取一部番的线路和剧集。"""
        from src.core.bangumi_rules import RuleError
        from src.core.bangumi_source import FetchError, chapters

        url = str(url or "").strip()
        if not url:
            return {"success": False, "error_key": "error.unknown"}

        try:
            rule = self._load_rule(rule_name=rule_name, repo=repo)
            items = chapters(rule, url, self._source_fetcher())
        except RuleError as exc:
            return {"success": False, "error_key": "bangumi.rule_unusable", "detail": str(exc)}
        except FetchError as exc:
            return {"success": False, "error_key": "error.network", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error_key": "error.network", "detail": str(exc)}

        return {
            "success": True,
            "rule": rule.name,
            "items": [item.to_dict() for item in items],
            "count": len(items),
        }

    def rule_streams(self, url: str, rule_name: str = "", repo: str = "") -> dict[str, Any]:
        """从剧集页找播放地址，并给出可以直接给 <video> 用的本地代理地址。

        找到 0 条是正常结果（这站点的地址在 JS 里拼的），返回空列表而不是错误。
        """

        from src.core.bangumi_rules import RuleError
        from src.core.bangumi_source import FetchError, resolve_streams

        url = str(url or "").strip()
        if not url:
            return {"success": False, "error_key": "error.unknown"}

        cfg = self._cfg()
        patterns = list(getattr(cfg, "stream_patterns", []) or []) if cfg is not None else []

        try:
            rule = self._load_rule(rule_name=rule_name, repo=repo)
            streams = resolve_streams(rule, url, self._source_fetcher(), patterns=patterns or None)
        except RuleError as exc:
            return {"success": False, "error_key": "bangumi.rule_unusable", "detail": str(exc)}
        except FetchError as exc:
            return {"success": False, "error_key": "error.network", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error_key": "error.network", "detail": str(exc)}

        referer = rule.referer or rule.base_url or ""
        items = []
        for stream in streams:
            item = stream.to_dict()
            # 走本地代理：站点要的是详情页 Referer，webview 设不了。
            item["play_url"] = self._bgm_proxy_url(stream.url, referer)
            items.append(item)

        return {
            "success": True,
            "rule": rule.name,
            "items": items,
            "count": len(items),
            "referer": referer,
            "page": url,
        }

    def _bgm_proxy_url(self, url: str, referer: str = "") -> str:
        from urllib.parse import quote

        try:
            from src.core.mediaserver import get_media_server

            base = get_media_server().base_url
        except Exception:  # noqa: BLE001 - 服务器没起就退原始地址
            return url
        suffix = f"?u={quote(url, safe='')}"
        if referer:
            suffix += f"&ref={quote(referer, safe='')}"
        return f"{base}/bgm-media{suffix}"
