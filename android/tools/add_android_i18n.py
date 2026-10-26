#!/usr/bin/env python3
"""Add the Android-only translation keys to both catalogues.

The Android build reuses the shared catalogue and adds its own keys under
the ``android`` namespace.  Both language files must carry the same keys,
so they are written from one table.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

#: key -> (zh_cn, en_us)
ANDROID_KEYS: dict[str, tuple[str, str]] = {
    "followed_artists": ("已关注歌手", "Followed artists"),
    "no_followed_artists": ("还没有关注任何歌手", "You have not followed any artist yet"),
    "artist_follow": ("关注", "Follow"),
    "artist_unfollow": ("取消关注", "Unfollow"),
    "artist_followed_toast": ("已关注 {name}", "Followed {name}"),
    "artist_unfollowed_toast": ("已取消关注 {name}", "Unfollowed {name}"),
    "artist_follow_failed": ("关注失败，请稍后再试", "Could not follow, try again later"),
    "artist_login_required": ("登录后才能关注歌手", "Sign in to follow artists"),
    "qr_login": ("扫码登录", "Sign in with QR code"),
    "phone_login": ("手机号登录", "Sign in with phone number"),
    "sign_out": ("退出登录", "Sign out"),
    "downloads": ("下载", "Downloads"),
    "account": ("我的", "Account"),
    "search_hint": ("输入关键词后按回车开始搜索", "Type and press enter to search"),
    "no_results": ("没有找到相关内容", "Nothing found"),
    "play_all": ("播放全部", "Play all"),
    "download_all": ("下载全部", "Download all"),
    "loading": ("加载中…", "Loading…"),
    "back": ("返回", "Back"),
    "tracks_count": ("{count} 首", "{count} tracks"),
    "artist_info": ("歌手信息", "Artist"),
    "albums": ("专辑", "Albums"),
    "songs": ("歌曲", "Songs"),
}


def main() -> int:
    for lang, index in (("zh_cn", 0), ("en_us", 1)):
        path = ROOT / "src" / "i18n" / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        section = data.setdefault("android", {})
        for key, values in ANDROID_KEYS.items():
            section[key] = values[index]
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"{lang}: android section now has {len(section)} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
