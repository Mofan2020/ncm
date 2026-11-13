"""Pixiv 会话文件的读写与失效判定。"""
from __future__ import annotations

import json

from src.auth.pixiv_login import PixivSession


class FakeClient:
    def __init__(self) -> None:
        self.cookie = ""
        self.refresh_calls = 0
        self.answer: dict = {}

    def apply_cookie(self, cookie: str) -> None:
        self.cookie = cookie

    def refresh_self(self) -> dict:
        self.refresh_calls += 1
        return dict(self.answer)

    @property
    def last_error(self) -> str:
        return getattr(self, "_last_error", "")


def _session(tmp_path, client=None) -> PixivSession:
    return PixivSession(client=client or FakeClient(),
                        path=tmp_path / "pixiv.json")


def test_saving_and_loading_keeps_the_account(tmp_path):
    session = _session(tmp_path)
    assert session.save("PHPSESSID=abc; device_token=d",
                        {"id": "42", "name": "星"}) is True

    again = _session(tmp_path)

    assert again.cookie == "PHPSESSID=abc; device_token=d"
    assert again.account["name"] == "星"
    assert again.account["id"] == "42"


def test_the_cookie_reaches_the_client_after_loading(tmp_path):
    _session(tmp_path).save("PHPSESSID=abc", {"id": "42"})
    client = FakeClient()

    _session(tmp_path, client)

    assert client.cookie == "PHPSESSID=abc", "读回来要装进客户端，否则等于没登录"


def test_a_half_written_file_is_not_left_behind(tmp_path):
    session = _session(tmp_path)
    session.save("PHPSESSID=abc", {"id": "42"})

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".pixiv-")]

    assert leftovers == [], "临时文件必须换名掉，留下的会被当成垃圾堆在配置目录"


def test_a_broken_file_logs_out_instead_of_crashing(tmp_path):
    path = tmp_path / "pixiv.json"
    path.write_text("{not json", encoding="utf-8")

    session = _session(tmp_path)

    assert session.is_logged_in is False
    assert session.cookie == ""


def test_the_account_is_not_refetched_when_it_is_known(tmp_path):
    client = FakeClient()
    client.answer = {"id": "42", "name": "星"}
    session = _session(tmp_path, client)
    session.save("PHPSESSID=abc", {"id": "42", "name": "星"})

    info = session.account_info()
    info = session.account_info()

    assert info["logged_in"] is True
    assert client.refresh_calls == 0, "缓存里有身份就别再打一次首页"


def test_refreshing_asks_pixiv(tmp_path):
    client = FakeClient()
    client.answer = {"id": "42", "name": "新名字", "avatar": "a.png"}
    session = _session(tmp_path, client)
    session.save("PHPSESSID=abc", {"id": "42", "name": "旧名字"})

    info = session.account_info(refresh=True)

    assert client.refresh_calls == 1
    assert info["name"] == "新名字"
    assert session.account.get("name") == "新名字"


def test_a_dead_cookie_is_reported_but_not_deleted(tmp_path):
    """过期只报过期。删掉就得重新登——而用户可能只是断网了一次。"""
    client = FakeClient()
    client.answer = {}
    session = _session(tmp_path, client)
    session.save("PHPSESSID=abc", {"id": "42", "name": "星"})

    info = session.account_info(refresh=True)

    assert info["expired"] is True
    assert session.cookie == "PHPSESSID=abc", "cookie 必须留着"
    assert (tmp_path / "pixiv.json").is_file()


def test_clearing_removes_the_file_and_the_client_cookie(tmp_path):
    client = FakeClient()
    session = _session(tmp_path, client)
    session.save("PHPSESSID=abc", {"id": "42"})

    assert session.clear() is True

    assert client.cookie == ""
    assert not (tmp_path / "pixiv.json").exists()
    assert session.account == {}


def test_saving_an_empty_cookie_is_a_logout(tmp_path):
    session = _session(tmp_path)
    session.save("PHPSESSID=abc", {"id": "42"})

    assert session.save("", {}) is True
    assert session.is_logged_in is False


def test_the_saved_file_carries_the_account_next_to_the_cookie(tmp_path):
    """界面上「已登录：某某」是启动时直接读文件的，不能等一次网络往返。"""
    session = _session(tmp_path)
    session.save("PHPSESSID=abc", {"id": "42", "name": "星", "avatar": "a.png"})

    data = json.loads((tmp_path / "pixiv.json").read_text(encoding="utf-8"))

    assert data["id"] == "42"
    assert data["name"] == "星"
    assert data["avatar"] == "a.png"
    assert data["saved_at"]
