"""从 webview 的 cookie store 里读 PHPSESSID。

用户报的是「登录窗口里登好了，SkycMedia 拿不到 cookie」。真机上开一个窗口
去读，拿回来 9 个 cookie，**每个字段都是 None**——于是这一条单独拉出来钉死。

根因：``webview.create_cookie()`` 返回 ``SimpleCookie``，也就是「cookie 名字
→ Morsel」的映射。``SimpleCookie`` 是 ``dict`` 的子类，所以代码走进了字典分
支，``item.get("name")`` 永远拿到 None，每个 cookie 都因为「没有名字」被丢
掉。窗口里登好了，程序一个 cookie 都读不到。

这里的样例全部按真的形状造：``http.cookies.SimpleCookie`` 就是 pywebview 返
回的那个类型本身，不是模仿。
"""
from __future__ import annotations

import time
from http.cookies import SimpleCookie

from src.core.pixiv import PixivClient
from src.gui.pixiv_bridge import PixivBridge


def _webview_cookie(name: str, value: str, domain: str = ".pixiv.net") -> SimpleCookie:
    """pywebview 返回的单个元素：一个只装一条的 SimpleCookie。"""
    cookie: SimpleCookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["domain"] = domain
    cookie[name]["secure"] = True
    cookie[name]["httponly"] = True
    return cookie


def test_a_simple_cookie_is_read_as_a_named_cookie() -> None:
    """**这条是那个 bug 本身。**

    SimpleCookie 是 dict 的子类，按字典取 ``name`` 取不到东西。读法必须改成
    「key 是名字、值是 Morsel」。
    """
    cookies = [
        _webview_cookie("PHPSESSID", "12345_abcdef"),
        _webview_cookie("device_token", "abcdef0123456789"),
    ]
    assert PixivClient.cookie_from_webview(cookies) == (
        "PHPSESSID=12345_abcdef; device_token=abcdef0123456789")


def test_phpsessid_comes_first() -> None:
    """整串被截断的时候，丢掉的那个不能是 PHPSESSID。"""
    cookies = [
        _webview_cookie("device_token", "t"),
        _webview_cookie("PHPSESSID", "s"),
    ]
    assert PixivClient.cookie_from_webview(cookies).startswith("PHPSESSID=s")


def test_cookies_that_are_not_pixiv_are_dropped() -> None:
    cookies = [
        _webview_cookie("PHPSESSID", "s"),
        _webview_cookie("_ga", "GA1.2.3", domain=".google-analytics.com"),
        _webview_cookie("__cf_bm", "cf", domain=".pixiv.net"),
    ]
    result = PixivClient.cookie_from_webview(cookies)
    assert "_ga" not in result
    assert "__cf_bm=cf" in result


def test_plain_dicts_and_objects_still_work() -> None:
    """别的后端 / 老版本给的是普通字典或对象，两种都要认。"""
    as_dict = [{"name": "PHPSESSID", "value": "d", "domain": ".pixiv.net"}]
    assert PixivClient.cookie_from_webview(as_dict) == "PHPSESSID=d"

    class Obj:
        name = "PHPSESSID"
        value = "o"
        domain = ".pixiv.net"

    assert PixivClient.cookie_from_webview([Obj()]) == "PHPSESSID=o"


def test_an_empty_or_odd_answer_is_not_an_error() -> None:
    assert PixivClient.cookie_from_webview([]) == ""
    assert PixivClient.cookie_from_webview(None) == ""
    assert PixivClient.cookie_from_webview([{}, "junk", 3]) == ""


def _bare_bridge() -> PixivBridge:
    """不跑 __init__：这里只测「读 cookie」那一个方法。"""
    bridge = object.__new__(PixivBridge)
    bridge._cookie_reader = None
    return bridge


def test_a_window_that_never_answers_does_not_hang_the_waiter(
        monkeypatch) -> None:
    """``get_cookies()`` 卡住的时候必须放弃，不能把等待线程一起拖死。

    两个后端都是「往主线程丢个活、等一个信号量」：主线程不应答，这一句就永
    远不返回。卡在那里的话，登录窗口开着也永远读不到 cookie，界面只会一直
    显示「等待登录」。
    """
    import src.gui.pixiv_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "COOKIE_READ_TIMEOUT", 0.3)

    class StuckWindow:
        def get_cookies(self):  # noqa: ANN201
            time.sleep(30)
            return []

    bridge = _bare_bridge()
    started = time.monotonic()
    result = bridge._read_window_cookie(StuckWindow())
    spent = time.monotonic() - started

    assert result == ""
    assert spent < 3.0, f"等了 {spent:.1f} 秒，没有按上限放弃"


def test_a_window_that_answers_is_read(monkeypatch) -> None:
    import src.gui.pixiv_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "COOKIE_READ_TIMEOUT", 3.0)

    class GoodWindow:
        def get_cookies(self):  # noqa: ANN201
            return [_webview_cookie("PHPSESSID", "ok")]

    assert _bare_bridge()._read_window_cookie(GoodWindow()) == "PHPSESSID=ok"
