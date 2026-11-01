"""B站内容的本地缓存。

网易云那边的歌单、歌曲信息都缓存在 :mod:`src.core.metacache` 里，B 站用同一套
存储（按 ``kind`` 分开），只是各类的过期时间不一样：

======================  ========  ==========================================
内容                    过期时间   为什么
======================  ========  ==========================================
搜索结果、热门、榜单      10 分钟   变动很慢，重复搜同样的词没必要再问一次
视频详情（标题、cid）      60 分钟   标题和封面几乎不变
弹幕                    24 小时   视频发出来之后弹幕就定了
评论                      5 分钟   有人来回刷，缓存久了会看到旧评论
个人主页资料              30 分钟   昵称和签名改得不勤，粉丝数差几十无所谓
个人主页投稿              15 分钟
收藏夹列表                5 分钟   收藏和取消收藏之后必须马上失效
视频的统计数字            5 分钟   点赞投币之后要立刻变
播放地址                  10 分钟   地址带签名，缓存太久会 403
关注状态                  5 分钟
======================  ========  ==========================================

**不缓存**的：观看历史、私信、未读数、硬币余额、账号信息——这些东西要的是实时。

写操作（点赞、投币、收藏、关注、发评论、发弹幕）之后要主动把对应的条目扔掉，
否则用户会看到「点完还是原来的数字」。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.core.metacache import get_meta_cache

__all__ = ["BiliCache", "BILI_TTL"]

log = logging.getLogger(__name__)

#: 每类内容的过期时间（秒）。
BILI_TTL: dict[str, float] = {
    "search": 600.0,          # 搜索结果
    "popular": 600.0,         # 热门 / 榜单
    "video": 3600.0,          # 视频详情
    "danmaku": 86400.0,       # 弹幕
    "comments": 300.0,        # 评论
    "space": 1800.0,          # 个人主页资料
    "space_videos": 900.0,    # 个人主页投稿
    "folders": 300.0,         # 收藏夹列表
    "folder_items": 300.0,    # 收藏夹里的内容
    "stat": 300.0,            # 视频统计数字
    "streams": 600.0,         # 播放地址
    "follow": 300.0,          # 关注状态
}

#: 缓存的存放目录名（和网易云的分开，清缓存时可以分开清）。
KIND_PREFIX = "bili_"


class BiliCache:
    """The Bilibili side of the on-disk cache."""

    def __init__(self, cache: Any = None) -> None:
        self._cache = cache if cache is not None else get_meta_cache()

    # ------------------------------------------------------------------ 基础
    def get(self, kind: str, key: str) -> Any | None:
        """The cached value, or ``None``."""
        try:
            return self._cache.get(KIND_PREFIX + kind, key)
        except Exception as exc:  # pragma: no cover - the cache is optional
            log.debug("bilibili cache read failed (%s): %s", kind, exc)
            return None

    def set(self, kind: str, key: str, value: Any) -> None:
        """Store a value.  Failures are ignored: the cache is optional."""
        ttl = BILI_TTL.get(kind, 300.0)
        try:
            self._cache.set(KIND_PREFIX + kind, key, value, ttl)
        except Exception as exc:  # pragma: no cover - the cache is optional
            log.debug("bilibili cache write failed (%s): %s", kind, exc)

    def discard(self, kind: str, key: str) -> None:
        """Drop one entry."""
        try:
            self._cache.discard(KIND_PREFIX + kind, key)
        except Exception as exc:  # pragma: no cover
            log.debug("bilibili cache discard failed (%s): %s", kind, exc)

    def discard_prefix(self, kind: str, prefix: str) -> None:
        """Drop every entry of ``kind`` whose key starts with ``prefix``."""
        try:
            self._cache.discard_prefix(KIND_PREFIX + kind, prefix)
        except Exception as exc:  # pragma: no cover
            log.debug("bilibili cache discard failed (%s): %s", kind, exc)

    def clear(self, kind: str | None = None) -> None:
        """Drop every Bilibili entry (or only one kind)."""
        try:
            self._cache.clear(KIND_PREFIX + kind if kind else None)
        except Exception as exc:  # pragma: no cover
            log.debug("bilibili cache clear failed: %s", exc)

    # ------------------------------------------------------------ 读：缓存优先
    def remember(self, kind: str, key: str,
                 loader: Callable[[], Any]) -> tuple[Any, bool]:
        """Return the cached value, or load it and remember it.

        The second element tells whether the value came from the disk.
        """
        cached = self.get(kind, key)
        if cached is not None:
            return cached, True
        value = loader()
        if value:
            self.set(kind, key, value)
        return value, False

    # ---------------------------------------------------------------- 失效处理
    def invalidate_video(self, bvid: str) -> None:
        """A video changed: its counters, comments and danmaku may be stale.

        The comments and the danmaku are keyed by ``<bvid>|<something>``,
        so they are dropped by prefix -- the cid of the danmaku is not known
        here.
        """
        bvid = str(bvid or "")
        if not bvid:
            return
        self.discard("stat", bvid)
        self.discard_prefix("comments", f"{bvid}|")
        self.discard_prefix("danmaku", f"{bvid}|")

    def invalidate_folders(self) -> None:
        """The favourite folders changed."""
        self.discard_prefix("folders", "")
        self.discard_prefix("folder_items", "")

    def invalidate_space(self, mid: str) -> None:
        """An uploader's profile changed."""
        self.discard("space", str(mid or ""))
        self.discard_prefix("space_videos", str(mid or ""))

    def invalidate_follow(self, mid: str) -> None:
        """The follow state changed."""
        self.discard("follow", str(mid or ""))
