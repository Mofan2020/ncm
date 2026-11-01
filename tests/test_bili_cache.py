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
