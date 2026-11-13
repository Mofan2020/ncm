"""Pixiv 作品下载。

规矩和 B 站 / Bangumi 那边一样：**先写 ``.part``，字节数对得上再原子改名**，
任何一条失败路径都要把 ``.part`` 删掉。半截的文件比没有更糟——看图软件打开
一张残缺的 jpg 只会给一行「无法解码」。

Pixiv 这边另有三个坑：

* **图片地址要带 Referer。** 不带 ``https://www.pixiv.net/`` 的请求一律 403
  （见 :data:`src.core.pixiv.PIXIV_REFERER`）。所以每个图片请求自己带头，
  不去改调用方那个 session 的默认值——同一个 session 还要发 JSON 请求。
* **一个作品是多张图。** ``pageCount`` 几张就几个文件，``_p<序号>`` 从 0
  开始，和 pixiv 自己的命名一致；扩展名只能从图片 URL 的后缀取，URL 上
  没有时退回响应的 content-type（同一个作品里 png 和 jpg 混着是常事）。
* **动图是一个 zip。** 站点「下载原图」给的就是那个 zip：帧图加一份每帧的
  毫秒延时，页面上那段循环是前端拼的。这里**不合成 GIF**——合成的活儿交给
  界面层（它自己按帧循环），下载器只负责把官方的 zip 原样存下来，再解到旁边
  的 ``<名字>_frames/`` 里，两种消费方式都能用。**不这么做的原因是 Pillow**：
  它会让冻结包大 16 MB，早就被踢出运行时依赖了，``tests/test_runtime_deps.py``
  会扫 ``src/**/*.py``，命中 ``PIL`` 直接红。

  解压要防 zip slip：条目名带 ``..`` 或是个绝对路径的一律丢掉，zip 是远端
  给的字节，不能让它决定写到哪个目录去。

以及一条从 :mod:`src.core.pixiv` 继承下来的约定：**这个模块不往调用方抛
异常**。错误全落在返回值的 ``error`` 里，界面层不用接异常。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from src.core.pixiv import PIXIV_REFERER, PIXIV_UA, is_nsfw

__all__ = ["PixivDownloader", "sanitize_filename", "DEFAULT_TEMPLATE"]

log = logging.getLogger("ncm.pixiv.download")

#: 文件名模板。``{page}`` 是 0 起的页序号（pixiv 自己的习惯）。
DEFAULT_TEMPLATE = "{artist} - {title} - {id}_p{page}"

#: 一次读多少字节。图片比视频大，块也就可以大一点。
CHUNK_SIZE = 256 * 1024

#: 整个 basename 的长度上限。Windows 的 260 字符路径限制是「目录 + 名字」，
#: 名字留 120 已经够长，再长只是把目录那一截挤掉。
MAX_NAME = 120

#: Windows 上一个都不能留的字符。中文、日文、全角符号都不在其中，不能动。
_BAD_CHARS = ('/', '\\', ':', '*', '?', '"', '<', '>', '|')

#: 控制字符（含换行、制表）一律换成 ``-``。用正则是因为这里要按「范围」判断，
#: 一个字符一个字符地 ``replace`` 会漏掉 ``\x01`` 这类没人写但系统会塞的。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: 认识的图片后缀。别的一概不当作「扩展名」，否则 ``1.2`` 这种标题里的点
#: 会被当成后缀。
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})

#: content-type → 后缀。URL 上没有后缀时的退路。
_CONTENT_TYPE_EXTS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

#: 完全看不出格式时的默认后缀。pixiv 的图绝大多数还是 jpg。
_DEFAULT_EXT = ".jpg"

#: ``illustType`` 为 2 就是动图。
_UGOIRA_TYPE = 2


class _Cancelled(Exception):
    """内部用：取消信号。不往外抛。"""


def sanitize_filename(name: str, max_len: int = MAX_NAME) -> str:
    """把任意字符串变成各平台都能用的文件名。

    只替换 Windows 禁用的那几个字符和控制字符。**中文原样保留**——这个项目
    里大半标题是中文，把非 ASCII 换成下划线的清洗方式等于把文件名毁了。
    """
    text = str(name or "")
    for char in _BAD_CHARS:
        text = text.replace(char, "-")
    text = _CONTROL_RE.sub("-", text)
    # 连续空白压成一个空格；首尾的空格和点去掉（Windows 上以点结尾的名字建不出来）。
    text = " ".join(text.split()).strip(" .")
    if not text:
        return "unknown"
    if len(text) > max_len:
        suffix = Path(text).suffix.lower()
        keep = suffix if suffix in _IMAGE_EXTS else ""
        cut = max_len - len(keep)
        text = text[:cut].rstrip(" .") + keep
    return text or "unknown"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _ext_from_url(url: str) -> str:
    """URL 路径上的图片后缀，认不出来就 ``""``。"""
    try:
        path = urlparse(str(url or "")).path
    except ValueError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _IMAGE_EXTS else ""


def _ext_from_content_type(content_type: str) -> str:
    head = str(content_type or "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTS.get(head, "")


def _normalize_ext(ext: str) -> str:
    ext = str(ext or "").strip().lower()
    if not ext:
        return ""
    return ext if ext.startswith(".") else f".{ext}"


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _result() -> dict[str, Any]:
    # zip / frames 只有动图才有值，别的作品留空串，调用方不用分支判 None。
    return {"ok": False, "files": [], "skipped": 0, "error": "",
            "zip": "", "frames": ""}


class PixivDownloader:
    """把一个作品（可能很多页、可能是动图）落到磁盘上。

    ``root`` 是落盘目录；``template`` 是文件名模板，占位符有 ``{artist}``
    ``{title}`` ``{id}`` ``{page}`` ``{page_count}`` ``{user_id}`` ``{type}``。
    ``session`` 可以注入（测试用假 session），默认自己开一个。

    进度回调的参数是 ``{"pid", "index", "total", "path", "status"}``，失败时
    多一个 ``error``；``status`` 是 ``downloading`` / ``done`` / ``skipped`` /
    ``failed``。``total`` 是页数（动图算一页）。
    """

    def __init__(
        self,
        client: Any,
        *,
        root: str | os.PathLike[str],
        template: str = DEFAULT_TEMPLATE,
        session: Any = None,
        timeout: float = 60.0,
    ) -> None:
        self.client = client
        self.root = Path(root).expanduser().resolve()
        self.template = str(template or DEFAULT_TEMPLATE)
        self._session = session if session is not None else requests.Session()
        self.timeout = float(timeout or 60.0)
        self.logger = logging.getLogger("ncm.pixiv.downloader")

    # ------------------------------------------------------------------ naming
    @staticmethod
    def render_name(template: str, work: dict[str, Any], page_index: int,
                    ext: str) -> str:
        """按模板渲染一个文件名（带后缀，已清洗、已截断）。

        模板写错（占位符不存在、花括号没配对）时退回默认模板，而不是抛
        ``KeyError``：模板是用户可配的，一个打错的字符不该让整次下载失败。
        """
        work = work if isinstance(work, dict) else {}
        index = _as_int(page_index, 0)
        values = {
            "artist": str(work.get("user_name") or work.get("artist") or ""),
            "title": str(work.get("title") or ""),
            "id": str(work.get("id") or ""),
            "page": index,
            "page_count": _as_int(work.get("page_count"), 1),
            "user_id": str(work.get("user_id") or ""),
            "type": _as_int(work.get("type"), 0),
        }
        text = ""
        for candidate in (str(template or ""), DEFAULT_TEMPLATE):
            try:
                text = str(candidate).format_map(_TemplateValues(values)).strip()
            except (KeyError, IndexError, ValueError, TypeError):
                text = ""
            if text:
                break
        if not text:
            text = f"{values['id'] or 'pixiv'}_p{index}"
        suffix = _normalize_ext(ext)
        # 整段（连后缀）一起截，sanitize 会保留后缀。
        return sanitize_filename(text + suffix, MAX_NAME)

    # ------------------------------------------------------------------ files
    def _ensure_root(self) -> str:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("pixiv 下载目录建不出来 %s: %s", self.root, exc)
            return f"下载目录建不出来：{exc}"
        return ""

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": PIXIV_UA,
            "Referer": PIXIV_REFERER,
            "Accept": "image/*,*/*;q=0.8",
        }

    @staticmethod
    def _emit(on_progress: Callable[[dict[str, Any]], None] | None,
              pid: str, index: int, total: int, path: str, status: str,
              error: str = "") -> None:
        if on_progress is None:
            return
        payload: dict[str, Any] = {"pid": pid, "index": index, "total": total,
                                   "path": path, "status": status}
        if error:
            payload["error"] = error
        try:
            on_progress(payload)
        except Exception as exc:  # noqa: BLE001 - 回调坏了不该打断下载
            log.debug("pixiv 下载进度回调失败: %s", exc)

    def _stream_to_part(self, url: str, part: Path,
                        cancel: threading.Event) -> tuple[str, int, str]:
        """把 ``url`` 写进 ``part``，返回 ``(content-type, 字节数, 错误)``。

        错误非空即失败（``"cancelled"`` 是取消）；失败时 ``part`` 已经删掉了。
        取 content-type 是为了 URL 上没后缀时能定扩展名。
        """
        written = 0
        content_type = ""
        try:
            with self._session.get(url, headers=self._headers(), stream=True,
                                   timeout=(10, self.timeout)) as response:
                status = int(getattr(response, "status_code", 200) or 200)
                if status != 200:
                    return "", 0, f"服务器回了 {status}。"
                headers = getattr(response, "headers", {}) or {}
                content_type = str(headers.get("content-type") or "")
                total = _as_int(headers.get("content-length"), 0)
                part.parent.mkdir(parents=True, exist_ok=True)
                with open(part, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if cancel.is_set():
                            raise _Cancelled
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
        except _Cancelled:
            _discard(part)
            return "", written, "cancelled"
        except Exception as exc:  # noqa: BLE001 - 网络错误一律降级成返回值
            _discard(part)
            self.logger.info("pixiv 下载失败 %s: %s", url, exc)
            return "", written, str(exc) or exc.__class__.__name__

        if not written:
            _discard(part)
            return content_type, 0, "服务器回了个空文件。"
        if total and written != total:
            # 断流：半个文件比没有更糟。
            _discard(part)
            return content_type, written, f"不完整：{written}/{total} 字节"
        return content_type, written, ""

    # ------------------------------------------------------------------ 单页
    @staticmethod
    def _pick_url(page: dict[str, Any], quality: str) -> str:
        """按要求的画质取地址，取不到就往下降级。

        列表接口给的地址不一定齐全，缺一档时往下退，总比整页下不了强。
        往上升一级（regular → original）也可以：原图只多几十 KB。
        """
        order = [quality, "original", "regular", "small", "thumb"]
        seen: set[str] = set()
        for key in order:
            if not key or key in seen:
                continue
            seen.add(key)
            value = str(page.get(key) or "").strip()
            if value.startswith(("http://", "https://")):
                return value
        return ""

    def _one_page(self, work: dict[str, Any], page: dict[str, Any], index: int,
                  total: int, quality: str,
                  on_progress: Callable[[dict[str, Any]], None] | None,
                  cancel: threading.Event) -> tuple[str, Path | None, str]:
        """下其中一页。返回 ``(状态, 路径, 错误)``。"""
        pid = str(work.get("id") or "")
        url = self._pick_url(page, quality)
        if not url:
            self._emit(on_progress, pid, index, total, "", "failed", "没有可用地址")
            return "failed", None, f"第 {index + 1} 页没有可用地址。"

        ext = _ext_from_url(url)
        # 后缀能定下来时，先按最终名字查一遍：已经下过就一个字节都不请求。
        if ext:
            candidate = self.root / self.render_name(self.template, work, index, ext)
            if candidate.is_file():
                size = candidate.stat().st_size
                if size > 0:
                    # 远程大小要发一次请求才知道；「存在且非空」就是这里能验的全部，
                    # 和 B 站那边一个判据。
                    self._emit(on_progress, pid, index, total, str(candidate), "skipped")
                    return "skipped", candidate, ""

        provisional = self.root / self.render_name(self.template, work, index, ext)
        part = provisional.with_name(provisional.name + ".part")
        self._emit(on_progress, pid, index, total, str(provisional), "downloading")
        # 回调里可能刚被取消（界面就是这么干的），所以这里要再看一眼。
        if cancel.is_set():
            return "cancelled", None, "cancelled"

        content_type, written, error = self._stream_to_part(url, part, cancel)
        if error == "cancelled":
            return "cancelled", None, "cancelled"
        if error:
            self._emit(on_progress, pid, index, total, str(provisional), "failed", error)
            return "failed", None, error

        final_ext = ext or _ext_from_content_type(content_type) or _DEFAULT_EXT
        target = self.root / self.render_name(self.template, work, index, final_ext)
        if target.is_file() and target.stat().st_size == written:
            # URL 没后缀时才知道最终名字，走到这里才发现早就有这份了。
            _discard(part)
            self._emit(on_progress, pid, index, total, str(target), "skipped")
            return "skipped", target, ""
        try:
            os.replace(part, target)
        except OSError as exc:
            _discard(part)
            self._emit(on_progress, pid, index, total, str(target), "failed", str(exc))
            return "failed", None, str(exc)
        self._emit(on_progress, pid, index, total, str(target), "done")
        return "completed", target, ""

    # ---------------------------------------------------------------- 多页作品
    def _download_pages(self, work: dict[str, Any], pid: str, quality: str,
                        on_progress: Callable[[dict[str, Any]], None] | None,
                        cancel: threading.Event, result: dict[str, Any]) -> None:
        pages = [page for page in (self.client.illust_pages(pid) or [])
                 if isinstance(page, dict)]
        if not pages:
            # 页列表接口挂了，作品详情里还有一份地址，够下一张。
            pages = [{"index": 0, "original": work.get("original") or "",
                      "regular": work.get("regular") or "",
                      "small": work.get("small") or "",
                      "thumb": work.get("thumb") or ""}]
        total = len(pages)
        errors: list[str] = []
        for position, page in enumerate(pages):
            if cancel.is_set():
                result["error"] = "cancelled"
                break
            index = _as_int(page.get("index"), position)
            status, path, message = self._one_page(
                work, page, index, total, quality, on_progress, cancel)
            if path is not None:
                result["files"].append(str(path))
            if status == "skipped":
                result["skipped"] += 1
            elif status == "cancelled":
                result["error"] = "cancelled"
                break
            elif status == "failed":
                errors.append(message or f"第 {index + 1} 页下载失败。")
        if not result["error"] and errors:
            result["error"] = "；".join(errors)
        result["ok"] = not result["error"]

    # -------------------------------------------------------------------- 动图
    def _download_ugoira(self, work: dict[str, Any], pid: str,
                         meta: dict[str, Any],
                         on_progress: Callable[[dict[str, Any]], None] | None,
                         cancel: threading.Event, result: dict[str, Any]) -> None:
        """官方 zip 原样存一份，再解到旁边的 ``<名字>_frames/`` 里。

        不合成 GIF：合成的活儿归界面层（它按帧循环），而且 Pillow 不能进
        ``src/``（见模块开头的说明）。
        """
        frames = [frame for frame in (meta.get("frames") or [])
                  if isinstance(frame, dict) and frame.get("file")]
        zip_path = self.root / self.render_name(self.template, work, 0, ".zip")
        frames_dir = self.root / sanitize_filename(f"{zip_path.stem}_frames")
        zip_url = str(meta.get("original_src") or meta.get("src") or "")
        result.update(zip=str(zip_path), frames=str(frames_dir))

        have_zip = zip_path.is_file() and zip_path.stat().st_size > 0
        have_frames = frames_dir.is_dir() and any(frames_dir.iterdir())
        if have_zip and have_frames:
            self._emit(on_progress, pid, 0, 1, str(zip_path), "skipped")
            result.update(ok=True, skipped=1, files=[str(zip_path)])
            return
        if not have_zip and not zip_url:
            result["error"] = "这个动图没有给出 zip 地址。"
            return
        if cancel.is_set():
            result["error"] = "cancelled"
            return

        if not have_zip:
            part = zip_path.with_name(zip_path.name + ".part")
            self._emit(on_progress, pid, 0, 1, str(zip_path), "downloading")
            _content_type, _written, error = self._stream_to_part(zip_url, part, cancel)
            if error == "cancelled":
                result["error"] = "cancelled"
                return
            if error:
                self._emit(on_progress, pid, 0, 1, str(zip_path), "failed", error)
                result["error"] = error
                return
            try:
                os.replace(part, zip_path)
            except OSError as exc:
                _discard(part)
                self._emit(on_progress, pid, 0, 1, str(zip_path), "failed", str(exc))
                result["error"] = str(exc)
                return

        # zip 已经在了但帧目录缺了（用户删过）：只重解一次，不再下载。
        if not have_frames:
            try:
                frames_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                result["error"] = f"帧目录建不出来：{exc}"
                return
            written = _extract_frames(zip_path, frames_dir, frames)
            if not written:
                result["error"] = "zip 里没有解出帧。"
                self._emit(on_progress, pid, 0, 1, str(zip_path), "failed",
                           result["error"])
                return

        result.update(ok=True, files=[str(zip_path)])
        self._emit(on_progress, pid, 0, 1, str(zip_path), "done")

    # ------------------------------------------------------------------ 入口
    def download(self, pid: str, *, quality: str = "original",
                 private_ok: bool = False,
                 on_progress: Callable[[dict[str, Any]], None] | None = None,
                 cancel: threading.Event | None = None) -> dict[str, Any]:
        """下一个作品，返回 ``{"ok", "files", "skipped", "error", "zip", "frames"}``。

        动图的 ``files`` 是那个 zip，``zip`` / ``frames`` 两个键给出 zip 路径和
        解出来的帧目录；单张 / 多页作品的这两个键是空串。

        ``quality`` 是 ``original`` / ``regular`` / ``small`` / ``thumb`` 之一，
        别的值按 ``original`` 处理。``private_ok`` 为假时 R-18 作品直接拒绝——
        和在设置里关掉成人内容时的行为一致。``cancel`` 置位后尽快停下，返回
        已经下完的文件（``error`` 是 ``"cancelled"``）。
        """
        result = _result()
        event = cancel if cancel is not None else threading.Event()
        quality = quality if quality in ("original", "regular", "small", "thumb") \
            else "original"
        try:
            self._download(pid, quality, private_ok, on_progress, event, result)
        except Exception as exc:  # noqa: BLE001 - 最后的兜底，绝不往外抛
            self.logger.warning("pixiv 下载出错 %s: %s", pid, exc)
            result["ok"] = False
            result["error"] = result["error"] or f"下载失败：{exc}"
        return result

    def _download(self, pid: str, quality: str, private_ok: bool,
                  on_progress: Callable[[dict[str, Any]], None] | None,
                  cancel: threading.Event, result: dict[str, Any]) -> None:
        pid = str(pid or "").strip()
        if not pid:
            result["error"] = "没有作品 id。"
            return
        work = self.client.illust(pid) or {}
        if not isinstance(work, dict) or not work.get("id"):
            result["error"] = (str(getattr(self.client, "last_error", "") or "")
                               or "读取作品失败。")
            return
        if not private_ok and is_nsfw(work):
            result["error"] = "这是 R-18 作品，先在设置里允许下载才能下。"
            return
        if cancel.is_set():
            result["error"] = "cancelled"
            return
        error = self._ensure_root()
        if error:
            result["error"] = error
            return

        if _as_int(work.get("type")) == _UGOIRA_TYPE:
            # 静止图这里的 ugoira_meta 会回 404，所以只在动图时才多问一次。
            meta = self.client.ugoira_meta(pid) or {}
            if isinstance(meta, dict) and meta.get("frames") and \
                    (meta.get("original_src") or meta.get("src")):
                self._download_ugoira(work, pid, meta, on_progress, cancel, result)
                return
        self._download_pages(work, pid, quality, on_progress, cancel, result)


class _TemplateValues(dict):
    """模板缺占位符时给空串，而不是抛 ``KeyError``。"""

    def __missing__(self, key: str) -> str:
        return ""


def _safe_member_name(raw: Any) -> str:
    """zip 条目名 → 安全的 basename；看着像路径穿越的一律给 ``""``。

    zip 是远端给的字节。``../../evil.txt``、``/etc/passwd``、``C:\\x`` 这三种
    写法都能写出目标目录（前两种是 zip slip 的经典形态），一律不认。
    """
    name = str(raw or "").replace("\\", "/")
    if not name or "\x00" in name:
        return ""
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return ""
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return parts[-1]


def _extract_frames(zip_path: Path, dest: Path,
                    frames: list[dict[str, Any]]) -> list[Path]:
    """把 zip 里的帧解到 ``dest``，帧名和 zip 里一致，返回解出来的路径。

    只按 ``frames`` 里的文件名解（``frames`` 为空时解全部），顺序按文件名排：
    pixiv 的 zip 里就是 ``000000.jpg`` 这样递增的。
    """
    order: list[str] = []
    for frame in frames:
        name = _safe_member_name(frame.get("file"))
        if name and name not in order:
            order.append(name)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_member_name(info.filename)
                if not name:
                    log.warning("pixiv 动图 zip 里有不安全的条目，跳过：%r",
                                info.filename)
                    continue
                members.setdefault(name, info)
            for name in sorted(order or list(members)):
                info = members.get(name)
                if info is None:
                    continue
                target = dest / name
                with archive.open(info) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                extracted.append(target)
    except (OSError, zipfile.BadZipFile) as exc:
        log.warning("pixiv 动图 zip 解不开 %s: %s", zip_path, exc)
        return []
    return extracted

