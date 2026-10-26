#!/usr/bin/env python3
"""Fetch SDL2's external dependencies as tarballs instead of cloning them.

Why this exists
---------------
p4a's ``sdl2_image`` / ``sdl2_mixer`` / ``sdl2_ttf`` recipes read SDL2's
``.gitmodules`` and run ``git clone --recursive`` for each dependency.  On
this machine a local accelerator (Steam++) intercepts HTTPS on 127.0.0.1
and serves github.com, but its git POST path -- upload-pack, which is what
``git clone`` speaks -- never answers.  The clone therefore hangs forever
with zero bytes received.  The direct route to GitHub is blocked (TCP 443
to the real address does not connect), and no git mirror is reachable, so
cloning is simply not an option here.

codeload.github.com serves the very same commit as a tarball over plain
HTTPS GET, and that path works.  So the sources are downloaded as tarballs
and unpacked where the recipe expects them.  The recipe skips the clone
when the target directory already exists and is not empty, so no p4a patch
is needed.

    python3 fetch_sdl2_deps.py [--bootstrap DIR] [--jobs N]

The bootstrap directory defaults to
``~/.python-for-android/build/bootstrap_builds/sdl2``.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import ssl
import sys
import tarfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The accelerator re-signs TLS; the combined bundle has both the system
# roots and the intercepting root, so both plain and intercepted hosts
# verify.
CA_BUNDLE = Path.home() / ".hermes/certs/combined-ca.pem"

CANDIDATE_TEMPLATES = (
    "https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{ref}",
    "https://codeload.github.com/{owner}/{repo}/tar.gz/refs/tags/{ref}",
    "https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}",
)


def ssl_context() -> ssl.SSLContext:
    if CA_BUNDLE.exists():
        return ssl.create_default_context(cafile=str(CA_BUNDLE))
    return ssl.create_default_context()


def parse_gitmodules(text: str) -> list[tuple[str, str, str]]:
    """Return (path, url, branch) for every submodule in a .gitmodules."""
    out = []
    for section in text.split('[submodule "')[1:]:
        parts = section.split(" = ")
        path = parts[1].split("\n")[0].strip()
        url = parts[2].split("\n")[0].strip()
        branch = parts[-1].strip()
        out.append((path, url, branch))
    return out


def owner_repo(url: str) -> tuple[str, str]:
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(\.git)?$", url)
    if not match:
        raise ValueError(f"not a github url: {url}")
    return match.group(1), match.group(2)


def download(url: str, dest: Path) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(request, timeout=300, context=ssl_context()) as response:
            if response.status != 200:
                return False
            with dest.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError) as exc:
        print(f"    {url} -> {exc}", flush=True)
        return False
    return True


def fetch_one(owner: str, repo: str, ref: str, dest: Path) -> bool:
    """Download one repo as a tarball and unpack it into dest."""
    if dest.exists() and any(dest.iterdir()):
        print(f"  skip {repo} (already there)", flush=True)
        return True

    tmp = dest.parent / f".{repo}.tarball"
    got = False
    for template in CANDIDATE_TEMPLATES:
        url = template.format(owner=owner, repo=repo, ref=ref)
        print(f"  {repo} @ {ref}", flush=True)
        if download(url, tmp):
            got = True
            break
    if not got:
        print(f"  FAILED {repo} @ {ref}", flush=True)
        return False

    staging = dest.parent / f".{repo}.unpack"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(tmp, "r:gz") as tar:
        # Refuse paths that would escape the target directory.
        for member in tar.getmembers():
            name = os.path.normpath(member.name)
            if name.startswith(("/", "..")) or ".." in Path(name).parts:
                print(f"  refused unsafe path in tarball: {name}", flush=True)
                return False
        tar.extractall(staging, filter="data")
    tmp.unlink(missing_ok=True)

    entries = list(staging.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        print(f"  unexpected tarball layout for {repo}", flush=True)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(entries[0]), str(dest))
    shutil.rmtree(staging, ignore_errors=True)
    print(f"  ok {repo} -> {dest}", flush=True)
    return True


def fetch_recursive(owner: str, repo: str, ref: str, dest: Path, depth: int = 0) -> bool:
    if not fetch_one(owner, repo, ref, dest):
        return False
    if depth >= 2:
        return True
    modules = dest / ".gitmodules"
    if not modules.exists():
        return True
    for path, url, branch in parse_gitmodules(modules.read_text(encoding="utf-8")):
        sub_owner, sub_repo = owner_repo(url)
        if not fetch_recursive(sub_owner, sub_repo, branch, dest / path, depth + 1):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap",
        default=str(Path.home() / ".python-for-android/build/bootstrap_builds/sdl2"),
    )
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()

    bootstrap = Path(args.bootstrap)
    if not bootstrap.is_dir():
        print(f"bootstrap directory not found: {bootstrap}", file=sys.stderr)
        return 1

    recipes = ["SDL2_image", "SDL2_mixer", "SDL2_ttf"]
    jobs: list[tuple[str, str, str, Path]] = []
    for recipe in recipes:
        modules = bootstrap / "jni" / recipe / ".gitmodules"
        if not modules.exists():
            print(f"no .gitmodules for {recipe}", file=sys.stderr)
            return 1
        for path, url, branch in parse_gitmodules(modules.read_text(encoding="utf-8")):
            owner, repo = owner_repo(url)
            jobs.append((owner, repo, branch, bootstrap / "jni" / recipe / path))

    print(f"fetching {len(jobs)} dependencies with {args.jobs} workers", flush=True)
    results: list[bool] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(fetch_recursive, owner, repo, branch, dest)
            for owner, repo, branch, dest in jobs
        ]
        for future in futures:
            results.append(future.result())

    ok = sum(1 for r in results if r)
    print(f"\n{ok}/{len(results)} dependencies fetched", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
