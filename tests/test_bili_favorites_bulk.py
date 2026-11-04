"""收藏夹的多选、批量改收藏夹、合集内容和番剧选集。

不打真网络：``_get`` / ``post_form`` 换成本地记录器，每个用例自己给回答。
这些用例守的是参数形状——B站这几条路由的参数装反、或者只给 aid 不给
``aid:2``，都只回一句「请求错误」，界面上看不出哪里错了。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from src.core.bili_download import BiliDownloader
from src.core.bilibili import BiliClient

MID = "42"


class Recorder:
    """Collects the requests the client would have sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, Any] | None = None,
            answer: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("get", path, dict(params or {})))
        return answer if answer is not None else {"code": 0, "data": {}}

    def post(self, path: str, data: dict[str, Any] | None = None,
             answer: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(("post", path, dict(data or {})))
        return answer if answer is not None else {"code": 0}

    def last(self) -> tuple[str, str, dict[str, Any]]:
        assert self.calls, "no request was sent"
        return self.calls[-1]

    def paths(self) -> list[str]:
        return [path for _method, path, _data in self.calls]


def make_client() -> tuple[BiliClient, Recorder]:
    client = BiliClient(cookie=f"SESSDATA=fake; bili_jct=csrf; DedeUserID={MID}")
    recorder = Recorder()
    client._get = recorder.get  # type: ignore[method-assign]
    client.post_form = recorder.post  # type: ignore[method-assign]
    client.video = lambda bvid: {"bvid": bvid, "aid": 777}  # type: ignore[method-assign]
    client._self_mid = lambda: MID  # type: ignore[method-assign]
    return client, recorder


# ------------------------------------------------------------------ 多收藏夹

def test_one_video_can_go_into_several_folders_in_one_request():
    client, recorder = make_client()

    result = client.favorite_video_multi("BV1xx411c7mD", ["11", "22"], ["33"])

    method, path, data = recorder.last()
    assert result["success"] is True
    assert (method, path) == ("post", "/x/v3/fav/resource/deal")
    # rid 是稿件，收藏夹 id 才在 add/del_media_ids 里，两边不能装反。
    assert data["rid"] == 777
    assert data["type"] == 2
    assert data["add_media_ids"] == "11,22"
    assert data["del_media_ids"] == "33"
    # 一条请求改完整张勾选表。
    assert len(recorder.calls) == 1


def test_favoriting_nothing_is_not_sent_at_all():
    client, recorder = make_client()

    result = client.favorite_video_multi("BV1xx411c7mD", [], [])

    assert result["success"] is False
    assert recorder.calls == []


def test_multi_favorite_reports_the_server_reason():
    client, _recorder = make_client()
    client.post_form = lambda path, data=None, **kw: {"code": -400, "message": "请求错误"}  # type: ignore[method-assign]

    result = client.favorite_video_multi("BV1xx411c7mD", ["11"], [])

    assert result["success"] is False
    assert result["message"]


# ------------------------------------------------------- 打开收藏夹时的勾选状态

def test_folder_list_knows_which_folders_already_hold_the_video():
    client, _recorder = make_client()
    client._get = lambda path, params=None: {  # type: ignore[method-assign]
        "code": 0,
        "data": {"list": [
            {"id": 11, "title": "默认收藏夹", "media_count": 3, "fav_state": 1},
            {"id": 22, "title": "稍后再看", "media_count": 9, "fav_state": 0},
        ]},
    }

    folders = client.favorite_folders_with_state("BV1xx411c7mD")

    assert [folder["favoured"] for folder in folders] == [True, False]
    assert folders[0]["id"] == "11"
    assert folders[1]["media_count"] == 9


def test_folder_state_asks_about_the_resource_or_nothing_comes_back():
    client, recorder = make_client()
    client._get = lambda path, params=None: {  # type: ignore[method-assign]
        "code": 0,
        "data": {"list": [{"id": 11, "title": "默认收藏夹", "fav_state": 1}]},
    }
    client._self_mid = lambda: MID  # type: ignore[method-assign]
    seen: list[dict[str, Any]] = []

    def fake_get(path, params=None):
        seen.append(dict(params or {}))
        return {"code": 0, "data": {"list": [{"id": 11, "title": "默认收藏夹", "fav_state": 1}]}}

    client._get = fake_get  # type: ignore[method-assign]

    client.favorite_folders_with_state("BV1xx411c7mD")

    # 不带 rid/type 时每个收藏夹都没有 fav_state，勾选表会全部显示成未收藏。
    assert seen and seen[-1]["rid"] == 777
    assert seen[-1]["type"] == 2
    assert seen[-1]["up_mid"] == MID


# ------------------------------------------------------- 批量移动 / 移出收藏夹

def test_moving_videos_sends_the_resource_type_pair():
    client, recorder = make_client()

    result = client.favorite_move("11", "22", [101, 202])

    method, path, data = recorder.last()
    assert result["success"] is True
    assert (method, path) == ("post", "/x/v3/fav/resource/move")
    # 收藏夹的批量接口要 "aid:2"，只给 aid 会被判成参数错误。
    assert data["resources"] == "101:2,202:2"
    assert data["src_media_id"] == "11"
    assert data["tar_media_id"] == "22"
    assert data["mid"] == MID
    assert result["count"] == 2


def test_copying_uses_the_copy_route():
    client, recorder = make_client()

    client.favorite_move("11", "22", [101], copy=True)

    assert recorder.last()[1] == "/x/v3/fav/resource/copy"


def test_moving_into_the_same_folder_is_refused_before_any_request():
    client, recorder = make_client()

    result = client.favorite_move("11", "11", [101])

    assert result["success"] is False
    assert recorder.calls == []


def test_batch_delete_names_the_folder_and_the_resources():
    client, recorder = make_client()

    result = client.favorite_batch_delete("11", [101, 202])

    method, path, data = recorder.last()
    assert result["success"] is True
    assert (method, path) == ("post", "/x/v3/fav/resource/batch-del")
    assert data["media_id"] == "11"
    assert data["resources"] == "101:2,202:2"


# --------------------------------------------------------------- 合集里的视频

def test_collection_videos_parses_the_space_shape():
    client, recorder = make_client()
    client._get = lambda path, params=None: {  # type: ignore[method-assign]
        "code": 0,
        "data": {
            "archives": [
                {"aid": 1, "bvid": "BV1", "title": "第一集", "pic": "http://i0.hdslb.com/a.jpg",
                 "duration": 61, "pubdate": 1700000000, "stat": {"view": 1200}},
                {"aid": 2, "title": "没有 bvid 的条目"},
            ],
            "page": {"page_num": 1, "page_size": 1},
        },
    }
    seen: list[dict[str, Any]] = []
    original = client._get

    def fake_get(path, params=None):
        seen.append(dict(params or {}))
        return original(path, params)

    client._get = fake_get  # type: ignore[method-assign]

    page = client.collection_videos("587216", "37737161")

    assert [item["bvid"] for item in page["items"]] == ["BV1"]
    assert page["items"][0]["cover"].startswith("https://")
    assert page["items"][0]["duration"] == 61
    assert seen[-1]["mid"] == "37737161"
    assert seen[-1]["season_id"] == "587216"


def test_collection_videos_without_mid_does_not_call_anything():
    client, recorder = make_client()

    page = client.collection_videos("587216", "")

    assert page["items"] == []
    assert page.get("error")
    assert recorder.calls == []


# --------------------------------------------------------------- 番剧选集

def test_bangumi_season_lists_the_episodes():
    client, _recorder = make_client()
    client._get = lambda path, params=None: {  # type: ignore[method-assign]
        "code": 0,
        "result": {
            "season_title": "三体",
            "episodes": [
                {"id": 3001, "aid": 111, "cid": 222, "title": "第1话",
                 "long_title": "红岸之谜"},
                {"id": 0, "title": "没有 ep_id 的条目"},
            ],
        },
    }

    answer = client.bangumi_season("26257")

    assert answer["success"] is True
    assert answer["title"] == "三体"
    assert [episode["ep_id"] for episode in answer["episodes"]] == [3001]
    assert answer["episodes"][0]["long_title"] == "红岸之谜"


def test_bangumi_season_passes_the_server_message_through():
    client, _recorder = make_client()
    client._get = lambda path, params=None: {"code": -404, "message": "啥都木有"}  # type: ignore[method-assign]

    answer = client.bangumi_season("26257")

    assert answer["success"] is False
    assert answer["episodes"] == []


# --------------------------------------------------------- 取流的排队（限流）

class _TimedClient:
    """Records when each resolve started and ended."""

    def __init__(self, hold: float = 0.02) -> None:
        self.hold = hold
        self.spans: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def video_streams(self, bvid, quality=0, with_muxed=True):
        start = time.monotonic()
        time.sleep(self.hold)
        end = time.monotonic()
        with self._lock:
            self.spans.append((start, end))
        return {"video": {"url": "http://cdn/video"}, "audio": {"url": "http://cdn/audio"}}


def _run_parallel(downloader: BiliDownloader, count: int) -> list[dict[str, Any]]:
    threads = [
        threading.Thread(target=downloader._resolve_paced, args=(f"BV{i}",))
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return []


def test_resolves_are_serial_and_keep_the_configured_gap(tmp_path):
    client = _TimedClient()
    downloader = BiliDownloader(tmp_path, client, interval=0.15, max_concurrent=3)

    _run_parallel(downloader, 3)

    spans = sorted(client.spans)
    assert len(spans) == 3
    # 一个接一个：后一个的开头不早于前一个的结尾。
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert later[0] >= earlier[1]
    # 而且中间还留着配置的间隔（带抖动，所以只验下界）。
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert later[0] - earlier[1] >= 0.14


def test_zero_interval_sends_them_back_to_back(tmp_path):
    client = _TimedClient()
    downloader = BiliDownloader(tmp_path, client, interval=0, max_concurrent=3)
    started = time.monotonic()

    _run_parallel(downloader, 3)

    # 三次取流总共只花三次 to 的时间，中间没有额外等待。
    assert time.monotonic() - started < 0.5
    assert len(client.spans) == 3


# ------------------------------------------------- 间隔一路传到下载器（接线）

def test_bridge_passes_the_interval_down_to_the_downloader(tmp_path, monkeypatch):
    """界面选的间隔必须真的进到下载器里，不然那条控件只是个摆设。"""
    import src.core.bili_download as download_module
    from src.gui.bilibili_bridge import BilibiliBridge

    seen: list[float] = []

    class Recorder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs.get("interval"))

        def set_progress_callback(self, _callback: Any) -> None:
            pass

        def set_done_callback(self, _callback: Any) -> None:
            pass

        def download(self, _items: list[dict[str, Any]]) -> list[Any]:
            return []

    monkeypatch.setattr(download_module, "BiliDownloader", Recorder)
    bridge = BilibiliBridge(client=make_client()[0])

    bridge.download([{"bvid": "BV1"}],
                    {"download_dir": str(tmp_path), "merge_audio": False, "interval": 3})
    bridge.download([{"bvid": "BV1"}],
                    {"download_dir": str(tmp_path), "merge_audio": False})

    assert seen == [3.0, download_module.DEFAULT_INTERVAL]


def test_the_interval_is_clamped_to_something_sane(tmp_path):
    client = _TimedClient()

    assert BiliDownloader(tmp_path, client, interval=0).interval == 0
    assert BiliDownloader(tmp_path, client, interval=999).interval == 10.0
