"""Bangumi (番组计划) 条目库客户端。

Bangumi 是一个 ACG 资料库：条目资料、评分、收藏统计、章节（剧集）列表都在
那里，但它**不托管视频**。视频要靠条目网页上的「资源」区给出的外部链接
（磁力 / 网盘 / 直链）。

这个模块只做资料库那一半：

* 搜索条目 ``GET /v0/subjects?keyword=&type=``
* 条目详情 ``GET /v0/subjects/{id}``
* 章节列表 ``GET /v0/episodes?subject_id=``
* 官方镜像列表 ``GET https://bangumi.wiki/api/v1/mirrors``

**没有账号。** Bangumi 支持登录，但这里不碰——收藏和观看历史只存在本地
（见 :mod:`src.core.bangumi_store`），任何请求都不带 cookie。

**访问与镜像。** 官方域名（``bangumi.tv`` / ``bgm.tv`` / ``chii.in``）在大陆
直连基本不通，DNS 还常被污染成别的段的地址。真正好用的是 ``bangumi.lat``
这一家镜像，它把 API 挂在 ``api.bangumi.lat`` 上，而且**没有反爬**。镜像
列表从官方发布页 ``bangumi.wiki`` 拉，那个页面本身也提供 JSON 接口，用户
可以在设置里自己添镜像、调顺序。

**不要不停地发请求。** 搜索和章节会被缓存（见
:mod:`src.core.bangumi_cache`），而且这里带一个请求节流器：同一个
:class:`BangumiClient` 的两次调用之间至少隔 ``min_interval`` 秒。抓资源的
时候更要命——那是别人的服务器。

客户端和 B 站那边一个规矩：**从不给调用方抛异常**。列表端点返回 ``[]``，
详情端点返回失败字典，UI 层不用去接一个转圈的异常。
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Any

import requests

__all__ = [
    "BangumiClient",
    "EPISODE_TYPE_NAMES",
    "BANGUMI_OFFICIAL_HOSTS",
    "BANGUMI_MIRROR_HOSTS",
    "BANGUMI_WIKI_MIRRORS",
    "BANGUMI_UA",
    "DEFAULT_RESOURCE_SITES",
    "render_resource_links",
]

log = logging.getLogger(__name__)

BANGUMI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 官方域名。境外直连用户走这里，国内用户基本不通。
BANGUMI_OFFICIAL_HOSTS: tuple[str, ...] = (
    "api.bgm.tv",
    "bangumi.tv",
    "bgm.tv",
    "chii.in",
)

#: 已知的第三方镜像。``api`` 是 API 主机，``main`` 是网页主机，
#: ``lain`` 是图床主机。镜像只保证 API 可用，网页主站可能挂反爬。
BANGUMI_MIRROR_HOSTS: tuple[str, ...] = (
    "api.bangumi.lat",
    "bangumi.lat",
    "www.bangumi.lat",
    "next.bangumi.lat",
    "lain.bangumi.lat",
    "doujin.bangumi.lat",
)

#: 官方镜像发布页的 JSON 接口——官方自己维护的镜像清单。
BANGUMI_WIKI_MIRRORS = "https://bangumi.wiki/api/v1/mirrors"

#: 资源站模板的默认值。用户可以在设置里改，也可以整个换成自己的站。
#: 模板里可以用的占位符：``{name}``（中文名）、``{name_cn}``、``{name_original}``、
#: ``{keyword}``、``{ep}``、``{title}``、``{subject_id}``。
DEFAULT_RESOURCE_SITES: list[dict[str, Any]] = [
    {
        "id": "bangumi-lat",
        "name": "Bangumi 镜像",
        "kind": "bangumi",
        "enabled": True,
        "search_url": "",
        "detail_url": "https://bangumi.lat/subject/{subject_id}",
        "note": "条目资料和章节从这里取，官网有反爬，下载链接可能取不到",
    },
]


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class BangumiClient:
    """只读地读 Bangumi 的条目资料。

    ``base_url`` 指向 API 主机，默认走 ``api.bangumi.lat``——它没有反爬。
    ``session`` 可以外部传进来（测试用假 session）。``min_interval`` 是两次
    请求之间的最小间隔，用来给自己和别人省点流量。
    """

    def __init__(
        self,
        base_url: str = "https://api.bangumi.lat",
        *,
        session: Any = None,
        min_interval: float = 1.0,
        timeout: float = 15.0,
        verify: bool = True,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self.verify = bool(verify)
        self.min_interval = float(max(0.0, min_interval))
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": BANGUMI_UA,
            "Accept": "application/json",
        })
        self._lock = threading.Lock()
        self._last_request = 0.0

    # ------------------------------------------------------------- plumbing
    def _wait_turn(self) -> None:
        """让出间隔。调用方已经拿着锁，所以两次请求不会撞在一起。"""
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        gap = now - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """打一个 GET。失败返回 ``None``，不抛。"""
        url = f"{self.base_url}{path}"
        with self._lock:
            self._wait_turn()
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.timeout, verify=self.verify
                )
            except Exception as exc:  # noqa: BLE001 - 网络错误一律降级
                log.warning("bangumi 请求失败 %s: %s", url, exc)
                return None
            if resp.status_code != 200:
                log.warning("bangumi %s 返回 %s", url, resp.status_code)
                return None
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("bangumi 返回不是 JSON %s: %s", url, exc)
                return None

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        """打一个 POST。失败返回 ``None``，不抛。

        搜索走的就是它——``GET /v0/subjects?keyword=`` 那个 keyword 参数
        是不起作用的，必须 POST 一个 JSON body 过去。
        """
        url = f"{self.base_url}{path}"
        with self._lock:
            self._wait_turn()
            try:
                resp = self.session.post(
                    url, json=body, timeout=self.timeout, verify=self.verify)
            except Exception as exc:  # noqa: BLE001 - 网络错误一律降级
                log.warning("bangumi 请求失败 %s: %s", url, exc)
                return None
            if resp.status_code != 200:
                log.warning("bangumi %s 返回 %s", url, resp.status_code)
                return None
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("bangumi 返回不是 JSON %s: %s", url, exc)
                return None

    # -------------------------------------------------------------- 资料库
    def search(
        self,
        keyword: str,
        *,
        subject_type: int = 2,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """搜条目。``subject_type``：2=动画，1=书籍，4=游戏，6=音乐，3=real。

        **这里用的是 ``POST /v0/search/subjects``**，不是 ``GET
        /v0/subjects?keyword=``。后者那个 keyword 参数**根本不起作用**——
        它无视关键词，按条目 ID 顺序返回一串无关的东西（搜「芙莉莲」会
        给你「鲁路修」）。搜索要走 POST，body 是 JSON：

        .. code-block:: json

            {"keyword": "芙莉莲", "filter": {"type": [2]}}

        不给 filter 的话会跨类型返回——同一句话会连书籍、音乐一起出来。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        body: dict[str, Any] = {"keyword": keyword}
        if subject_type:
            body["filter"] = {"type": [int(subject_type)]}
        payload = self._post("/v0/search/subjects", body)
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if offset:
                offset -= 1
                continue
            out.append(self._normalize_subject(item))
            if len(out) >= max(1, min(int(limit), 100)):
                break
        return out

    def subject(self, subject_id: int | str) -> dict[str, Any] | None:
        """条目详情。"""
        try:
            sid = int(subject_id)
        except (TypeError, ValueError):
            return None
        payload = self._get(f"/v0/subjects/{sid}")
        if not isinstance(payload, dict) or payload.get("code") not in (None, 200):
            return None
        if not payload.get("id"):
            return None
        return self._normalize_subject(payload)

    def episodes(self, subject_id: int | str, *, episode_type: int | None = None,
                 limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """章节（剧集）列表。

        ``episode_type``：0=正片，1=片头，2=片尾，3=OVA，4=特别篇。
        不给就全要。

        **排序按类型分组**：正片（type=0）排最前，然后片头、片尾、特别篇。
        接口返回的是按 ``sort`` 混在一起的——片头片的 ``sort`` 也是 1、2、3，
        只按 ``sort`` 排会让那些没有标题的条目插到正片中间去。

        每条还带一个 ``label``：接口给的 ``name_cn`` 常常是空的（片头片尾
        特别篇都没有标题），这时候用「第 N 集」或者类型名顶上，前端就能
        直接显示，不用自己拼。
        """
        try:
            sid = int(subject_id)
        except (TypeError, ValueError):
            return []
        params: dict[str, Any] = {
            "subject_id": sid,
            "limit": max(1, min(int(limit), 500)),
            "offset": max(0, int(offset)),
        }
        if episode_type is not None:
            params["type"] = int(episode_type)
        payload = self._get("/v0/episodes", params)
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            etype = item.get("type")
            try:
                etype = int(etype)
            except (TypeError, ValueError):
                etype = 0
            ep = item.get("ep")
            name_cn = item.get("name_cn") or ""
            sort_value = item.get("sort")
            out.append({
                "id": item.get("id"),
                "subject_id": item.get("subject_id"),
                "ep": ep,
                "sort": sort_value,
                "type": etype,
                "type_name": EPISODE_TYPE_NAMES.get(etype, ""),
                "disc": item.get("disc"),
                "name": item.get("name") or "",
                "name_cn": name_cn,
                "label": name_cn or _episode_label(etype, ep, sort_value),
                "desc": item.get("desc") or "",
                "duration": item.get("duration") or "",
                "duration_seconds": item.get("duration_seconds"),
                "airdate": item.get("airdate") or "",
                "comment": item.get("comment"),
            })
        # 先按类型分组（正片在前），组内再按 sort / ep。
        out.sort(key=lambda e: (e.get("type") or 0, e.get("sort") or 0,
                                e.get("ep") or 0))
        return out

    def calendar(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """每日放送。接口在部分镜像上不一定有，失败就返回空。"""
        payload = self._get("/v0/calendar", {
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
        })
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [self._normalize_subject(item) for item in data
                if isinstance(item, dict)]

    # ------------------------------------------------------------- 镜像管理
    @staticmethod
    def fetch_mirrors(session: Any = None, *,
                      timeout: float = 15.0,
                      verify: bool = True) -> list[dict[str, Any]]:
        """从官方发布页拉镜像清单。

        返回 ``[{"id", "name", "url", "api_hosts", "main_hosts", "recommended"}]``。
        拉不到就返回空列表（调用方回退到内置的）。
        """
        sess = session or requests
        try:
            resp = sess.get(BANGUMI_WIKI_MIRRORS, timeout=timeout, verify=verify,
                            headers={"User-Agent": BANGUMI_UA})
        except Exception as exc:  # noqa: BLE001
            log.warning("拉取 bangumi 镜像列表失败: %s", exc)
            return []
        if getattr(resp, "status_code", 0) != 200:
            return []
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return []
        mirrors = payload.get("mirrors") if isinstance(payload, dict) else None
        if not isinstance(mirrors, list):
            return []
        out: list[dict[str, Any]] = []
        for raw in mirrors:
            if not isinstance(raw, dict):
                continue
            services = raw.get("services")
            api_hosts: list[str] = []
            main_hosts: list[str] = []
            if isinstance(services, list):
                for svc in services:
                    if not isinstance(svc, dict):
                        continue
                    hosts = [str(h).lower() for h in (svc.get("hosts") or [])
                             if isinstance(h, str)]
                    sid = str(svc.get("id") or "")
                    if sid == "api":
                        api_hosts.extend(hosts)
                    elif sid in ("main", "next"):
                        main_hosts.extend(hosts)
            out.append({
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or raw.get("id") or ""),
                "url": str(raw.get("url") or ""),
                "api_hosts": api_hosts,
                "main_hosts": main_hosts,
                "recommended": bool(raw.get("recommended")),
                "note": str(raw.get("note") or ""),
            })
        return out

    @staticmethod
    def built_in_mirrors() -> list[dict[str, Any]]:
        """内置镜像。顺序即默认顺序。"""
        return [
            {
                "id": "bangumi-lat",
                "name": "bangumi.lat（API 可用）",
                "url": "https://bangumi.lat",
                "api_hosts": ["api.bangumi.lat"],
                "main_hosts": ["bangumi.lat", "www.bangumi.lat"],
                "recommended": True,
                "note": "本站维护的镜像，API 无反爬",
            },
            {
                "id": "official-tv",
                "name": "bangumi.tv（官方）",
                "url": "https://bangumi.tv",
                "api_hosts": ["api.bgm.tv"],
                "main_hosts": ["bangumi.tv", "www.bangumi.tv"],
                "recommended": False,
                "note": "官方域名，境外直连可用",
            },
            {
                "id": "official-bgm",
                "name": "bgm.tv（官方）",
                "url": "https://bgm.tv",
                "api_hosts": ["api.bgm.tv"],
                "main_hosts": ["bgm.tv", "www.bgm.tv"],
                "recommended": False,
                "note": "官方域名，境外直连可用",
            },
            {
                "id": "official-chii",
                "name": "chii.in（官方）",
                "url": "https://chii.in",
                "api_hosts": ["api.bgm.tv"],
                "main_hosts": ["chii.in", "www.chii.in"],
                "recommended": False,
                "note": "官方域名，境外直连可用",
            },
        ]

    # ---------------------------------------------------------- 归一化
    @staticmethod
    def _normalize_subject(raw: dict[str, Any]) -> dict[str, Any]:
        """把两种深度的条目（列表里的简版 / 详情里的完整版）压成同一个形状。

        搜索结果里没有 ``summary`` / ``rating``，详情里有。UI 层不想处理两套
        字段，就在这里补齐。
        """
        images = raw.get("images") or {}
        if not isinstance(images, dict):
            images = {}
        rating = raw.get("rating") or {}
        if not isinstance(rating, dict):
            rating = {}
        collection = raw.get("collection") or {}
        if not isinstance(collection, dict):
            collection = {}
        infobox = raw.get("infobox") or {}
        if not isinstance(infobox, dict):
            infobox = {}
        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        meta_tags = raw.get("meta_tags") or []
        if not isinstance(meta_tags, list):
            meta_tags = []
        return {
            "id": raw.get("id"),
            "name": raw.get("name") or "",
            "name_cn": raw.get("name_cn") or "",
            "type": raw.get("type"),
            "type_name": _type_name(raw.get("type")),
            "platform": raw.get("platform") or "",
            "date": raw.get("date") or "",
            "summary": raw.get("summary") or "",
            "images": {
                "large": images.get("large") or images.get("common") or "",
                "common": images.get("common") or images.get("medium") or "",
                "medium": images.get("medium") or "",
                "small": images.get("small") or "",
                "grid": images.get("grid") or "",
            },
            "rating_score": rating.get("score"),
            "rating_count": rating.get("count"),
            "rating_total": rating.get("total"),
            "collection": {
                "wish": collection.get("wish"),
                "collect": collection.get("collect"),
                "doing": collection.get("doing"),
                "on_hold": collection.get("on_hold"),
                "dropped": collection.get("dropped"),
            },
            "total_episodes": raw.get("total_episodes"),
            "episodes": raw.get("episodes"),
            "volumes": raw.get("volumes"),
            "locked": bool(raw.get("locked")),
            "nsfw": bool(raw.get("nsfw")),
            "meta_tags": [str(t) for t in meta_tags][:12],
            "tags": [
                {"name": str(t.get("name") or ""), "count": t.get("count")}
                for t in tags
                if isinstance(t, dict) and t.get("name")
            ][:20],
            "infobox": {str(k): v for k, v in infobox.items()},
        }


_TYPE_NAMES = {1: "书籍", 2: "动画", 3: "真人剧", 4: "游戏", 5: "音乐",
               6: "音乐", 7: "書籍"}

#: 章节类型。0 是正片，其余是片头片尾这类附属内容。
EPISODE_TYPE_NAMES = {
    0: "正片",
    1: "片头",
    2: "片尾",
    3: "特别篇",
    4: "OVA",
}


def _episode_label(etype: int, ep: Any, sort: Any) -> str:
    """给没有标题的章节一个能显示的名字。

    正片用集数（「第 3 集」），片头片尾特别篇这类没有集数的，用类型名
    加上序号（「特别篇 2」）。
    """
    try:
        number = int(ep)
    except (TypeError, ValueError):
        number = 0
    if etype == 0 and number > 0:
        return f"第 {number} 集"
    try:
        ordinal = int(sort)
    except (TypeError, ValueError):
        ordinal = 0
    name = EPISODE_TYPE_NAMES.get(etype, "章节")
    return f"{name} {ordinal}" if ordinal else name


def _type_name(value: Any) -> str:
    try:
        return _TYPE_NAMES.get(int(value), "")
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------- 资源站模板
def render_resource_links(
    sites: list[dict[str, Any]] | None,
    *,
    subject: dict[str, Any] | None = None,
    episode: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按用户配的资源站模板生成下载链接。

    模板里的占位符：

    ``{name}`` 条目原名（英文/日文）
    ``{name_cn}`` 中文名
    ``{keyword}`` 搜索用的关键词（中文名优先，没有就用原名）
    ``{ep}`` 集数
    ``{title}`` 集标题
    ``{subject_id}`` 条目 ID

    ``sites`` 里每一项要有 ``detail_url``（条目页）和可选的 ``search_url``。
    模板渲染不出来（缺占位符对应的值）就跳过这一项，不报错。

    这是纯字符串拼接——**不会**去请求那些站点。请求由下载那一步做，
    而且那里有节流和失败重试。
    """
    out: list[dict[str, Any]] = []
    if not sites:
        return out
    subject = subject or {}
    episode = episode or {}
    name = str(subject.get("name") or "")
    name_cn = str(subject.get("name_cn") or "")
    keyword = name_cn or name
    fields = {
        "name": name,
        "name_cn": name_cn,
        "keyword": keyword,
        "ep": str(episode.get("ep") or ""),
        "title": str(episode.get("name_cn") or episode.get("name") or ""),
        "subject_id": str(subject.get("id") or ""),
    }
    for site in sites:
        if not isinstance(site, dict):
            continue
        if not site.get("enabled", True):
            continue
        try:
            detail = _render_template(str(site.get("detail_url") or ""), fields)
            search = _render_template(str(site.get("search_url") or ""), fields)
        except Exception:  # noqa: BLE001 - 模板坏了就跳过
            continue
        if not detail and not search:
            continue
        out.append({
            "id": str(site.get("id") or ""),
            "name": str(site.get("name") or site.get("id") or ""),
            "kind": str(site.get("kind") or ""),
            "detail_url": detail,
            "search_url": search,
            "note": str(site.get("note") or ""),
        })
    return out


def _render_template(template: str, fields: dict[str, str]) -> str:
    """渲染 ``{name}`` 这样的占位符。缺值就整条作废。"""
    if not template:
        return ""
    out = template
    for key, value in fields.items():
        token = "{" + key + "}"
        if token not in out:
            continue
        if value == "":
            # 缺值会让 URL 变成半截的，比没有更糟。
            return ""
        out = out.replace(token, urllib.parse.quote(value, safe=""))
    if "{" in out and "}" in out:
        # 还剩下没处理过的占位符
        return ""
    return out
