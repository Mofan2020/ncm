"""B站缓存。

用假的缓存对象，不碰磁盘。
"""
from __future__ import annotations

from src.core.bili_cache import BiliCache


class FakeCache:
    """假缓存：记住写进去的东西。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], object] = {}
        self.cleared: list[str | None] = []

    def get(self, kind: str, key: str):
        return self.items.get((kind, key))

    def set(self, kind: str, key: str, value, ttl: float) -> None:
        self.items[(kind, key)] = value

    def discard(self, kind: str, key: str) -> None:
        self.items.pop((kind, key), None)

    def discard_prefix(self, kind: str, prefix: str) -> int:
        gone = 0
        for entry in list(self.items):
            if entry[0] == kind and entry[1].startswith(prefix):
                del self.items[entry]
                gone += 1
        return gone

    def clear(self, kind: str | None = None) -> int:
        self.cleared.append(kind)
        gone = 0
        for entry in list(self.items):
            if kind is None or entry[0] == kind:
                del self.items[entry]
                gone += 1
        return gone


def make_cache() -> tuple[BiliCache, FakeCache]:
    fake = FakeCache()
    return BiliCache(cache=fake), fake


def test_a_value_is_remembered_and_read_back():
    cache, fake = make_cache()

    cache.set("search", "abc", [{"bvid": "BV1"}])

    assert cache.get("search", "abc") == [{"bvid": "BV1"}]
    # 存进去的时候用的是 B 站自己的目录名，不会跟网易云的混在一起。
    assert ("bili_search", "abc") in fake.items


def test_the_second_call_does_not_hit_the_network():
    cache, _fake = make_cache()
    calls = []

    def loader():
        calls.append(1)
        return [{"bvid": "BV1"}]

    first, from_disk = cache.remember("search", "abc", loader)
    second, from_disk_two = cache.remember("search", "abc", loader)

    assert first == second
    assert len(calls) == 1
    assert from_disk is False
    assert from_disk_two is True


def test_a_video_change_drops_the_counters_comments_and_danmaku():
    cache, fake = make_cache()
    cache.set("stat", "BV1", {"like": 1})
    cache.set("comments", "BV1|1|hot", ["a"])
    cache.set("danmaku", "BV1|7", ["b"])

    cache.invalidate_video("BV1")

    assert cache.get("stat", "BV1") is None
    assert cache.get("comments", "BV1|1|hot") is None
    assert cache.get("danmaku", "BV1|7") is None
    assert fake.items == {}


def test_the_folders_and_the_follow_state_are_dropped():
    cache, _fake = make_cache()
    cache.set("folders", "all", [{"id": "9"}])
    cache.set("follow", "42", {"following": True})

    cache.invalidate_folders()
    cache.invalidate_follow("42")

    assert cache.get("folders", "all") is None
    assert cache.get("follow", "42") is None


def test_the_danmaku_lives_longer_than_the_comments():
    cache, _fake = make_cache()
    cache.set("danmaku", "BV1|7", ["b"])
    cache.set("comments", "BV1|1", ["c"])

    # 两类内容用的是不同的过期时间：弹幕不会变，评论会。
    from src.core.bili_cache import BILI_TTL

    assert BILI_TTL["danmaku"] > BILI_TTL["comments"]
    assert BILI_TTL["danmaku"] >= 86400
    assert BILI_TTL["comments"] <= 600


def test_the_folder_change_drops_the_folder_contents_too():
    """收藏夹里的内容也要清。

    只清列表是不够的：用户刚收藏了一个新视频，列表里的旧条目还带着
    「不在这个收藏夹里」的状态，界面就显示成「收藏没生效」。
    """
    cache, _fake = make_cache()
    cache.set("folders", "all", [{"id": "9"}])
    cache.set("folder_items", "9|1|30", [{"bvid": "BV1"}])

    cache.invalidate_folders()

    assert cache.get("folders", "all") is None
    assert cache.get("folder_items", "9|1|30") is None


def test_the_account_cache_is_dropped_on_demand():
    cache, _fake = make_cache()
    cache.set("account", "current", {"uname": "甲", "face": "http://x/1.jpg"})

    cache.invalidate_account()

    assert cache.get("account", "current") is None


def test_the_collections_and_the_bangumi_are_dropped_on_demand():
    cache, _fake = make_cache()
    cache.set("collections", "1|20", [{"id": "5"}])
    cache.set("collections", "2|20", [{"id": "6"}])
    cache.set("bangumi", "all", [{"season_id": 7}])

    cache.invalidate_collections()
    cache.invalidate_bangumi()

    assert cache.get("collections", "1|20") is None
    assert cache.get("collections", "2|20") is None
    assert cache.get("bangumi", "all") is None


def test_the_account_cache_lives_longer_than_the_folders():
    """头像昵称几乎不变，收藏夹随时会变——两者的过期时间不能一样。"""
    from src.core.bili_cache import BILI_TTL

    assert BILI_TTL["account"] > BILI_TTL["folders"]
    assert BILI_TTL["collections"] > BILI_TTL["folders"]
