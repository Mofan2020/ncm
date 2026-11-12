"""分P（选集）的契约。

普通投稿也能有多集：合集投稿和「分P」在 B站接口里是同一个 `pages`
数组，每一条有自己的 `cid`。取流和下载都必须能指定用哪一条，否则
一集 40 分钟的视频永远只能下到第一集。
"""

from __future__ import annotations

import logging

from src.core.bili_download import BiliDownloader


def _bare_downloader(client) -> BiliDownloader:
    """一个不用 __init__ 的下载器：只测取流那一步，不碰磁盘和网络。"""
    downloader = BiliDownloader.__new__(BiliDownloader)
    downloader.client = client
    downloader.quality = 0  # B站这边是 qn 整数，不是网易云的音质字符串
    downloader.logger = logging.getLogger("test")
    return downloader


class _RecordingClient:
    """记下取流时用的是哪一个 cid。"""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def video_streams(self, bvid, quality=0, with_muxed=True, cid=0):
        self.seen.append(cid)
        return {"success": True, "video": None, "audio": None, "muxed": None}


def test_the_download_asks_for_the_episode_the_track_names() -> None:
    """track 里带的 cid 要一路传到取流那一步。"""
    client = _RecordingClient()
    downloader = _bare_downloader(client)

    downloader._resolve("BV1Ka4y1M72M", cid=222)

    assert client.seen == [222]


def test_a_client_without_the_cid_parameter_still_works() -> None:
    """旧的核心客户端没有 cid 参数——取流不能因此挂掉。"""

    class _OldClient:
        def __init__(self) -> None:
            self.calls = 0

        def video_streams(self, bvid, quality=0, with_muxed=True):
            self.calls += 1
            return {"success": True}

    client = _OldClient()
    answer = _bare_downloader(client)._resolve("BV1aa", cid=5)

    assert answer == {"success": True}
    assert client.calls == 1


def test_the_bridge_hands_the_episode_list_to_the_window() -> None:
    """选集列表要跟着取流一起回去，界面才有东西可选。"""

    class _Client:
        def video_streams(self, bvid, quality=0, with_muxed=True, cid=0):
            return {
                "success": True,
                "bvid": bvid,
                "cid": 222 if cid else 111,
                "duration": 60,
                "title": "多集视频",
                "author": "甲",
                "author_mid": "1",
                "cover": "c",
                "pages": [
                    {"cid": 111, "page": 1, "title": "第一集", "duration": 60},
                    {"cid": 222, "page": 2, "title": "第二集", "duration": 90},
                ],
                "page_title": "第二集" if cid else "第一集",
                "qualities": [],
                "audio": None,
                "video": None,
                "muxed": None,
                "flac": None,
                "dolby": None,
            }

    from src.gui.bilibili_bridge import BilibiliBridge

    bridge = BilibiliBridge.__new__(BilibiliBridge)
    bridge._client = _Client()
    bridge._media = None

    answer = bridge.video_streams("BV1Ka4y1M72M", cid=222)

    assert answer["success"] is True
    assert answer["cid"] == 222
    assert answer["page_title"] == "第二集"
    assert [page["cid"] for page in answer["pages"]] == [111, 222]
    assert answer["pages"][1]["title"] == "第二集"


def test_the_view_of_a_video_carries_its_pages() -> None:
    """详情接口里的 pages 要解析出来，字段名是 part 不是 title。"""
    from src.core.bilibili import BiliClient

    client = BiliClient.__new__(BiliClient)
    item = client._from_view({
        "bvid": "BV1Ka4y1M72M",
        "aid": 1,
        "title": "多集",
        "owner": {"name": "甲", "mid": 1},
        "pages": [
            {"cid": 111, "page": 1, "part": "开场", "duration": 60},
            {"cid": 222, "page": 2, "part": "正片", "duration": 90},
        ],
    })

    assert [page["cid"] for page in item["pages"]] == [111, 222]
    assert item["pages"][1]["title"] == "正片"
    assert item["pages"][1]["page"] == 2
    assert item["pages"][1]["duration"] == 90


def test_a_single_episode_video_reports_an_empty_or_one_item_list() -> None:
    """没有 pages 数组时给空列表，界面据此隐藏选集。"""
    from src.core.bilibili import BiliClient

    client = BiliClient.__new__(BiliClient)
    item = client._from_view({"bvid": "BV1aa", "title": "单集", "owner": {}})

    assert item["pages"] == []
