#!/usr/bin/env python3
"""Mutation check for the picture messages of the Bilibili private messages.

The tests have to fail when the implementation is broken.  Each mutation
below breaks one rule on purpose and names the test that has to catch it.

The source tree is never touched: the tree is copied into a temporary
directory, the mutation is applied to the copy, and the copy is what runs.
The original tree therefore needs no "restore" step, and a killed run
leaves nothing behind.

Usage:  python3 scripts/check-bili-im-mutations.py
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "core" / "bili_im.py"
TESTS = "tests/test_bili_im.py"

#: Mutations that are known to behave the same.  They are reported apart from
#: the ones that survived, so a real gap is not lost in the noise.
BEHAVIOUR_EQUIVALENT: set[str] = set()

#: (name, snippet to find, snippet to replace)
MUTATIONS: list[tuple[str, str, str]] = [
    (
        "a failed upload still sends the message",
        """        uploaded = self.upload_image(image_path)
        if not uploaded.get("success"):
            return {"success": False,
                    "message": str(uploaded.get("message") or "图片没有上传成功")}""",
        """        uploaded = self.upload_image(image_path)
        if False:
            return {"success": False,
                    "message": str(uploaded.get("message") or "图片没有上传成功")}""",
    ),
    (
        "a failed send is reported as a success",
        """        sent = self._send(talker_id, MSG_TYPE_IMAGE, payload, session_id)
        if not sent.get("success"):
            return sent""",
        """        sent = self._send(talker_id, MSG_TYPE_IMAGE, payload, session_id)
        if False:
            return sent""",
    ),
    (
        "a picture is sent as a text message",
        "sent = self._send(talker_id, MSG_TYPE_IMAGE, payload, session_id)",
        "sent = self._send(talker_id, MSG_TYPE_TEXT, payload, session_id)",
    ),
    (
        "the picture address is written under the wrong key",
        '_IMAGE_URL_KEY = "url"',
        '_IMAGE_URL_KEY = "link"',
    ),
    (
        "the uploaded file is sent under the wrong field name",
        '_UPLOAD_FILE_FIELD = "file"',
        '_UPLOAD_FILE_FIELD = "image"',
    ),
    (
        "the picture is uploaded to the wrong host",
        'BILI_IM_UPLOAD_BASE = "https://customerservice.bilibili.com"',
        'BILI_IM_UPLOAD_BASE = "https://api.vc.bilibili.com"',
    ),
    (
        "the size of the picture is left out",
        '            "size": int(uploaded.get("size") or 0),\n',
        "",
    ),
    (
        "a picture message shows the JSON instead of the picture",
        """            image = _image_info(parsed)
            if image:
                # 图片消息没有文字正文，content 里就是图片本身。
                content = ""
            else:
                content = _parse_content(entry.get("content"))""",
        """            image = _image_info(parsed)
            content = _parse_content(entry.get("content"))""",
    ),
    (
        "the conversation list shows the JSON of a picture",
        """                "last_message": ("[图片]" if last_image
                                 else _parse_content(last.get("content"))),""",
        """                "last_message": _parse_content(last.get("content")),""",
    ),
    (
        "the upload is sent without a csrf",
        """            "mobi_app": "web",
            "csrf": csrf,
        }
        body = self._post_multipart(""",
        """            "mobi_app": "web",
        }
        body = self._post_multipart(""",
    ),
]


def run_tests(tree: pathlib.Path) -> tuple[int, str]:
    """Run the private-message tests against ``tree``.  Returns (code, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    original = SOURCE.read_text(encoding="utf-8")

    # Every snippet has to be in the source.  A snippet that is not there
    # means the script itself is broken -- that is not "the mutation
    # survived", and the two must not be mixed up.
    for name, snippet, _replacement in MUTATIONS:
        if snippet not in original:
            print(f"ERROR: the script itself is broken: {name}: "
                  f"the snippet was not found in {SOURCE}")
            return 2

    caught: list[str] = []
    equivalent: list[str] = []
    survived: list[str] = []
    broken: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ncm-bili-im-mutate-") as tmp:
        tree = pathlib.Path(tmp) / "tree"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "node_modules", "*.pyc"))

        # The unmutated copy has to pass first: otherwise every verdict
        # below is meaningless.
        code, output = run_tests(tree)
        if code != 0:
            print(f"BASELINE FAILED (exit {code}) -- the mutation run is "
                  f"meaningless:")
            print(output[-3000:])
            return 2
        print("baseline: pass")

        for name, snippet, replacement in MUTATIONS:
            target = tree / SOURCE.relative_to(ROOT)
            target.write_text(original.replace(snippet, replacement, 1),
                               encoding="utf-8")
            # A mutation of the same length inside the same second can leave
            # a stale .pyc behind, and Python believes it.
            for pyc in tree.rglob("__pycache__/*.pyc"):
                try:
                    pyc.unlink()
                except OSError:
                    pass
            code, output = run_tests(tree)
            if code == 1:
                caught.append(name)
                print(f"CAUGHT   {name}")
            elif code in (0, 5):
                if name in BEHAVIOUR_EQUIVALENT:
                    equivalent.append(name)
                    print(f"EQUIVALENT {name}")
                else:
                    survived.append(name)
                    print(f"SURVIVED {name}  <-- the test does not cover this")
            else:
                # exit 2/3/4: the test run itself broke, not the mutation.
                broken.append(name)
                print(f"BROKEN   {name}  (pytest exit {code}, not a failure)")
            target.write_text(original, encoding="utf-8")

    print()
    print(f"caught {len(caught)}/{len(MUTATIONS)}")
    if equivalent:
        print("known equivalent:", ", ".join(equivalent))
    if survived:
        print("SURVIVED:", ", ".join(survived))
    if broken:
        print("BROKEN:", ", ".join(broken))
    return 1 if (survived or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
