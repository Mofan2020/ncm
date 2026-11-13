"""Pixiv 备份与恢复的测试。

全程假对象：不登录、不打 pixiv 的写接口。覆盖的东西：

* 导出的 ``pixiv`` 一节有哪些键，公开 / 私密收藏分开读
* 分页读到没有下一页为止，读失败只让这一节空掉，不影响别的部分
* 导出闸门按「有账号登录」判断：只登录 Pixiv 也能导出
* 老备份（没有 ``pixiv`` 一节）导入时整段跳过
* 当前没登录 Pixiv 时只报「已跳过」，不让整个导入失败
* 恢复收藏和关注调用哪些写接口、可见性有没有带过去、进度有没有上报
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import backup as backup_module
from src.core.backup import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    apply_import,
    export_pixiv_data,
    plan_import,
)

# ------------------------------------------------------------------- 假对象

class FakePixiv:
    """假的 PixivClient：读接口按用例预先写好的数据回答，写接口只记录。

    分页行为跟真客户端一样：``total`` 是全量条数，``has_more`` 由偏移算
    出来。``raises`` 里的键会在对应方法第一次被调用时抛异常。
    """

    def __init__(self, bookmarks: dict[str, list[dict]] | None = None,
                 following: list[dict] | None = None,
                 raises: dict[str, Exception] | None = None) -> None:
        self._bookmarks = bookmarks or {}
        self._following = following or []
        self._raises = dict(raises or {})
        self.calls: list[tuple] = []
        self.bookmark_answer: dict = {"ok": True, "message": ""}
        self.follow_answer: dict = {"ok": True, "message": ""}

    def bookmarks(self, *, offset: int = 0, limit: int = 48,
                  rest: str = "show") -> dict:
        self.calls.append(("bookmarks", offset, limit, rest))
        error = self._raises.pop(f"bookmarks:{rest}", None)
        if error is not None:
            raise error
        works = self._bookmarks.get(rest) or []
        page = works[offset:offset + limit]
        return {"works": page, "total": len(works),
                "has_more": offset + len(page) < len(works)}

    def following(self, *, offset: int = 0, limit: int = 24) -> dict:
        self.calls.append(("following", offset, limit))
        error = self._raises.pop("following", None)
        if error is not None:
            raise error
        page = self._following[offset:offset + limit]
        return {"users": page, "total": len(self._following),
                "has_more": offset + len(page) < len(self._following)}

    def bookmark_add(self, pid: str, *, private: bool = False) -> dict:
        self.calls.append(("bookmark_add", pid, private))
        return dict(self.bookmark_answer)

    def follow(self, uid: str) -> dict:
        self.calls.append(("follow", uid))
        return dict(self.follow_answer)


def _work(pid: str, **kwargs) -> dict:
    work = {"id": pid, "title": f"作品 {pid}", "user_id": "7",
            "user_name": "画师甲", "thumb": f"https://i.pximg.net/c/250x250_80_a2/{pid}.jpg",
            "private": False}
    work.update(kwargs)
    return work


def _user(uid: str, **kwargs) -> dict:
    user = {"id": uid, "name": f"画师 {uid}", "avatar": f"https://i.pximg.net/u/{uid}.jpg"}
    user.update(kwargs)
    return user


def _payload(**extra) -> dict:
    """一个最小的、合法的备份内容。"""
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "account": {},
        "liked_song_ids": [],
        "playlists": [],
        "followed_artists": [],
        "liked_albums": [],
        "subscribed_djradios": [],
        "liked_mvs": [],
        "play_record": [],
        "local_state": {},
    }
    payload.update(extra)
    return payload


# --------------------------------------------------------------------- 导出

def test_the_pixiv_section_has_the_fields_the_restore_needs():
    client = FakePixiv(bookmarks={"show": [_work("1")], "hide": [_work("2")]},
                       following=[_user("9")])

    section = export_pixiv_data(client, account={"id": "42", "name": "星"},
                                sleep=lambda seconds: None)

    bookmark = section["bookmarks"][0]
    assert set(bookmark) == {"id", "title", "user_id", "user_name", "thumb",
                             "private", "bookmarked_at"}
    assert bookmark["title"] == "作品 1"
    assert bookmark["user_id"] == "7"
    assert bookmark["thumb"].startswith("https://i.pximg.net/")
    assert section["following"] == [{"id": "9", "name": "画师 9",
                                     "avatar": "https://i.pximg.net/u/9.jpg"}]
    assert section["account"]["id"] == "42"


def test_public_and_private_bookmarks_are_read_separately():
    """可见性只能靠两条列表分开读出来，合着读会丢。"""
    client = FakePixiv(bookmarks={"show": [_work("1")],
                                  "hide": [_work("2", private=True)]})

    section = export_pixiv_data(client, sleep=lambda seconds: None)

    rests = [call[3] for call in client.calls if call[0] == "bookmarks"]
    assert rests == ["show", "hide"]
    assert [(item["id"], item["private"]) for item in section["bookmarks"]] == [
        ("1", False), ("2", True)]
    assert section["counts"] == {"bookmarks": 2, "public": 1, "private": 1,
                                 "following": 0, "history": 0}


def test_bookmarks_are_paged_until_there_is_no_more():
    works = [_work(str(index)) for index in range(55)]
    client = FakePixiv(bookmarks={"show": works})

    section = export_pixiv_data(client, sleep=lambda seconds: None)

    offsets = [call[1] for call in client.calls
               if call[0] == "bookmarks" and call[3] == "show"]
    assert offsets == [0, 48]
    assert len(section["bookmarks"]) == 55


def test_a_broken_bookmark_read_does_not_lose_the_rest():
    client = FakePixiv(bookmarks={"show": [_work("1")]},
                       following=[_user("9")],
                       raises={"bookmarks:show": RuntimeError("boom")})

    section = export_pixiv_data(client, sleep=lambda seconds: None)

    assert section["bookmarks"] == []
    assert [item["id"] for item in section["following"]] == ["9"]
    assert section["errors"][0]["section"] == "pixiv_bookmarks:show"


def test_the_history_handed_in_is_kept_when_there_is_one():
    client = FakePixiv()

    section = export_pixiv_data(
        client, history=[{"id": "3", "title": "看过", "viewed_at": "2026-01-01"}],
        sleep=lambda seconds: None)

    assert section["history"] == [{"id": "3", "title": "看过", "user_id": "",
                                   "user_name": "", "thumb": "",
                                   "viewed_at": "2026-01-01"}]
    assert section["counts"]["history"] == 1


def test_export_progress_is_reported_under_the_pixiv_phase():
    seen: list[tuple[str, int, int]] = []
    client = FakePixiv(bookmarks={"show": [_work("1")]}, following=[_user("9")])

    export_pixiv_data(client, progress=lambda *args: seen.append(args),
                      sleep=lambda seconds: None)

    assert seen and all(phase == "pixiv" for phase, _, _ in seen)


# ------------------------------------------------------------------ 导入计划

def test_a_backup_without_the_pixiv_section_is_skipped():
    client = FakePixiv()

    plan = plan_import(None, _payload(), uid="", pixiv=client)

    assert plan["ok"] is True
    assert plan["pixiv"]["present"] is False
    assert plan["pixiv"]["available"] is False
    assert plan["pixiv"]["message"] == ""
    assert client.calls == []


def test_the_plan_counts_what_is_still_missing():
    client = FakePixiv(bookmarks={"show": [_work("1"), _work("2")]},
                       following=[_user("9"), _user("10")])
    payload = _payload(pixiv={
        "account": {},
        "bookmarks": [{"id": "1", "title": "a", "private": False},
                      {"id": "2", "title": "b", "private": True}],
        "following": [{"id": "9", "name": "x"}, {"id": "10", "name": "y"}],
    })

    plan = plan_import(None, payload, uid="", pixiv=client)

    assert plan["pixiv"]["bookmarks"]["total"] == 2
    assert plan["pixiv"]["bookmarks"]["to_add"] == []
    assert plan["pixiv"]["bookmarks"]["already"] == 2
    assert plan["pixiv"]["following"]["to_add"] == []
    assert plan["summary"]["pixiv_bookmarks_total"] == 2
    assert plan["summary"]["pixiv_following_to_add"] == 0


def test_the_plan_lists_the_new_ones_to_add():
    client = FakePixiv(bookmarks={"show": [_work("1")]},
                       following=[_user("9")])
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False},
                      {"id": "5", "title": "new", "private": True}],
        "following": [{"id": "9", "name": "x"}, {"id": "11", "name": "new"}],
    })

    plan = plan_import(None, payload, uid="", pixiv=client)

    assert [item["id"] for item in plan["pixiv"]["bookmarks"]["to_add"]] == ["5"]
    assert plan["pixiv"]["bookmarks"]["to_add"][0]["private"] is True
    assert [item["id"] for item in plan["pixiv"]["following"]["to_add"]] == ["11"]
    assert plan["summary"]["pixiv_bookmarks_to_add"] == 1
    assert plan["summary"]["pixiv_following_to_add"] == 1


def test_pixiv_not_signed_in_is_reported_as_skipped_not_failed():
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False}],
        "following": [{"id": "9", "name": "x"}],
    })

    plan = plan_import(None, payload, uid="", pixiv=None)
    result = apply_import(None, payload, plan, None, pixiv=None,
                          sleep=lambda seconds: None, batch_pause=0)

    assert plan["ok"] is True
    assert plan["pixiv"]["present"] is True
    assert plan["pixiv"]["available"] is False
    assert plan["pixiv"]["message"] == "Pixiv 未登录，已跳过"
    assert plan["pixiv"]["bookmarks"]["total"] == 1
    assert result["ok"] is True
    assert result["pixiv"] == {"bookmarks_added": 0, "bookmarks_skipped": 0,
                               "followed": 0, "skipped": 2}
    # 跳过不是失败。
    assert result["failed"] == 0
    assert result["errors"] == []


# ---------------------------------------------------------------------- 恢复

def _plan_with(payload: dict, client: FakePixiv) -> dict:
    return plan_import(None, payload, uid="", pixiv=client)


def test_restore_bookmarks_keeps_the_visibility_and_follows_the_artists():
    client = FakePixiv()
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False},
                      {"id": "2", "title": "b", "private": True}],
        "following": [{"id": "9", "name": "x"}],
    })

    result = apply_import(None, payload, _plan_with(payload, client), None,
                          pixiv=client, sleep=lambda seconds: None, batch_pause=0)

    writes = [call for call in client.calls
              if call[0] in ("bookmark_add", "follow")]
    assert writes == [("bookmark_add", "1", False),
                      ("bookmark_add", "2", True),
                      ("follow", "9")]
    assert result["pixiv"]["bookmarks_added"] == 2
    assert result["pixiv"]["followed"] == 1
    assert result["failed"] == 0


def test_restore_reports_progress_for_pixiv():
    seen: list[tuple[str, int, int]] = []
    client = FakePixiv()
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False},
                      {"id": "2", "title": "b", "private": False}],
        "following": [{"id": "9", "name": "x"}],
    })

    apply_import(None, payload, _plan_with(payload, client), None, pixiv=client,
                 progress=lambda *args: seen.append(args),
                 sleep=lambda seconds: None, batch_pause=0)

    pixiv_steps = [(current, total) for phase, current, total in seen
                   if phase == "pixiv"]
    assert pixiv_steps[0] == (0, 3)
    assert pixiv_steps[-1] == (3, 3)


def test_a_rate_limited_bookmark_is_retried_once():
    client = FakePixiv()
    answers = [{"ok": False, "message": "操作过于频繁，请稍后再试。"},
               {"ok": True, "message": ""}]
    client.bookmark_add = lambda pid, private=False: (
        client.calls.append(("bookmark_add", pid, private)),
        answers.pop(0) if answers else {"ok": True, "message": ""})[1]
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False}],
    })

    result = apply_import(None, payload, _plan_with(payload, client), None,
                          pixiv=client, sleep=lambda seconds: None, batch_pause=0)

    assert result["pixiv"]["bookmarks_added"] == 1
    assert result["failed"] == 0


def test_restore_stops_when_the_pixiv_session_is_gone():
    client = FakePixiv()
    client.bookmark_answer = {"ok": False, "message": "请先登录 Pixiv。"}
    payload = _payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False},
                      {"id": "2", "title": "b", "private": False}],
        "following": [{"id": "9", "name": "x"}],
    })

    result = apply_import(None, payload, _plan_with(payload, client), None,
                          pixiv=client, sleep=lambda seconds: None, batch_pause=0)

    # 会话没了就别再一条一条试：第二条收藏没发出去。
    bookmark_writes = [call for call in client.calls if call[0] == "bookmark_add"]
    assert bookmark_writes == [("bookmark_add", "1", False)]
    assert result["pixiv"]["bookmarks_added"] == 0
    assert result["failed"] >= 1
    assert any("not_logged_in" in item["message"] for item in result["errors"])


# ------------------------------------------------------------------ 导出闸门

class FakePixivSession:
    """只回答「登录了没有、是谁」，不联网。"""

    def __init__(self, logged_in: bool, account: dict | None = None,
                 client: FakePixiv | None = None) -> None:
        self.logged_in = logged_in
        self.account = account or {"id": "42", "name": "星", "account": "skyc",
                                   "avatar": "", "premium": False, "following": 3}
        self.client = client or FakePixiv()

    def account_info(self, refresh: bool = False) -> dict:
        if not self.logged_in:
            return {"logged_in": False, "expired": False}
        return {**self.account, "logged_in": True, "expired": False}


def _bridge_for_export(bridge, monkeypatch, tmp_path, *, pixiv_logged_in: bool,
                       client: FakePixiv | None = None) -> str:
    """把一个真实的 GuiBridge 拆成「只有 Pixiv 登录」的样子。"""
    from src.gui.pixiv_bridge import PixivBridge

    session = FakePixivSession(
        pixiv_logged_in, client=client or FakePixiv(bookmarks={"show": [_work("1")]}))
    fake = PixivBridge(session=session)
    monkeypatch.setattr(bridge, "pixiv", fake)
    monkeypatch.setattr(bridge, "login_manager", SimpleNamespace(user_info=None))
    monkeypatch.setattr(bridge, "bilibili", SimpleNamespace(session=None, client=None))
    target = tmp_path / "backup.ncmbk"
    monkeypatch.setattr(bridge, "_choose_backup_save_path", lambda nickname: str(target))
    return str(target)


def test_one_signed_in_account_is_enough_to_export(bridge, monkeypatch, tmp_path):
    """只登录了 Pixiv：照样导出，文件里就是 Pixiv 那一节。"""
    path = _bridge_for_export(bridge, monkeypatch, tmp_path, pixiv_logged_in=True)

    result = bridge.export_account_backup()

    assert result["success"] is True
    assert result["pixiv"] is True
    assert result["netease"] is False
    assert result["pixiv_bookmarks"] == 1
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["pixiv"]["bookmarks"][0]["id"] == "1"
    assert saved["pixiv"]["account"]["id"] == "42"
    assert saved["account"] == {}


def test_nothing_signed_in_refuses_to_export(bridge, monkeypatch, tmp_path):
    path = _bridge_for_export(bridge, monkeypatch, tmp_path, pixiv_logged_in=False)

    result = bridge.export_account_backup()

    assert result == {"success": False, "error_key": "backup.login_required"}
    assert not Path(path).exists()


def test_a_broken_pixiv_export_still_writes_the_file(bridge, monkeypatch, tmp_path):
    client = FakePixiv(raises={"bookmarks:show": RuntimeError("boom")})
    path = _bridge_for_export(bridge, monkeypatch, tmp_path,
                              pixiv_logged_in=True, client=client)

    result = bridge.export_account_backup()

    assert result["success"] is True
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["pixiv"]["bookmarks"] == []
    assert saved["pixiv"]["errors"][0]["section"] == "pixiv_bookmarks:show"
    assert saved["format"] == BACKUP_FORMAT and saved["version"] == BACKUP_VERSION


# --------------------------------------------------------------- 导入闸门

def test_import_reports_the_pixiv_skip_when_it_is_not_signed_in(
        bridge, monkeypatch, tmp_path):
    """网易云登录着、Pixiv 没登录：整个导入成功，Pixiv 那节写「已跳过」。"""
    from src.gui.pixiv_bridge import PixivBridge

    fake_pixiv = PixivBridge(session=FakePixivSession(False))
    monkeypatch.setattr(bridge, "pixiv", fake_pixiv)
    monkeypatch.setattr(bridge, "bilibili", SimpleNamespace(session=None, client=None))
    user = SimpleNamespace(user_id="1", nickname="甲", avatar_url="",
                           vip_type=0, profile={})
    monkeypatch.setattr(bridge, "login_manager", SimpleNamespace(user_info=user))
    # 假网易云接口补上备份要问的那几个读方法，别让它退到重试。
    api = bridge.api
    api.get_followed_artists = lambda limit=100, offset=0: {"artists": [], "has_more": False}
    api.get_liked_albums = lambda uid, limit=100, offset=0: {"albums": [], "has_more": False}
    api.get_subscribed_djradios = lambda uid, limit=100, offset=0: {
        "djradios": [], "has_more": False}
    api.get_subscribed_mvs = lambda uid, limit=100, offset=0: {"mvs": [], "has_more": False}
    api.get_play_record = lambda uid, scope="all": []
    path = tmp_path / "old.ncmbk"
    path.write_text(json.dumps(_payload(pixiv={
        "bookmarks": [{"id": "1", "title": "a", "private": False}],
        "following": [{"id": "9", "name": "x"}],
    })), encoding="utf-8")

    result = bridge.import_account_backup(str(path))

    assert result["success"] is True
    assert result["pixiv"]["message"] == "Pixiv 未登录，已跳过"
    assert result["pixiv"]["bookmarks"]["total"] == 1
    applied = bridge.apply_account_backup(None)
    assert applied["success"] is True
    assert applied["pixiv"]["skipped"] == 2
    assert applied["failed"] == 0


@pytest.mark.parametrize("version", [3, 4])
def test_an_old_backup_still_imports_without_pixiv(version):
    payload = _payload()
    payload["version"] = version

    plan = plan_import(None, payload, uid="", pixiv=None)

    assert plan["pixiv"]["present"] is False
    assert backup_module.BACKUP_VERSION >= version
