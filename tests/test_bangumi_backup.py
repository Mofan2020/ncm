"""ncmbk 备份里的 Bangumi 那一节。

要验的三件事：

* 导出的文件里有 Bangumi 的收藏和观看历史；
* 导入的时候把它们写回本地；
* **导入不发任何网络请求**——Bangumi 没有账号，那些数据本来就只在本机。

最后一件事用「没有 session 可用」来证明：导入一个备份不应该因为
拿不到客户端而失败。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.core.backup import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    apply_import,
    export_bangumi_data,
    plan_import,
    read_backup_file,
    write_backup_file,
)
from src.core.bangumi_store import BangumiStore


class FakeStore:
    """假 store：记下合并了些什么。"""

    def __init__(self, favorites=None, history=None):
        self.favorites = list(favorites or [])
        self.history = list(history or [])
        self.merged_favorites = None
        self.merged_history = None

    def exportable_state(self):
        return {"favorites": list(self.favorites),
                "history": list(self.history)}

    def merge_favorites(self, entries):
        self.merged_favorites = list(entries or [])
        return len(self.merged_favorites)

    def merge_history(self, entries):
        self.merged_history = list(entries or [])
        return len(self.merged_history)


def test_export_bangumi_data_picks_up_favorites_and_history():
    store = FakeStore(
        favorites=[{"subject_id": "400602", "name_cn": "葬送的芙莉莲",
                    "cover": "https://lain.bangumi.lat/c.jpg",
                    "added": 100.0}],
        history=[{"subject_id": "400602", "episode_id": "1227087", "ep": 1,
                  "name_cn": "冒险的结束", "watched_at": 200.0}],
    )
    section = export_bangumi_data(store)
    assert section["errors"] == []
    assert len(section["favorites"]) == 1
    assert section["favorites"][0]["subject_id"] == "400602"
    assert section["favorites"][0]["name_cn"] == "葬送的芙莉莲"
    assert len(section["history"]) == 1
    assert section["history"][0]["episode_id"] == "1227087"
    assert section["history"][0]["ep"] == 1


def test_export_bangumi_data_skips_broken_rows():
    store = FakeStore(
        favorites=[{"name_cn": "没有 id"}, {"subject_id": "1", "name_cn": "好的"}],
        history=[{"subject_id": "1"}, "不是字典"],
    )
    section = export_bangumi_data(store)
    assert len(section["favorites"]) == 1
    assert len(section["history"]) == 0


def test_export_bangumi_data_survives_a_broken_store():
    class BrokenStore:
        def exportable_state(self):
            raise RuntimeError("磁盘坏了")

    section = export_bangumi_data(BrokenStore())
    assert section["favorites"] == []
    assert section["history"] == []
    assert section["errors"]


def test_export_bangumi_data_reports_progress():
    seen = []
    export_bangumi_data(FakeStore(), progress=lambda p, c, t: seen.append(p))
    assert seen
    assert seen[0] == "bangumi"


def test_roundtrip_through_a_backup_file(tmp_path: Path):
    """存进去再读出来，Bangumi 那一节还在。"""
    store = FakeStore(
        favorites=[{"subject_id": "1", "name_cn": "甲"}],
        history=[{"subject_id": "1", "episode_id": "11", "ep": 1}],
    )
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(store),
    }
    path = tmp_path / "backup.ncmbk"
    write_backup_file(path, payload)
    loaded = read_backup_file(path)
    assert len(loaded["bangumi"]["favorites"]) == 1
    assert loaded["bangumi"]["favorites"][0]["name_cn"] == "甲"
    assert len(loaded["bangumi"]["history"]) == 1


def test_plan_reports_the_bangumi_section():
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(FakeStore(
            favorites=[{"subject_id": "1", "name_cn": "甲"}],
            history=[{"subject_id": "1", "episode_id": "11", "ep": 1}])),
    }
    plan = plan_import(None, payload, uid="")
    section = plan["bangumi"]
    assert section["available"] is True
    assert section["favorite_count"] == 1
    assert section["history_count"] == 1


def test_plan_without_bangumi_section():
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION}
    plan = plan_import(None, payload, uid="")
    assert plan["bangumi"]["available"] is False
    assert plan["bangumi"]["favorites"] == []


def test_apply_restores_bangumi_to_the_local_store():
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(FakeStore(
            favorites=[{"subject_id": "1", "name_cn": "甲"}],
            history=[{"subject_id": "1", "episode_id": "11", "ep": 1}])),
    }
    plan = plan_import(None, payload, uid="")
    assert plan is not None
    store = FakeStore()
    result = apply_import(None, payload, plan, {}, store=store)
    assert result["ok"] is True
    assert result["bangumi"]["favorites_added"] == 1
    assert result["bangumi"]["history_added"] == 1
    assert store.merged_favorites[0]["subject_id"] == "1"
    assert store.merged_history[0]["episode_id"] == "11"


def test_apply_without_a_store_does_not_fail():
    """没有 store（比如只导入了网易云那一半）也不能报错。"""
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(FakeStore(
            favorites=[{"subject_id": "1", "name_cn": "甲"}])),
    }
    plan = plan_import(None, payload, uid="")
    result = apply_import(None, payload, plan, {}, store=None)
    assert result["ok"] is True
    assert result["bangumi"]["favorites_added"] == 0


def test_real_store_roundtrip(tmp_path: Path):
    """真的 store：备份 → 换台机器 → 恢复，数据都在。"""
    source = BangumiStore(tmp_path / "a.json")
    source.set_favorite("400602", True, {"name_cn": "葬送的芙莉莲"})
    source.record_episode("400602", "1227087", {"ep": 1, "name_cn": "冒险的结束"})

    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(source),
    }
    path = tmp_path / "backup.ncmbk"
    write_backup_file(path, payload)
    loaded = read_backup_file(path)

    target = BangumiStore(tmp_path / "b.json")
    plan = plan_import(None, loaded, uid="")
    apply_import(None, loaded, plan, {}, store=target)

    assert target.is_favorite("400602") is True
    assert target.favorites()[0]["name_cn"] == "葬送的芙莉莲"
    history = target.history()
    assert len(history) == 1
    assert history[0]["episode_id"] == "1227087"
    assert history[0]["ep"] == 1


def test_backup_file_keeps_the_bangumi_section_as_json(tmp_path: Path):
    """文件里的那一节要是能直接读的 JSON。"""
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bangumi": export_bangumi_data(FakeStore(
            favorites=[{"subject_id": "1", "name_cn": "甲"}])),
    }
    path = tmp_path / "backup.ncmbk"
    write_backup_file(path, payload)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["bangumi"]["favorites"][0]["name_cn"] == "甲"
