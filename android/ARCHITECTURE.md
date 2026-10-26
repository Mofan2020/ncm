# Android 版架构设计（ncm-android）

本文是 Android 端开发的契约。桌面端与 Android 端**共享业务逻辑，分离表现层**。

## 0. 一句话

同一套 Python 业务核心（`src/core/`），两种表现层：桌面端 pywebview + HTML/CSS/JS，
Android 端 Kivy + 原生控件。Android 版不是「网页套壳」，是真正的原生控件树。

## 1. 为什么能这样拆

`src/core/`、`src/auth/`、`src/config/`、`src/i18n/` 四个目录**零 webview 依赖**
（已核对：无 `import webview`、无 `from src.gui`），共约 12400 行业务逻辑，
全部可以原样跑在 Android 的 Python 3.11 上。

不能复用的是 `src/gui/`：
- `bridge.py`（2508 行）—— 逐个方法对应 `window.pywebview.api.<name>()`，
  是 WebView 专属的调用面
- `assets/`（HTML/CSS/JS）—— 桌面布局，手机上不适用
- `theme.py` —— 调 macOS `NSAppearance` / Windows DWM

## 2. 分层

```
android/
  main.py              入口：Kivy App，解析 --self-test 等参数
  ncmkivy/
    __init__.py
    app.py             NcmApp：窗口/主题/生命周期，全局状态
    controller.py      Controller：唯一业务入口，Kivy 与 core 之间的边界
    player.py          音频播放：pyjnius MediaPlayer + 进度/状态回调
    media.py           封面/图片加载与缓存
    screens/           每个 screen 一个 .py
    widgets/           可复用控件
    theme.py           Material 3 色板 + 尺寸分档
  buildozer.spec       打包配置
```

`controller.py` 是关键：它对上层（Kivy screen）暴露**同步/异步的普通 Python 方法**，
对下层只调用 `src/core/` 的公开 API。Kivy 代码永远不直接碰 `NeteaseAPI`。

## 3. 复用映射（core → controller）

| 桌面 bridge 方法 | Android controller 方法 | 复用的 core API |
|---|---|---|
| `get_playlists` | `list_playlists()` | `NeteaseAPI.get_user_playlists_all` |
| `get_playlist_tracks` | `playlist_tracks(id)` | `NeteaseAPI.get_playlist_songs` |
| `get_liked_tracks` | `liked_tracks()` | `NeteaseAPI.get_liked_song_ids` + `get_songs_detail` |
| `search_tracks` / `search_multi` | `search(kw, offset)` | `NeteaseAPI.search_multi` |
| `get_artist_detail` | `artist_page(id)` | `NeteaseAPI.get_artist_detail` + `get_artist_songs` |
| 关注/取消关注 | `follow_artist(id, on)` | `NeteaseAPI.set_artist_follow` |
| 已关注列表 | `followed_artists()` | `NeteaseAPI.get_followed_artists` |
| `resolve_track` | `resolve(track)` | `NeteaseAPI.get_song_url_info` + `MediaServer` |
| `get_track_lyrics` | `lyrics(track)` | `NeteaseAPI.get_lyrics` + `build_timed_lyrics` |
| `start_download` | `download(tracks)` | `SongDownloader` |
| `get_favorites` / `set_favorite` | `favorites()` / `set_favorite()` | `Library` + `NeteaseAPI.set_song_like` |
| `get_local_tracks` | `local_tracks()` | `LocalMusicLibrary` |
| `get_login_status` / `login_qrcode` | `login_status()` / `start_qr_login()` | `LoginManager` |
| `get_settings` / `update_settings` | `settings()` / `save_settings()` | `Settings` |
| `get_user_profile` | `user_profile()` | `LoginManager` + `NeteaseAPI.get_user_detail` |

### 3.1 音频播放

桌面端用 `<audio>` 元素流式播放（`MediaServer` 提供 `http://127.0.0.1:<port>/m/<token>/...`）。
Android 端没有 `<audio>`，改用 **pyjnius → `android.media.MediaPlayer`**：

- `MediaServer` 照旧启动（`src/core/mediaserver.py` 原样复用），提供同一个
  `http://127.0.0.1:<port>/m/<token>/online/<song_id>` URL
- `MediaPlayer.setDataSource(url)` 直接吃这个 URL，**不走 WebView**，所以没有
  CORS / `Referer` / 沙箱 origin 的问题
- `MediaPlayer` 支持 `seekTo` / `getCurrentPosition` / `getDuration`，对应桌面的
  进度条、跳转、上一首下一首
- 回调 `onCompletion` / `onError` 映射到 Controller 的 `on_track_finished` / `on_play_error`

播放状态通过 `core/library.py` 的 session resume 落盘，与桌面端共用同一套语义。

### 3.2 存储

Android 的下载目录与本地音乐目录受 scoped storage 限制：

- 下载目录：默认 `<外部私有目录>/Music`，通过 `MediaStore` 扫描可见
- 本地音乐：走 `MediaStore` 的 `IS_MUSIC` 查询，而不是直接遍历文件夹
  （`LocalMusicLibrary` 的目录扫描逻辑在 Android 上不能直接用，需要一条
  MediaStore 版的适配）
- 需要在 `buildozer.spec` 里声明 `READ_MEDIA_AUDIO`（API 33+）与
  `WRITE_EXTERNAL_STORAGE`（API ≤ 28）

## 4. UI 规范（Material 3 + 尺寸分档）

用户要求：针对安卓手机、平板等不同尺寸分别优化，且「不要有显示不下的 UI」。

### 4.1 尺寸分档

按**可用宽度**（不是设备型号）分三档，Kivy 用 `Window.width` 判定：

| 档位 | 宽度 | 布局 |
|---|---|---|
| `compact` | < 600dp | 单栏，底部导航栏（NavigationBar），列表全屏，详情为全屏页 |
| `medium` | 600–839dp | 单栏 + 右侧详情占 40%，或列表/详情双栏 |
| `expanded` | ≥ 840dp | 双栏常驻：左列表 + 右详情，侧边导航栏 |

断点用 dp（`kivy.metrics.dp`），不是 px。手机横屏时宽度会跨过 `compact`，
布局要能实时重排（监听 `Window.size`）。

### 4.2 硬性规则

- **触控目标 ≥ 48dp**（Material 3 标准），列表行、图标按钮、chips 都适用
- **文字不截断**：歌名/歌手名用 `text_size` + `shorten=True` 单行省略，
  副标题两行封顶；不允许 `text_size` 硬编码
- **不横向溢出**：所有可滚动容器 `scroll_type=['bars','content']`，
  横向不滚动；长列表用 `RecycleView`，不用 `BoxLayout` 堆几千个控件
- **底部安全区**：内容底部 padding = `dp(16) + insets.bottom`
  （Android 手势条），用 `Window.bind(on_keyboard/on_resize)` 拿 `safe_area`
- **字号用 sp 语义**：Kivy 的 `sp()`，跟随系统字体缩放
- **深色模式**：跟随系统，`theme.py` 里定义 M3 light/dark 两套色板

### 4.3 屏幕清单

| Screen | 内容 |
|---|---|
| `HomeScreen` | 音源入口（我的歌单/我喜欢的/本地/最近/搜索）+ 正在播放条 |
| `PlaylistScreen` | 歌单列表；`compact` 下点进去是 `PlaylistDetailScreen` |
| `PlaylistDetailScreen` | 歌单曲目、下载全部、播放全部 |
| `SearchScreen` | 搜索框 + 结果（歌/歌单/歌手/专辑分档） |
| `ArtistScreen` | 歌手主页：头像、名字、简介、关注按钮、全部歌曲（分页加载） |
| `FollowedArtistsScreen` | 已关注歌手列表 |
| `LocalMusicScreen` | 本地音乐（MediaStore） |
| `DownloadsScreen` | 下载队列与进度 |
| `PlayerSheet` | 底部播放面板（`compact` 用 BottomSheet 样式，`expanded` 常驻右栏） |
| `LyricsScreen` | 全屏歌词 |
| `LoginScreen` | 扫码/手机/cookie 三种方式 |

## 5. i18n

复用 `src/i18n/*.json` 与 `src.i18n.t()`。Android 新增的文案**加到同一批 JSON 里**，
不另开文件——这样 `tests/test_i18n.py` 的双语一致性检查继续生效。

## 6. 打包

`buildozer.spec` 要点：

- `requirements = python3,kivy,requests,pyyaml,qrcode` （**不要 pillow**，见瘦身任务）
- `android.api = 34`，`android.minapi = 24`
- `android.archs = arm64-v8a`（单架构，体积最小）
- `android.release_artifact = aab`，`android.debug_artifact = apk`
- 权限在 `android.permissions` 里列表声明
- `orientation = all`：平板横屏要能用，锁成 portrait 会把这一档丢

**打包要用 `./build_apk.sh`，不要直接跑 `buildozer android debug`。**
`source.dir = .` 意味着只打包 `android/` 目录，而业务核心在仓库根的 `../src`。
脚本会先把它 rsync 到 `android/src`（已 gitignore），再确认
`src/core/api.py`、`src/version.py` 都在，然后才调 buildozer。
漏了这一步，APK 首启就会死在 `import src.core`。

体积：sdl2 之前的工具链包编译一次就缓存进 `~/.python-for-android/packages`，
之后重打包只花几分钟。

## 7. 桌面端与 Android 端的差异（明确不做）

- 桌面歌词：桌面专属，Android 用系统通知栏 + 锁屏控制代替
- 桌面壁纸取色 / 液态玻璃：Android 用 Material 3 动态取色（API 31+）
- 主题跟随系统：两端都做，但实现不同

## 8. 自检

`android/main.py` 复用桌面端 `--self-test` 的思路：检查资源存在、依赖可导入、
MediaServer 能起。不依赖图形界面，CI 可跑。
