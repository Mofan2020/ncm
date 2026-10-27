#!/usr/bin/env python3
"""Apply the python-for-android patch this build needs, idempotently.

The ``android`` recipe declares only setuptools in its pyproject.toml while
its setup.py does ``import Cython``.  pip's isolated build environment has
no Cython, so the build dies with::

    ModuleNotFoundError: No module named 'Cython'

The fix is to build that one recipe without isolation.  It cannot be turned
off globally: kivy pins exact cython/setuptools/wheel versions and the host
environment has different ones, so every other recipe *needs* the isolation.

The patch edits the toolchain's site-packages, which lives outside the
repository -- a rebuilt toolchain comes back unpatched and the build fails
again with no hint why.  So it is applied here on every build, and applying
it twice is a no-op.

    python android/tools/patch_p4a.py [--buildozer /path/to/buildozer]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ANCHOR = """        build_args = [
            "-m",
            "build",
            "--wheel",
        ]
"""

PATCH = '''
        # The ``android`` package declares only setuptools in its
        # pyproject.toml while its setup.py does ``import Cython``, so an
        # isolated build dies with "No module named 'Cython'".  Every other
        # recipe declares its build requirements properly and *needs* the
        # isolation (kivy wants exact cython/setuptools/wheel versions, and
        # the host env has different ones), so the flag is not global.
        if self.name == "android":
            build_args.append("--no-isolation")
'''

MARKER = 'if self.name == "android":'


def recipe_path(buildozer: Path | None) -> Path:
    """Locate pythonforandroid/recipe.py inside the buildozer environment."""
    python = buildozer.parent / "python" if buildozer else Path(sys.executable)
    try:
        out = subprocess.run(
            [str(python), "-c",
             "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"cannot ask {python} for its site-packages: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    return Path(out.stdout.strip()) / "pythonforandroid" / "recipe.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buildozer", type=Path, default=None,
                        help="path to the buildozer executable")
    args = parser.parse_args()

    path = recipe_path(args.buildozer)
    if not path.is_file():
        print(f"cannot find {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"p4a is already patched: {path}")
        return 0

    if ANCHOR not in text:
        print(f"{path} does not look like the version this patch was written "
              "for -- the anchor is missing", file=sys.stderr)
        return 1

    path.write_text(text.replace(ANCHOR, ANCHOR + PATCH, 1), encoding="utf-8")
    print(f"patched {path}: the android recipe now builds without isolation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
