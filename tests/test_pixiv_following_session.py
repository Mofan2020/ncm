"""关注列表：pixiv 改了参数和字段名，程序得跟上。

实测（2026-11-14，带真会话）：

- ``/ajax/user/{id}/following`` **只认 ``rest=show``**。换成 ``all`` 是 400，
  少传也是 400。``lang=zh`` 是可以的。
- **没有会话也是 400**，不是 401。所以界面上只会挂一句「服务器回了 400。」，
  用户根本不知道是登录过期了。
- 回答里的两个标志跟直觉是反的：``following`` 才是「我关注了对方」，
  ``followed`` 是「对方也关注了我」。旧代码只认 ``isFollowed``，
  于是关注列表里每个人的按钮都显示成「关注」。

这里把这三件事钉住。
"""
from __future__ import annotations

from src.core.pixiv import PixivClient

FOLLOW_PATH = "/ajax/user/42/following"


class _FakeClient(PixivClient):
    """照着 pixiv 的脾气来：rest 不对就 400。"""

    def __init__(self, answers: dict | None = None) -> None:
        super().__init__()
        self._answers = answers or {}
        self.params: dict = {}
        self.refreshed = 0
        self._self = {"id": "42"}

    def _ajax(self, path: str, params=None, **kwargs):  # noqa: ANN001, ANN201
        self.params = dict(params or {})
        if path == FOLLOW_PATH and (params or {}).get("rest") != "show":
            self.last_error = "服务器回了 400。"
            return None
        answer = self._answers.get(path)
        if answer is None:
            self.last_error = "没有安排回答"
            return None
        self.last_error = ""
        return answer

    def refresh_self(self) -> dict:  # noqa: D102
        self.refreshed += 1
        self._self = {"id": "42"}
        return self._self


def _users() -> dict:
    return {
        "users": [
            {
                "userId": "120935239",
                "userName": "瀬乃",
                "profileImageUrl": "https://i.pximg.net/a.jpg",
                "userComment": "插画",
                "following": True,
                "followed": False,
            },
        ],
        "total": 1,
    }


def test_the_route_is_asked_with_rest_show() -> None:
    """rest 不是 show，pixiv 直接 400。"""
    client = _FakeClient({FOLLOW_PATH: _users()})
    body = client.following()
    assert client.params["rest"] == "show"
    assert len(body["users"]) == 1


def test_the_two_flags_are_mapped_the_right_way() -> None:
    """``following`` 才是「我关注了对方」。"""
    client = _FakeClient({FOLLOW_PATH: _users()})
    user = client.following()["users"][0]
    assert user["followed"] is True, "关注列表里的人，我一定是关注了他的"
    assert user["follows_me"] is False
    assert user["comment"] == "插画"


def test_a_dead_session_says_the_session_expired() -> None:
    """带着死 cookie 去问，pixiv 对这条路线回 400——要说的是人话。"""
    client = _FakeClient({})
    client.apply_cookie("PHPSESSID=deadbeef")
    body = client.following()
    assert "登录已失效" in body["error"]
    assert client.session_expired is True
    assert client.signed_in is False


def test_a_guest_is_told_to_log_in() -> None:
    client = _FakeClient({})
    body = client.following()
    assert "登录" in body["error"]
    assert body["users"] == []


def test_the_own_id_is_fetched_when_the_client_forgot_it() -> None:
    """刚启动时还不知道自己是谁：问一次，别直接说「没登录」。"""
    client = _FakeClient({FOLLOW_PATH: _users()})
    client.apply_cookie("PHPSESSID=alive")
    body = client.following()
    assert client.refreshed == 1
    assert len(body["users"]) == 1
