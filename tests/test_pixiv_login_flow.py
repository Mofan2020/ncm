"""登录窗口的监视线程：它必须在窗口活着的时候一直看着，而且关窗时再补看一眼。

这里钉三件事，正好是用户报的那两句话：

1. ``window.native is None`` **不代表窗口关掉了**——窗口刚建出来时它就是
   None。以前把它当「已关闭」，线程第一拍就退出，窗口没人关、cookie 没人
   取、界面上一直写未登录。
2. 登完之后要**自己把窗口关掉**，不是让用户手动关。
3. 用户看着页面已经进了首页、自己手动关窗，**也得算登录**——关窗时再读一次
   cookie 再验一次。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.gui import pixiv_bridge as bridge_mod
from src.gui.pixiv_bridge import PixivBridge

GOOD_COOKIE = "PHPSESSID=abc123; p_ab_id=9"


class _Event:
    """够用的 ``window.events.closed``。"""

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):  # noqa: ANN001, ANN204
        self._handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in list(self._handlers):
            handler()


class _Events:
    def __init__(self) -> None:
        self.closed = _Event()


class _FakeWindow:
    """一扇假窗口。``cookies`` 可以中途换，模拟「用户登完了」。"""

    def __init__(self, cookies: str = "", native=None) -> None:  # noqa: ANN001
        self.cookies = cookies
        self.native = native
        self.events = _Events()
        self.destroyed = False
        self.reads = 0

    def get_cookies(self):
        self.reads += 1
        if not self.cookies:
            return []
        return [{"name": part.split("=", 1)[0], "value": part.split("=", 1)[1],
                 "domain": ".pixiv.net"}
                for part in self.cookies.split("; ") if "=" in part]

    def destroy(self) -> None:
        self.destroyed = True


class _FakeSession:
    def __init__(self, account: dict | None = None) -> None:
        self.saved: list[tuple[str, dict]] = []
        self._account = account if account is not None else {}
        self.client = type("C", (), {"last_error": ""})()

    def save(self, cookie, info):  # noqa: ANN001
        self.saved.append((cookie, info))

    def account_info(self, refresh=False):  # noqa: ANN001
        return dict(self._account)


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    # 轮询间隔在测试里没意义，压到毫秒级。
    monkeypatch.setattr(bridge_mod, "COOKIE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(bridge_mod, "COOKIE_READ_TIMEOUT", 2.0)


def test_a_window_without_a_native_object_is_not_a_closed_window() -> None:
    """这条就是那个 bug 的本体。"""
    assert PixivBridge._window_gone(_FakeWindow(native=None)) is False
    assert PixivBridge._window_gone(_FakeWindow(native=object())) is False
    assert PixivBridge._window_gone(_FakeWindow(native=None)) is False


def test_it_closes_the_window_by_itself_after_the_login_lands() -> None:
    session = _FakeSession({"id": "42", "name": "狐狸", "account": "fox"})
    bridge = PixivBridge(session=session)
    window = _FakeWindow()
    # login_open() 会先把它记下来，_close_login_window() 靠的就是这个引用。
    bridge._login_window = window

    thread = threading.Thread(target=bridge._watch_login, args=(window,), daemon=True)
    thread.start()
    time.sleep(0.1)
    window.cookies = GOOD_COOKIE          # 用户此刻登完了
    thread.join(3)

    assert not thread.is_alive(), "登完了还挂在循环里，窗口就永远不关"
    assert window.destroyed is True, "登完之后窗口要自己关掉"
    assert bridge._login_state == "ok"
    assert session.saved and session.saved[-1][1].get("id") == "42"


def test_closing_the_window_by_hand_still_counts_as_signed_in() -> None:
    """关窗之后补的那一眼：cookie 好就是登录，不是「用户不想登了」。

    这条直接测那一小步。放进整条循环里测会被轮询抢先生效——循环自己就把
    cookie 读走了，看不出「关窗之后还有没有再读一眼」。
    """
    session = _FakeSession({"id": "42", "name": "狐狸"})
    bridge = PixivBridge(session=session)
    window = _FakeWindow(cookies=GOOD_COOKIE)

    bridge._final_login_check(window, "")

    assert bridge._login_state == "ok"
    assert session.saved, "读到了 cookie 就得装进会话"


def test_that_last_look_falls_back_to_what_the_loop_already_saw() -> None:
    """窗口已经关掉、cookie store 读不出来时，用轮询最后读到的那串。"""
    session = _FakeSession({"id": "7", "name": "狐狸"})
    bridge = PixivBridge(session=session)
    bridge._final_login_check(_FakeWindow(cookies=""), GOOD_COOKIE)
    assert bridge._login_state == "ok"


def test_the_close_event_stops_the_watcher() -> None:
    """窗口关掉之后线程要退出来，不能一直转。"""
    bridge = PixivBridge(session=_FakeSession({}))
    window = _FakeWindow(cookies="")
    thread = threading.Thread(target=bridge._watch_login, args=(window,), daemon=True)
    thread.start()
    time.sleep(0.05)
    window.events.closed.fire()
    thread.join(3)
    assert not thread.is_alive(), "关窗之后监视线程还在转"
    assert bridge._login_state == "cancelled"
    assert window.destroyed is False, "用户自己关的窗，不用我们再去 destroy"


def test_closing_the_window_without_a_login_stays_cancelled() -> None:
    session = _FakeSession({})
    bridge = PixivBridge(session=session)
    window = _FakeWindow(cookies="")
    thread = threading.Thread(target=bridge._watch_login, args=(window,), daemon=True)
    thread.start()
    window.events.closed.fire()
    thread.join(3)
    assert bridge._login_state == "cancelled"


def test_a_dead_cookie_does_not_turn_into_a_success() -> None:
    """cookie 里没有 PHPSESSID，或者验不出身份，都不能算登录。"""
    session = _FakeSession({})            # 读不出 id
    bridge = PixivBridge(session=session)
    window = _FakeWindow(cookies="p_ab_id=9")
    bridge.login_cancel()
    thread = threading.Thread(target=bridge._watch_login, args=(window,), daemon=True)
    thread.start()
    window.events.closed.fire()
    thread.join(3)
    assert bridge._login_state == "cancelled"
    assert session.saved == []


def test_login_open_reuses_the_open_window_instead_of_a_second_one() -> None:
    """点第二次不该再开一扇窗。"""
    class _Webview:
        def __init__(self) -> None:
            self.created = 0

        def create_window(self, **kwargs):  # noqa: ANN003, ANN201
            self.created += 1
            return _FakeWindow()

    view = _Webview()
    bridge = PixivBridge(session=_FakeSession({}))
    first = bridge.login_open(webview=view)
    assert first["success"] is True
    second = bridge.login_open(webview=view)
    assert second["state"] == "waiting"
    assert view.created == 1
    bridge.login_cancel()
