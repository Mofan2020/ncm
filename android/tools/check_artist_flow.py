#!/usr/bin/env python3
"""End-to-end check of the artist flow through the Android controller.

Hits the real API (read-only) and prints exactly what the UI would receive.
Writes are NOT performed: follow/unfollow would change the account, and the
rule is that only the user does that by hand.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "android"))

from ncmkivy.controller import Controller  # noqa: E402

ARTIST_ID = "6452"  # 周杰伦


def show(label: str, value: object, limit: int = 600) -> None:
    print(f"\n##### {label}")
    print(json.dumps(value, ensure_ascii=False, indent=1)[:limit])


def main() -> int:
    controller = Controller()
    status = controller.login_status()
    print("logged_in:", status.get("logged_in"), "| nickname:", status.get("nickname"))

    # 1) the artist page: header + first page of songs
    page = controller.artist_page(ARTIST_ID)
    if not page.get("success"):
        print("FAILED:", page)
        return 1
    artist = page["artist"]
    print("\n##### artist_page")
    print(f"  name       : {artist['name']}")
    print(f"  alias      : {artist['alias']}")
    print(f"  cover_url  : {artist['cover_url'][:60]}")
    print(f"  avatar_url : {artist['avatar_url'][:60]}")
    print(f"  followed   : {artist['followed']}  (fans {artist['fans_count']})")
    print(f"  music_size : {artist['music_size']} | album_size: {artist['album_size']}")
    print(f"  description: {len(artist['description'])} chars")
    print(f"  description head: {artist['description'][:70]!r}")
    print(f"  songs      : {len(page['songs'])} on first page | total {page['total']}"
          f" | has_more {page['has_more']}")
    for song in page["songs"][:3]:
        print(f"    - {song['name']} / {song['artists']} / {song['album']}"
              f" / {song['duration']}ms / vip={song['vip']}")

    # 2) the next page
    more = controller.artist_songs(ARTIST_ID, offset=len(page["songs"]), limit=5)
    print(f"\n##### artist_songs(offset={len(page['songs'])})")
    print(f"  got {len(more['songs'])} | has_more {more['has_more']}")
    for song in more["songs"][:3]:
        print(f"    - {song['name']} / {song['artists']}")

    # 3) the followed-artists list
    followed = controller.followed_artists(limit=5)
    print("\n##### followed_artists")
    print(f"  code: {followed['code']} | count: {followed['count']}"
          f" | has_more: {followed['has_more']}")
    for item in followed["artists"][:5]:
        print(f"    - {item['name']} (id {item['id']}, {item['album_size']} albums)")

    # 4) the follow state of one artist (read only)
    from src.core.api import get_api

    state = get_api().get_artist_follow_state(ARTIST_ID)
    print("\n##### follow_state (read only, no write sent)")
    print(f"  {state}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
