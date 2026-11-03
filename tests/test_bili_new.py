"""离线测试：合集订阅、追番、用户动态、番剧播放流、推荐流翻页参数。

不打真网络——传输层是整个换掉的，每个用例自己构造接口回答。
"""
from __future__ import annotations

from typing import Any

from src.core.bilibili import BiliClient

MID = "1946836486"

NAV_PAYLOAD = {
    "code": 0,
    "data": {
        "isLogin": True,
        "mid": int(MID),
        "uname": "xkm星空喵",
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    },
}

SPI_PAYLOAD = {"code": 0, "data": {"b_3": "B3-DEV", "b_4": "B4-DEV"}}

SEASONS_PAYLOAD = {
    "code": 0,
    "data": {
        "items_lists": {
            "page": {"page_num": 1, "page_size": 20, "total": 2},
            "seasons_list": [
                {
                    "archives": [{"aid": 1, "bvid": "BV1xx411c7mD", "title": "第一集"}],
                    "meta": {
                        "category": 0,
                        "cover": "http://archive.biliimg.com/bfs/archive/cover-a.jpg",
                        "description": "合集简介",
                        "mid": 37737161,
                        "name": "合集·拾枝杂谈",
                        "season_id": 587216,
                        "total": 10,
                    },
                },
                {
                    "archives": [],
                    "meta": {
                        "cover": "//archive.biliimg.com/bfs/archive/cover-b.jpg",
                        "name": "水浅王八多",
                        "season_id": 1227671,
                        "total": 2,
                    },
                },
            ],
            "series_list": [],
        }
    },
}

BANGUMI_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "season_id": 26257,
                "media_id": 28223012,
                "title": "三体",
                "cover": "http://i0.hdslb.com/bfs/bangumi/image/t3.png",
                "total_count": 15,
                "progress": "看到第3话",
                "follow_status": 2,
                "new_ep": {"id": 705756, "index_show": "全15话"},
                "url": "https://www.bilibili.com/bangumi/play/ss26257",
            },
            {
                "season_id": 46585,
                "media_id": 28238124,
                "title": "灵笼 第二季",
                "square_cover": "http://i0.hdslb.com/bfs/bangumi/image/t4.png",
                "total_count": 12,
                "progress": "",
                "follow_status": 3,
                "new_ep": {"id": 1633654, "index_show": "全12话"},
            },
        ]
    },
}

DYNAMIC_PAGE_1 = {
    "code": 0,
    "data": {
        "items": [
            {
                "id_str": "1260542398530846739",
                "type": "DYNAMIC_TYPE_DRAW",
                "modules": {
                    "module_author": {
                        "mid": 1946836486,
                        "name": "xkm星空喵",
                        "face": "http://i0.hdslb.com/bfs/face/me.jpg",
                        "pub_ts": 1792331298,
                        "pub_time": "2小时前",
                    },
                    "module_dynamic": {
                        "desc": {"text": "两张图，随手拍"},
                        "major": {
                            "type": "MAJOR_TYPE_OPUS",
                            "opus": {
                                "pics": [
                                    {"url": "http://i0.hdslb.com/bfs/new_dyn/a.png", "width": 512},
                                    {"url": "http://i0.hdslb.com/bfs/new_dyn/b.png", "width": 512},
                                ],
                                "summary": {"text": "两张图，随手拍"},
                            },
                        },
                    },
                },
            },
            {
                "id_str": "1254345203155730486",
                "type": "DYNAMIC_TYPE_AV",
                "modules": {
                    "module_author": {"mid": 1946836486, "name": "xkm星空喵", "pub_ts": 1790888401},
                    "module_dynamic": {
                        "desc": None,
                        "major": {
                            "type": "MAJOR_TYPE_ARCHIVE",
                            "archive": {
                                "bvid": "BV1Z8a66dETs",
                                "aid": "117366138607029",
                                "title": "DeepSleep",
                                "cover": "http://i0.hdslb.com/bfs/archive/v.jpg",
                                "duration_text": "12:34",
                                "desc": "项目地址",
                            },
                        },
                    },
                },
            },
            {
                "id_str": "1250000000000000001",
                "type": "DYNAMIC_TYPE_FORWARD",
                "modules": {
                    "module_author": {"mid": 1946836486, "name": "xkm星空喵", "pub_ts": 1790000000},
                    "module_dynamic": {"desc": {"text": "转发一下"}, "major": None},
                },
                "orig": {
                    "id_str": "1240000000000000002",
                    "type": "DYNAMIC_TYPE_DRAW",
                    "modules": {
                        "module_author": {"mid": 42, "name": "原作者", "pub_ts": 1789000000},
                        "module_dynamic": {
                            "desc": {"text": "原动态正文"},
                            "major": {
                                "type": "MAJOR_TYPE_DRAW",
                                "draw": {"items": [{"src": "http://i0.hdslb.com/bfs/draw/x.jpg"}]},
                            },
                        },
                    },
                },
            },
        ],
        "offset": "1044400681921806345",
        "has_more": False,
    },
}

PGC_PAYLOAD = {
    "code": 0,
    "result": {
        "quality": 80,
        "timelength": 1394090,
        "accept_quality": [112, 80, 64, 32, 16],
        "support_formats": [
            {"quality": 112, "new_description": "1080P+ 高码率", "format": "flv1080"},
            {"quality": 80, "new_description": "1080P 高清", "format": "flv1080"},
            {"quality": 64, "new_description": "720P 高清", "format": "flv720"},
        ],
        "dash": {
            "duration": 1394,
            "video": [
                {
                    "id": 64,
                    "baseUrl": "https://upos.example/720.m4s",
                    "bandwidth": 900000,
                    "width": 1280,
                    "height": 720,
                    "codecs": "avc1.64001F",
                    "mimeType": "video/mp4",
                },
                {
                    "id": 80,
                    "baseUrl": "https://upos.example/1080.m4s",
                    "backupUrl": ["http://upos.example/1080.bak.m4s"],
                    "bandwidth": 1800000,
                    "width": 1920,
                    "height": 1080,
                    "codecs": "avc1.640028",
                    "mimeType": "video/mp4",
                },
            ],
            "audio": [
                {"id": 30216, "bandwidth": 65536, "baseUrl": "https://upos.example/a64.m4s"},
                {"id": 30280, "bandwidth": 192000, "baseUrl": "https://upos.example/a192.m4s"},
            ],
        },
        "durl": [
            {
                "url": "http://upos.example/preview.mp4",
                "size": 33946377,
                "backup_url": ["https://upos.example/preview.bak.mp4"],
            }
        ],
    },
}


class FakeJar:
    """Minimal stand-in for a requests cookie jar."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def set(self, name: str, value: str, domain: str | None = None) -> None:
        self.cookies[name] = value

    def clear(self, **kwargs: Any) -> None:
        name = kwargs.get("name")
        if name is None:
            self.cookies.clear()
            return
        self.cookies.pop(name, None)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers by url fragment; an unknown route answers HTTP 404."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.jar = FakeJar()
        self.cookies = self.jar
        self.calls: list[dict[str, Any]] = []

    def _route(self, url: str, params: dict[str, Any]) -> FakeResponse:
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if callable(payload):
                    return FakeResponse(payload(dict(params)))
                return FakeResponse(payload)
        return FakeResponse({"code": -404, "message": "no route"}, status_code=404)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "method": "GET"})
        return self._route(url, params or {})

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(data or {}), "method": "POST"})
        return self._route(url, data or {})


def make_client(routes: dict[str, Any]) -> tuple[BiliClient, FakeSession]:
    session = FakeSession(routes)
    client = BiliClient(session=session, cookie=f"SESSDATA=abc; DedeUserID={MID}")
    return client, session


def calls_to(session: FakeSession, fragment: str) -> list[dict[str, Any]]:
    return [call for call in session.calls if fragment in call["url"]]


# ---------------------------------------------------------------------------
# subscribed_collections
# ---------------------------------------------------------------------------

COLLECTED_PAYLOAD = {
    "code": 0,
    "data": {
        "count": 2,
        "has_more": False,
        "list": [
            {
                "id": 3134817,
                "fid": 0,
                "mid": 187681452,
                "type": 21,  # 视频合集
                "title": "柑宝鸣潮二创",
                "cover": "http://archive.biliimg.com/bfs/archive/c1.jpg",
                "intro": "海鸥别叫了",
                "media_count": 43,
                "upper": {"mid": 187681452, "name": "柑宝"},
            },
            {
                "id": 44233921,
                "fid": 442339,
                "mid": 7792521,
                "type": 11,  # 收藏夹，必须被过滤掉
                "title": "默认收藏夹",
                "cover": "http://archive.biliimg.com/bfs/archive/c2.jpg",
                "media_count": 85,
                "upper": {"mid": 7792521, "name": "放假君"},
            },
        ],
    },
}


def test_subscribed_collections_reads_the_collected_collections():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/v3/fav/folder/collected/list": COLLECTED_PAYLOAD,
    })
    items = client.subscribed_collections()

    assert len(items) == 1  # type=11 的收藏夹被过滤
    first = items[0]
    assert first["id"] == "3134817"
    assert first["title"] == "柑宝鸣潮二创"
    assert first["cover"] == "https://archive.biliimg.com/bfs/archive/c1.jpg"
    assert first["count"] == 43
    assert first["author_mid"] == "187681452"
    assert first["author_name"] == "柑宝"
    assert first["url"] == "https://space.bilibili.com/187681452/lists/3134817?type=season"

    call = calls_to(session, "folder/collected/list")[0]
    assert call["params"]["up_mid"] == MID
    # 不带 platform=web 时接口不会返回合集，只回收藏夹
    assert call["params"]["platform"] == "web"


def test_subscribed_collections_falls_back_to_the_space_collections():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/v3/fav/folder/collected/list": {"code": 0, "data": {"list": []}},
        "/x/polymer/web-space/seasons_series_list": SEASONS_PAYLOAD,
    })
    items = client.subscribed_collections()

    assert len(items) == 2
    first = items[0]
    assert first["id"] == "587216"
    assert first["title"] == "合集·拾枝杂谈"
    assert first["cover"] == "https://archive.biliimg.com/bfs/archive/cover-a.jpg"
    assert first["count"] == 10
    assert first["description"] == "合集简介"
    assert first["author_mid"] == "37737161"
    assert first["url"] == "https://space.bilibili.com/37737161/lists/587216?type=season"
    # 协议相对地址也要补成 https
    assert items[1]["cover"] == "https://archive.biliimg.com/bfs/archive/cover-b.jpg"

    call = calls_to(session, "seasons_series_list")[0]
    assert call["params"]["mid"] == MID
    assert call["params"]["page_num"] == 1
    assert call["params"]["page_size"] == 20


def test_subscribed_collections_returns_empty_list_when_both_routes_refuse():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/v3/fav/folder/collected/list": {"code": -400, "message": "请求错误"},
        "/x/polymer/web-space/seasons_series_list": {"code": -400, "message": "请求错误"},
    })
    assert client.subscribed_collections() == []


def test_subscribed_collections_returns_empty_without_a_mid():
    client, session = make_client({})
    client.set_cookie("")
    assert client.subscribed_collections() == []
    assert calls_to(session, "seasons_series_list") == []


# ---------------------------------------------------------------------------
# bangumi
# ---------------------------------------------------------------------------

def test_bangumi_falls_back_to_the_space_route_and_normalises_fields():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        # v3 那条现在 404，必须能退回 space 那条
        "/x/v2/follow/bangumi/v3/follows": {"code": -404, "message": "啥都木有"},
        "/x/space/bangumi/follow/list": BANGUMI_PAYLOAD,
    })
    shows = client.bangumi()

    assert len(shows) == 2
    san_ti = shows[0]
    assert san_ti["season_id"] == 26257
    assert san_ti["media_id"] == 28223012
    assert san_ti["title"] == "三体"
    assert san_ti["cover"].startswith("https://i0.hdslb.com/")
    assert san_ti["total"] == 15
    assert san_ti["progress"] == "看到第3话"
    assert san_ti["watched"] == 3
    assert san_ti["progress_percent"] == 20
    assert san_ti["ep_id"] == 705756
    assert san_ti["ep_index"] == "全15话"
    assert san_ti["follow_status"] == 2

    finished = shows[1]
    assert finished["progress"] == ""
    assert finished["progress_percent"] == 100  # follow_status=3 表示看过
    assert finished["cover"].startswith("https://")  # square_cover 兜底
    assert finished["url"] == "https://www.bilibili.com/bangumi/play/ss46585"

    fallback_call = calls_to(session, "bangumi/follow/list")[0]
    assert fallback_call["params"]["vmid"] == MID
    assert fallback_call["params"]["type"] == 1


def test_bangumi_returns_empty_list_when_both_routes_refuse():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/v2/follow/bangumi/v3/follows": {"code": -404},
        "/x/space/bangumi/follow/list": {"code": 53013, "message": "用户隐私设置未公开"},
    })
    assert client.bangumi() == []


# ---------------------------------------------------------------------------
# dynamics
# ---------------------------------------------------------------------------

def test_dynamics_normalises_draw_video_and_forward():
    client, session = make_client({
        "/x/polymer/web-dynamic/v1/feed/space": DYNAMIC_PAGE_1,
    })
    items = client.dynamics(MID)

    assert len(items) == 3
    draw, video, forward = items

    assert draw["type"] == "draw"
    assert draw["id"] == "1260542398530846739"
    assert draw["author_mid"] == MID
    assert draw["author_name"] == "xkm星空喵"
    assert draw["author_face"] == "https://i0.hdslb.com/bfs/face/me.jpg"
    assert draw["pub_time"] == 1792331298
    assert draw["text"] == "两张图，随手拍"
    # 新版的图片在 major.opus.pics[].url
    assert draw["images"] == [
        "https://i0.hdslb.com/bfs/new_dyn/a.png",
        "https://i0.hdslb.com/bfs/new_dyn/b.png",
    ]
    assert draw["cover"] == "https://i0.hdslb.com/bfs/new_dyn/a.png"
    assert draw["bvid"] == ""

    assert video["type"] == "video"
    assert video["bvid"] == "BV1Z8a66dETs"
    assert video["title"] == "DeepSleep"
    assert video["cover"] == "https://i0.hdslb.com/bfs/archive/v.jpg"
    assert video["duration"] == 754  # "12:34"
    assert video["url"] == "https://www.bilibili.com/video/BV1Z8a66dETs"

    assert forward["type"] == "forward"
    assert forward["text"] == "转发一下"
    orig = forward["orig"]
    assert orig["type"] == "draw"
    assert orig["author_name"] == "原作者"
    assert orig["text"] == "原动态正文"
    # 老版的图片在 major.draw.items[].src
    assert orig["images"] == ["https://i0.hdslb.com/bfs/draw/x.jpg"]
    assert forward["cover"] == "https://i0.hdslb.com/bfs/draw/x.jpg"

    call = calls_to(session, "feed/space")[0]
    assert call["params"]["host_mid"] == MID
    assert call["params"]["offset"] == ""


def test_dynamics_walks_the_offset_for_later_pages():
    pages = [
        {
            "code": 0,
            "data": {"items": [DYNAMIC_PAGE_1["data"]["items"][1]], "offset": "OPAQUE-2",
                     "has_more": True},
        },
        DYNAMIC_PAGE_1,
    ]

    def answer(params: dict[str, Any]) -> dict[str, Any]:
        return pages[1] if params.get("offset") else pages[0]

    client, session = make_client({"/x/polymer/web-dynamic/v1/feed/space": answer})
    items = client.dynamics(MID, page=2)

    assert len(items) == 4
    offsets = [call["params"]["offset"] for call in calls_to(session, "feed/space")]
    assert offsets == ["", "OPAQUE-2"]


def test_dynamics_returns_empty_list_on_refusal():
    client, _ = make_client({
        "/x/polymer/web-dynamic/v1/feed/space": {"code": -352, "message": "风控校验失败"},
    })
    assert client.dynamics(MID) == []
    assert client.dynamics("") == []


# ---------------------------------------------------------------------------
# bangumi_streams
# ---------------------------------------------------------------------------

def test_bangumi_streams_reads_the_result_block_not_data():
    client, session = make_client({"/pgc/player/web/playurl": PGC_PAYLOAD})
    result = client.bangumi_streams(1172, episode_id=21278)

    assert result["success"] is True
    assert result["season_id"] == 1172
    assert result["ep_id"] == 21278
    assert result["duration"] == 1394  # timelength 是毫秒

    video = result["video"]
    assert video["url"] == "https://upos.example/1080.m4s"
    assert video["backup_urls"] == ["https://upos.example/1080.bak.m4s"]
    assert video["height"] == 1080
    assert video["codecs"] == "avc1.640028"

    audio = result["audio"]
    assert audio["url"] == "https://upos.example/a192.m4s"  # 最高码率
    assert audio["bandwidth"] == 192000

    # PGC 的 accept_quality 是 JSON 数组，_build_qualities 会切错，这里是数组形态
    ids = [item["id"] for item in result["qualities"]]
    assert ids == [112, 80, 64, 32, 16]
    by_id = {item["id"]: item for item in result["qualities"]}
    assert by_id[80]["name"] == "1080P 高清"
    assert by_id[80]["available"] is True
    assert by_id[80]["current"] is True
    assert by_id[112]["current"] is False

    # MP4 预览流作为 muxed 兜底
    assert result["muxed"]["url"] == "https://upos.example/preview.mp4"
    assert result["muxed"]["quality"] == 80

    params = calls_to(session, "pgc/player/web/playurl")[0]["params"]
    assert params["ep_id"] == 21278
    assert params["season_id"] == 1172
    assert params["fnval"] == 4048
    assert params["fourk"] == 1


def test_bangumi_streams_survives_a_refusal_and_a_missing_id():
    client, _ = make_client({
        "/pgc/player/web/playurl": {"code": -404, "message": "啥都木有"},
    })
    result = client.bangumi_streams(1172, episode_id=21278)
    assert result["success"] is False
    assert result["video"] is None and result["audio"] is None
    assert result["message"]

    empty = client.bangumi_streams(0, 0)
    assert empty["success"] is False


# ---------------------------------------------------------------------------
# recommend: scroll 参数决定发哪一套 fresh_*
# ---------------------------------------------------------------------------

RCMD_PAYLOAD = {"code": 0, "data": {"item": []}}


def test_recommend_refresh_sends_the_first_screen_parameters():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/wbi/index/top/feed/rcmd": RCMD_PAYLOAD,
    })
    client.recommend(page_size=12)

    params = calls_to(session, "feed/rcmd")[0]["params"]
    assert params["fresh_type"] == "1"
    assert params["fresh_idx"] == "1"
    assert params["fresh_idx_1h"] == "1"
    assert params["brush"] == "1"
    assert params["y_num"] == "8"
    assert params["last_y_num"] == "8"
    assert params["web_location"] == "1430650"


def test_recommend_scroll_sends_the_paging_parameters():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/wbi/index/top/feed/rcmd": RCMD_PAYLOAD,
    })
    client.recommend(page_size=12, fresh_index=3, scroll=True)

    params = calls_to(session, "feed/rcmd")[0]["params"]
    assert params["fresh_type"] == "4"
    assert params["fresh_idx"] == "3"
    assert params["fresh_idx_1h"] == "3"
    assert params["brush"] == "3"
    assert params["y_num"] == "3"
    assert params["last_y_num"] == "3"
