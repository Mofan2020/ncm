"""B站投稿的单元测试。

全部用假传输层和临时目录，不打真网络，也不碰真实账号：

* 分区列表能从回答里读出 tid 和名字
* 投稿的表单参数齐全（标题、分区、简介、标签、csrf、complete_token）
* 服务端报错时返回 ``success False``，不抛异常
* 本地检查不过去的时候一个请求都不发
* 没有 ffmpeg 时截帧返回空串
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.bili_upload import BiliUploader


class FakeResponse:
    """假 HTTP 回答。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if isinstance(
            payload, dict | list) else str(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """假传输层：按 URL 回答，并把每次调用记下来。"""

    def __init__(self, routes=None) -> None:
        self.routes = routes or {}
        # 长路由先匹配，否则 "/x/vuweb/pre" 会把 "/x/vuweb/pre/upload" 吃掉。
        self._patterns = sorted(routes or {}, key=len, reverse=True)
        self.calls: list[dict] = []

    def _answer(self, method: str, url: str, kwargs: dict):
        self.calls.append({
            "method": method,
            "url": url,
            "params": dict(kwargs.get("params") or {}),
            "data": dict(kwargs.get("data") or {}),
            "files": kwargs.get("files") or {},
        })
        for pattern in self._patterns:
            if pattern in url:
                payload = self.routes[pattern]
                if callable(payload):
                    return FakeResponse(payload(self))
                return FakeResponse(payload)
        return FakeResponse({"code": -404, "message": "no route", "data": None})

    def get(self, url: str, params=None, headers=None, timeout=None):
        return self._answer("GET", url, {"params": params})

    def post(self, url: str, data=None, files=None, headers=None, timeout=None):
        return self._answer("POST", url, {"data": data, "files": files})


class FakeClient:
    """假 B站客户端。"""

    def __init__(self, session: FakeSession, signed_in: bool = True) -> None:
        self._session = session
        self.session = session
        self.is_logged_in = signed_in

    def csrf(self) -> str:
        return "csrf-token"

    def _headers(self) -> dict:
        return {"User-Agent": "UA", "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com"}

    def wbi_key(self) -> str:
        return ""


CATEGORY_ANSWER = {
    "code": 0,
    "message": "0",
    "data": {
        "archive_pre": [
            {"tid": 1, "tname": "动画", "tybe": "动画"},
            {"tid": 28, "tname": "音乐", "tybe": "音乐"},
            {"tid": 160, "tname": "生活", "tybe": "生活", "children": [
                {"tid": 161, "tname": "手工"},
            ]},
        ]
    },
}

PRE_ANSWER = {
    "code": 0,
    "message": "0",
    "data": {
        "upload_id": "up-1",
        "endpoint": "https://member.bilibili.com/x/vuweb/pre/upload?up=1",
        "biz_id": "biz-1",
        "complete_token": "token-from-pre",
        "mid": "42",
        "extras": {"bili_filename": "abc123.mp4"},
    },
}

UPLOAD_ANSWER = {
    "code": 0,
    "message": "0",
    "data": {"complete_token": "token-from-upload", "extras": {}},
}

ADD_ANSWER = {
    "code": 0,
    "message": "0",
    "data": {"aid": 987654, "bvid": "BV1xx411c7mD"},
}


def _uploader(routes) -> tuple[BiliUploader, FakeSession]:
    session = FakeSession(routes)
    return BiliUploader(FakeClient(session)), session


# --------------------------------------------------------------------- 分区
def test_categories_reads_tid_and_names():
    uploader, _ = _uploader({"/x/vuweb/pre/category": CATEGORY_ANSWER})
    result = uploader.categories()

    assert result["success"] is True
    items = result["categories"]
    by_tid = {item["tid"]: item for item in items}
    assert by_tid[1]["name"] == "动画"
    assert by_tid[28]["name"] == "音乐"
    assert by_tid[28]["parent"] == "音乐"
    # 子分区也在列表里，父分区是它的名字。
    assert by_tid[161]["name"] == "手工"
    assert by_tid[161]["parent"] == "生活"


def test_categories_falls_back_to_the_other_route():
    routes = {
        "/x/vuweb/pre/category": {"code": -404, "message": "没有这个接口"},
        "/x/vupre/web/archive/pre": CATEGORY_ANSWER,
    }
    uploader, _ = _uploader(routes)
    result = uploader.categories()

    assert result["success"] is True
    assert result["categories"]


def test_categories_failure_is_reported_not_raised():
    uploader, _ = _uploader({"/x/vuweb/pre/category": {
        "code": -101, "message": "账号未登录", "data": None}})
    result = uploader.categories()

    assert result["success"] is False
    assert result["categories"] == []
    assert "未登录" in result["message"] or "登录" in result["message"]


# --------------------------------------------------------------------- 投稿
def test_upload_sends_the_expected_form(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")
    uploader, session = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": UPLOAD_ANSWER,
        "/x/vuweb/add": ADD_ANSWER,
    })

    result = uploader.upload(str(video), "我的标题", 28, desc="简介内容",
                              tags="标签一,标签二")

    assert result["success"] is True, result["message"]
    assert result["aid"] == "987654"
    assert result["bvid"] == "BV1xx411c7mD"

    add_calls = [call for call in session.calls if call["url"].endswith("/x/vuweb/add")]
    assert len(add_calls) == 1
    form = add_calls[0]["data"]
    assert form["title"] == "我的标题"
    assert form["tid"] == "28"
    assert form["desc"] == "简介内容"
    assert form["tag"] == "标签一,标签二"
    assert form["csrf"] == "csrf-token"
    assert form["complete_token"] == "token-from-upload"
    assert form["filename"] == "abc123.mp4"
    assert form["copyright"] == "1"

    # 上传地址是从回答里读出来的，不是写死的。
    upload_calls = [call for call in session.calls if "pre/upload" in call["url"]]
    assert len(upload_calls) == 1
    assert "file" in upload_calls[0]["files"]
    assert upload_calls[0]["data"]["biz_id"] == "biz-1"
    assert upload_calls[0]["data"]["mid"] == "42"

    names = [step["name"] for step in result["steps"]]
    assert names == ["本地检查", "预上传", "上传文件", "提交"]
    assert all(step["ok"] for step in result["steps"])


def test_upload_uses_the_complete_token_from_the_pre_answer(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    # 上传的回答里没有 complete_token，那就用预上传给的那个。
    uploader, session = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": {"code": 0, "data": {}},
        "/x/vuweb/add": ADD_ANSWER,
    })
    uploader.upload(str(video), "标题", 28)
    add_call = [call for call in session.calls if call["url"].endswith("/x/vuweb/add")][0]
    assert add_call["data"]["complete_token"] == "token-from-pre"


def test_upload_sends_the_cover_when_there_is_one(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    uploader, session = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": {
            "code": 0,
            "data": {"complete_token": "t", "url": "//i0.hdslb.com/bfs/archive/cover.jpg"},
        },
        "/x/vuweb/add": ADD_ANSWER,
    })

    result = uploader.upload(str(video), "标题", 28, cover_path=str(cover))
    assert result["success"] is True, result["message"]
    add_call = [call for call in session.calls if call["url"].endswith("/x/vuweb/add")][0]
    assert add_call["data"]["cover"] == "//i0.hdslb.com/bfs/archive/cover.jpg"


def test_upload_goes_on_when_the_cover_fails(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, session = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": UPLOAD_ANSWER,
        "/x/vuweb/add": ADD_ANSWER,
    })
    # 封面文件不存在：投稿继续，但封面这一步是失败的。
    result = uploader.upload(str(video), "标题", 28,
                              cover_path=str(tmp_path / "nope.jpg"))
    assert result["success"] is True, result["message"]
    cover_step = [step for step in result["steps"] if step["name"] == "上传封面"][0]
    assert cover_step["ok"] is False
    add_call = [call for call in session.calls if call["url"].endswith("/x/vuweb/add")][0]
    assert add_call["data"]["cover"] == ""


def test_upload_failure_returns_false_instead_of_raising(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, _ = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": UPLOAD_ANSWER,
        "/x/vuweb/add": {"code": -352, "message": "操作过于频繁", "data": None},
    })

    result = uploader.upload(str(video), "标题", 28)

    assert result["success"] is False
    assert result["aid"] == ""
    assert result["bvid"] == ""
    assert "风控" in result["message"] or "频繁" in result["message"]
    failed = [step for step in result["steps"] if not step["ok"]]
    assert failed and failed[-1]["name"] == "提交"


def test_upload_reports_a_captcha_demand(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, _ = _uploader({
        "/x/vuweb/pre": PRE_ANSWER,
        "/x/vuweb/pre/upload": UPLOAD_ANSWER,
        "/x/vuweb/add": {"code": -352, "message": "需要验证码", "data": None},
    })
    result = uploader.upload(str(video), "标题", 28)
    assert result["success"] is False
    assert "验证码" in result["message"]


def test_upload_fails_when_the_pre_upload_answers_an_error(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, session = _uploader({
        "/x/vuweb/pre": {"code": -400, "message": "参数错误", "data": None},
    })
    result = uploader.upload(str(video), "标题", 28)

    assert result["success"] is False
    assert not any(call["url"].endswith("/x/vuweb/add") for call in session.calls)
    assert result["steps"][-1]["name"] == "预上传"
    assert result["steps"][-1]["ok"] is False


# ----------------------------------------------------------------- 本地检查
def test_missing_file_sends_no_request(tmp_path: Path):
    uploader, session = _uploader({})
    result = uploader.upload(str(tmp_path / "nope.mp4"), "标题", 28)

    assert result["success"] is False
    assert session.calls == []
    assert "找不到" in result["message"]
    assert result["steps"][0]["name"] == "本地检查"
    assert result["steps"][0]["ok"] is False


def test_empty_file_sends_no_request(tmp_path: Path):
    video = tmp_path / "empty.mp4"
    video.write_bytes(b"")
    uploader, session = _uploader({})
    result = uploader.upload(str(video), "标题", 28)

    assert result["success"] is False
    assert session.calls == []
    assert "空" in result["message"]


def test_empty_title_sends_no_request(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, session = _uploader({})
    result = uploader.upload(str(video), "   ", 28)

    assert result["success"] is False
    assert session.calls == []
    assert "标题" in result["message"]


def test_missing_category_sends_no_request(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    uploader, session = _uploader({})
    result = uploader.upload(str(video), "标题", 0)

    assert result["success"] is False
    assert session.calls == []
    assert "分区" in result["message"]


# --------------------------------------------------------------------- 截帧
def test_cover_from_frame_without_ffmpeg(monkeypatch, tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("src.core.bili_upload.shutil.which", lambda name: None)

    uploader, _ = _uploader({})
    assert uploader.cover_from_frame(str(video)) == ""


def test_cover_from_frame_with_a_missing_video(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.core.bili_upload.shutil.which", lambda name: "/usr/bin/ffmpeg")
    uploader, _ = _uploader({})
    assert uploader.cover_from_frame(str(tmp_path / "nope.mp4")) == ""


@pytest.mark.skipif(not Path("/usr/bin/ffmpeg").exists()
                    and not Path("/opt/homebrew/bin/ffmpeg").exists(),
                    reason="ffmpeg 不在预期位置")
def test_cover_from_frame_runs_ffmpeg(monkeypatch, tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("src.core.bili_upload.shutil.which", lambda name: "/bin/echo")
    uploader, _ = _uploader({})
    # echo 不会真的截帧，所以拿不到图；这里只要求它不抛异常。
    assert isinstance(uploader.cover_from_frame(str(video)), str)
