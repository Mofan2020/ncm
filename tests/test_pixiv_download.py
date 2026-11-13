"""Pixiv 下载器的测试。

全部离线：图片和 zip 由假 session 直接给字节，不碰真网络。

要验的几件事：文件名（中文要留住、Windows 非法字符要换掉、长度要截断）、
多页作品一页一个文件、已存在就跳过且不再发请求、失败不留 ``.part``、
动图能拼成帧数正确的 GIF、取消能提前停。
"""
from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

from src.core.pixiv_download import PixivDownloader, sanitize_filename

#: 假 CDN 给的图。长度固定，方便造「已存在且大小一致」。
IMG = b"\xff\xd8\xff" + b"x" * 61


class FakeResponse:
    """够用的假响应。"""

    def __init__(self, content=b"", status_code=200, headers=None, chunk=7):
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}
        self._chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=None):
        size = self._chunk or len(self._content) or 1
        for index in range(0, len(self._content), size):
            yield self._content[index:index + size]


class FakeSession:
    """按地址回字节，并记下被请求过的地址。"""

    def __init__(self, routes=None, status_code=200, headers=None):
        self.routes = dict(routes or {})
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.calls: list[str] = []

    def get(self, url, stream=None, timeout=None, headers=None):  # noqa: ARG002
        self.calls.append(url)
        if self.status_code != 200:
            return FakeResponse(b"", status_code=self.status_code)
        body = self.routes.get(url, IMG)
        content_type = "image/jpeg"
        if url.endswith(".png"):
            content_type = "image/png"
        elif url.endswith(".zip"):
            content_type = "application/zip"
        head = {"content-type": content_type,
                "content-length": str(len(body))}
        head.update(self.headers)
        return FakeResponse(body, headers=head)


class FakeClient:
    """假 Pixiv 客户端：只回测试要的那几样。"""

    def __init__(self, work, pages=None, ugoira=None):
        self.work = work
        self.pages = list(pages or [])
        self.ugoira = dict(ugoira or {})
        self.last_error = ""
        self.calls: list[str] = []

    def illust(self, pid):
        self.calls.append(f"illust:{pid}")
        return dict(self.work)

    def illust_pages(self, pid):
        self.calls.append(f"pages:{pid}")
        return [dict(page) for page in self.pages]

    def ugoira_meta(self, pid):
        self.calls.append(f"ugoira:{pid}")
        return dict(self.ugoira)


def page(index: int, ext: str = ".jpg") -> dict:
    url = f"https://i.pximg.net/img-original/12345678_p{index}{ext}"
    return {"index": index, "thumb": url, "small": url, "regular": url, "original": url,
            "width": 800, "height": 600}


def work(**extra) -> dict:
    base = {"id": "12345678", "title": "夜空のカナタ", "user_name": "絵師",
            "user_id": "99", "page_count": 1, "type": 0, "nsfw": False,
            "original": "", "regular": "", "small": "", "thumb": ""}
    base.update(extra)
    return base


def build_downloader(tmp_path: Path, client, session=None, template=None) -> PixivDownloader:
    kwargs = {"root": tmp_path / "dl", "session": session or FakeSession()}
    if template is not None:
        kwargs["template"] = template
    return PixivDownloader(client, **kwargs)


# ------------------------------------------------------------------ 文件名
def test_render_name_keeps_the_chinese_and_drops_the_bad_characters():
    name = PixivDownloader.render_name(
        "{artist} - {title} - {id}_p{page}",
        work(title='夜/の:カナタ*?「冬」', user_name="絵師<A>|B"), 0, ".jpg")

    assert name.endswith(".jpg")
    # 中文、日文原样留着——换成下划线就把整个标题毁了。
    assert "夜" in name
    assert "カナタ" in name
    assert "「冬」" in name
    for char in '/\\:*?"<>|':
        assert char not in name


def test_render_name_truncates_the_whole_basename_and_keeps_the_extension():
    name = PixivDownloader.render_name("{title}", work(title="长" * 400), 0, ".png")

    assert name.endswith(".png")
    assert len(name) <= 120
    assert name.startswith("长长长")


def test_render_name_survives_a_broken_template():
    name = PixivDownloader.render_name("{artist} {nope} {", work(), 1, ".jpg")
    assert name.endswith(".jpg")
    assert name and name != ".jpg"


def test_render_name_numbers_the_pages_from_zero():
    name = PixivDownloader.render_name("{id}_p{page}", work(), 2, ".jpg")
    assert name == "12345678_p2.jpg"
    # 带格式说明的写法也要能用。
    assert PixivDownloader.render_name("{id}_p{page:02d}", work(), 2, ".jpg") == \
        "12345678_p02.jpg"


def test_sanitize_filename_strips_trailing_dots_and_control_characters():
    assert sanitize_filename("  .a\x01b.  ") == "a-b"
    assert sanitize_filename("") == "unknown"


# ------------------------------------------------------------------ 多页
def test_a_multi_page_work_writes_one_file_per_page(tmp_path: Path):
    client = FakeClient(work(page_count=3), [page(0, ".jpg"), page(1, ".png"),
                                             page(2, ".jpg")])
    session = FakeSession()
    seen: list[dict] = []
    downloader = build_downloader(tmp_path, client, session)

    result = downloader.download("12345678", on_progress=seen.append)

    assert result["ok"] is True, result["error"]
    names = sorted(Path(path).name for path in result["files"])
    assert names == ["絵師 - 夜空のカナタ - 12345678_p0.jpg",
                     "絵師 - 夜空のカナタ - 12345678_p1.png",
                     "絵師 - 夜空のカナタ - 12345678_p2.jpg"]
    assert all(Path(path).is_file() for path in result["files"])
    # 每页一条 downloading + 一条 done。
    assert [item["status"] for item in seen] == \
        ["downloading", "done"] * 3
    assert {item["total"] for item in seen} == {3}
    assert [item["index"] for item in seen] == [0, 0, 1, 1, 2, 2]
    assert all(item["pid"] == "12345678" for item in seen)
    # 静态作品没有 zip / 帧目录。
    assert result["zip"] == ""
    assert result["frames"] == ""
    assert not list((tmp_path / "dl").glob("*.part"))


def test_the_extension_falls_back_to_the_content_type(tmp_path: Path):
    # URL 上没有后缀，只能从 content-type 猜（这里假 CDN 回 image/jpeg）。
    odd = {"index": 0, "original": "https://i.pximg.net/img-original/12345678_p0"}
    client = FakeClient(work(), [odd])
    downloader = build_downloader(tmp_path, client)

    result = downloader.download("12345678")

    assert result["ok"] is True, result["error"]
    assert Path(result["files"][0]).name.endswith(".jpg")


# ------------------------------------------------------------------ 跳过
def test_an_existing_file_is_skipped_without_asking_the_server(tmp_path: Path):
    client = FakeClient(work(), [page(0)])
    session = FakeSession()
    root = tmp_path / "dl"
    root.mkdir(parents=True)
    existing = root / "絵師 - 夜空のカナタ - 12345678_p0.jpg"
    existing.write_bytes(IMG)
    downloader = build_downloader(tmp_path, client, session)

    result = downloader.download("12345678")

    assert result["ok"] is True
    assert result["skipped"] == 1
    assert result["files"] == [str(existing)]
    assert session.calls == []
    assert existing.read_bytes() == IMG


# ------------------------------------------------------------------ 失败
def test_a_failed_download_leaves_no_part_file(tmp_path: Path):
    client = FakeClient(work(page_count=2), [page(0), page(1)])
    session = FakeSession(status_code=500)
    seen: list[dict] = []
    downloader = build_downloader(tmp_path, client, session)

    result = downloader.download("12345678", on_progress=seen.append)

    assert result["ok"] is False
    assert "500" in result["error"]
    assert result["files"] == []
    assert not list((tmp_path / "dl").glob("*.part"))
    assert not list((tmp_path / "dl").glob("*.jpg"))
    assert "failed" in [item["status"] for item in seen]


def test_a_truncated_stream_is_a_failure(tmp_path: Path):
    client = FakeClient(work(), [page(0)])

    class ShortSession(FakeSession):
        def get(self, url, stream=None, timeout=None, headers=None):  # noqa: ARG002
            self.calls.append(url)
            return FakeResponse(IMG, headers={"content-length": "9999"})

    downloader = build_downloader(tmp_path, client, ShortSession())
    result = downloader.download("12345678")

    assert result["ok"] is False
    assert "不完整" in result["error"]
    assert not list((tmp_path / "dl").glob("*.part"))


def test_an_unknown_work_is_reported_not_raised(tmp_path: Path):
    class BrokenClient(FakeClient):
        def illust(self, pid):
            self.last_error = "作品不存在。"
            return {}

    downloader = build_downloader(tmp_path, BrokenClient(work()))
    result = downloader.download("404")

    assert result["ok"] is False
    assert result["error"] == "作品不存在。"


def test_an_r18_work_is_refused_unless_it_is_allowed(tmp_path: Path):
    client = FakeClient(work(x_restrict=2, nsfw=True), [page(0)])
    session = FakeSession()
    downloader = build_downloader(tmp_path, client, session)

    refused = downloader.download("12345678")
    assert refused["ok"] is False
    assert "R-18" in refused["error"]
    assert session.calls == []

    allowed = downloader.download("12345678", private_ok=True)
    assert allowed["ok"] is True, allowed["error"]
    assert len(allowed["files"]) == 1


# ------------------------------------------------------------------ 动图
#: 帧图的内容下载器根本不看（它只负责搬字节），所以用一段固定字节就够。
FRAME = b"\x89PNG\r\n\x1a\n" + b"f" * 24


def make_zip(entries) -> bytes:
    """``[(条目名, 内容), ...]`` → zip 字节。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_a_ugoira_keeps_the_zip_and_unpacks_the_frames(tmp_path: Path):
    frames = [{"file": "000000.png", "delay": 60},
              {"file": "000001.png", "delay": 90},
              {"file": "000002.png", "delay": 120}]
    zip_url = "https://i.pximg.net/img-zip-ugoira/12345678_ugoira600x600.zip"
    payload = make_zip([("000002.png", FRAME + b"2"),
                        ("000000.png", FRAME + b"0"),
                        ("000001.png", FRAME + b"1")])
    client = FakeClient(work(title="動く", type=2), [],
                        {"src": zip_url, "original_src": zip_url,
                         "mime_type": "image/png", "frames": frames})
    session = FakeSession(routes={zip_url: payload})
    downloader = build_downloader(tmp_path, client, session)

    result = downloader.download("12345678")

    assert result["ok"] is True, result["error"]
    # zip 原样留着，就是官方「下载原图」给的那个文件。
    zip_path = Path(result["zip"])
    assert zip_path.suffix == ".zip"
    assert zip_path.is_absolute()
    assert zip_path.read_bytes() == payload
    assert result["files"] == [str(zip_path)]
    # 帧解在旁边的目录里，文件名和 zip 里一致。
    frames_dir = Path(result["frames"])
    assert frames_dir.is_dir()
    assert frames_dir.is_absolute()
    assert frames_dir.parent == zip_path.parent
    assert sorted(path.name for path in frames_dir.iterdir()) == \
        ["000000.png", "000001.png", "000002.png"]
    assert not list((tmp_path / "dl").glob("*.part"))


def test_a_zip_slip_entry_never_lands_outside_the_frames_directory(tmp_path: Path):
    frames = [{"file": "000000.png", "delay": 60}]
    zip_url = "https://i.pximg.net/img-zip-ugoira/12345678_ugoira600x600.zip"
    payload = make_zip([("000000.png", FRAME),
                        ("../../evil.txt", b"pwned"),
                        ("/absolute-evil.txt", b"pwned")])
    client = FakeClient(work(title="動く", type=2), [],
                        {"src": zip_url, "frames": frames})
    downloader = build_downloader(tmp_path, client, FakeSession(routes={zip_url: payload}))

    result = downloader.download("12345678")

    assert result["ok"] is True, result["error"]
    frames_dir = Path(result["frames"])
    assert [path.name for path in frames_dir.iterdir()] == ["000000.png"]
    # 一个字节都不许写到帧目录外面去。
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "dl" / "evil.txt").exists()
    assert not (tmp_path / "absolute-evil.txt").exists()
    assert not list(tmp_path.rglob("evil.txt"))


def test_a_second_run_of_a_ugoira_is_skipped(tmp_path: Path):
    zip_url = "https://i.pximg.net/img-zip-ugoira/12345678_ugoira600x600.zip"
    frames = [{"file": "000000.png", "delay": 60}]
    payload = make_zip([("000000.png", FRAME)])
    client = FakeClient(work(title="動く", type=2), [],
                        {"src": zip_url, "frames": frames})
    session = FakeSession(routes={zip_url: payload})
    downloader = build_downloader(tmp_path, client, session)

    assert downloader.download("12345678")["ok"] is True
    session.calls.clear()
    again = downloader.download("12345678")

    assert again["ok"] is True
    assert again["skipped"] == 1
    assert session.calls == []


# ------------------------------------------------------------------ 取消
def test_cancel_stops_after_the_first_progress_callback(tmp_path: Path):
    client = FakeClient(work(page_count=3), [page(0), page(1), page(2)])
    session = FakeSession()
    downloader = build_downloader(tmp_path, client, session)
    cancel = threading.Event()

    def on_progress(payload: dict) -> None:
        # 界面在第一回调里点「取消」就是这种时序。
        cancel.set()

    result = downloader.download("12345678", on_progress=on_progress, cancel=cancel)

    assert result["ok"] is False
    assert result["error"] == "cancelled"
    assert result["files"] == []
    assert session.calls == []
    assert not list((tmp_path / "dl").glob("*.part"))
