#!/usr/bin/env python3
"""Mutation check for the Hi-Res track of a Bilibili video.

The tests have to fail when the implementation is broken.  Each mutation
below breaks one rule on purpose and names the test that has to catch it.

The source tree is never touched: the tests are copied into a temporary
directory, the mutation is applied to the copy, and the copy is what runs.
The original tree therefore needs no "restore" step, and a killed run
leaves nothing behind.

Usage:  python3 scripts/check-hires-mutations.py
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "core" / "bilibili.py"

#: (name, snippet to find, snippet to replace, test that must fail)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the ordinary track is taken for the Dolby track",
        'match = entry_id == _AUDIO_ID_DOLBY or "ec-3" in codecs or "ac-3" in codecs',
        'match = entry_id in (_AUDIO_ID_DOLBY, _AUDIO_ID_FLAC) or "mp4a.40.5" in codecs',
        "test_an_ordinary_aac_track_is_not_mistaken_for_dolby",
    ),
    (
        "the lossless track is looked for under the wrong id",
        'match = entry_id == _AUDIO_ID_FLAC or "flac" in codecs',
        'match = entry_id == _AUDIO_ID_AAC_192 or "flac" in codecs',
        "test_the_lossless_track_is_found_by_its_id_alone",
    ),
    (
        "the lossless track is only looked for under its codec name",
        'match = entry_id == _AUDIO_ID_FLAC or "flac" in codecs',
        'match = "flac" in codecs',
        "test_the_lossless_track_is_found_by_its_id_alone",
    ),
    (
        "the flag of the video is read as the permission of the account",
        'result["hires_available"] = bool(flac_block.get("display")) or bool(result["flac"])\n'
        '        result["hires_granted"] = bool(result["flac"])',
        'result["hires_available"] = bool(result["flac"])\n'
        '        result["hires_granted"] = bool(result["flac"])',
        "test_a_video_with_a_lossless_track_says_so",
    ),
    (
        "the permission of the account is dropped",
        'result["hires_granted"] = bool(result["flac"])\n'
        '        result["dolby_available"]',
        'result["hires_granted"] = False\n'
        '        result["dolby_available"]',
        "test_the_track_is_handed_over_when_the_account_may_have_it",
    ),
    (
        "the lossless track is not handed over at all",
        'result["flac"] = self._pick_audio(flac_section) or self._pick_special_audio(\n'
        '            dash.get("audio") or [], flac=True\n'
        '        )',
        'result["flac"] = None',
        "test_the_track_is_handed_over_when_the_account_may_have_it",
    ),
]


def run_tests(work: pathlib.Path, test_file: str) -> tuple[int, str]:
    """Run one test file in the copy; returns (exit code, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_node(work: pathlib.Path, script: str) -> tuple[int, str]:
    """Run one check script in the copy; returns (exit code, output)."""
    node = shutil.which("node")
    if node is None:
        return 0, "node is not installed -- the check cannot run"
    proc = subprocess.run(
        [node, str(work / "scripts" / script)],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


#: The mutations of the frontend, checked by the Node scripts.  The file
#: they live in is the one the scripts read.
JS_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the switch is shown for a video without a track",
        "field.hidden = !available;",
        "field.hidden = false;",
        "a video without a track hides the switch",
    ),
    (
        "the switch is usable without the membership",
        "box.disabled = !granted;",
        "box.disabled = false;",
        "and it is disabled",
    ),
    (
        "the switch is ticked without a track",
        "box.checked = !!wanted && granted;",
        "box.checked = !!wanted;",
        "and it is off",
    ),
    (
        "the reason for the greyed-out switch is dropped",
        "label.setAttribute('data-i18n', granted ? 'bili.hires_on' : 'bili.hires_needs_member');",
        "label.setAttribute('data-i18n', 'bili.hires_on');",
        "and it says why",
    ),
    (
        "the setting is ignored",
        "const wanted = biliPlayer.hiresOverride === null\n"
        "        ? !!(state.settings && state.settings.bili && state.settings.bili.hires_enabled)\n"
        "        : biliPlayer.hiresOverride;",
        "const wanted = false;",
        "the setting on ticks the switch",
    ),
]


def main() -> int:
    original = SOURCE.read_text(encoding="utf-8")
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ncm-hires-mutation-") as tmp:
        work = pathlib.Path(tmp)
        shutil.copytree(ROOT / "src", work / "src")
        shutil.copytree(ROOT / "tests", work / "tests")
        shutil.copytree(ROOT / "scripts", work / "scripts")
        shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
        for name in ("main.py",):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, work / name)

        # The baseline has to pass, or every conclusion below is about a
        # broken harness instead of about the mutation.
        code, output = run_tests(work, "tests/test_bili_hires.py")
        if code != 0:
            print("BASELINE FAILED -- the harness is broken, not the code:")
            print(output[-3000:])
            return 2
        print("baseline: green")

        for name, find, replace, expected in MUTATIONS:
            if find not in original:
                print(f"[BROKEN] {name}: the snippet is not in the source")
                failures.append(name)
                continue
            mutated = original.replace(find, replace, 1)
            if mutated == original:
                print(f"[BROKEN] {name}: the replacement changed nothing")
                failures.append(name)
                continue
            target = work / "src" / "core" / "bilibili.py"
            target.write_text(mutated, encoding="utf-8")
            # The byte cache would keep the old code alive: it is valid as
            # long as the mtime (in seconds) and the size are unchanged.
            for cache in (work / "src" / "core" / "__pycache__").glob("bilibili*.pyc"):
                cache.unlink()

            code, output = run_tests(work, "tests/test_bili_hires.py")
            if code == 0:
                print(f"[NOT CAUGHT] {name}")
                failures.append(name)
            else:
                caught = expected in output
                mark = "caught" if caught else "caught (other test)"
                print(f"[CAUGHT] {name} -- {mark}")

            # Put the original back before the next mutation.
            target.write_text(original, encoding="utf-8")
            for cache in (work / "src" / "core" / "__pycache__").glob("bilibili*.pyc"):
                cache.unlink()

        # ------------------------------------------- the frontend
        app_js = work / "src" / "gui" / "assets" / "app.js"
        original_js = app_js.read_text(encoding="utf-8")
        code, output = run_node(work, "check-bili-hires-switch.mjs")
        if code != 0:
            print("BASELINE FAILED -- the frontend check is broken:")
            print(output[-3000:])
            return 2
        print("baseline (frontend): green")

        for name, find, replace, expected in JS_MUTATIONS:
            if find not in original_js:
                print(f"[BROKEN] {name}: the snippet is not in app.js")
                failures.append(name)
                continue
            mutated = original_js.replace(find, replace, 1)
            if mutated == original_js:
                print(f"[BROKEN] {name}: the replacement changed nothing")
                failures.append(name)
                continue
            app_js.write_text(mutated, encoding="utf-8")
            code, output = run_node(work, "check-bili-hires-switch.mjs")
            if code == 0:
                print(f"[NOT CAUGHT] {name}")
                failures.append(name)
            else:
                caught = expected in output
                mark = "caught" if caught else "caught (other check)"
                print(f"[CAUGHT] {name} -- {mark}")
            app_js.write_text(original_js, encoding="utf-8")

    if failures:
        print(f"\n{len(failures)} mutation(s) survived: {failures}")
        return 1
    print("\nall mutations were caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
