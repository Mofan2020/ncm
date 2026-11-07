"""Bangumi 的本地收藏和观看历史。

**只存在本地。** Bangumi 有账号体系，能把收藏和进度同步到云端，但这里一个
账号都不碰——不登录、不发 cookie、不写云端。所有东西都是本机的一份 JSON。

这跟 B 站那边的 ``library.json`` 是两套独立的数据：网易云的收藏和 B 站的
收藏互不相干，Bangumi 的也是。各存各的文件，ncmbk 备份的时候各导各的段。

======================  ========  ==========================================
内容                    过期时间   为什么
======================  ========  ==========================================
搜索结果                 10 分钟   变动很慢，重复搜同一个词没必要再问
条目详情                 60 分钟   标题、封面、简介几乎不变
章节列表                 60 分钟   集数表定了就不改
每日放送                 10 分钟
======================  ========  ==========================================

收藏和观看历史**不缓存**——它们是本地状态，要的是实时。

**不要不停地发请求。** 搜索和章节都走缓存，而且客户端自带节流。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["BangumiStore", "BangumiCache", "BANGUMI_TTL"]

log = logging.getLogger(__name__)

#: 每类内容的过期时间（秒）。
BANGUMI_TTL: dict[str, float] = {
    "search": 600.0,     # 搜索结果
    "subject": 3600.0,   # 条目详情
    "episodes": 3600.0,  # 章节列表
    "calendar": 600.0,   # 每日放送
}

#: 收藏和观看历史各留多少条。
MAX_FAVORITES = 500
MAX_HISTORY = 1000

#: 空结果的标记。底下的缓存不存空值（空列表、空字典一律跳过），
#: 所以空结果包一层再存，读出来再拆回去。
_EMPTY_MARK = "__ncm_bangumi_empty__"


def _wrap(value: Any) -> Any:
    """把空结果包成非空的，好让缓存愿意收下。"""
    if value is None:
        return None
    if isinstance(value, list | dict | str) and not value:
        return {_EMPTY_MARK: True}
    return value


def _unwrap(value: Any) -> Any:
    """把包过的空结果拆回去。"""
    if isinstance(value, dict) and value.get(_EMPTY_MARK) is True:
        return []
    return value


class BangumiCache:
    """条目资料的缓存。放在磁盘上，重启还在。"""

    def __init__(self, cache: Any = None) -> None:
        if cache is None:
            from src.core.metacache import get_meta_cache

            cache = get_meta_cache()
        self._cache = cache

    def get(self, kind: str, key: str) -> Any | None:
        return self._cache.get(kind, key)

    def set(self, kind: str, key: str, value: Any) -> None:
        ttl = BANGUMI_TTL.get(kind, 600.0)
        try:
            self._cache.set(kind, key, _wrap(value), ttl)
        except Exception as exc:  # noqa: BLE001 - 缓存坏了不该拖垮功能
            log.warning("bangumi 缓存写入失败 %s/%s: %s", kind, key, exc)

    def discard(self, kind: str, key: str) -> None:
        try:
            self._cache.discard(kind, key)
        except Exception as exc:  # noqa: BLE001
            log.warning("bangumi 缓存清除失败 %s/%s: %s", kind, key, exc)

    def clear(self, kind: str | None = None) -> None:
        try:
            self._cache.clear(kind)
        except Exception as exc:  # noqa: BLE001
            log.warning("bangumi 缓存清空失败: %s", exc)

    def remember(self, kind: str, key: str, produce: Any) -> Any:
        """拿缓存；没有就调 ``produce()`` 造一个，然后存起来。

        **空结果也缓存。** 搜一个不存在的词会得到一个空列表——空列表不存
        的话，下一次搜同一个词就又打一次服务器。搜不存在的东西是常事，
        每次都问一遍既慢又招人烦。所以空结果包成一个非空的标记存起来，
        读出来的时候再拆回空列表。
        """
        cached = self.get(kind, key)
        if cached is not None:
            return _unwrap(cached)
        value = produce()
        if value is not None:
            self.set(kind, key, value)
        return value


class BangumiStore:
    """本地收藏和观看历史。

    存成一个 JSON 文件：``{"favorites": [...], "history": [...]}``。
    收藏项记 ``{subject_id, name, name_cn, cover, added}``，
    历史项记 ``{subject_id, episode_id, ep, name, name_cn, cover, watched_at}``。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path else self.default_path()
        self._data: dict[str, Any] = {"favorites": [], "history": []}
        self._load()

    # ------------------------------------------------------------ 存储位置
    @staticmethod
    def default_path() -> Path:
        """默认位置，跟 B 站那份 ``bilibili.json`` 放一起。"""
        base = Path.home() / "Library" / "Application Support" / "NeteaseMusicDownloader"
        if not base.exists():
            base = Path.home() / ".config" / "NeteaseMusicDownloader"
        return base / "bangumi.json"

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("读 bangumi 本地数据失败 %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("bangumi 本地数据不是合法 JSON %s: %s", self._path, exc)
            return
        if not isinstance(data, dict):
            return
        favorites = data.get("favorites")
        history = data.get("history")
        self._data = {
            "favorites": [f for f in (favorites or []) if isinstance(f, dict)],
            "history": [h for h in (history or []) if isinstance(h, dict)],
        }

    def save(self, *, force: bool = False) -> None:
        """写盘。

        ``force=False`` 时只在有改动后才写——收藏这类操作不频繁，没必要
        每次都碰磁盘。
        """
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(self._data, ensure_ascii=False, indent=2)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError as exc:
                log.warning("写 bangumi 本地数据失败 %s: %s", self._path, exc)

    # -------------------------------------------------------------- 收藏
    def favorites(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._data["favorites"]]

    def is_favorite(self, subject_id: int | str) -> bool:
        key = str(subject_id)
        with self._lock:
            return any(str(item.get("subject_id")) == key
                       for item in self._data["favorites"])

    def set_favorite(self, subject_id: int | str, favorite: bool,
                     meta: dict[str, Any] | None = None) -> bool:
        """收藏 / 取消收藏。返回收藏后的状态。"""
        key = str(subject_id)
        if not key or key == "None":
            return False
        with self._lock:
            existing = [item for item in self._data["favorites"]
                        if str(item.get("subject_id")) != key]
            if favorite:
                entry: dict[str, Any] = {
                    "subject_id": key,
                    "name": "",
                    "name_cn": "",
                    "cover": "",
                    "added": time.time(),
                }
                if meta:
                    for field in ("name", "name_cn", "cover", "total_episodes"):
                        if field in meta:
                            entry[field] = meta[field]
                self._data["favorites"] = [entry, *existing][:MAX_FAVORITES]
            else:
                self._data["favorites"] = existing
        self.save(force=True)
        return bool(favorite)

    def remove_favorite(self, subject_id: int | str) -> bool:
        return not self.set_favorite(subject_id, False)

    # ---------------------------------------------------------- 观看历史
    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._data["history"]]

    def record_episode(self, subject_id: int | str, episode_id: int | str,
                       meta: dict[str, Any] | None = None) -> bool:
        """记一条观看历史。同一个条目只留最新的一集。"""
        skey = str(subject_id)
        ekey = str(episode_id)
        if not skey or skey == "None" or not ekey or ekey == "None":
            return False
        with self._lock:
            others = [item for item in self._data["history"]
                      if str(item.get("subject_id")) != skey]
            entry: dict[str, Any] = {
                "subject_id": skey,
                "episode_id": ekey,
                "ep": 0,
                "name": "",
                "name_cn": "",
                "cover": "",
                "watched_at": time.time(),
            }
            if meta:
                for field in ("ep", "name", "name_cn", "cover"):
                    if field in meta:
                        entry[field] = meta[field]
            self._data["history"] = [entry, *others][:MAX_HISTORY]
        self.save(force=True)
        return True

    def clear_history(self) -> int:
        with self._lock:
            count = len(self._data["history"])
            self._data["history"] = []
        self.save(force=True)
        return count

    def history_for(self, subject_id: int | str) -> dict[str, Any] | None:
        key = str(subject_id)
        with self._lock:
            for item in self._data["history"]:
                if str(item.get("subject_id")) == key:
                    return dict(item)
        return None

    # ---------------------------------------------------------- 备份用
    def exportable_state(self) -> dict[str, Any]:
        """给 ncmbk 用的那一份。"""
        with self._lock:
            return {
                "favorites": [dict(item) for item in self._data["favorites"]],
                "history": [dict(item) for item in self._data["history"]],
            }

    def merge_favorites(self, entries: list[dict[str, Any]] | None) -> int:
        """从备份里合收藏。已有的跳过，不覆盖。"""
        added = 0
        with self._lock:
            have = {str(item.get("subject_id")) for item in self._data["favorites"]}
            for raw in entries or []:
                if not isinstance(raw, dict):
                    continue
                key = str(raw.get("subject_id") or "")
                if not key or key in have:
                    continue
                have.add(key)
                self._data["favorites"].append(dict(raw))
                added += 1
            self._data["favorites"] = self._data["favorites"][:MAX_FAVORITES]
        if added:
            self.save(force=True)
        return added

    def merge_history(self, entries: list[dict[str, Any]] | None) -> int:
        """从备份里合观看历史。已有同条目的用备份里那份（时间更准）。"""
        added = 0
        with self._lock:
            by_subject = {str(item.get("subject_id")): item
                          for item in self._data["history"]}
            for raw in entries or []:
                if not isinstance(raw, dict):
                    continue
                key = str(raw.get("subject_id") or "")
                if not key:
                    continue
                if key in by_subject:
                    old = by_subject[key]
                    if float(raw.get("watched_at") or 0) > float(
                            old.get("watched_at") or 0):
                        by_subject[key] = dict(raw)
                        added += 1
                    continue
                by_subject[key] = dict(raw)
                added += 1
            self._data["history"] = sorted(
                by_subject.values(),
                key=lambda item: float(item.get("watched_at") or 0),
                reverse=True,
            )[:MAX_HISTORY]
        if added:
            self.save(force=True)
        return added
