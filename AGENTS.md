# AGENTS.md — NetEase Cloud Music Playlist Downloader

## Project Overview
A Python desktop application (pywebview GUI) for downloading songs from NetEase Cloud Music (网易云音乐) playlists. Supports login (QR code / phone / cookie), concurrent downloads, and bilingual UI (zh_cn / en_us).

**Entry point:** `main.py` (GUI)  
**Core modules:** `src/core/api.py`, `src/core/downloader.py`, `src/auth/login.py`  
**GUI:** `src/gui/bridge.py` + `src/gui/assets/` (HTML/CSS/JS)  
**Dependencies:** `requests`, `pywebview`, `cryptography`, `PyYAML`, `qrcode[pil]`

---

## Commands

### Run (GUI)
```bash
python3 main.py
```

### Install dependencies
```bash
pip3 install -r requirements.txt
```

### Build executable (PyInstaller)
```bash
pip3 install pyinstaller
pyinstaller --clean --noconfirm main.spec
# macOS: dist/NeteaseMusicDownloader.app
# Windows: dist/NeteaseMusicDownloader/
```

### Automated builds
GitHub Actions (`.github/workflows/build.yml`) builds on tag push (`v*`) for macOS (x64/arm64) and Windows (x64). Artifacts uploaded to GitHub Releases.

---

## Architecture

| Path | Responsibility |
|------|----------------|
| `main.py` | Entry point — creates pywebview window, wires `GuiBridge` |
| `src/gui/bridge.py` | `GuiBridge` — JS↔Python API, orchestrates login/download/playlist |
| `src/gui/assets/index.html` | Main UI — header, playlist panel, download queue, modals |
| `src/gui/assets/style.css` | Full stylesheet with light/dark themes, responsive layout |
| `src/gui/assets/app.js` | Frontend logic — state management, API calls, rendering |
| `src/core/api.py` | `NeteaseAPI` — HTTP requests, domain rotation, retry, quality fallback |
| `src/core/downloader.py` | `SongDownloader` — concurrent downloads (ThreadPoolExecutor), progress tracking |
| `src/auth/login.py` | `LoginManager` — QR code, phone verification, cookie import |
| `src/config/settings.py` | `Settings` — YAML-persisted config (singleton) |
| `src/i18n/` | JSON translations (`zh_cn.json`, `en_us.json`) |

**Key behaviors:**
- Concurrent downloads: default 2, max 3 (user-configurable in UI)
- Quality fallback: requested → standard if unavailable
- Login methods: QR code (auto-polling), phone + verification code, manual cookie import
- Settings persisted to `~/Library/Application Support/NeteaseMusicDownloader/settings.yaml` (macOS) or `%APPDATA%` (Windows)
- Language switchable in settings (zh_cn / en_us), persisted across sessions
- Temp file `.tmp` → atomic move on completion
- Download pause/resume/cancel via threading events

---

## Quality Levels
| Value | Bitrate | Format |
|-------|---------|--------|
| `standard` | 128kbps | MP3 |
| `higher` | 192kbps | MP3 |
| `exhigh` | 320kbps | MP3 |
| `lossless` | FLAC | FLAC |
| `hires` | Hi-Res | FLAC |

---

## Common Issues

| Issue | Cause / Fix |
|-------|-------------|
| `403` / rate limit | API rotates domains; exponential backoff |
| QR code login fails | Check network; try phone or cookie login |
| VIP songs require login | Login required for fee>0 tracks |
| macOS permission errors | Fallback dirs: `~/Downloads/Music`, `./downloads` |
| pywebview window blank | Ensure `pip install pywebview` + system WebView2 (Windows) / WebKit (macOS) |

---

## No Test Suite / Lint / CI for code
No automated tests or linting. Verify changes manually:
```bash
python3 main.py  # Launch GUI and test core flows
```

---

## Platform Notes
- **macOS**: Primary target. Uses `open` folder command, native signals.
- **Windows**: Supported via pywebview CEF backend. GitHub Actions builds `.exe`.
- **Linux**: Not officially supported but may work with minor path adjustments.