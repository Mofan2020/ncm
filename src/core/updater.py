"""Download and install SkycMedia updates from GitHub Releases.

The app ships as a PyInstaller bundle: ``SkycMedia.app`` on macOS and a
single ``SkycMedia.exe`` on Windows.  A GitHub release therefore carries one
artefact per platform (``SkycMedia-macOS-arm64.dmg``, ``SkycMedia-Windows-
x64.zip``, ...) and this module has to pick the right one, download it and
put it in place of the running copy.

Only the standard library is used -- ``urllib.request`` for the network side
-- so the module keeps working inside a frozen bundle without pulling in
another dependency.

Every entry point returns a plain dict and never raises: an update check must
not break the app, and a failed download or install must leave the machine
exactly as it was found.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: Base URL of the GitHub REST API.
API_ROOT = "https://api.github.com/repos"

#: The repository the releases are published to.
DEFAULT_REPO = "Mofan2020/ncm"

#: GitHub rejects requests without a user agent, so always send one.
USER_AGENT = "SkycMedia-updater"

#: How much of the release body is kept for the UI.
NOTES_LIMIT = 500

#: The first version-like chunk of a tag: ``v1.2.3-beta`` -> ``1.2.3``.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")

#: What is left of a tag after the numbers: ``-beta``, ``-rc1``, ``""``.
_SUFFIX_RE = re.compile(r"[-_.]?(alpha|beta|preview|pre|rc|dev|nightly|snapshot)\d*", re.IGNORECASE)


class _NoReleases(Exception):
    """The repository has no release at all (HTTP 404)."""


class _RateLimited(Exception):
    """GitHub refused the request because of rate limiting (HTTP 403/429)."""


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """``"v1.2.3-beta"`` -> ``(1, 2, 3)``.

    The numbers are taken from the first version-like chunk of the text, so a
    tag, a release title or a plain version string all work.  A pre-release
    suffix is ignored here -- the numbers decide the order.
    Unparsable text yields ``(0, 0, 0)``, which sorts lowest.
    """
    if not text:
        return (0, 0, 0)
    match = _VERSION_RE.search(str(text))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.group(0).split("."))


def _is_prerelease(text: str) -> bool:
    """Whether a tag carries a pre-release suffix such as ``-beta``."""
    if not text:
        return False
    raw = str(text)
    match = _VERSION_RE.search(raw)
    suffix = raw[match.end():] if match else raw
    return bool(_SUFFIX_RE.match(suffix.strip()))


def _pad(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Pad both tuples with zeros so ``1.2`` and ``1.2.0`` compare equal."""
    length = max(len(left), len(right))
    return (
        left + (0,) * (length - len(left)),
        right + (0,) * (length - len(right)),
    )


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a strictly newer version than ``current``.

    The comparison is numeric component by component, so ``1.10.0`` is newer
    than ``1.9.9`` -- a string compare gets that backwards.  When the numbers
    are equal a pre-release loses against the plain release of the same
    version, so ``1.2.3-beta`` is not newer than ``1.2.3``.
    """
    newer, older = _pad(parse_version(latest), parse_version(current))
    if newer != older:
        return newer > older
    if _is_prerelease(latest) != _is_prerelease(current):
        return not _is_prerelease(latest)
    return False


def _strip_v(tag: str) -> str:
    """``"v1.2.3"`` -> ``"1.2.3"``; a tag without the prefix is kept."""
    text = str(tag or "")
    if text[:1] in ("v", "V"):
        return text[1:]
    return text


# --------------------------------------------------------------------------
# release notes
# --------------------------------------------------------------------------
def _plain_text(body: str) -> str:
    """Strip the markdown decoration from a release body."""
    text = body or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _notes(body: str, limit: int = NOTES_LIMIT) -> str:
    """The first ``limit`` characters of the release body as plain text."""
    return _plain_text(body)[:limit]


# --------------------------------------------------------------------------
# release assets
# --------------------------------------------------------------------------
def _asset_arch(name: str) -> str:
    """The CPU architecture encoded in an artefact name, if any."""
    low = name.lower()
    if "x86_64" in low or "amd64" in low or "x64" in low:
        return "x64"
    if "aarch64" in low or "arm64" in low:
        return "arm64"
    if "x86" in low or "i386" in low or "i686" in low:
        return "x86"
    return ""


def _asset_kind(name: str) -> str:
    """Classify a release artefact.

    ``"windows-x64"``, ``"macos-arm64"``, ``"source"`` or ``"other"``.  The
    architecture is part of the kind because the macOS build ships one
    artefact per CPU and the app has to pick the one it can run.
    """
    low = name.lower()
    if "source" in low and low.endswith((".zip", ".tar.gz", ".tgz")):
        return "source"
    if low.endswith(".exe") or "windows" in low:
        arch = _asset_arch(low)
        return f"windows-{arch}" if arch else "windows"
    if "macos" in low or "darwin" in low or low.endswith(".dmg"):
        arch = _asset_arch(low)
        return f"macos-{arch}" if arch else "macos"
    if "linux" in low:
        arch = _asset_arch(low)
        return f"linux-{arch}" if arch else "linux"
    return "other"


def _asset_list(release: dict[str, Any]) -> list[dict[str, Any]]:
    """The downloadable files of a release, in the shape the UI expects."""
    assets: list[dict[str, Any]] = []
    for item in release.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append({
            "name": name,
            "size": size,
            "url": str(item.get("browser_download_url") or item.get("url") or ""),
            "kind": _asset_kind(name),
        })
    return assets


# --------------------------------------------------------------------------
# talking to GitHub
# --------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext | None:
    """A TLS context that actually trusts somebody.

    ``urllib`` builds its context from the OpenSSL paths that were compiled
    into the interpreter.  Inside a PyInstaller bundle those paths point at
    the build machine and do not exist, so the context ends up with zero
    trusted roots and every HTTPS request fails with "certificate verify
    failed".  ``certifi`` ships with the app (it comes with ``requests``) and
    carries a CA bundle that does exist, so fall back to it.

    Returns ``None`` when no usable context can be built, in which case the
    stock behaviour is kept.
    """
    try:
        context = ssl.create_default_context()
        if context.get_ca_certs():
            return context
    except Exception:  # pragma: no cover - depends on the machine
        log.debug("default SSL context unusable", exc_info=True)
    try:
        import certifi
    except ImportError:
        return None
    try:
        fallback = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        fallback.load_verify_locations(cafile=certifi.where())
        return fallback
    except Exception:  # pragma: no cover - depends on the machine
        log.debug("certifi CA bundle unusable", exc_info=True)
        return None


def _get_json(url: str, timeout: float) -> Any:
    """GET a GitHub API endpoint and decode the JSON body.

    Raises ``_NoReleases`` for 404, ``_RateLimited`` for 403/429 and lets
    every other error through.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NoReleases(str(exc)) from exc
        if exc.code in (403, 429):
            raise _RateLimited(str(exc)) from exc
        raise
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("GitHub API 返回的不是合法的 JSON") from exc


def _pick_latest(releases: list[Any]) -> dict[str, Any] | None:
    """The newest non-draft entry of a release list."""
    best: dict[str, Any] | None = None
    best_version: tuple[int, ...] = ()
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        version = parse_version(release.get("tag_name") or "")
        if best is None or version > best_version:
            best, best_version = release, version
    return best


def check_for_update(
    current_version: str,
    repo: str = DEFAULT_REPO,
    timeout: float = 10.0,
    include_prerelease: bool = False,
) -> dict[str, Any]:
    """Ask GitHub whether a newer release exists.

    Returns a dict with ``success``, ``current``, ``latest``, ``version``,
    ``url``, ``notes``, ``assets`` and ``message``.  ``latest`` is the raw
    tag, ``version`` the same tag without the leading ``v``.

    Never raises: a missing release, a rate limit and a dead network all end
    up as ``success: False`` with a message that says what happened.
    """
    result: dict[str, Any] = {
        "success": False,
        "current": current_version,
        "latest": "",
        "version": "",
        "url": "",
        "notes": "",
        "assets": [],
        "message": "",
    }

    if include_prerelease:
        url = f"{API_ROOT}/{repo}/releases"
    else:
        url = f"{API_ROOT}/{repo}/releases/latest"

    try:
        payload = _get_json(url, timeout)
    except _NoReleases:
        result["message"] = "该仓库还没有发布任何版本"
        return result
    except _RateLimited:
        result["message"] = "GitHub 接口被限流了，请稍后再试"
        return result
    except Exception as exc:  # network trouble, bad JSON, anything else
        log.info("update check failed: %s", exc)
        result["message"] = f"检查更新失败：{exc}"
        return result

    release: dict[str, Any] | None
    if include_prerelease:
        if not isinstance(payload, list):
            result["message"] = "GitHub 接口返回的数据格式不对"
            return result
        release = _pick_latest(payload)
        if release is None:
            result["message"] = "该仓库还没有发布任何版本"
            return result
    else:
        if not isinstance(payload, dict):
            result["message"] = "GitHub 接口返回的数据格式不对"
            return result
        release = payload

    tag = str(release.get("tag_name") or "")
    version = _strip_v(tag)
    result["success"] = True
    result["latest"] = tag
    result["version"] = version
    result["url"] = str(release.get("html_url") or f"https://github.com/{repo}/releases/latest")
    result["notes"] = _notes(str(release.get("body") or ""))
    result["assets"] = _asset_list(release)
    if version and is_newer(version, current_version):
        result["message"] = f"发现新版本 {version}"
    else:
        result["message"] = f"当前版本 {current_version} 已是最新"
    return result


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------
def download_asset(
    url: str,
    dest_path: str,
    timeout: float = 60.0,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Download a release artefact to ``dest_path``.

    The bytes land in a ``.part`` file first and are moved into place at the
    end, so a broken download never leaves a half file behind.  ``progress``
    is called with ``(downloaded_bytes, total_bytes)`` while the file grows;
    ``total_bytes`` is ``0`` when the server does not send a length.

    Returns a dict with ``success``, ``path``, ``bytes`` and ``message``.
    """
    dest = os.path.abspath(dest_path)
    partial = dest + ".part"
    downloaded = 0
    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
        )
        # The asset URL redirects to a CDN, so the opener has to follow it --
        # and it has to do so with the same TLS context.
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context())
        )
        with opener.open(request, timeout=timeout) as response:
            headers = getattr(response, "headers", None)
            length = headers.get("Content-Length") if headers is not None else None
            try:
                total = int(length) if length is not None else 0
            except (TypeError, ValueError):
                total = 0
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(partial, "wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        try:
                            progress(downloaded, total)
                        except Exception:  # a broken UI callback must not stop the download
                            log.debug("progress callback failed", exc_info=True)
        os.replace(partial, dest)
    except Exception as exc:
        log.info("download failed: %s", exc)
        try:
            os.remove(partial)
        except OSError:
            pass
        return {
            "success": False,
            "path": dest,
            "bytes": 0,
            "message": f"下载失败：{exc}",
        }
    return {
        "success": True,
        "path": dest,
        "bytes": downloaded,
        "message": f"已下载 {downloaded} 字节",
    }


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------
def _platform_name() -> str:
    """``"windows"``, ``"macos"`` or the raw platform of the running system."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def _default_target() -> str:
    """The bundle (macOS) or the executable (Windows) that is running now."""
    if _platform_name() == "windows":
        return os.path.abspath(sys.executable)
    parts = os.path.abspath(sys.executable).split(os.sep)
    for index, part in enumerate(parts):
        if part.endswith(".app"):
            return os.sep.join(parts[:index + 1])
    return ""


def _is_writable(path: str) -> bool:
    """Whether the directory holding ``path`` may be written to."""
    parent = os.path.dirname(os.path.abspath(path)) or os.path.abspath(path)
    return os.access(parent, os.W_OK)


def _find_bundle(root: str) -> str:
    """The first ``.app`` bundle inside ``root`` (not recursive)."""
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return ""
    for entry in entries:
        if entry.endswith(".app"):
            return os.path.join(root, entry)
    return ""


def _prepare_bundle(source: str, workdir: str) -> str:
    """Turn a downloaded artefact into a ``.app`` bundle on disk.

    Accepts an ``.app`` bundle, a ``.zip`` containing one (that is what the
    release pipeline publishes) and a ``.dmg`` (mounted read-only for the
    copy).  Returns an empty string when nothing usable was found.
    """
    if source.endswith(".app") and os.path.isdir(source):
        return source

    if source.endswith(".zip"):
        with zipfile.ZipFile(source) as archive:
            archive.extractall(workdir)
        return _find_bundle(workdir)

    if source.endswith(".dmg"):
        mount = os.path.join(workdir, "mount")
        os.makedirs(mount, exist_ok=True)
        try:
            subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, source],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.info("cannot mount %s: %s", source, exc)
            return ""
        try:
            bundle = _find_bundle(mount)
            if not bundle:
                return ""
            copied = os.path.join(workdir, "SkycMedia.app")
            shutil.copytree(bundle, copied, symlinks=True)
            return copied
        finally:
            try:
                subprocess.run(
                    ["hdiutil", "detach", mount],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except (subprocess.SubprocessError, OSError):  # pragma: no cover - unmount is best effort
                log.warning("cannot unmount %s", mount)

    return ""


def _install_macos(source: str, target: str) -> dict[str, Any]:
    """Replace the running ``.app`` with a freshly downloaded one."""
    if not os.path.isdir(target):
        return {
            "success": False,
            "message": f"要替换的应用不存在：{target}",
            "needs_restart": False,
        }
    if not _is_writable(target):
        return {
            "success": False,
            "message": f"没有写入权限，请手动替换到 {target}",
            "needs_restart": False,
        }

    workdir = tempfile.mkdtemp(prefix="skycmedia-update-")
    try:
        try:
            bundle = _prepare_bundle(source, workdir)
        except Exception as exc:
            log.info("cannot prepare %s: %s", source, exc)
            bundle = ""
        if not bundle:
            return {
                "success": False,
                "message": f"看不懂这个更新文件：{source}",
                "needs_restart": False,
            }

        backup = target + ".old"
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        try:
            os.rename(target, backup)
        except OSError as exc:
            return {
                "success": False,
                "message": f"无法替换 {target}：{exc}",
                "needs_restart": False,
            }
        try:
            shutil.move(bundle, target)
        except Exception as exc:
            # Roll back: the old bundle has to go back exactly where it was.
            try:
                if os.path.exists(target):
                    shutil.rmtree(target, ignore_errors=True)
                os.rename(backup, target)
            except OSError:  # pragma: no cover - the disk turned against us
                log.exception("rollback failed for %s", target)
            return {
                "success": False,
                "message": f"更新失败，已恢复原文件：{exc}",
                "needs_restart": False,
            }
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "success": True,
        "message": f"已更新到 {target}，重启后生效",
        "needs_restart": True,
    }


def _find_exe(root: str, preferred: str = "") -> str:
    """The ``.exe`` inside an unpacked archive, if there is one."""
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return ""
    executables = [entry for entry in entries if entry.lower().endswith(".exe")]
    if preferred:
        for entry in executables:
            if entry.lower() == preferred.lower():
                return os.path.join(root, entry)
    for entry in executables:
        return os.path.join(root, entry)
    return ""


def _install_windows(source: str, target: str) -> dict[str, Any]:
    """Replace the running ``.exe`` with a freshly downloaded one.

    Windows locks the running executable, so it is renamed first and the new
    file is copied into place.  The renamed copy stays until the next start
    because it cannot be deleted while the process holds it open.
    """
    if not os.path.isfile(target):
        return {
            "success": False,
            "message": f"要替换的程序不存在：{target}",
            "needs_restart": False,
        }
    if not _is_writable(target):
        return {
            "success": False,
            "message": f"没有写入权限，请手动替换到 {target}",
            "needs_restart": False,
        }

    workdir = ""
    new_binary = source
    if source.endswith(".zip"):
        # The release publishes the .exe inside a zip; unpack it first.
        workdir = tempfile.mkdtemp(prefix="skycmedia-update-")
        try:
            with zipfile.ZipFile(source) as archive:
                archive.extractall(workdir)
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            return {
                "success": False,
                "message": f"解包更新文件失败：{exc}",
                "needs_restart": False,
            }
        new_binary = _find_exe(workdir, os.path.basename(target))
        if not new_binary:
            shutil.rmtree(workdir, ignore_errors=True)
            return {
                "success": False,
                "message": f"这个更新包里没有可执行文件：{source}",
                "needs_restart": False,
            }

    backup = target + ".old"
    if os.path.exists(backup):
        # The leftover of the previous update -- it is not running anymore.
        try:
            os.remove(backup)
        except OSError:
            pass

    try:
        os.replace(target, backup)
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return {
            "success": False,
            "message": f"无法替换正在运行的程序：{exc}",
            "needs_restart": False,
        }
    try:
        shutil.copy2(new_binary, target)
    except Exception as exc:
        try:
            os.replace(backup, target)
        except OSError:  # pragma: no cover - the disk turned against us
            log.exception("rollback failed for %s", target)
        shutil.rmtree(workdir, ignore_errors=True)
        return {
            "success": False,
            "message": f"更新失败，已恢复原文件：{exc}",
            "needs_restart": False,
        }
    shutil.rmtree(workdir, ignore_errors=True)

    return {
        "success": True,
        "message": f"已更新到 {target}，重启后生效",
        "needs_restart": True,
    }


def install_update(source_path: str, target_path: str = "") -> dict[str, Any]:
    """Put a downloaded update in place of the running application.

    ``source_path`` is the artefact that was downloaded: a ``.app`` bundle, a
    ``.zip`` or a ``.dmg`` on macOS, the new ``.exe`` on Windows.
    ``target_path`` is the bundle (macOS) or the executable (Windows) that
    has to be replaced; when it is empty the running one is used.

    The old copy is only thrown away after the new one is in place, and every
    failure rolls back to the state before the call.  Returns a dict with
    ``success``, ``message`` and ``needs_restart``.
    """
    source = os.path.abspath(source_path)
    if not os.path.exists(source):
        return {
            "success": False,
            "message": f"找不到更新文件：{source}",
            "needs_restart": False,
        }

    target = os.path.abspath(target_path) if target_path else _default_target()
    if not target:
        return {
            "success": False,
            "message": "不知道该把更新装到哪里，请手动替换",
            "needs_restart": False,
        }

    if _platform_name() == "windows":
        return _install_windows(source, target)
    return _install_macos(source, target)
