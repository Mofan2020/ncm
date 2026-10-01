# AGENTS.md — NetEase Cloud Music Playlist Downloader

## Project Overview

A **pywebview** desktop app (HTML/CSS/JS frontend + Python backend) for NetEase Cloud
Music (网易云音乐): it downloads playlist songs and it plays music. The window has two
tabs — **download** and **player** — over the same backend. The player covers online
songs, account playlists, local folders, lyrics, favourites and resume-on-start.
One codebase targets **Windows and macOS**; the UI ships in **zh_cn + en_us**.

**Entry point:** `main.py` · **Bridge:** `src/gui/bridge.py` · **Frontend:** `src/gui/assets/`
**Targets:** Windows x64, macOS arm64, macOS x64 (no Linux support, no CLI anymore)

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
#  Windows -> dist/NeteaseMusicDownloader.exe
#  macOS   -> dist/NeteaseMusicDownloader.app
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
| `src/core/mediaserver.py` | `MediaServer` — 127.0.0.1 loopback HTTP for playback (Range, cover art, CDN proxying) |
| `src/core/library.py` | `Library` — play counts, favourites, recent list, resume session (`library.json`) |
| `src/core/localmusic.py` | `LocalMusicLibrary` — folder scan, dependency-free tag/lyrics/art parsing, index cache |
| `src/core/downloader.py` | `SongDownloader` — thread pool (≤3), `.part` + atomic move, pause/cancel |
| `src/auth/login.py` | `LoginManager` — QR / phone captcha / cookie import, session + status |
| `src/config/settings.py` | `Settings` singleton, YAML persistence, clamping, tolerant loading |
| `src/i18n/` | `I18n` + `zh_cn.json` / `en_us.json` |
| `src/gui/bridge.py` | `GuiBridge` — every JS-callable method (returns JSON-safe data) |
| `src/gui/theme.py` | Native window chrome: macOS `NSAppearance`, Windows dark title bar |
| `src/gui/assets/` | `index.html` (markup + no-flash theme bootstrap), `style.css`, `app.js` |
| `tests/` | pytest suite: crypto, api, downloader, settings, i18n, theme, bridge, live, library, localmusic, mediaserver, lyrics, player bridge (`tests/audio_fixtures.py` synthesises real MP3/FLAC bytes) |
| `scripts/check-player-js.mjs` | Statistical checks for the frontend queue logic (node, run manually) |
| `legacy/` | The abandoned v2.0 CLI (`netease_api.py`, `cli_downloader.py`) — **not maintained** |

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

## Behaviours that are easy to break

- **Pacing.** NetEase answers `code 400` when requests are fired too fast; `NeteaseAPI`
  enforces `MIN_REQUEST_INTERVAL` and retries with backoff. Don't remove it.
- **QR status codes.** `800` = expired, `801` = waiting for scan, `802` = scanned,
  `803` = **success**. Getting this wrong makes login impossible while looking fine.
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

---

## Conventions

- i18n: no hardcoded user-facing text. Static markup uses `data-i18n*` attributes,
  Python uses `src.i18n.t()`, and `tests/test_i18n.py` enforces key parity, identical
  `{placeholder}` sets and the presence of every `download.status_*` / `error.reason.*` key.
- Settings: unknown YAML keys are **ignored** (never crash on an older config file);
  `max_concurrent` is clamped to 1–3.
- Version lives only in `src/version.py`; `main.spec` and the UI read it from there.
- The macOS bundle id is `com.skyc8266.neteasemusicdownloader` — never use a reverse-DNS
  name under someone else's domain.
- Comments explain *why*; no development notes in user-visible strings.
- Player settings live in the `playback:` block of `settings.yaml`; unknown keys are
  ignored and out-of-range values are clamped (`play_mode` falls back to `list`).
- `report_play_count` is **off** by default. The local counter is the record of truth;
  the `/api/feedback/weblog` report only runs when the user opts in *and* is signed in.
- The app name and bundle id stay `NeteaseMusicDownloader` / `com.skyc8266.neteasemusicdownloader`
  even though the app now plays music too: renaming would move the config directory and
  invalidate existing installs.
