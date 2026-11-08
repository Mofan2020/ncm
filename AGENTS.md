# AGENTS.md — SkycMedia

## Project Overview

A **pywebview** desktop app (HTML/CSS/JS frontend + Python backend) that is a
third-party client for **NetEase Cloud Music (网易云音乐) and Bilibili (哔哩哔哩)**:
it downloads music and videos, it plays them, and it keeps the account data of
both services in one backup file. One codebase targets **Windows and macOS**;
the UI ships in **zh_cn + en_us**.

The **Android build has been dropped** (see `docs/announcement.md`). Only the
desktop app is maintained from now on.

**Entry point:** `main.py` · **Bridge:** `src/gui/bridge.py` ·
**Frontend:** `src/gui/assets/` · **Targets:** Windows x64, macOS arm64,
macOS x64 (no Linux support, no CLI anymore)

---

## Commands

```bash
pip3 install -r requirements.txt          # runtime deps
python3 main.py                           # run the GUI from source
python3 main.py --debug                   # verbose logging + devtools
python3 main.py --self-test [--report F]  # headless asset/dependency check (CI uses this)

pip3 install -r requirements-dev.txt      # adds pyinstaller, pytest, ruff, pillow
ruff check .                              # lint (CI gate)
pytest -q                                 # unit tests (hermetic, no network)
node scripts/check-player-js.mjs          # play-mode statistics (needs node, not in CI)
NCM_LIVE=1 pytest tests/test_live_api.py  # optional: hits the real API

pyinstaller --clean --noconfirm main.spec # package
#  Windows -> dist/SkycMedia.exe
#  macOS   -> dist/SkycMedia.app
python3 scripts/make_icon.py              # regenerate assets/icon.{png,ico,icns}
```

CI: `.github/workflows/build.yml` — `test` (ruff + pytest) → `build-macos` (arm64 on
`macos-15`, x64 on `macos-15-intel`), `build-windows` (x64) → `release` (tag `v*` only).
Every build job runs the packaged binary's `--self-test` before uploading, so a broken
bundle can never reach a release.

---

## Architecture

| Path | Responsibility |
|------|----------------|
| `main.py` | Window creation, geometry/theme bootstrap, logging, `--self-test` |
| `src/version.py` | Single source of truth: name, version, bundle id, author |
| `src/resources.py` | `resource_path()` — resolves bundled data from source **and** PyInstaller |
| `src/core/aes.py` | AES-128-ECB in pure Python (no native/OpenSSL dependency) |
| `src/core/crypto.py` | eapi request signing (AES-ECB + MD5 digest) |
| `src/core/api.py` | `NeteaseAPI` — eapi + legacy endpoints, pacing, retries, quality fallback |
| `src/core/bilibili.py` | `BiliClient` — Bilibili web API: search, hot lists, login, favourites, play URLs |
| `src/core/bili_favorites.py` | Writes to Bilibili: create a folder, add videos to it |
| `src/core/bili_download.py` | Downloads a Bilibili video and its audio track |
| `src/core/bili_danmaku.py` | Danmaku: a hand-written protobuf parser, plus sending and filtering |
| `src/core/bili_im.py` | Private messages: conversations, history, sending |
| `src/core/bili_upload.py` | Uploading a local video (pre-upload → file → cover → submit) |
| `src/core/bili_cache.py` | TTL cache for the Bilibili reads (per-kind lifetimes) |
| `src/core/mediaserver.py` | `MediaServer` — 127.0.0.1 loopback HTTP for playback (Range, cover art, CDN proxying) |
| `src/core/library.py` | `Library` — play counts, favourites, recent list, resume session (`library.json`) |
| `src/core/localmusic.py` | `LocalMusicLibrary` — folder scan, dependency-free tag/lyrics/art parsing, index cache |
| `src/core/downloader.py` | `SongDownloader` — thread pool (≤3), `.part` + atomic move, pause/cancel |
| `src/core/backup.py` | `.ncmbk` export/import of the account data of both services |
| `src/auth/login.py` | `LoginManager` — NetEase QR / phone captcha / cookie import |
| `src/auth/bili_login.py` | `BiliSession` — the remembered Bilibili cookie (`bilibili.json`) |
| `src/config/settings.py` | `Settings` singleton, YAML persistence, clamping, tolerant loading |
| `src/i18n/` | `I18n` + `zh_cn.json` / `en_us.json` |
| `src/gui/bridge.py` | `GuiBridge` — every JS-callable method (returns JSON-safe data) |
| `src/gui/bilibili_bridge.py` | `BilibiliBridge` — translates the Bilibili client for the frontend |
| `src/gui/theme.py` | Native window chrome: macOS `NSAppearance`, Windows dark title bar |
| `src/gui/assets/` | `index.html` (markup + no-flash theme bootstrap), `style.css`, `app.js`, `bili-player.js`, `bili-player.html` + `bili-player-window.js` (the Bilibili player window) |
| `tests/` | pytest suite: crypto, api, downloader, settings, i18n, theme, bridge, live, library, localmusic, mediaserver, lyrics, player bridge (`tests/audio_fixtures.py` synthesises real MP3/FLAC bytes) |
| `scripts/check-player-js.mjs` | Statistical checks for the frontend queue logic (node, run manually) |

### Request flow

```
JS (app.js) ──pywebview.api.BridgeMethod()──▶ GuiBridge ──▶ NeteaseAPI / LoginManager
        ▲                                          │
        └──── onDownloadProgress / onDownloadStats / onLogin*  (window.evaluate_js)
```

Backend→frontend pushes go through `GuiBridge._call_js()` into the global `on*`
functions of `app.js`. Renaming one side without the other silently breaks the UI,
so `tests/test_bridge.py` asserts the payload shapes.

Playback audio never travels through the bridge: `GuiBridge.resolve_track()` returns a
`http://127.0.0.1:<port>/m/<token>/...` URL and the `<audio>` element streams it.

```
<audio> ──GET (Range)──▶ MediaServer ──┬─▶ local file (whitelisted roots)
                                       └─▶ NetEase CDN (Referer: music.163.com)
```

Bilibili goes through the same server, one route further down:

```
<video> ──GET (Range)──▶ MediaServer /bili ──▶ Bilibili CDN (Referer + cookie)
```

`/m/<token>/bili?u=<cdn url>` is the **only** way the webview reaches the Bilibili
CDN. The proxy adds the site `Referer` and the signed-in cookie, forwards `Range`
and **only** accepts hosts under `hdslb.com` / `bilivideo.com` / `bilibili.com` —
without that check the loopback server would be an open proxy.

---

## NetEase endpoint map (verified live 2026-09-30)

| Need | Endpoint | Notes |
|------|----------|-------|
| Playlist detail | eapi `/api/v6/playlist/detail` | legacy `/api/v3/playlist/detail` as fallback |
| Missing tracks | eapi `/api/v3/song/detail` (`c=[{id}]`) | for playlists with `trackCount > len(tracks)` |
| Play URL | eapi `/api/song/enhance/player/url/v1` | `ids`/`level`/`encodeType`, per-level retry |
| Lyrics | eapi `/api/song/lyric` | `id`/`lv=-1`/`kv=-1`/`tv=-1`; `/v1` answers 400, the legacy GET route is dead |
| Account state | `/api/w/nuser/account/get` | anonymous ⇒ `account: null` ⇒ "not signed in" |
| QR login | `/api/login/qrcode/unikey` + `/api/login/qrcode/client/login` | `type=1` |
| SMS code | `/api/sms/captcha/sent` | parameter is **`cellphone`**, not `phone` |
| Phone login | `/api/w/login/cellphone` | `phone` + `captcha` (+ `ctcode`) |
| Logout | `/api/logout` | |
| Search | eapi `/api/cloudsearch/pc` | `s` / `type=1` / `limit` / `offset` |
| Account playlists | eapi `/api/user/playlist` | `uid` + `limit`/`offset`, `more` for paging |
| Artist profile | eapi `/api/v1/artist/{id}` | the profile **and** the hot songs; songs sit next to the profile under `hotSongs`, not inside it |
| Artist songs (all) | eapi `/api/v1/artist/songs` | `id`/`limit`/`offset`/`total=true`; answers `total` + `more` |
| Artist albums | eapi `/api/artist/albums/{id}` | path-style; the flat `/api/artist/album` answers `400` |
| Artist follow state | eapi `/api/artist/follow/count/get` | `isFollow` + `fansCnt` + `followDayCnt` |
| Follow / unfollow artist | eapi `/api/artist/sub` and `/api/artist/unsub` | **two routes**, one per direction; the param is `artistId` (not `id`), plus `artistIds` as a stringified list |
| Followed artists | eapi `/api/artist/sublist` | `data` / `count` / `hasMore`; `code 301` when not signed in |
| Favourite / unfavourite | eapi `/api/song/like` | `trackId` + `like` |
| Liked song ids | eapi `/api/song/like/get` | `uid` |
| Play report | eapi `/api/feedback/weblog` | best effort, only when the user opted in |

`/api/radio/like`, `/api/scrobble` and `/api/v1/play/record` are **not** usable here
(-460 / 404 / -2), which is why play counts are kept locally and only *reported* to
`/api/feedback/weblog`.

**Do not use `/weapi/*`.** On some networks (verified here) every weapi request returns
HTTP 200 with an **empty body**, including deliberately malformed payloads, so failures
are indistinguishable from success. `/api/song/url/v1`, `/api/login/status` and
`/api/sent/verificationcode` are **404** — they are dead routes, see `docs/notes.md`.

---

## Bilibili endpoint map

Full reference: `docs/bili-notes.md`. The endpoints in use:

| Need | Endpoint | Notes |
|------|----------|-------|
| Hot list | `/x/web-interface/popular` | site wide; ignores the partition |
| Partition feed | `/x/web-interface/wbi/search/type` | `tids` filters the partition |
| Ranking | `/x/web-interface/ranking/v2` | main partitions only; the music areas answer `-400` |
| Video detail | `/x/web-interface/view` | `bvid` → `cid`, `duration`, `owner` |
| Play URLs | `/x/player/wbi/playurl` | WBI signature, `qn`/`fnval=4048`/`fourk=1` |
| Account state | `/x/web-interface/nav` | `isLogin`, `mid`, `uname`, `vipStatus` |
| QR login | `passport.bilibili.com/x/passport-login/web/qrcode/generate` + `/qrcode/poll` | the cookie arrives in `Set-Cookie`, **not** in the JSON |
| Password login | `passport.bilibili.com/x/passport-login/web/login` | needs the geetest challenge |
| SMS login | `passport.bilibili.com/x/passport-login/web/sms/sendcode` + `/sms/login` | |
| Register | `passport.bilibili.com/x/passport-register/web/sendcode` + `/register` | |
| Favourite folders | `/x/v3/fav/folder/created/list-all` | `up_mid`; `attr` bit 0 = private |
| Folder content | `/x/v3/fav/resource/list` | `media_id`, `pn`, `ps` |
| Create a folder | `/x/v3/fav/folder/add` | write; needs `csrf`, `title`, `privacy` |
| Rename a folder / change its privacy | `/x/v3/fav/folder/edit` | write; needs `csrf`, `media_id` |
| Delete a folder | `/x/v3/fav/folder/del` | write; needs `csrf`, `media_id` |
| Add to a folder | `/x/v3/fav/resource/deal` | write; needs `csrf`, `add_media_ids` = `"<aid>:2"` |
| Video counters | `/x/web-interface/archive/stat` | view / danmaku / reply / favourite / coin / like |
| Like a video | `/x/web-interface/archive/like` | write; `like` = 1 or 2, needs `csrf` |
| Coins | `/x/web-interface/coin/add` + `/coin/number` | write; `multiply` 1–2; the balance route is a GET |
| Comments | `/x/v2/reply/main` + `/x/v2/reply/add` + `/x/v2/reply/action` | the two write routes need `csrf` |
| Replies of a comment | `/x/v2/reply/reply` | `oid`, `root`, `pn`, `ps` — the replies of one thread |
| Danmaku | `/x/v2/dm/web/seg.so` + `/x/v2/dm/post` | the read route answers **protobuf**, not JSON |
| Watch history | `/x/web-interface/history/cursor` | WBI signed; page through `cursor.max` |
| Uploader profile | `/x/space/wbi/acc/info` + `/x/space/wbi/arc/search` + `/x/space/upstat` | the first two are WBI signed, `upstat` is not |
| Follow / unfollow | `/x/relation/modify` + `/x/relation` | `act` = 1 / 2 on the same route |
| Followings / followers | `/x/relation/followings` + `/x/relation/followers` | `vmid`, `pn`, `ps`; WBI signed |
| Private messages | `api.vc.bilibili.com/session_svr/v1/session/notify_fetch` + `sess_svr/v1/sess/get_messages` + `sess_svr/v1/sess/send_msg` | a **different gateway**, no WBI |
| Uploading | `member.bilibili.com/x/vuweb/pre` + `/x/vuweb/add` | pre-upload first, then the file, then the form |

---

## Behaviours that are easy to break

- **Pacing.** NetEase answers `code 400` when requests are fired too fast; `NeteaseAPI`
  enforces `MIN_REQUEST_INTERVAL` and retries with backoff. Don't remove it.
- **QR status codes.** `800` = expired, `801` = waiting for scan, `802` = scanned,
  `803` = **success**. Getting this wrong makes login impossible while looking fine.
  Bilibili uses its own set: `0` = success, `86101` = waiting, `86090` = scanned,
  `86038` = expired.
- **URL resolution happens inside the download worker** (signed URLs expire); never
  pre-resolve a whole playlist on the UI thread.
- **Extension comes from the API's `type`**, never from the requested level: asking for
  FLAC often returns a 320 kbps MP3.
- **Verification before publish:** stream to `<name>.part`, compare bytes with the
  announced size, then move atomically. Every failure path must delete the temp file.
- **Theme:** `<head>` resolves the theme from the URL query before the first paint
  (`?theme=dark&mode=dark`), `setTheme()` keeps the DOM, the toggle and the **native
  window chrome** in sync via `bridge.set_theme()`.
- **Frozen builds:** everything must be loaded through `resource_path()`; `__file__`
  is meaningless inside a PyInstaller bundle.
- **`window.pywebview.api` exists before the methods are injected.** The frontend must
  wait for `pywebviewready` (or poll for a known method), otherwise the first calls
  resolve to `undefined` and the UI shows raw i18n keys.
- **The media server is sandboxed.** Every path goes through `MediaServer.resolve_local()`,
  which rejects anything outside the allowed roots; `x-same-origin` is not a defence and
  the token in the URL is. Keep the bind on `127.0.0.1`.
- **The Bilibili proxy is sandboxed too.** `/bili` only fetches hosts under the
  Bilibili domains; everything else answers 403. The cookie goes in the request
  headers, never in the URL.
- **A Bilibili login cookie must reach the media server.** `BiliSession.apply_to_media()`
  hands it over; without it the CDN answers 403 and the video never starts.
- **macOS has no `MediaSource`.** The WKWebView there cannot play the separated
  DASH streams, so `BiliClient.video_streams()` asks for a single file
  (`fnval=1`, up to 720P) as a fallback. Without it the player would only ever
  play the sound on macOS. The Windows WebView2 does have `MediaSource` and
  uses the DASH streams.
- **The Bilibili video plays in a window of its own.** `open_bili_player_window()`
  opens `bili-player.html` through the loopback server with the same bridge as
  the main window, so the main window stays usable while the video runs. The
  window carries the comments, the danmaku and the interactions; the inline
  player of the main window is only the fallback for a plain browser.
- **A helper module that is loaded by name needs to be in `hiddenimports`.**
  `bilibili_bridge.py` reaches the danmaku / private message / upload helpers
  through `importlib.import_module("src.core.<name>")`, which PyInstaller's
  static analysis cannot see. Without the entry in `main.spec` the frozen app
  answers "模块不可用" for everything. `tests/test_bili_bundle.py` keeps the
  two lists in sync.
- **The destination of a download is known before the bytes arrive.** The
  "already downloaded, skip it" check runs against the destination path: an
  empty path resolves to the current directory, which exists and is not
  empty, so every file would be reported as "skipped".
- **Shuffle must stay independent of play counts.** It picks uniformly among all tracks
  except the current one. `node scripts/check-player-js.mjs` fails if that changes.
- **Local indexing is cache-keyed by path + mtime/size**, while the track map is keyed by
  `local:<path>`. Comparing the two key spaces directly silently disables the cache
  (and makes every scan quadratic — see docs/notes.md).
- **`sidecar_lyrics_path()` is called once per file.** It must not list the directory
  unless the direct `.lrc` is missing, and that listing is cached per folder.
- **Nothing may ship a whole library across the bridge.** `get_local_tracks()` is paged
  (200 tracks per call) and play counts are pushed once at startup.
- **Playback streams; it never buffers a whole file.** `MediaServer` copies in 256 KB
  chunks — a 60 MB track must not add 60 MB to RSS.
- **The resume session is only written when it actually changed** (queue/index/mode, or
  the position moved by more than a second), otherwise the player rewrites it every few
  seconds.
- **Lyric highlight must apply `player.lyricOffset` before comparing times.** The offset is a
  per-track playback shift (-5000..+5000 ms); the slider writes it in real time and
  `updateLyricHighlight()` is the only place that should subtract it from
  `audio.currentTime * 1000`.  Re-fetching lyrics on slider input is wrong: it is a
  *playback* offset, not a request parameter.
- **The artist profile does not carry its own songs.** `/api/v1/artist/{id}` answers
  `{artist: {...}, hotSongs: [...]}` — the hot songs are a **sibling** of `artist`, not a
  field inside it. Reading `artist.get("hotSongs")` silently yields an empty list, which
  is how the artist page shipped with no songs. `get_artist_detail()` folds the songs
  back in; use it instead of reaching into the raw answer.
- **Artist songs and playlist songs use different field names.** `/api/v1/artist/songs`
  answers `artists` / `album` / `duration`, while the playlist and search routes answer
  `ar` / `al` / `dt`. Every consumer has to accept both spellings — `_track_payload()`
  and the Android `Controller._track()` already do.
- **Follow and unfollow are two routes, and the param is `artistId`.** `/api/artist/sub`
  and `/api/artist/unsub`; sending `id` instead answers `400 参数错误`, which looks like a
  malformed request rather than a wrong parameter name.
- **The follow state has its own route.** The `followed` flag on the artist payload is
  not reliable for the "am I following this" question; `/api/artist/follow/count/get`
  answers `isFollow` and is what the follow button reads.
- **Comments, danmaku and uploading trip the risk control.** `-352` (Gaia) and
  `-401` mean "come back later": **never retry them**, the answer will not
  change. Send the request once and tell the user it was blocked.
- **The danmaku feed is protobuf.** `x/v2/dm/web/seg.so` answers binary
  `bili.community.dm.v1`; the parser in `src/core/bili_danmaku.py` is
  hand-written on purpose (no new dependency).
- **Private messages live on another gateway.** `api.vc.bilibili.com` does not
  take the WBI signature, and the `content` field is a JSON **string**.
- **Uploading is a three-step dance.** pre-upload (`/x/vuweb/pre`) → upload the
  file to the address it returns → submit the form (`/x/vuweb/add`). The
  addresses and the field names come out of the pre-upload answer; never hardcode
  them, Bilibili changes them often.
- **A write must never be sent with the real account during development.**
  Follow/unfollow, like, the playlist writes and the Bilibili favourite writes all
  change the account; the tests fake the transport instead, and the user verifies the
  real round-trip by hand. An automated write probe has already put the account into a
  bad state once.
- **The Bilibili cache has per-kind lifetimes.** `src/core/bili_cache.py` keeps slow
  content long (danmaku 24 h, a video 60 min) and fast content short (comments
  5 min). Account state, watch history, private messages and the coin balance are
  **never cached** — they change with the account, not with the clock.
- **Invalidate a cache entry by its prefix, not by the bare id.** The comment key
  is `"{bvid}|{page}|{sort}"` and the danmaku key is `"{bvid}|{cid}"`, so a write
  drops the prefix `"{bvid}|"`. Dropping the bare bvid would also hit every other
  bvid that starts with the same characters.
- **"Clear the cache" has to reach the Bilibili entries too.** `metacache.clear()`
  and `metacache.stats()` walk every subdirectory, otherwise the Bilibili cache
  would survive the one button that is supposed to get rid of it.

---

## The `.ncmbk` backup file

The extension stays `.ncmbk` — it is the compatibility promise to the users who
already have backups. The content is JSON, version 4:

```json
{
  "format": "ncm-account-backup",
  "version": 4,
  "sources": {"account": "cloud", "favorites": "cloud"},
  "account": {...},
  "liked_song_ids": ["1"],
  "playlists": [{"id": "7", "song_ids": ["1"]}],
  "followed_artists": [...],
  "liked_albums": [...], "subscribed_djradios": [...], "liked_mvs": [...],
  "play_record": [...],
  "local_state": {"play_counts": {...}, "favorites": [...], "recent": [...]},
  "bilibili": {"account": {...}, "folders": [{"id": "9", "videos": [...]}]}
}
```

Rules:

- **Export needs one of the two accounts, not both.** Signed in to NetEase only,
  to Bilibili only, or to both — the export always works, and the file says which
  part came from where (`sources`).
- **Import is tolerant.** A file without the `bilibili` section (version 1–3) still
  imports; a file with it imports the Bilibili part only when a Bilibili account is
  signed in, and says so when it skips it. Every valid part is imported, nothing is
  refused because another part is missing.

---

## Conventions

- **The release notes are the whole file, and they only describe this release.**
  The release job passes `release-notes.md` to `gh release create --notes-file`,
  so the file *is* the description on the release page. Before tagging, the
  file holds **only what changed since the last published version** — no
  `## 即将发布` heading, no `---` / `## 历史版本` divider, no older versions
  copied along. Every past release already has its own page and its own tag,
  so repeating them here only makes the page unreadable (it grew to 48
  sections and 237 lines once). Aim for a handful of `##` headings, one or two
  sentences each, and delete the previous version's text when the next one
  ships.
- **A tag builds a pre-release, not a stable one.** The release job creates it
  with `--prerelease`, so `/releases/latest` keeps pointing at the last stable
  version.  Promote it after the round-trip has been tested by hand:
  `gh release edit <tag> --prerelease=false`.  Check the result with
  `gh release view <tag> --json isDraft,isPrerelease` -- both flags matter, and
  a release left as a draft is invisible to the download buttons.
- i18n: no hardcoded user-facing text. Static markup uses `data-i18n*` attributes,
  Python uses `src.i18n.t()`, and `tests/test_i18n.py` enforces key parity, identical
  `{placeholder}` sets and the presence of every `download.status_*` / `error.reason.*` key.
  A **list** of paragraphs is flattened to numbered keys (`disclaimer.body.1`, `.2`, …) --
  the webview only carries strings, so `flatten_translations()` never emits a list.
- Icons: no emoji in the UI. `src/gui/assets/icons.js` holds a generated subset of
  Feather (MIT, see `FEATHER-LICENSE.txt`); markup uses `data-icon="name"` and the
  frontend calls `setIcon()`.  Regenerate with the build script rather than hand-editing
  the generated file.
- The disclaimer shows on the first start and after any release that changes its text
  (bump `DISCLAIMER_VERSION` in `src/version.py` when the wording changes).
- The account panel is **read-only**.  `/api/user/profile/update` exists but answers
  `code 250` for third-party clients, so there is deliberately no nickname editor.
- Settings: unknown YAML keys are **ignored** (never crash on an older config file);
  `max_concurrent` is clamped to 1–3.
- Version lives only in `src/version.py`; `main.spec` and the UI read it from there.
- **版本号按改动性质递增，不按提交次数。** 判据只有一个：用户能不能看出多了什么。
  - 能 —— 加了功能、换了交互、行为变了 → **minor +1，patch 归零**（3.6.0 → 3.7.0）
  - 不能 —— 只修错、改文档、调性能、改内部结构 → **patch +1**
  - 一批改动里既有功能又有修复 → 按功能算，升 minor
  - **patch 号到 9 还没升过 minor，说明这段时间的功能都被当成补丁发了，判错了。**
    下一次改动直接按 minor 走。
- The macOS bundle id is `com.skyc8266.skycmedia` — never use a reverse-DNS
  name under someone else's domain.
- Comments explain *why*; no development notes in user-visible strings.
- Player settings live in the `playback:` block of `settings.yaml`; unknown keys are
  ignored and out-of-range values are clamped (`play_mode` falls back to `list`).
- `report_play_count` is **off** by default. The local counter is the record of truth;
  the `/api/feedback/weblog` report only runs when the user opts in *and* is signed in.
- The app name and bundle id are `SkycMedia` / `com.skyc8266.skycmedia`.
  The project was renamed from `NeteaseMusicDownloader`; the config directory
  keeps the old name on machines that already used it (`src/config/settings.py`
  falls back to it), so an upgrade does not ask the user to sign in again.
- **对外文档（README、公告、帮助信息）不要有 AI 味**：不用口号句、加粗引导句、
  清单式排比；不要出现只有开发者懂的行话，换成普通用户能读懂的说法。
