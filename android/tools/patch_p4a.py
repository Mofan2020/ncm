#!/usr/bin/env python3
"""Apply the python-for-android patches this build needs, idempotently.

Three problems, all in the toolchain's site-packages, which lives outside the
repository -- a rebuilt toolchain comes back unpatched and the build fails
again with no hint why.  So they are applied here on every build, and
applying them twice is a no-op.

1. **The ``android`` recipe builds without isolation.**
   It declares only setuptools in its pyproject.toml while its setup.py does
   ``import Cython``.  pip's isolated build environment has no Cython, so the
   build dies with::

       ModuleNotFoundError: No module named 'Cython'

   The flag cannot be turned off globally: kivy pins exact cython/setuptools/
   wheel versions and the host environment has different ones, so every other
   recipe *needs* the isolation.

2. **The Python modules are installed with the target's platform tags.**
   The wheels for Android are tagged ``android_24_arm64_v8a``, but the pip
   that runs the install only knows the host's platform, so it refuses every
   one of them with "is not a supported wheel on this platform".

3. **The build venv does not upgrade its pip.**
   The upgrade pulls a newer pip from the mirror whose cli imports names the
   installed ``pip._internal.exceptions`` no longer has, and every later pip
   call in that venv dies with "cannot import name
   BuildDependencyImportError".  The seeded pip works; leave it alone.

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

#: The pip self-upgrade inside the build venv.
PIP_UPGRADE = '"source venv/bin/activate && pip install -U pip"'
PIP_VERSION = '"source venv/bin/activate && pip --version"'

#: The install of the pure-Python modules into the dist.
INSTALL_LINE = '"install -v --target'
#: The line it becomes.  Written in pieces so the quoting stays readable.
INSTALL_NEW = ('"install -v --target ' + "'{0}'"
               + ' --no-deps " + platforms + " -r requirements.txt"')
#: How the patched line reads, so a second run can tell.
BUILD_MARKER = '--no-deps " + platforms + " -r requirements.txt'


def site_packages(buildozer: Path | None) -> Path:
    """The site-packages of the buildozer environment."""
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
    return Path(out.stdout.strip())


def indent_of(line: str) -> str:
    """The leading whitespace of a line."""
    return line[:len(line) - len(line.lstrip())]


def patch_recipe(path: Path) -> bool:
    """Let the android recipe build without isolation."""
    if not path.is_file():
        print(f"cannot find {path}", file=sys.stderr)
        return False
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {path.name}")
        return True
    if ANCHOR not in text:
        print(f"{path} does not look like the version this patch was written "
              "for -- the anchor is missing", file=sys.stderr)
        return False
    path.write_text(text.replace(ANCHOR, ANCHOR + PATCH, 1), encoding="utf-8")
    print(f"patched {path.name}: the android recipe builds without isolation")
    return True


def patch_build(path: Path) -> bool:
    """Install the modules with the target's tags, and leave pip alone."""
    if not path.is_file():
        print(f"cannot find {path}", file=sys.stderr)
        return False
    text = path.read_text(encoding="utf-8")
    if BUILD_MARKER in text or PIP_VERSION in text:
        print(f"already patched: {path.name}")
        return True

    lines = text.splitlines()
    changed = False

    # 3. the pip self-upgrade
    for index, line in enumerate(lines):
        if PIP_UPGRADE not in line:
            continue
        lines[index] = indent_of(line) + PIP_VERSION
        # the note goes above the call, not inside its parentheses
        call = index
        for back in range(index, 0, -1):
            if "shprint(sh.bash, '-c', (" in lines[back]:
                call = back
                break
        outer = indent_of(lines[call])
        lines[call:call] = [
            outer + "# The upgrade is what breaks this venv: pip is pulled from",
            outer + "# the mirror as a newer version whose cli imports names the",
            outer + "# installed pip._internal.exceptions no longer has, and every",
            outer + "# later pip call in this venv dies with \"cannot import name",
            outer + "# BuildDependencyImportError\".  The seeded pip works.",
        ]
        changed = True
        break

    # 2. the platform tags on the install
    for index, line in enumerate(lines):
        if INSTALL_LINE not in line:
            continue
        lines[index] = indent_of(line) + INSTALL_NEW
        # the block belongs next to the shprint call that uses it, so it takes
        # that call's indentation, not the string's
        call = index
        for back in range(index, 0, -1):
            if "shprint(sh.bash, '-c', (" in lines[back]:
                call = back
                break
        outer = indent_of(lines[call])
        lines[call:call] = [
            outer + "# The wheels for this target are tagged for Android, but",
            outer + "# the host pip only knows the host's platform: without the",
            outer + "# tags it refuses every one of them.",
            outer + "platforms = ' '.join(",
            outer + "    '--platform={}'.format(tag)",
            outer + "    for tag in PyProjectRecipe.get_wheel_platform_tags("
                    "arch.arch, ctx))",
        ]
        changed = True
        break

    if not changed:
        print(f"{path} does not look like the version this patch was written "
              "for -- the anchors are missing", file=sys.stderr)
        return False

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"patched {path.name}: the modules are installed with the target's "
          "platform tags, and pip is left alone")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buildozer", type=Path, default=None,
                        help="path to the buildozer executable")
    args = parser.parse_args()

    p4a = site_packages(args.buildozer) / "pythonforandroid"
    ok = patch_recipe(p4a / "recipe.py")
    ok = patch_build(p4a / "build.py") and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
