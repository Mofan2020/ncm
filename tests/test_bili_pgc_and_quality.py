"""PGC 搜索与「实际播放的画质」。

两件事都踩过：
- 主站搜索只回 UGC 稿件，番剧/影视根本不在结果里，聚合搜索搜不到正片；
- 取流时请求 1080P、视频最高只有 720P，界面还写着「自动」，看着像设置没生效。

不打真网络：``_get`` 换成本地回答，每个用例自己给数据。
"""
from __future__ import annotations

from typing import Any

from src.core.bilibili import BiliClient


class Recorder:
    """Answers ``_get`` per path and keeps what was asked."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((path, params))
        # 两条 PGC 搜索走的是同一个 path，靠 search_type 区分回答。
        search_type = params.get("search_type")
        if search_type and search_type in self.answers:
            return self.answers[search_type]
        for key, answer in self.answers.items():
            if key in path:
                return answer
        return {"code": 0, "data": {}}

    def search_types(self) -> list[str]:
        return [params.get("search_type") for path, params in self.calls
                if "search/type" in path]


def make_client(answers: dict[str, dict[str, Any]]) -> tuple[BiliClient, Recorder]:
    client = BiliClient(cookie="SESSDATA=fake; DedeUserID=42")
    client._ensure_buvid = lambda: None  # type: ignore[method-assign]
    client._ensure_mixin_key = lambda: "mixin"  # type: ignore[method-assign]
    recorder = Recorder(answers)
    client._get = recorder  # type: ignore[method-assign]
    return client, recorder


# ------------------------------------------------------------------ PGC 搜索

PGC_ANSWERS = {
    "media_bangumi": {
        "code": 0,
        "data": {"result": [
            {
                "season_id": 26257,
                "media_id": 28223012,
                "title": '<em class="keyword">三体</em>',
                "cover": "//i0.hdslb.com/bfs/bangumi/ss26257.jpg",
                "url": "https://www.bilibili.com/bangumi/play/ss26257",
                "index_show": "全15话",
                "season_type_name": "国创",
                "styles": "科幻/剧情",
                "areas": "中国",
                "media_score": {"score": 8.6, "user_count": 12000},
                "is_follow": 1,
            },
            # 同一条出现在两个搜索类型里：只该留一份。
            {"season_id": 26257, "title": "重复条目"},
            # 没有 season_id 的条目播放不了，直接丢掉。
            {"title": "没有季号的条目"},
        ]},
    },
    "media_ft": {
        "code": 0,
        "data": {"result": [
            {
                "season_id": 40001,
                "title": "流浪地球",
                "cover": "http://i0.hdslb.com/bfs/bangumi/movie.jpg",
                "index_show": "电影",
                "season_type_name": "电影",
                "styles": "科幻",
                "areas": "中国",
                "media_score": {"score": 9.1},
            },
        ]},
    },
}


def test_pgc_search_asks_both_types_and_merges_them():
    client, recorder = make_client(PGC_ANSWERS)

    items = client.search_pgc("三体")

    assert recorder.search_types() == ["media_bangumi", "media_ft"]
    titles = [item["title"] for item in items]
    # 高亮标签必须剥掉，否则列表上显示的是 HTML 源码。
    assert titles == ["三体", "流浪地球"]
    assert [item["type"] for item in items] == ["bangumi", "cinema"]


def test_pgc_search_normalises_the_fields_the_frontend_uses():
    client, _recorder = make_client(PGC_ANSWERS)

    first = client.search_pgc("三体")[0]

    assert first["season_id"] == 26257
    assert first["kind"] == "pgc"
    assert first["season_type_name"] == "国创"
    assert first["index_show"] == "全15话"
    assert first["styles"] == ["科幻", "剧情"]
    assert first["score"] == 8.6
    # 封面要能直接喂给 <img>，协议相对的地址得补全。
    assert first["cover"].startswith("https://")


def test_one_pgc_type_failing_does_not_empty_the_other():
    client, _recorder = make_client({
        "media_bangumi": {"code": -412, "message": "请求被拦截"},
        "media_ft": PGC_ANSWERS["media_ft"],
    })

    items = client.search_pgc("流浪地球")

    assert [item["title"] for item in items] == ["流浪地球"]


def test_pgc_search_without_a_keyword_does_not_call_anything():
    client, recorder = make_client(PGC_ANSWERS)

    assert client.search_pgc("   ") == []
    assert recorder.calls == []


# --------------------------------------------------- 实际播放的画质（供界面显示）

def _playurl_answer(ids: list[int]) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "quality": max(ids) if ids else 0,
            "accept_quality": ids,
            "dash": {
                "video": [
                    {
                        "id": qn,
                        "baseUrl": f"http://cdn/video-{qn}.m4s",
                        "codecs": "avc1.640032",
                        "bandwidth": 1000 * qn,
                        "width": 1920,
                        "height": qn,
                    }
                    for qn in ids
                ],
                "audio": [{"id": 30280, "baseUrl": "http://cdn/audio.m4s",
                           "codecs": "mp4a.40.2", "bandwidth": 128000}],
            },
        },
    }


def _dash_block(ids: list[int]) -> dict[str, Any]:
    """The ``data`` block of a playurl answer (DASH, no muxed file)."""
    return _playurl_answer(ids)["data"]


def _stream_client(ids: list[int]) -> BiliClient:
    client, _recorder = make_client({
        "/x/web-interface/view": {
            "code": 0,
            "data": {
                "cid": 111,
                "duration": 60,
                "title": "视频",
                "pic": "http://i0.hdslb.com/cover.jpg",
                "owner": {"name": "UP", "mid": 42},
            },
        },
        "/x/player/wbi/playurl": _playurl_answer(ids),
    })
    return client


def test_the_quality_that_is_actually_played_is_reported():
    client = _stream_client([16, 32, 64, 80])

    answer = client.video_streams("BV1xx411c7mD", quality=64)

    # 请求 720P：选的就得是 720P，并把「实际是哪个」写回来。
    assert answer["requested_quality"] == 64
    assert answer["picked_quality"] == 64
    assert answer["video"]["url"].endswith("video-64.m4s")


def test_a_quality_the_video_does_not_have_falls_back_and_says_so():
    client = _stream_client([16, 32, 64])

    answer = client.video_streams("BV1xx411c7mD", quality=112)

    # 请求 1080P+，这条视频最高 720P：播的是 720P，界面要显示 720P 而不是「自动」。
    assert answer["requested_quality"] == 112
    assert answer["picked_quality"] == 64


def test_auto_asks_for_the_best_and_reports_it():
    client = _stream_client([16, 32, 64, 80])

    answer = client.video_streams("BV1xx411c7mD", quality=0)

    assert answer["requested_quality"] == 0
    assert answer["picked_quality"] == 80


def test_bangumi_streams_report_the_played_quality_too():
    client, _recorder = make_client({
        "/pgc/player/web/playurl": {
            "code": 0,
            "result": {
                **_dash_block([32, 64, 80]),
                "timelength": 1440000,
                "title": "第一话",
            },
        },
    })

    answer = client.bangumi_streams(26257, 3001, quality=64)

    assert answer["requested_quality"] == 64
    assert answer["picked_quality"] == 64
