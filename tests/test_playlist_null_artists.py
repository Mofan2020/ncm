"""Issue #2：大歌单查询时 artist 名字为 null 会炸。

用户报告：歌单有 917 首时
``TypeError: sequence item 0: expected str instance, NoneType found``
出在 ``_track_payload``。网易云在大歌单里会给某些 artist 一个 null 的
名字，``dict.get("name", "Unknown")`` 在键存在而值为 null 时返回的是
null，不是默认值，交给 ``", ".join()`` 就抛 TypeError。
"""
from __future__ import annotations

import pytest

from src.gui.bridge import GuiBridge


@pytest.fixture()
def bridge(isolated_home):
    instance = GuiBridge()
    yield instance


def test_a_null_artist_name_does_not_break_the_payload(bridge):
    song = {
        "id": 123,
        "name": "歌",
        "ar": [{"id": 1, "name": None}, {"id": 2, "name": "正常"}],
    }
    result = bridge._track_payload(0, song)
    assert "正常" in result["artists"]
    assert "Unknown" in result["artists"]


def test_every_artist_name_is_a_string(bridge):
    """名字为 null 的 artist 不能把整个歌单带崩。"""
    song = {
        "id": 123,
        "name": "歌",
        "ar": [{"id": 1, "name": None}, {"id": 2, "name": "正常"}],
    }
    result = bridge._track_payload(0, song)
    assert isinstance(result["artists"], str)
    # 每一段都得是字符串，不能混进 None。
    for part in result["artists"].split(", "):
        assert isinstance(part, str)
        assert part


def test_an_artist_without_a_name_at_all(bridge):
    song = {"id": 1, "name": "歌", "ar": [{"id": 7}]}
    result = bridge._track_payload(0, song)
    assert result["artists"] == "Unknown"


def test_no_artists_at_all(bridge):
    result = bridge._track_payload(0, {"id": 1, "name": "歌", "ar": []})
    assert result["artists"] == "Unknown"


def test_a_whole_big_playlist_of_null_names(bridge):
    """917 首里有一半的 artist 名字是 null，也不该抛。"""
    songs = []
    for index in range(917):
        songs.append({
            "id": index,
            "name": f"歌{index}",
            "ar": [{"id": index, "name": None if index % 2 else f"歌手{index}"}],
        })
    payloads = [bridge._track_payload(index, song)
                for index, song in enumerate(songs)]
    assert len(payloads) == 917
    assert all(isinstance(item["artists"], str) for item in payloads)


def test_a_null_artist_id_is_skipped(bridge):
    """artist_list 只收有 id 和名字的，null id 不能变成 "None"。"""
    song = {
        "id": 1,
        "name": "歌",
        "ar": [{"id": None, "name": "甲"}, {"id": 2, "name": "乙"}],
    }
    result = bridge._track_payload(0, song)
    ids = [item["id"] for item in result["artist_list"]]
    assert "None" not in ids
    assert "2" in ids
