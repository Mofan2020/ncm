"""The auto updater: release lookup, download and installation.

Nothing in here touches the network -- every HTTP call is faked at the
``urllib.request.urlopen`` level.
"""
from __future__ import annotations

import email.message
import io
import json
import ssl
import urllib.error
import zipfile
from pathlib import Path

import pytest

from src.core import updater


# --------------------------------------------------------------------------
# fake HTTP layer
# --------------------------------------------------------------------------
class FakeResponse:
    """Stand-in for the object ``urlopen`` returns."""

    def __init__(self, payload: bytes = b"", headers: dict[str, str] | None = None):
        self._buffer = io.BytesIO(payload)
        self.headers = headers or {}

    def read(self, size: int | None = None) -> bytes:
        if size is None:
            return self._buffer.read()
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    """A real ``HTTPError`` so the ``except`` clauses are exercised."""
    return urllib.error.HTTPError(
        "https://api.github.com/repos/Mofan2020/ncm/releases/latest",
        code,
        "boom",
        email.message.Message(),
        io.BytesIO(b""),
    )


@pytest.fixture()
def fake_get(monkeypatch):
    """Replace the HTTP layer and collect the requests that were made."""

    calls: list[object] = []

    def install(handler):
        def urlopen(request, timeout=None, context=None):
            calls.append(request)
            return handler(request)

        class FakeOpener:
            def open(self, request, timeout=None):
                calls.append(request)
                return handler(request)

        monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(updater.urllib.request, "build_opener", lambda *a, **k: FakeOpener())
        return calls

    return install


def release_payload(**overrides) -> dict:
    payload = {
        "tag_name": "v1.2.0",
        "name": "SkycMedia v1.2.0",
        "html_url": "https://github.com/Mofan2020/ncm/releases/tag/v1.2.0",
        "body": "## Fixed\n- the player no longer stutters",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "SkycMedia-macOS-arm64.dmg",
                "size": 1234,
                "browser_download_url": "https://example.invalid/SkycMedia-macOS-arm64.dmg",
            },
            {
                "name": "SkycMedia-Windows-x64.zip",
                "size": 5678,
                "browser_download_url": "https://example.invalid/SkycMedia-Windows-x64.zip",
            },
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# version parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("v1.2.3", (1, 2, 3)),
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3-beta", (1, 2, 3)),
    ("v1.10.0", (1, 10, 0)),
    ("release-2.0.1", (2, 0, 1)),
    ("", (0, 0, 0)),
    ("nonsense", (0, 0, 0)),
])
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


def test_a_prerelease_tag_parses_to_its_numbers():
    assert updater.parse_version("v1.2.3-beta") == (1, 2, 3)


def test_the_comparison_is_numeric_not_lexicographic():
    """1.10.0 is newer than 1.9.9 -- a string compare gets that backwards."""
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert not updater.is_newer("v1.9.9", "1.10.0")


def test_a_prerelease_is_not_newer_than_the_same_release():
    assert not updater.is_newer("v1.2.3-beta", "1.2.3")
    assert updater.is_newer("v1.2.3", "1.2.3-beta")


def test_a_prerelease_is_newer_than_an_older_release():
    assert updater.is_newer("v1.2.3-beta", "1.2.2")


def test_the_same_version_is_not_newer():
    assert not updater.is_newer("v1.2.3", "1.2.3")
    assert not updater.is_newer("1.2.3", "v1.2.3")


def test_versions_with_a_different_number_of_parts_are_compared():
    assert updater.is_newer("1.2.1", "1.2")
    assert not updater.is_newer("1.2", "1.2.0")


# --------------------------------------------------------------------------
# check_for_update
# --------------------------------------------------------------------------
def test_a_newer_release_is_reported(fake_get):
    calls = fake_get(lambda request: FakeResponse(json.dumps(release_payload()).encode()))
    result = updater.check_for_update("1.1.0")

    assert result["success"] is True
    assert result["current"] == "1.1.0"
    assert result["version"] == "1.2.0"
    assert result["url"] == "https://github.com/Mofan2020/ncm/releases/tag/v1.2.0"
    assert "Fixed" in result["notes"]
    assert result["message"]
    assert len(calls) == 1
    assert calls[0].full_url == "https://api.github.com/repos/Mofan2020/ncm/releases/latest"
    assert calls[0].get_header("Accept") == "application/vnd.github+json"


def test_the_release_assets_are_listed_with_a_kind(fake_get):
    fake_get(lambda request: FakeResponse(json.dumps(release_payload()).encode()))
    result = updater.check_for_update("1.1.0")

    kinds = {asset["name"]: asset["kind"] for asset in result["assets"]}
    assert kinds["SkycMedia-macOS-arm64.dmg"] == "macos-arm64"
    assert kinds["SkycMedia-Windows-x64.zip"] == "windows-x64"
    assert result["assets"][0]["size"] == 1234
    assert result["assets"][0]["url"].endswith(".dmg")


def test_the_current_release_is_not_an_update(fake_get):
    fake_get(lambda request: FakeResponse(json.dumps(release_payload(tag_name="v1.1.0")).encode()))
    result = updater.check_for_update("1.1.0")
    assert result["success"] is True
    assert "最新" in result["message"]


def test_a_missing_release_is_reported_in_plain_words(fake_get):
    def handler(request):
        raise http_error(404)

    fake_get(handler)
    result = updater.check_for_update("1.1.0")

    assert result["success"] is False
    assert "还没有发布任何版本" in result["message"]
    assert result["version"] == ""
    assert result["assets"] == []


@pytest.mark.parametrize("code", [403, 429])
def test_rate_limiting_is_reported_in_plain_words(fake_get, code):
    def handler(request):
        raise http_error(code)

    fake_get(handler)
    result = updater.check_for_update("1.1.0")

    assert result["success"] is False
    assert "限流" in result["message"]


def test_a_network_failure_does_not_raise(fake_get):
    def handler(request):
        raise urllib.error.URLError("offline")

    fake_get(handler)
    result = updater.check_for_update("1.1.0")

    assert result["success"] is False
    assert "检查更新失败" in result["message"]


def test_broken_json_does_not_raise(fake_get):
    fake_get(lambda request: FakeResponse(b"<html>nope</html>"))
    result = updater.check_for_update("1.1.0")
    assert result["success"] is False


def test_prereleases_come_from_the_release_list(fake_get):
    payload = [
        {"tag_name": "v2.0.0", "draft": True, "prerelease": True, "body": ""},
        {"tag_name": "v1.5.0", "draft": False, "prerelease": True, "body": "beta"},
        {"tag_name": "v1.4.0", "draft": False, "prerelease": False, "body": "stable"},
    ]
    calls = fake_get(lambda request: FakeResponse(json.dumps(payload).encode()))
    result = updater.check_for_update("1.1.0", include_prerelease=True)

    assert result["success"] is True
    assert result["version"] == "1.5.0"
    assert calls[0].full_url == "https://api.github.com/repos/Mofan2020/ncm/releases"


# --------------------------------------------------------------------------
# the version channel
# --------------------------------------------------------------------------
def test_a_prerelease_is_skipped_when_it_is_not_wanted():
    """The user picked the "latest" channel: a beta must not be offered,
    even when its version number is the higher one."""
    releases = [
        {"tag_name": "v2.0.0", "draft": False, "prerelease": True},
        {"tag_name": "v1.5.0", "draft": False, "prerelease": False},
        {"tag_name": "v1.6.0", "draft": True, "prerelease": False},
    ]

    picked = updater._pick_latest(releases, allow_prerelease=False)

    assert picked is not None
    assert picked["tag_name"] == "v1.5.0"


def test_a_prerelease_is_picked_when_it_is_wanted():
    releases = [
        {"tag_name": "v2.0.0", "draft": False, "prerelease": True},
        {"tag_name": "v1.5.0", "draft": False, "prerelease": False},
        {"tag_name": "v1.6.0", "draft": True, "prerelease": False},
    ]

    picked = updater._pick_latest(releases, allow_prerelease=True)

    assert picked is not None
    assert picked["tag_name"] == "v2.0.0"


def test_the_latest_channel_never_answers_a_prerelease(monkeypatch):
    """``include_prerelease=False`` must not hand out a beta, whatever the
    endpoint says."""
    payload = [
        {"tag_name": "v2.0.0", "draft": False, "prerelease": True, "body": "beta"},
        {"tag_name": "v1.5.0", "draft": False, "prerelease": False, "body": "stable"},
    ]
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout, verify=True: payload)

    result = updater.check_for_update("1.1.0", include_prerelease=False,
                                      allow_prerelease=False)

    assert result["success"] is True
    assert result["version"] == "1.5.0"
    assert "beta" not in result["notes"]


def test_a_single_prerelease_answer_is_refused(monkeypatch):
    """The stable endpoint must not answer with a pre-release either."""
    payload = release_payload(tag_name="v2.0.0", prerelease=True)
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout, verify=True: payload)

    result = updater.check_for_update("1.1.0", include_prerelease=False,
                                      allow_prerelease=False)

    assert result["success"] is False
    assert result["version"] == ""
    assert "正式版本" in result["message"]


def test_the_prerelease_channel_reaches_the_release_list(monkeypatch):
    seen: list[str] = []

    def fake_get_json(url, timeout, verify=True):
        seen.append(url)
        return [{"tag_name": "v1.5.0", "draft": False, "prerelease": True}]

    monkeypatch.setattr(updater, "_get_json", fake_get_json)

    result = updater.check_for_update("1.1.0", include_prerelease=True)

    assert seen == ["https://api.github.com/repos/Mofan2020/ncm/releases"]
    assert result["version"] == "1.5.0"


def test_the_latest_channel_reaches_the_latest_endpoint(monkeypatch):
    seen: list[str] = []

    def fake_get_json(url, timeout, verify=True):
        seen.append(url)
        return release_payload()

    monkeypatch.setattr(updater, "_get_json", fake_get_json)

    updater.check_for_update("1.1.0", include_prerelease=False)

    assert seen == ["https://api.github.com/repos/Mofan2020/ncm/releases/latest"]


def test_the_verification_switch_reaches_the_request(monkeypatch):
    seen: list[bool] = []

    def fake_get_json(url, timeout, verify=True):
        seen.append(verify)
        return release_payload()

    monkeypatch.setattr(updater, "_get_json", fake_get_json)

    updater.check_for_update("1.1.0", verify=False)
    updater.check_for_update("1.1.0", verify=True)

    assert seen == [False, True]


def test_the_download_carries_the_verification_switch(monkeypatch, tmp_path):
    """The download has to use the same TLS setting as the check."""
    seen: list[bool] = []

    class FakeOpener:
        def open(self, request, timeout=None):
            return FakeResponse(b"payload")

    monkeypatch.setattr(updater.urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener())

    def fake_ssl_context(verify: bool = True):
        seen.append(verify)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(updater, "_ssl_context", fake_ssl_context)

    updater.download_asset("https://example.invalid/app.exe",
                           str(tmp_path / "app.exe"), verify=False)

    assert seen == [False]


def test_another_repository_can_be_asked(fake_get):
    calls = fake_get(lambda request: FakeResponse(json.dumps(release_payload()).encode()))
    updater.check_for_update("1.1.0", repo="someone/else")
    assert calls[0].full_url == "https://api.github.com/repos/someone/else/releases/latest"


def test_the_notes_are_cut_to_500_characters(fake_get):
    body = "x" * 900
    fake_get(lambda request: FakeResponse(json.dumps(release_payload(body=body)).encode()))
    result = updater.check_for_update("1.1.0")
    assert len(result["notes"]) == 500


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------
def test_a_context_without_roots_falls_back_to_certifi(monkeypatch):
    """A frozen bundle has no system CA path -- without a fallback every
    HTTPS request fails with "certificate verify failed"."""
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(updater.ssl, "create_default_context", lambda *a, **k: empty)

    context = updater._ssl_context()

    assert context is not None
    assert context.get_ca_certs(), "the fallback context must trust somebody"


def test_a_usable_default_context_is_kept(monkeypatch):
    class Usable:
        def get_ca_certs(self):
            return [{"subject": ()}]

    monkeypatch.setattr(updater.ssl, "create_default_context", lambda *a, **k: Usable())
    assert isinstance(updater._ssl_context(), Usable)


def test_the_certificate_check_can_be_turned_off():
    """A proxy that re-signs the TLS connection breaks the check; the user
    can turn it off."""
    context = updater._ssl_context(verify=False)

    assert context is not None
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_the_certificate_is_checked_by_default():
    context = updater._ssl_context()
    assert context is not None
    assert context.verify_mode != ssl.CERT_NONE


def test_the_context_is_built_without_a_verification():
    """``ssl.CERT_NONE`` and a hostname check cannot live together -- the
    hostname check has to be off before the mode is set, otherwise Python
    raises ``ValueError``."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with pytest.raises(ValueError):
        context.verify_mode = ssl.CERT_NONE

    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_the_check_reaches_the_transport(monkeypatch):
    """The context handed to urllib has to be the unverified one."""
    seen: list[ssl.SSLContext | None] = []

    def fake_urlopen(request, timeout=None, context=None):
        seen.append(context)
        return FakeResponse(json.dumps(release_payload()).encode())

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    updater.check_for_update("1.1.0", verify=False)

    assert seen
    assert seen[0] is not None
    assert seen[0].verify_mode == ssl.CERT_NONE


# --------------------------------------------------------------------------
# download_asset
# --------------------------------------------------------------------------
def test_the_file_is_written_to_disk(fake_get, tmp_path):
    payload = b"binary-payload" * 100
    fake_get(lambda request: FakeResponse(payload))
    dest = tmp_path / "SkycMedia.exe"

    result = updater.download_asset("https://example.invalid/app.exe", str(dest))

    assert result["success"] is True
    assert result["path"] == str(dest)
    assert result["bytes"] == len(payload)
    assert dest.read_bytes() == payload
    assert not (tmp_path / "SkycMedia.exe.part").exists()


def test_the_progress_callback_is_called(fake_get, tmp_path):
    payload = b"0123456789"
    fake_get(lambda request: FakeResponse(payload, {"Content-Length": str(len(payload))}))
    seen: list[tuple[int, int]] = []

    updater.download_asset(
        "https://example.invalid/app.exe",
        str(tmp_path / "app.exe"),
        progress=lambda done, total: seen.append((done, total)),
    )

    assert seen
    assert seen[-1] == (len(payload), len(payload))
    assert all(total == len(payload) for _, total in seen)


def test_a_failed_download_leaves_nothing_behind(fake_get, tmp_path):
    def handler(request):
        raise urllib.error.URLError("offline")

    fake_get(handler)
    dest = tmp_path / "SkycMedia.exe"

    result = updater.download_asset("https://example.invalid/app.exe", str(dest))

    assert result["success"] is False
    assert result["bytes"] == 0
    assert "下载失败" in result["message"]
    assert not dest.exists()
    assert not (tmp_path / "SkycMedia.exe.part").exists()


# --------------------------------------------------------------------------
# install_update
# --------------------------------------------------------------------------
def make_bundle(root: Path, name: str = "SkycMedia.app", marker: str = "old") -> Path:
    bundle = root / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "MacOS" / "SkycMedia").write_text(marker)
    return bundle


def test_a_new_bundle_replaces_the_old_one(tmp_path):
    target = make_bundle(tmp_path / "install", marker="old")
    source = make_bundle(tmp_path / "download", marker="new")

    result = updater.install_update(str(source), str(target))

    assert result["success"] is True
    assert result["needs_restart"] is True
    assert (target / "Contents" / "MacOS" / "SkycMedia").read_text() == "new"
    assert not (tmp_path / "install" / "SkycMedia.app.old").exists()


def test_a_zip_is_unpacked_and_installed(tmp_path):
    target = make_bundle(tmp_path / "install", marker="old")
    source_dir = make_bundle(tmp_path / "download", marker="new")
    archive = tmp_path / "SkycMedia-macOS-arm64.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source_dir.rglob("*"):
            zf.write(path, path.relative_to(source_dir.parent))

    result = updater.install_update(str(archive), str(target))

    assert result["success"] is True
    assert (target / "Contents" / "MacOS" / "SkycMedia").read_text() == "new"


def test_a_failing_install_rolls_back_to_the_old_bundle(tmp_path, monkeypatch):
    target = make_bundle(tmp_path / "install", marker="old")
    source = make_bundle(tmp_path / "download", marker="new")

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(updater.shutil, "move", boom)

    result = updater.install_update(str(source), str(target))

    assert result["success"] is False
    assert "已恢复原文件" in result["message"]
    assert result["needs_restart"] is False
    # The old bundle is back where it was, the new one is nowhere.
    assert (target / "Contents" / "MacOS" / "SkycMedia").read_text() == "old"
    assert not (tmp_path / "install" / "SkycMedia.app.old").exists()


def test_nothing_is_touched_when_the_target_is_not_writable(tmp_path, monkeypatch):
    target = make_bundle(tmp_path / "install", marker="old")
    source = make_bundle(tmp_path / "download", marker="new")

    monkeypatch.setattr(updater, "_is_writable", lambda path: False)

    result = updater.install_update(str(source), str(target))

    assert result["success"] is False
    assert f"请手动替换到 {target}" in result["message"]
    assert (target / "Contents" / "MacOS" / "SkycMedia").read_text() == "old"


def test_a_missing_download_is_reported(tmp_path):
    result = updater.install_update(str(tmp_path / "nope.exe"))
    assert result["success"] is False
    assert "找不到更新文件" in result["message"]


def test_a_broken_archive_leaves_the_old_bundle_alone(tmp_path):
    target = make_bundle(tmp_path / "install", marker="old")
    archive = tmp_path / "SkycMedia-macOS-arm64.zip"
    archive.write_bytes(b"not a zip file at all")

    result = updater.install_update(str(archive), str(target))

    assert result["success"] is False
    assert (target / "Contents" / "MacOS" / "SkycMedia").read_text() == "old"
    assert not (tmp_path / "install" / "SkycMedia.app.old").exists()


def test_the_windows_executable_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_platform_name", lambda: "windows")
    target = tmp_path / "SkycMedia.exe"
    target.write_text("old")
    source = tmp_path / "new.exe"
    source.write_text("new")

    result = updater.install_update(str(source), str(target))

    assert result["success"] is True
    assert result["needs_restart"] is True
    assert target.read_text() == "new"
    # The running executable cannot be deleted, so it is kept next to it.
    assert (tmp_path / "SkycMedia.exe.old").read_text() == "old"


def test_a_failing_windows_install_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_platform_name", lambda: "windows")
    target = tmp_path / "SkycMedia.exe"
    target.write_text("old")
    source = tmp_path / "new.exe"
    source.write_text("new")

    def boom(*args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(updater.shutil, "copy2", boom)

    result = updater.install_update(str(source), str(target))

    assert result["success"] is False
    assert "已恢复原文件" in result["message"]
    assert target.read_text() == "old"
    assert not (tmp_path / "SkycMedia.exe.old").exists()


def test_the_windows_zip_is_unpacked_and_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_platform_name", lambda: "windows")
    target = tmp_path / "SkycMedia.exe"
    target.write_text("old")
    archive = tmp_path / "SkycMedia-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SkycMedia.exe", "new")

    result = updater.install_update(str(archive), str(target))

    assert result["success"] is True
    assert target.read_text() == "new"
    assert (tmp_path / "SkycMedia.exe.old").read_text() == "old"


# --------------------------------------------------------------------------
# the bridge wiring
# --------------------------------------------------------------------------
def test_the_bridge_reports_the_update_switches(bridge):
    """The settings panel reads the switches back; without them in the
    payload the panel always shows the defaults."""
    ui = bridge.get_settings()["ui"]
    assert ui["update_include_prerelease"] is False
    assert ui["update_skip_ssl_verify"] is False


def test_the_bridge_stores_the_update_switches(bridge):
    result = bridge.update_settings("ui", {
        "update_include_prerelease": True,
        "update_skip_ssl_verify": True,
    })

    assert result["success"] is True
    assert result["settings"]["ui"]["update_include_prerelease"] is True
    assert result["settings"]["ui"]["update_skip_ssl_verify"] is True
    assert bridge.settings.ui.update_include_prerelease is True
    assert bridge.settings.ui.update_skip_ssl_verify is True


def test_the_bridge_uses_the_channel_from_the_settings(bridge, monkeypatch):
    """The channel comes from the settings, not from the caller."""
    seen: list[bool] = []

    def fake_check(current_version, **kwargs):
        seen.append(bool(kwargs.get("include_prerelease")))
        return {"success": True, "version": "1.2.0", "latest": "v1.2.0",
                "assets": [], "notes": "", "message": "发现新版本 1.2.0"}

    import src.core.updater as updater_module

    monkeypatch.setattr(updater_module, "check_for_update", fake_check)

    bridge.check_app_update()
    assert seen == [False]

    bridge.settings.update_ui(update_include_prerelease=True)
    bridge.check_app_update()
    assert seen == [False, True]


def test_the_bridge_passes_the_verification_setting(bridge, monkeypatch):
    seen: list[bool] = []

    def fake_check(current_version, **kwargs):
        seen.append(bool(kwargs.get("verify")))
        return {"success": True, "version": "1.2.0", "latest": "v1.2.0",
                "assets": [], "notes": "", "message": "发现新版本 1.2.0"}

    import src.core.updater as updater_module

    monkeypatch.setattr(updater_module, "check_for_update", fake_check)

    bridge.check_app_update()
    assert seen == [True]

    bridge.settings.update_ui(update_skip_ssl_verify=True)
    bridge.check_app_update()
    assert seen == [True, False]


def test_the_bridge_download_uses_the_verification_setting(bridge, monkeypatch):
    seen: list[bool] = []

    def fake_download(url, dest_path, **kwargs):
        seen.append(bool(kwargs.get("verify")))
        return {"success": True, "path": dest_path, "bytes": 1, "message": ""}

    import src.core.updater as updater_module

    monkeypatch.setattr(updater_module, "download_asset", fake_download)

    bridge.download_app_update("https://example.invalid/app.exe", "app.exe")
    assert seen == [True]

    bridge.settings.update_ui(update_skip_ssl_verify=True)
    bridge.download_app_update("https://example.invalid/app.exe", "app.exe")
    assert seen == [True, False]
