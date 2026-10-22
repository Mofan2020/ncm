# app.js 改写蓝图

> 目标：在不破坏 AGENTS.md 列出的关键行为约束的前提下，把 `src/gui/assets/app.js`（4829 行）拆成清晰的逻辑模块；把所有 `getElementById` / `querySelector`、`state.api.*` 调用、`window.onXxx` 回调按行号归类；定位「歌单末尾混入 200+ 首其他歌单的歌曲」与「歌词 / 桌面歌词不可用」的根因。

---

## 0. 模块切分总览（行号 → 职责）

| # | 模块 | 行号 | 职责 | 备注 |
|---|------|------|------|------|
| M0 | 文件头 / 全局状态 / 工具常量 | 1–90 | `state`、`player`、播放模式常量、壁纸取色画布 | 跨模块共享，重写时保留为「store + utils」 |
| M1 | 启动 / Bridge 握手 / i18n | 91–249 | `boot`、`waitForApi`、`loadTranslations`、`t`、`applyTranslations`、`safeRender`、`refreshDynamicTexts`、`failureReason` | AGENTS.md: `pywebviewready` 必须保留 |
| M2 | 工具与对话框 | 251–329 | `fmtBytes`/`fmtSpeed`/`fmtDuration`/`fmtTime`、`levelText`、`showToast`、`showError`、`openModal`/`closeModal`、`confirmDialog`、`resolveConfirm` | 注意 1976 行有一个**重复定义** `closeModal(modalId)`，迁移时合并 |
| M3 | 设置面板 | 331–649 | `loadSettings`、`fillSettingsForm`、`fillLanguageSelect`、`renderFolderList`、`setTheme`/`updateThemeButton`/`cycleTheme`、`watchSystemTheme`、`applyAppearance`、`sampleAccent`/`clearAccent`、`saveSettings`、`applyPlaybackSettings`、`resetSettings`、`chooseDirectory`、`changeLanguage`、`chooseBackground`、`clearBackground` | 设置面板的 DOM ID 在新 HTML 里很多已不存在，需逐个重映射 |
| M4 | 登录（QR / 手机 / Cookie） | 651–816 | `refreshLoginStatus`、`updateLoginBadge`、`openLoginModal`、`switchLoginTab`、`refreshQrcode`、`sendPhoneCode`、`startCountdown`、`doPhoneLogin`、`doCookieLogin` | AGENTS.md: 800/801/802/803 状态码语义不能变；`throttled` 走 180s 倒计时必须保留 |
| M5 | 下载 Tab（URL 取歌单） | 818–960 | `fetchPlaylist`、`renderPlaylist`、`renderTracks`、`toggleSong`、`selectAll`/`deselectAll`、`updateSelectionCount` | 下载 Tab 的 DOM 已被新 HTML 完整保留（`playlist-input`、`song-list`、`btn-start`…），基本无需改 |
| M6 | 下载运行 / 进度 / 队列 | 962–1121 | `startDownload`、`downloadOptions`、`togglePause`、`cancelDownload`、`openFolder`、`updateDownloadControls`、`resetProgress`、`updateStatusPill`、`updateStatus`、`updateSongRow`、`upsertQueueItem` | `state.api.start_download` 之后完全靠 `onDownloadProgress`/`onDownloadStats`/`onDownloadComplete` 推动 |
| M7 | 播放视图骨架 / 顶部 tab 切换 | 1123–1165 | `audio` 元素常量、`showView`、`selectSource` | `showView` 现在用 `.top-tab` 切换，新 HTML 保留 `#top-tabs` 与 `.top-tab`，无需改 |
| M8 | 我的歌单 / 推荐歌单 / 榜单 / 私人 FM / 新歌 / MV | 1167–2301 | `loadPlaylists` / `renderPlaylists` / `openPlaylist` / `backToPlaylists`、`createPlaylist` / `editPlaylist` / `deletePlaylist` / `toggleSubscribePlaylist`、`loadLiked`、`loadRecommendPlaylists`、`loadPersonalFM`、`loadNewSongs`、`loadRecommendMVs`、`loadTopLists` / `renderTopLists` / `openTopList`、`loadAddTracksSource`、`loadLocalPlaylists` / `renderLocalPlaylists`、`openLocalPlaylist` / `renderLocalPlaylistTracks`、`loadLocal` / `loadRecent` | ⚠️ **「歌单末尾混入 200+ 首」bug 在此区域内**（见 §4） |
| M9 | 歌单管理 / 拖拽 / 详情模态 / 加歌 / 导入导出 / 智能歌单（占位） | 1666–2095 | `createPlaylist`/`editPlaylist`/`deletePlaylist`/`toggleSubscribePlaylist` 第二次定义、`openPlaylistDetailModal`、`loadPlaylistTracksForModal`、`renderPlaylistTracksInModal`、`handlePlaylistTrackDrag*`、`reorderPlaylistTracks`、`removeTrackFromPlaylist`、`addTracksToPlaylist`/`openAddTracksModal`/`loadAddTracksSource`/`confirmAddTracksToPlaylist`、`openSmartRuleModal`（占位）、`importPlaylist`/`parseM3U`/`parsePLS`/`exportPlaylist`/`generateM3U`/`generatePLS` | ⚠️ **1668–1717 第二次定义与 1297–1346 第一次定义完全相同，迁移时必须合并为一份** |
| M10 | 多类型搜索 / 搜索建议 / 专辑 / 艺人 / 公共头部渲染 | 2097–2576 | `doSearch` / `showSearchResults`（重复定义）/ `setListTitle`、`doMultiSearch` / `renderSearchResults` / `switchSearchType`、`openAlbumDetail` / `openArtistDetail` / `showArtistAlbums`、`initSearchSuggest`、`setListCount`、`renderSourceHeader` | `showSearchResults` 在 2139、2149 重复定义两次，行为相同，合并 |
| M11 | 可见队列 / 曲目行渲染 / 收藏 / 播放数 | 2588–2979 | `playTracksInto`、`currentKey`/`trackKeyOf`/`playCountOf`、`renderTrackList`、`findTrackRow`/`refreshTrackRow`、`buildTrackRow`、`loadPlayerExtras`、`toggleFavorite` | `buildTrackRow` 是单点真理，曲目行结构变化时只改这里 |
| M12 | 播放核心（play/pause/next/seek/mode/volume） | 2981–3446 | `renderLyricsLoading`/`renderLyricSource`、`loadLyrics`、`renderLyricLines`、`renderKaraokeLines`、`updateLyricsOffsetLabel`、`toggleDualLineLyrics`/`toggleKaraokeLyrics`/`updateLyricsModeButtons`、`openLyricsEditor`/`closeLyricsEditor`/`buildLyricsEditorTimeline`/`updateLyricsEditorTime`/`applyLyricsEditorChanges`（重复）/`saveLyricsFromEditor`、`updateLyricHighlight`、`renderNowPlaying`、`updateFavoriteButton`、`highlightPlayingRow`、`updatePlayButton`、`updateProgressUi`、`saveSession`/`startSessionTimer`/`restoreSession` | 注意 **3169、3268 都定义 `closeLyricsEditor`；3274、3288 都定义 `applyLyricsEditorChanges`** —— 全部合并 |
| M13 | 快速下载 / 本地音乐管理 / 统计 | 3478–3564 | `downloadTrack`、`addLocalFolder`/`removeLocalFolder`/`rescanLocal`、`updateLocalStatsText`、`updatePlayStatsText`、`resetPlayCounts` | AGENTS.md: `get_local_tracks` 必须分页 200，扫描缓存键空间要清楚 |
| M14 | Phase 1: 队列抽屉 | 3570–3836 | `toggleQueueDrawer`/`closeQueueDrawer`、`renderQueueDrawer`、`handleDrag*`、`moveQueueItem`、`removeFromQueue`、`clearQueue`、`addTracksToQueue`、`downloadTracks`、`saveQueueAsPlaylist` | 新 HTML 里 `#queue-drawer`、`#queue-drawer-backdrop`、`#queue-drawer-list` 都已存在 |
| M15 | 桌面歌词窗口 | 3838–3935 | `toggleDesktopLyrics` / `openDesktopLyrics` / `closeDesktopLyrics` / `pushLyricsToDesktop` / `onOpenDesktopLyrics` / `onCloseDesktopLyrics` / `onDesktopLyricsUpdate` | ⚠️ **桌面歌词「用不了」的根因**（见 §5） |
| M16 | 交叉淡入淡出（第二个 `<audio>`） | 3937–4083 | `initCrossfade`、`updateCrossfadeLabel`、`loadCurrentWithCrossfade`、`crossfadeTo`、`preloadNextTrack` | `loadCurrent` 被 `loadCurrentWithCrossfade` **直接覆盖**（line 4013–4015）—— 这是埋雷点之一 |
| M17 | 系统 Media Session / 睡眠定时器 / 播放统计 / 设置辅助 | 4086–4352 | `initMediaSession`/`updateMediaSessionMetadata`（含三处 monkey-patch: `onAudioPlay`/`onAudioPause`/`updateProgressUi`）、`initSleepTimer`/`startSleepTimer`/`clearSleepTimer`/`startFadeOut`/`executeSleepAction`/`updateSleepTimerUI`/`showSleepTimerPopover`、`renderPlaybackStatsDashboard`/`onPlaybackSettingsTabShown`、`updateDesktopLyricsOpacityLabel` | 这些覆盖原函数的写法脆弱，重写时改用事件总线 |
| M18 | 后端 → 前端回调（必须在 window 顶层） | 4355–4373 | `onOpenDesktopLyrics` / `onCloseDesktopLyrics` / `onDesktopLyricsUpdate` / `onSleepTimerSet` | 与 M15 的同名函数**互相覆盖**（line 3922–3934 与 4357–4367）—— 当前以后者为准；但二者行为不一致（M15 的 `onOpenDesktopLyrics` 会 `openDesktopLyrics()`，M18 的只 `pushLyricsToDesktop()`）。**这是 bug**：桥会调 `onOpenDesktopLyrics` 时，只刷新歌词，不会真正开窗口 |
| M19 | 关于页 / 全部静态事件绑定 | 4375–4670 | `renderAbout`、`bindStaticEvents`（含全部 `addEventListener`） | 所有 `#btn-xxx` / `#pb-xxx` 引用都要按新 HTML 重新核对 |
| M20 | `<audio>` 事件 / 后端 → 前端推送回调 | 4672–4829 | `onAudioPlay` / `onAudioPause` / `onAudioEnded` / `onAudioLoadedMetadata` / `onAudioError`、`onStatusUpdate` / `onDownloadProgress` / `onDownloadStats` / `onDownloadComplete` / `onDownloadFailed`、`onLoginStatusChange` / `onLoginMessage` / `onQrcodeUpdate` / `onLoginSuccess` / `onLoginFailed`、`onLanguageChanged` | **这批函数必须保持全局可见**，因为 `GuiBridge._call_js(...)` 通过 `window.onXxx` 调它们 |

---

## 1. 所有 `getElementById` / `querySelector` / `querySelectorAll` 调用清单

> 「迁移」一栏：✓ 保留 / ✗ 改名 / 🗑 删除 / ⚠ 合并重复 / 🔗 改成面板内选择器

| 行 | 调用 | 模块 | 用途 | 是否仍对应新 HTML | 迁移 |
|----|------|------|------|-------------------|------|
| 190 | `querySelectorAll('[data-i18n]')` | M1 | 应用 i18n 文本 | ✓ | 保留 |
| 193 | `querySelectorAll('[data-i18n-placeholder]')` | M1 | placeholder | ✓ | 保留 |
| 196 | `querySelectorAll('[data-i18n-title]')` | M1 | title | ✓ | 保留 |
| 223 | `querySelectorAll('.pill[data-status]')` | M1 | 重渲染状态徽标 | ✓ | 保留 |
| 226 | `querySelectorAll('.queue-item')` | M1 | 重渲染队列项 | ✓ | 保留 |
| 227, 231 | `item.querySelector('.queue-status' / '.reason')` | M1 | 队列状态 | ✓ | 保留 |
| 288 | `getElementById('toast-container')` | M2 | toast 容器 | ✓（新 HTML L951） | 保留 |
| 309–311 | `getElementById(id)` | M2 | 通用 open/close modal | ✓ | 保留，但确认 modal 已存在 `#confirm-modal` |
| 317 | `getElementById('confirm-message')` | M2 | 确认对话框文本 | ✓（新 HTML L696） | 保留 |
| 358 | `getElementById('download-dir-input')` | M3 | 下载目录 | ✓（新 HTML L623） | 保留 |
| 379 | `getElementById('background-input')` | M3 | 背景图 URL | ✓（新 HTML L472） | 保留 |
| 392–395 | `getElementById('background-blur'/'background-dim'/'blur-value'/'dim-value')` | M3 | 背景模糊 / 暗化 | ✓（新 HTML L480–485） | 保留 |
| 401 | `getElementById('language-select')` | M3 | 语言下拉 | ✓（新 HTML L456） | 保留 |
| 414 | `getElementById('folder-list')` | M3 | 文件夹列表 | ✓（新 HTML L596） | 保留 |
| 443, 448 | `getElementById(id)` | M3 | 通用 setValue/setChecked | ✓ | 保留 |
| 467 | `getElementById('theme-select')` | M3 | 主题下拉 | ✓（新 HTML L460） | 保留 |
| 474 | `getElementById('btn-theme')` | M3 | 顶部主题按钮 | ✓（新 HTML L51） | 保留 |
| 565–597 | `getElementById('default-quality-select'/'max-concurrent-select'/'overwrite-files-check'/'download-lyrics-check'/'lyrics-translation-check'/'priority-select'/'online-quality-select'/'resume-playback-check'/'report-play-check'/'show-translation-check'/'crossfade-duration'/'exclusive-mode-select'/'sleep-timer-select'/'language-select'/'theme-select'/'background-blur'/'background-dim'/'glass-check'/'accent-check'/'desktop-lyrics-font-size'/'desktop-lyrics-color'/'desktop-lyrics-opacity'/'desktop-lyrics-translation-check'/'remember-login-check'/'debug-check')` | M3 | 设置面板表单 | ✓（新 HTML L630–666 都有） | 保留 |
| 655 | `getElementById('download-dir-input')` | M3 | 选完目录回填 | ✓ | 保留 |
| 702 | `getElementById('btn-login')` | M4 | 登录按钮 | ✓（新 HTML L53） | 保留 |
| 738–741 | `querySelectorAll('#login-tabs .tab' / '#login-modal .tab-panel')` | M4 | 登录 modal tab 切换 | ✓（新 HTML L386 / `.tab-panel` 仍存在） | 保留 |
| 751, 796 | `getElementById('phone-input')` | M4 | 手机号输入 | ✓（新 HTML L407） | 保留 |
| 756, 775 | `getElementById('btn-send-code')` | M4 | 发送验证码按钮 | ✓（新 HTML L409） | 保留 |
| 797 | `getElementById('code-input')` | M4 | 验证码输入 | ✓（新 HTML L415） | 保留 |
| 809 | `getElementById('cookie-input')` | M4 | Cookie 登录 | ✓（新 HTML L425） | 保留 |
| 821 | `getElementById('playlist-input')` | M5 | URL 输入 | ✓（新 HTML L71） | 保留 |
| 827 | `getElementById('btn-fetch')` | M5 | 取歌单按钮 | ✓（新 HTML L73） | 保留 |
| 842 | `getElementById('download-queue')` | M6 | 队列容器 | ✓（新 HTML L164） | 保留 |
| 846–848 | `getElementById('playlist-card'/'song-card'/'btn-start')` | M5/M6 | 显示下载卡片 | ✓ | 保留 |
| 862–867 | `getElementById('playlist-name'/'playlist-creator'/'playlist-count'/'playlist-cover')` | M5 | 渲染歌单信息 | ✓（新 HTML L114–117） | 保留 |
| 881 | `getElementById('song-list')` | M5 | 渲染曲目列表 | ✓（新 HTML L131） | 保留 |
| 937, 943 | `querySelectorAll('#song-list input[type="checkbox"]')` | M5 | 全选 / 反选 | ✓ | 保留 |
| 948 | `getElementById('tracks-count')` | M5 | 已选数显示 | ✓（新 HTML L123） | 保留 |
| 971–974 | `getElementById('quality-select'/'concurrent-select'/'overwrite-check'/'lyrics-check')` | M6 | 下载 Tab 选项 | ✓（新 HTML L79–101） | 保留 |
| 1032–1044 | `getElementById('btn-start'/'btn-pause'/'btn-cancel'/'progress-fill'/'progress-text'/'overall-speed')` | M6 | 下载控件 | ✓ | 保留 |
| 1055 | `getElementById('status-text')` | M6 | 状态条 | ✓（新 HTML L369） | 保留 |
| 1060–1068 | `querySelector('.song-row[data-index=...]')` + 子查询 | M6 | 行更新 | ✓ | 保留 |
| 1076, 1077 | `getElementById('download-queue'/'empty-queue')` | M6 | 队列写入 | ✓ | 保留 |
| 1092, 1097–1101 | `item.querySelector('.queue-item-title'/'.progress-fill'/'.queue-status'/'.queue-speed'/'.queue-size'/'.reason')` | M6 | 行内字段 | ✓ | 保留 |
| 1127 | `getElementById('audio')` | M7 | `<audio>` | ✓（新 HTML L374） | 保留 |
| 1131–1133 | `querySelectorAll('.top-tab' / '.view')` | M7 | 顶 tab 切换 | ✓（新 HTML 保留 `.top-tab` / `.view`） | 保留 |
| 1146 | `querySelectorAll('.source-tab')` | M7/M8 | 源切换 | ✓（新 HTML 保留 `.source-tab`） | 保留 |
| 1148–1152 | `getElementById('local-actions'/'local-playlist-actions'/'btn-list-back'/'btn-list-more'/'search-type-tabs')` | M7 | 工具条显隐 | ✓（新 HTML L238/L245/L212/L256/L223） | 保留 |
| 1169, 1171, 1187, 1274, 1282, 1350, 1363, 1391, 1405–1410, 1414, 1428, 1538–1546, 2118, 2168–2172, 2186, 2235–2239, 2252, 2290–2298, 2320, 2377–2378, 2426–2427 | `getElementById('playlist-list' / 'track-list' / 'btn-list-back' / 'btn-list-more' / 'local-actions' / 'local-playlist-actions')` | M8 | 歌单 / 曲目双列表 | ✓（新 HTML L251/L252 都还在） | 保留 |
| 1550, 1656, 2579, 2612, 2383, 2431, 2475 | `getElementById('track-list')` + 子查询 | M8/M10/M11/M12 | 曲目渲染 | ✓ | 保留 |
| 1635, 1848 | `querySelectorAll('.playlist-track-item')` | M9 | 拖拽清理 | ✓（新 HTML 模态里保留） | 保留 |
| 1721–1737 | `getElementById('playlist-detail-modal'/'playlist-name-input'/'playlist-description-input'/'playlist-privacy-select'/'playlist-cover-input'/'playlist-tracks-list'/'btn-playlist-remove-selected'/'playlist-sort-select'/'smart-rules-list'/'smart-rule-preview-count')` | M9 | 详情 modal | ✓（新 HTML L708–773） | 保留，但 `#btn-playlist-remove-selected` / `#playlist-sort-select` 仍存在 |
| 1740, 1743 | `querySelectorAll('.playlist-detail-tab' / '.playlist-detail-panel')` | M9 | tab 切换 | ✓ | 保留 |
| 1760, 1761, 1765, 1862, 1865 | `getElementById('btn-playlist-remove-selected' / 'playlist-sort-select' / 'playlist-tracks-list')` + 子查询 | M9 | 详情里 tracks | ✓ | 保留 |
| 1878–1888 | `getElementById('playlist-add-tracks-modal'/'playlist-add-tracks-search'/'playlist-add-tracks-list'/'playlist-add-tracks-selected-count')` + `querySelectorAll('.playlist-add-tracks-tab')` | M9 | 加歌 modal | ✓（新 HTML L787–807） | 保留 |
| 1952–1957 | `querySelectorAll('#playlist-add-tracks-list input[type="checkbox"]:checked')` + 计数 | M9 | 选曲 | ✓ | 保留 |
| 1977 | `getElementById(modalId)` | M9 | 通用 close modal | ✓ | 保留，与 M2 closeModal 合并 |
| 1991–1993, 2036–2038 | `getElementById('playlist-import-format' / 'playlist-import-file' / 'playlist-import-merge' / 'playlist-export-format' / 'playlist-export-select' / 'playlist-export-extended')` | M9 | 导入导出 | ✓（新 HTML L881–910） | 保留 |
| 2098, 2106, 2306, 2498, 2499, 4477, 1364 | `getElementById('search-input' / 'search-suggest')` | M4/M8/M10 | 搜索 | ✓（新 HTML L178/L180） | 保留 |
| 2116, 2119, 2133, 2211, 2230, 2318, 2321, 2325, 2329, 2346, 2363 | `querySelectorAll('.source-tab' / '.search-type-tab')` + `getElementById('playlist-list' / 'btn-list-more' / 'btn-search' / 'search-type-tabs')` | M10 | 搜索 / 翻页 | ✓ | 保留 |
| 2160, 2546 | `getElementById('list-title' / 'list-count')` | M10 | 标题 / 计数 | ✓（新 HTML L214/L215） | 保留 |
| 2403, 2406, 2409, 2412, 2450, 2453 | 子查询 `[data-action="play-all"/"add-queue"/"download-all"/"play-hot"/"view-albums"]` 与 `[data-artist-id]` | M10 | 专辑 / 艺人内操作 | ✓（JS 自己生成） | 保留 |
| 2626, 2637, 2639, 2654, 3396, 3399 | `querySelectorAll('.track-row')` + 子查询 | M11/M12 | 曲目行 | ✓ | 保留 |
| 2899 | `getElementById('pb-mode')` | M12 | 模式按钮 | ✓（新 HTML L335） | 保留 |
| 2940, 2942 | `getElementById('pb-volume' / 'pb-mute')` | M12 | 音量 | ✓（新 HTML L363/L361） | 保留 |
| 2984–2999, 3003, 3017, 3027, 3037, 3106, 3308–3310 | `getElementById('lyrics-scroll' / 'btn-save-lyrics' / 'lyrics-source')` + 子查询 | M12 | 歌词面板 | ✓（新 HTML L294/L286/L280） | 保留 |
| 3110, 3111 | `getElementById('lyrics-offset-value' / 'lyrics-offset')` | M12 | 歌词偏移 | ✓（新 HTML L587/L588） | 保留 |
| 3143, 3144 | `getElementById('btn-lyrics-dual' / 'btn-lyrics-karaoke')` | M12 | 歌词模式 | ✓（新 HTML L282/L284） | 保留 |
| 3159, 3170, 3176, 3177, 3248, 3258, 3259, 3269 | `getElementById('lyrics-editor-modal' / 'lyrics-editor-timeline' / 'lyrics-editor-lines' / 'lyrics-editor-seek' / 'lyrics-editor-time')` | M12 | 歌词编辑 modal | ✓（新 HTML L926–941） | 保留 |
| 3326–3335 | `getElementById('np-title'/'np-artist'/'np-album'/'np-kind'/'np-plays'/'np-quality'/'np-cover'/'pb-cover'/'pb-title'/'pb-artist')` | M12 | 正在播放 | ✓（新 HTML L267–273 / L324/L326/L327） | 保留 |
| 3380 | `getElementById('pb-fav')` | M12 | bar 收藏 | ✓（新 HTML L342） | 保留 |
| 3412 | `getElementById('pb-play')` | M12 | bar 播放 | ✓（新 HTML L338） | 保留 |
| 3422–3424 | `getElementById('pb-fill' / 'pb-current' / 'pb-duration')` | M12 | bar 进度 | ✓（新 HTML L354/L352/L356） | 保留 |
| 3519 | `getElementById('btn-rescan')` | M13 | 重扫 | ✓（新 HTML L241） | 保留 |
| 3532, 3544 | `getElementById('local-stats-text' / 'play-stats-text')` | M13 | 统计文字 | ✓（新 HTML L603/L608） | 保留 |
| 3573, 3574, 3595, 3596, 3612, 3722 | `getElementById('queue-drawer' / 'queue-drawer-backdrop' / 'queue-drawer-list')` + `querySelectorAll('.queue-item-drawer')` | M14 | 队列抽屉 | ✓（新 HTML L302/L317/L312） | 保留 |
| 3952 | `getElementById('crossfade-value')` | M16 | 交叉淡入淡出值 | ✓（新 HTML L532） | 保留 |
| 4257, 4258 | `getElementById('pb-sleep-timer' / 'sleep-timer-select')` | M17 | 睡眠按钮 | ✓（新 HTML L348/L556） | 保留 |
| 4284 | `getElementById('playback-stats-dashboard')` | M17 | 统计 dashboard | ✓（新 HTML L615） | 保留 |
| 4348, 4349 | `getElementById('desktop-lyrics-opacity-value' / 'desktop-lyrics-opacity')` | M17 | 桌面歌词透明度 | ✓（新 HTML L579/L580） | 保留 |
| 4379, 4381 | `getElementById('about-version' / 'about-author')` | M19 | 关于 | ✓（新 HTML L671/L673） | 保留 |
| 4389–4670 | 全部 `bindStaticEvents` 里的 `getElementById('top-tabs' / 'btn-fetch' / 'playlist-input' / 'btn-select-all' / ... / 'pb-play' / 'pb-next' / ... / 'lyrics-editor-seek' / 'sleep-timer-select' / 'login-tabs' / 'settings-tabs')` | M19 | 静态事件绑定 | ✓（新 HTML 全部保留） | 保留 |
| 4641 | `querySelector('.modal:not([hidden])')` | M19 | Esc 关闭 modal | ✓ | 保留 |
| 4740–4743 | `getElementById('progress-fill' / 'progress-text' / 'overall-speed')` | M20 | 下载推送 | ✓ | 保留 |
| 4767 | `querySelectorAll('.pill[data-status="waiting"]')` | M20 | 下载完成重置 | ✓ | 保留 |
| 4780, 4801, 4808, 4809 | `getElementById('qrcode-status' / 'qrcode-image')` | M20 | 登录推送 | ✓（新 HTML L397/L395） | 保留 |

---

## 2. 所有 `state.api.*` / `window.pywebview.api.*` 调用清单（按模块）

> 注：`pywebview.api.xxx` 和 `state.api.xxx` 是同一个对象，前者出现在 M16 的桌面歌词窗口、`executeSleepAction` 的兜底分支（line 4246），其他都是 `state.api.xxx`。迁移时统一通过 `state.api` 调即可。

| 行 | 调用 | 模块 | 用途 |
|----|------|------|------|
| 106 | `state.api.get_app_info` | M1 | 启动时拿版本/作者 |
| 128 | `window.pywebview.api.get_settings` (typeof) | M1 | Bridge 握手探针 |
| 164, 169 | `state.api.get_translations` | M1 | 取 i18n |
| 312 | `state.api.cancel_login` | M2 | 关闭登录 modal |
| 334 | `state.api.get_settings` | M3 | 加载设置 |
| 470 | `state.api.set_theme` | M3 | 同步原生窗口主题 |
| 601, 604 | `state.api.update_settings` | M3 | 保存设置 |
| 631, 4535 | `state.api.set_crossfade_duration` | M3/M16 | 同步交叉淡入淡出时长 |
| 644 | `state.api.reset_settings` | M3 | 还原默认 |
| 653 | `state.api.choose_directory` | M3 | 选下载目录 |
| 661 | `state.api.set_language` | M3 | 切换语言 |
| 672 | `state.api.pick_background_image` | M3 | 选背景图 |
| 684 | `state.api.clear_background_image` | M3 | 清背景图 |
| 696 | `state.api.get_login_status` | M4 | 取登录态 |
| 717 | `state.api.logout` | M4 | 注销 |
| 742 | `state.api.login_qrcode` | M4 | 启 QR |
| 746 | `state.api.refresh_qrcode` | M4 | 刷 QR |
| 761 | `state.api.send_phone_code` | M4 | 发验证码 |
| 802 | `state.api.login_phone` | M4 | 手机号登录 |
| 814 | `state.api.login_cookie` | M4 | Cookie 登录 |
| 833 | `state.api.fetch_playlist` | M5 | 取歌单 + 全部 tracks |
| 977 | `state.api.start_download` | M6 | 启动下载 worker |
| 1006, 1010 | `state.api.resume_download` / `pause_download` | M6 | 暂停/恢复 |
| 1019 | `state.api.cancel_download` | M6 | 取消 |
| 1027 | `state.api.open_download_folder` | M6 | 打开目录 |
| 1172 | `state.api.get_playlists` | M8 | 我的歌单列表 |
| 1276, 1754, 2046 | `state.api.get_playlist_tracks` | M8/M9 | 取歌单曲目 |
| 1301, 1672 | `state.api.create_playlist` | M8/M9 | **重复调用**，合并 |
| 1314, 1685 | `state.api.update_playlist` | M8/M9 | **重复调用**，合并 |
| 1327, 1698 | `state.api.delete_playlist` | M8/M9 | **重复调用**，合并 |
| 1338, 1709 | `state.api.subscribe_playlist` | M8/M9 | **重复调用**，合并 |
| 1351, 1906 | `state.api.get_liked_tracks` | M8/M9 | 我喜欢 |
| 1370, 1903 | `state.api.get_local_tracks` | M8/M9 | 本地音乐分页（AGENTS.md: 必须 200/页） |
| 1392, 1909 | `state.api.get_recent_tracks` | M8/M9 | 最近播放 |
| 1412 | `state.api.get_local_playlists` | M8 | 本地歌单列表 |
| 1501 | `state.api.create_local_playlist` | M8 | 新建本地歌单 |
| 1514 | `state.api.update_local_playlist` | M8 | 改名 |
| 1527 | `state.api.delete_local_playlist` | M8 | 删除 |
| 1641 | `state.api.reorder_local_playlist_tracks` | M8 | 拖拽排序 |
| 1651 | `state.api.remove_tracks_from_local_playlist` | M8 | 从本地歌单移除 |
| 1857 | `state.api.remove_tracks_from_playlist` | M9 | 从网易云歌单移除 |
| 1964 | `state.api.add_tracks_to_playlist` | M9 | 加歌到网易云歌单 |
| 2122 | `state.api.search_tracks` | M10 | 单类型搜索 |
| 2170 | `state.api.get_recommend_playlists` | M10 | 每日推荐 |
| 2187 | `state.api.get_personal_fm` | M10 | 私人 FM |
| 2200 | `state.api.get_new_songs` | M10 | 新歌速递 |
| 2219 | `state.api.get_recommend_mvs` | M10 | 推荐 MV |
| 2237 | `state.api.get_top_lists` | M10 | 排行榜列表 |
| 2291 | `state.api.get_top_list_tracks` | M10 | 榜单曲目 |
| 2332 | `state.api.search_multi` | M10 | 多类型搜索 |
| 2372 | `state.api.get_album_detail` | M10 | 专辑详情 |
| 2421 | `state.api.get_artist_detail` | M10 | 艺人详情 |
| 2510 | `state.api.search_suggest` | M10 | 搜索建议 |
| 2742 | `state.api.reveal_path` | M11 | 本地文件显示所在文件夹 |
| 2784, 3972 | `state.api.resolve_track` | M12/M16 | 取播放 URL（AGENTS.md: 流式，不缓存） |
| 2888 | `state.api.update_settings('playback', { play_mode })` | M12 | 持久化播放模式 |
| 2932 | `state.api.update_settings('playback', { volume })` | M12 | 持久化音量 |
| 2955 | `state.api.get_play_stats` | M11 | 播放统计 + 收藏 |
| 2968 | `state.api.set_favorite` | M11 | 收藏 / 取消 |
| 3008 | `state.api.get_track_lyrics` | M12 | 取歌词（保存可选） |
| 3439 | `state.api.save_playback_state` | M12 | 保存会话 |
| 3457 | `state.api.load_playback_state` | M12 | 恢复会话（AGENTS.md: 启动时只触发一次） |
| 3486, 3807 | `state.api.download_tracks` | M11/M14 | 单/批下载 |
| 3498 | `state.api.add_local_folder` | M13 | 加本地文件夹 |
| 3511 | `state.api.remove_local_folder` | M13 | 移除 |
| 3513 | `state.api.get_settings` | M13 | 重新拉设置 |
| 3522 | `state.api.scan_local_music` | M13 | 全量重扫 |
| 3534 | `state.api.get_local_stats` | M13 | 本地统计 |
| 3556 | `state.api.reset_play_counts` | M13 | 清播放数 |
| 3827 | `state.api.save_queue_as_playlist` | M14 | 队列保存为歌单 |
| 3862, 3864 | `window.pywebview.api.open_desktop_lyrics` | M15 | 打开桌面歌词窗口 |
| 3902, 3903 | `state.api.update_desktop_lyrics` | M15 | 推送歌词到桌面歌词窗口 |
| 4078 | `state.api.preload_next_track` | M16 | 预下载下一首 |
| 4246 | `window.pywebview.api.logout` | M17 | 睡眠定时器「退出」分支兜底 |
| 4288 | `state.api.get_detailed_playback_stats` | M17 | 详细统计 |
| 4429 | `state.api.open_external` | M19 | 关于页打开主页 |
| 4678 | `state.api.record_play` | M20 | 记录一次播放 |

---

## 3. 后端 → 前端推送回调（`window.onXxx` / `_call_js`）

> 这些函数**必须在 `window` 上是全局函数**，因为 `bridge.py` 通过 `self._call_js("onXxx", payload)` 直接调。重写时用 `window.onXxx = ...` 或挂到一个 `window.__callbacks` 总线上，否则静默失败。

| 回调 | 行号（同名定义可能有多个） | 模块 | 触发源（bridge.py） | 备注 |
|------|--------------------------|------|---------------------|------|
| `onLanguageChanged(translations)` | 4826 | M20 | `_call_js` from `set_language` / `reset_settings` | 唯一 |
| `onStatusUpdate(payload)` | 4727 | M20 | `_call_js("onStatusUpdate", ...)` | 唯一 |
| `onDownloadProgress(task)` | 4731 | M20 | `_on_download_progress` (bridge.py:623) | 唯一 |
| `onDownloadStats(stats)` | 4736 | M20 | `_on_download_stats` (bridge.py:626) | 唯一 |
| `onDownloadComplete(payload)` | 4747 | M20 | `_call_js` from `_launch_download` (bridge.py:585) | 唯一 |
| `onDownloadFailed(payload)` | 4773 | M20 | `_call_js` from `_launch_download` (bridge.py:583) | 唯一 |
| `onLoginStatusChange(payload)` | 4779 | M20 | `_on_login_status_change` (bridge.py:389) | 唯一 |
| `onLoginMessage(payload)` | 4800 | M20 | `_on_login_message` (bridge.py:397) | 唯一 |
| `onQrcodeUpdate(payload)` | 4807 | M20 | `_on_qrcode_update` (bridge.py:402) | 唯一 |
| `onLoginSuccess(payload)` | 4814 | M20 | `_on_login_success` (bridge.py:408) | 唯一 |
| `onLoginFailed(payload)` | 4821 | M20 | `_on_login_failed` (bridge.py:421) | 唯一 |
| `onOpenDesktopLyrics()` | 3922, 4357 ⚠ | M15/M18 | `_call_js("onOpenDesktopLyrics", {})` (bridge.py:1617/1625/1656/1660) | **同名两次定义，行为冲突**（见 §5） |
| `onCloseDesktopLyrics()` | 3928, 4361 ⚠ | M15/M18 | `_call_js("onCloseDesktopLyrics", {})` (bridge.py:1665/1675) | 同名两次定义，行为相同，合并 |
| `onDesktopLyricsUpdate(payload)` | 3932, 4365 ⚠ | M15/M18 | `_call_js("onDesktopLyricsUpdate", {lines,index,full})` (bridge.py:1687/1691) | 同名两次定义，都 pushLyricsToDesktop，合并 |
| `onSleepTimerSet(payload)` | 4369 | M18 | `_call_js("onSleepTimerSet", {minutes,action})` (bridge.py:1698) | 唯一 |

---

## 4. 「歌单末尾混入 200+ 首其他歌单的歌曲」—— 根因分析

### 4.1 数据流（以「我的歌单」为例）

```
用户点击 #source-tabs[data-source="playlists"]
        │
        ▼
selectSource('playlists')           (M7, line 1142)
        │  player.sourceTab = 'playlists'
        │  player.playlistOpen = null      ◄── 关键：清掉当前打开的歌单状态
        │
        ▼
loadPlaylists()                     (M8, line 1167)
        │  state.api.get_playlists() → { success, playlists }
        │  player.playlists = result.playlists    ◄── 缓存所有歌单的元数据
        │  renderPlaylists(player.playlists)
        │     └─► 写入 #playlist-list（每行点击 → openPlaylist(playlist)）
        │
用户点击某歌单行
        ▼
openPlaylist(playlist)               (M8, line 1272)
        │  player.playlistOpen = playlist
        │  state.api.get_playlist_tracks(playlist.id) → { success, tracks }
        │  playTracksInto(result.tracks, { kind, id, name })
        │     └─► player.visible = tracks.slice()   ◄── 关键：覆盖「可见队列」
        │     └─► renderTrackList()                  ◄── 把 track-list 填好
        │     └─► setListCount(tracks.length)
        │
用户点击 track-list 里的某一行
        ▼
buildTrackRow(...) → 点击 → startQueue(player.visible, index, source)
        │  player.queue = tracks.slice()   ◄── 真正的播放队列
        │  playAt(index, { autoplay: true })
        │     └─► loadCurrent() → audio.src = ...
```

### 4.2 bug 高发位置（按可疑度排序）

**(A) `openLocalPlaylist` 直用 `playlist.tracks`，而 `get_local_playlists` 把 tracks 全量塞进元数据（bridge.py:1347 `lp.playlists`）**  
- 位置：`openLocalPlaylist` (line 1536) → `renderLocalPlaylistTracks(playlist.tracks || [])`  
- 风险：如果某条本地歌单被外部工具写过、或 settings.yaml 中残留了别的歌单的 `tracks` 字段（手动改 YAML），打开时就把全部混合进去。**这是「200+ 首混入」最可能的源头**。修复：让 `openLocalPlaylist` 改为只读 `playlist.tracks` 中带 `playlist_id === playlist.id` 的条目，或者干脆从后端单拉一次 tracks。

**(B) `loadLocal` 的累积分页与 `player.visible` 错位**  
- 位置：`loadLocal` (line 1362) → `LOCAL_PAGE = 200` (line 1360)  
- 流程：`player.localTracks = player.localTracks.concat(page)` 然后 `playTracksInto(player.localTracks, ...)`。  
- 风险：如果用户在「本地音乐」tab 点过一次「加载更多」（翻页），再切到「我的歌单」点开某个歌单，**但后端** `get_playlist_tracks` 返回的 tracks 数量正常（约 50–100），不会混入 200+。  
- 但是！`get_local_tracks`（bridge.py:1451）默认 `limit=200, offset=0`。如果「本地歌单管理」那侧的代码改去复用 `loadLocal(more)` 把累加值当成歌单曲目渲染，就会出现「歌单显示 200+ 首」。

**(C) `playTracksInto` 直接 `tracks.slice()` 但 `tracks` 是引用**  
- 位置：`playTracksInto` (line 2589) → `player.visible = tracks.slice()`  
- 风险：低。`.slice()` 已经做了浅拷贝，正常情况不会污染。但 `openPlaylist` 之后没有把 `player.playlistOpen` 的 `tracks` 字段同步，等下次再点别的歌单时，老 `playlistOpen.tracks` 可能被旧的引用继续被 `loadRecommendPlaylists` 等链路读到。

**(D) `openPlaylist` 没有清空 `track-list`，而 `renderTracks`/`renderTrackList` 是全量 innerHTML 覆盖，所以正常情况 OK；但若 `result.success === false`，仅把 `playlist-list` 重渲染为歌单列表，`track-list` 还残留上一份**，下一步 `renderTrackList` 在 `applyTranslations` 触发时会把残留 + `player.visible`（旧值）一并画出来。  
- 触发路径：`applyTranslations` → `refreshDynamicTexts` (line 239) → `renderTrackList()` (line 2611)  
- 这是 `safeRender('refreshDynamicTexts', ...)` 包裹的，但 `player.visible` 没清的情况下，老队列仍会被画。

**(E) `onDownloadProgress` 触发 `updateSongRow`，但 `state.tracks` 是「下载 Tab 的歌单 tracks」，与播放队列完全无关**，不会导致本 bug。仅说明一下避免混淆。

### 4.3 修复方向（重写时的清单）

1. **`openLocalPlaylist` 改成异步从后端取一次 tracks**，不再相信元数据里的 `playlist.tracks` 字段（如果元数据里 tracks 是空就显示空，bridge 加一个 `get_local_playlist_tracks(id)`）。
2. **`selectSource` 切源时把 `player.visible = []` 并清空 `#track-list`**——避免源切换后残留。
3. **`applyTranslations` 触发 `renderTrackList` 前先判 `player.visibleSource?.id` 与当前源是否一致**，不一致就跳过。
4. **`renderTrackList` 是单一真理**，所有「在 #track-list 画东西」的路径必须走它，不允许 `renderLocalPlaylistTracks` 单独往 `track-list` 写与 `renderTrackList` 同语义的内容。

---

## 5. 「歌词功能不太好用」 + 「桌面歌词用不了」—— 根因分析

### 5.1 歌词主面板数据流

```
playAt(index)
   │  audio.src = resolved.url
   │  audio.load()
   ▼
loadCurrent()                         (M12, line 2771)
   │  player.current = track
   │  renderNowPlaying() / highlightPlayingRow()
   │  renderLyricsLoading()           ◄── #lyrics-scroll 显示「loading」
   │  loadLyrics(track)               ◄── 真正去拿歌词
   │     └─► state.api.get_track_lyrics(track, save=false)
   │           │
   │           ├─ has_lyrics === true
   │           │   player.lyrics = result.lines
   │           │   player.lyricSource = result.source
   │           │   renderLyricLines()
   │           │
   │           └─ has_lyrics === false / 异常
   │               player.lyrics = []
   │               #lyrics-scroll 显示「暂无歌词」
   ▼
<audio>.timeupdate                    (M12, line 4663)
   │  updateProgressUi()
   │  updateLyricHighlight()          ◄── 二分查找 + 滚动
```

### 5.2 桌面歌词窗口数据流（双路径）

**路径 1（pywebview 原生窗口，主路径）**

```
openDesktopLyrics()                   (M15, line 3848)
   │  检查 player.desktopLyricsWindow 是否还在
   │  如果 pywebview.api.open_desktop_lyrics 存在
   │     state.api.open_desktop_lyrics()
   │       └─► bridge.py: open_desktop_lyrics()
   │             ├─ 创建 pywebview 原生窗口（frameless, on_top, easy_drag）
   │             ├─ 把 settings 写到 URL query
   │             └─ _call_js("onOpenDesktopLyrics", {})    ◄── 推回前端
   ▼
前端 onOpenDesktopLyrics()            (M15/M18, line 3922 或 4357)
   │  ⚠ 两个版本行为不同：
   │     · line 3922: 直接调 openDesktopLyrics()（再次调用会无限循环！）
   │     · line 4357: 仅 pushLyricsToDesktop()
   │
loadLyrics(track, save=?)             (M12, line 3001)
   │  state.api.get_track_lyrics()
   │  pushLyricsToDesktop()           ◄── 这才是真正"送歌词到桌面"
   │     └─► state.api.update_desktop_lyrics([], player.lyricIndex)
   │           └─► bridge.py: update_desktop_lyrics(lines, index)
   │                 ├─ first call: _call_js("onDesktopLyricsUpdate", {full:true,...})
   │                 └─ later: _call_js("onDesktopLyricsUpdate", {index, full:false})
   ▼
前端 onDesktopLyricsUpdate(payload)   (M15/M18, line 3932 或 4365)
   │  pushLyricsToDesktop()
   │     ⚠ 注意：桥推送回前端后，前端又调 update_desktop_lyrics() 给桥
   │       形成「推 → 触发 → 推」的循环！第一行 lines=[]，
   │       后续只送 index，但 _call_js 仍然每秒触发多次。
```

**路径 2（浏览器 `window.open` 兜底）**

```
openDesktopLyrics()                   (M15, line 3848)
   │  如果 pywebview.api.open_desktop_lyrics 不存在
   │     player.desktopLyricsWindow = window.open(url, 'desktop-lyrics', ...)
   ▼
桌面歌词窗口加载 desktop-lyrics.html
   │  读 URL 参数 → localStorage['ncm:desktop-lyrics:settings']
   │  初始化 → announceReady() → opener.postMessage({type:'ready'})
   ▼
主窗口没有接收 desktop-lyrics 的消息监听器
   ⚠ 当前主窗口只在 pushLyricsToDesktop 里 postMessage / setItem，
     但桌面歌词通过 localStorage('ncm:desktop-lyrics:update') 通知主窗口
     没有任何反向监听。
```

### 5.3 主面板歌词「不太好用」的可能原因

**(A) `updateLyricHighlight` 的二分查找**（line 3294–3319）—— 二分逻辑本身是对的，时间复杂度 `O(log n)`。但：
- 歌词行 `time` 单位是**毫秒**（line 3048 `wrapper.dataset.time = String(line.time)`，`line.time` 是 ms）。
- `audio.currentTime * 1000` 也是 ms。一致。
- 唯一注意点：`player.lyrics[mid].time <= ms` 时记录 `found = mid` 然后 `low = mid + 1`，最终 `found` 是「最后一条 ≤ 当前时间」的索引，**符合 LRC 习惯**。
- ⚠ 但是 `renderLyricLines` 在 dual-line 模式下把每个 wrapper 内的 originalLine/translationLine 都设了 `dataset.time`，但 `updateLyricHighlight` 用 `box.querySelectorAll('.lyric-line')` 拿的是原始歌词行（每个 wrapper 里只有 originalLine 带 `.lyric-line` class）—— 正常 OK。dual-line 模式下 `.lyric-line` 会翻倍（original + translation），高亮会同时点亮两行。

**(B) `applyLyricsEditorChanges` 改完歌词不会重新渲染 / 不会写入当前播放**

`applyLyricsEditorChanges`（line 3274/3288）是占位实现，只弹 toast 关闭 modal：
```js
function applyLyricsEditorChanges() {
    showToast('info', t('lyrics.editor_apply'), 2000);
    closeLyricsEditor();
}
```
未把编辑后的 `player.lyrics` 写回磁盘，也未推到桌面歌词窗口。

**(C) `lyricOffset` 改值会重新 `loadLyrics(player.current, false)`**（line 4566–4574），但 **bridge 端 `get_track_lyrics` 没看到 offset 入参**——说明 offset 只在 UI 上变化，没真正影响 LRC 时间偏移。可能根本没生效。修复：把 offset 加到 `get_track_lyrics(track, save, offsetMs)`，bridge 拿到后给所有 `line.time` 加 offset。

**(D) 自动滚动在用户滚轮/触摸后暂停 4 秒再恢复**（line 4602–4613）。但 `lastScrollAt` 只在 wheel/touchstart/mousedown 时设，**键盘方向键滚动不算**，这倒不算 bug，只是设计选择。

**(E) 重复定义导致旧版本覆盖新版本**：`closeLyricsEditor` 出现两次（line 3169、3268），第二次覆盖第一次；`applyLyricsEditorChanges` 两次（line 3274、3288），同样。功能上等价于同一份，但代码混乱，重写时合并。

### 5.4 桌面歌词「用不了」的可能原因（最关键）

**(1) `onOpenDesktopLyrics` 二次定义冲突** —— 上面 §3 已列出。**line 3922 的版本会再次调 `openDesktopLyrics()`，导致无限递归**（如果 `pywebview.api.open_desktop_lyrics` 存在，bridge 重新推 `onOpenDesktopLyrics`）。line 4357 的版本安全（仅 `pushLyricsToDesktop()`）。但 JS 函数声明是 hoisted 的，**后面那个（4357）会覆盖前面的**。所以现在实际行为是「按了按钮后桥创建窗口并推 `onOpenDesktopLyrics` → 前端只 push 已有歌词」，**桌面窗口会开，但只有初始的 lyricIndex 0，没有任何歌词 lines 数据**（因为 `pushLyricsToDesktop` 调 `state.api.update_desktop_lyrics([], lyricIndex)` —— `lines` 是空数组）。

**(2) `state.api.update_desktop_lyrics` 走的是 `_call_js("onDesktopLyricsUpdate", ...)`**，把数据推回前端 `onDesktopLyricsUpdate` → 再次 `pushLyricsToDesktop()` → 再次 `state.api.update_desktop_lyrics()` → **自反馈循环**。看起来是 bug。

**(3) 第一行 `lines` 是空数组** —— `pushLyricsToDesktop()` 调用 `state.api.update_desktop_lyrics([], player.lyricIndex)`（line 3903），**根本就没把 `player.lyrics` 传过去**！这导致 `bridge.py:1682–1691`：
```python
if not self._desktop_lyrics_initialized:
    self._call_js("onDesktopLyricsUpdate", {"lines": lines, "index": index, "full": True})
    self._desktop_lyrics_initialized = True
else:
    self._call_js("onDesktopLyricsUpdate", {"index": index, "full": False})
```
首次收到 `lines=[]`，所以桌面歌词窗口的 `state.lines` 被设为 `[]` —— 桌面窗口永远没有歌词内容。

**(4) 桌面歌词窗口的 `desktop-lyrics.js` 仅通过 `opener.postMessage` 接收 `ncm:desktop-lyrics` 消息**（line 152）。但 pywebview 多窗口之间**不能用 `opener`**（它们是不同的 native window，`window.opener` 是 null）。pywebview 的窗口间通信靠 `_call_js` 推到**主窗口的全局函数**，而不是窗口对窗口。**当前实现没有任何代码把桌面歌词窗口的全局作用域暴露给主窗口**，所以主窗口调 `pushLyricsToDesktop()` 走的是 `state.api.update_desktop_lyrics` —— 它又通过 `_call_js` 把数据**推回主窗口自己**，永远到不了桌面歌词窗口。

**(5) `player.desktopLyricsWindow`（line 3867）只在 `window.open` 兜底分支被赋值；pywebview 主路径下窗口引用永远在 Python 侧**，前端拿不到窗口对象，所以 `player.desktopLyricsWindow.postMessage(...)` 在主路径下永远无效。

**(6) `localStorage.setItem('ncm:desktop-lyrics:update', ...)` 兜底**（line 3916）—— pywebview 多窗口**不共享 localStorage**（各窗口独立 WKWebView 实例），所以兜底也无效。

**(7) 桌面歌词窗口只有 `handleMessage` 接收消息**，但 main 那边通过 `_call_js` 推的是 `onDesktopLyricsUpdate(payload)`（line 4365 调 pushLyricsToDesktop）。**`pushLyricsToDesktop` 不传 lyrics lines，只传 lyricIndex**。即使把消息送到桌面窗口，桌面窗口也没有完整的歌词数据。

### 5.5 桌面歌词修复方向

1. **明确通信路径**：pywebview 创建的窗口之间**应该**通过 `bridge.py` 维护的 `_desktop_lyrics_window` 直接调 `evaluate_js('handleMessage(payload)')`。把 `desktop-lyrics.js` 的 `handleMessage` 暴露到 `window`（已经是函数，可直接调），让 `bridge.py:update_desktop_lyrics` 在桌面歌词窗口上执行 `self._desktop_lyrics_window.evaluate_js(...)`。
2. **传完整 lyrics lines**：前端 `pushLyricsToDesktop` 应该 `state.api.update_desktop_lyrics(player.lyrics, player.lyricIndex)`（**不是空数组**）。bridge 端判 `full=True` 时把 `lines` 完整推过去。
3. **去掉 `_call_js("onDesktopLyricsUpdate", ...)` 这条自反馈**：在 `bridge.py` 里直接 `_desktop_lyrics_window.evaluate_js(f"handleMessage({json.dumps(payload)})")`，不要回到主窗口。
4. **去掉 `onOpenDesktopLyrics` 的两次定义**：保留 line 4357 那个（仅 push 当前歌词），删除 line 3922 的（避免递归）。
5. **去掉 `localStorage` 兜底**：pywebview 不共享，不要把误导的代码留着。
6. **保留 `window.open` 兜底分支**：仅在非 pywebview 环境下有用；不需要 `player.desktopLyricsWindow` 在主路径里被赋值，因为主窗口根本不持有子窗口引用。

---

## 6. 改写时必须保留的「行为约束」（来自 AGENTS.md）

| 约束 | 涉及模块 | 重写时如何保留 |
|------|---------|---------------|
| **Pacing**：不破坏 `MIN_REQUEST_INTERVAL` | M8/M10（搜索） | 不要并发调 `state.api.search_tracks` |
| **QR 状态码 800/801/802/803** | M4/M20 | `onLoginStatusChange` 已知码与未知码分支保留 |
| **URL 解析在下载 worker 内部** | M6 | `startDownload` 把整个 options 丢给后端，不预解析 |
| **扩展名取 API 返回的 type，不取请求 level** | M6 | 不在前端解析 quality→ext |
| **`.part` + atomic move + 删除临时文件** | M6（后端）/ 不涉及前端 | —— |
| **Theme：URL query 优先级 + bridge.set_theme 同步原生 chrome** | M1/M3 | `setTheme` 必须 `state.api.set_theme(mode)` |
| **资源路径走 `resource_path()`** | 不涉及前端 | —— |
| **`window.pywebview.api` 在方法注入前不可用，必须等 `pywebviewready`** | M1 | `waitForApi()` 必须先于任何 `state.api.xxx` |
| **媒体服务器 sandboxed** | M12/M16 | 前端不直连文件系统，只走 `resolve_track()` 拿 URL |
| **Shuffle 与播放数独立** | M12 | `nextIndex`/`previousIndex` 必须用 `Math.random()`，不引入 count 权重 |
| **本地扫描缓存键空间是 `path+mtime/size`，track map 键是 `local:<path>`** | M13 | 前端不直接读这两个键空间，只用 `get_local_tracks` / `scan_local_music` |
| **`sidecar_lyrics_path()` 不列目录** | 不涉及前端 | —— |
| **分页 200 首（`get_local_tracks`）** | M8/M9 | `LOCAL_PAGE = 200` 不能改 |
| **媒体服务器流式 256KB chunks** | M12/M16 | `<audio>` 元素直接吃 URL，不要 `fetch().arrayBuffer()` |
| **resume session only-when-changed** | M12 | `saveSession` 必须保留 `now - lastSaveAt < 5000` 短路 + `force` 标记 |
| **`report_play_count` 默认关闭** | M11 | `onAudioPlay` → `state.api.record_play` 在用户开启时才上报（后端已处理）；前端必须保留 `pendingRecord` 标记 |
| **`playback:` YAML 块 + 未知键忽略 + 越界 clamp** | M3 | `applyPlaybackSettings` 信任 `state.settings.playback` |
| **`play_mode` 越界回落 `list`** | M12 | `setMode` 已 clamp |
| **i18n: 不写硬编码字符串** | 全部 | 新模块也要走 `t(key)` |

---

## 7. 重复 / 死代码清单（重写时一并清理）

| 行 | 问题 | 处理 |
|----|------|------|
| 1297–1346 与 1668–1717 | `createPlaylist` / `editPlaylist` / `deletePlaylist` / `toggleSubscribePlaylist` 完全重复 | 保留一份（建议保留 1297 起始那份，离 `loadPlaylists` 更近） |
| 1976 | `closeModal(modalId)` 与 310–313 的 `closeModal(id)` 重名但形参名不同 | 合并成一份 |
| 3169, 3268 | `closeLyricsEditor` 重复 | 合并 |
| 3274, 3288 | `applyLyricsEditorChanges` 重复 | 合并 |
| 2139, 2149 | `showSearchResults` 重复 | 合并 |
| 3922 vs 4357 | `onOpenDesktopLyrics` 行为冲突 | 删 3922，留 4357（仅 pushLyricsToDesktop） |
| 3928 vs 4361 | `onCloseDesktopLyrics` 行为相同 | 合并 |
| 3932 vs 4365 | `onDesktopLyricsUpdate` 行为相同 | 合并 |
| 4013–4015 | `loadCurrent = loadCurrentWithCrossfade` 直接覆盖 | 改为「在 `initCrossfade` 后立刻覆盖」的显式 monkey-patch，或重命名 `loadCurrent` 不再出现「替换」字样 |
| 4140–4162 | `onAudioPlay` / `onAudioPause` / `updateProgressUi` 三处 monkey-patch | 改为事件总线或干脆把 Media Session 更新内联到 `onAudioPlay` 等函数里 |
| 1846–1849 | `handlePlaylistTrackDragEnd` 没清理自身源，只清理全部 | 与 1633 `handleLocalPlaylistTrackDragEnd` 雷同，建议抽出 `clearDragState` |
| 1983–1986 | `openSmartRuleModal` 仅弹 toast | 删除或保留为占位（新增 `#smart-rule-modal` 需要重新启用） |
| 1990–2095 | `importPlaylist` / `exportPlaylist` 部分未连接桥，模态 `<input type="file">` 只读，不创建文件 | 重写时要么删，要么完整实现 |
| 4283–4337 | `renderPlaybackStatsDashboard` 用 inline CSS，没接到设置面板 tab 切换 | 新 HTML 已留 `#playback-stats-dashboard`，需要让设置面板的「Playback」tab 切到时调用 `onPlaybackSettingsTabShown`（目前没有 wiring） |
| 4341–4343 | `onPlaybackSettingsTabShown` 未被任何地方调 | 在设置 modal 的 tab 切换里调用 |
| 4355–4367 | 「Backend Callbacks」区段重复了 M15 的同名函数 | 整段移到 M15 |

---

## 8. 「侧边栏 vs 旧 source-tabs」的差异（迁移侧边栏 HTML 时注意）

新 HTML 是侧边栏 + 内容的 player 布局（line 173–299），source-tabs 在 `.player-side` 内而不是顶部。当前 JS 通过 `document.querySelectorAll('.source-tab')` 仍能定位（line 1146、2116、2318），**不需要改选择器**。

但是新 HTML 的「侧边栏」还包含了一个 `#sidebar-nav` / `data-source`（line 188–209，旧 HTML 标记）的分组结构（旧设计）。**新 HTML 把侧边栏分组去掉了，全部 source-tabs 平铺**。所以：
- 旧版 `.source-tab` 的来源切换逻辑完全保留；
- 旧版分组标题 `online_sources` / `local_sources` / `search` 在新 HTML **不存在**（直接被 `<h4 class="sidebar-section-title">` 在 line 188-208 之前的旧 HTML 里，**新 HTML 已删除**），对应的 i18n key 仍然存在但页面没用到——重写时要么补回分组（建议加回来，对应「在线 / 本地 / 搜索」三组），要么删 i18n key。

---

## 9. 推荐的章节划分（重写后的目录结构建议）

```
app.js（保持单文件，但用 IIFE / ES module 风格拆章节）
├─ §1  Header（顶部 tab + 主题 + 登录 + 设置入口）
├─ §2  Bootstrap（boot / waitForApi / loadTranslations / applyTranslations）
├─ §3  Store（state / player 的 getter-setter）
├─ §4  Modals & Toasts & Confirms
├─ §5  Settings & Appearance（设置面板 + 主题 + 壁纸 + 取色）
├─ §6  Login（QR / 手机 / Cookie + 状态推送回调）
├─ §7  Download Tab（URL 取歌单 + 选曲 + 启动下载 + 进度推送回调）
├─ §8  Player Source Router（selectSource）
├─ §9  Playlists（NetEase / Local / Recommend / Top / New / MV / Search）
├─ §10 Track List Rendering（playTracksInto / renderTrackList / buildTrackRow）
├─ §11 Playback Core（playAt / loadCurrent / advance / mode / seek / volume）
├─ §12 Lyrics（主面板歌词 + 偏移 + 编辑）
├─ §13 Desktop Lyrics Window（单独成章节，所有 onOpenDesktopLyrics 等在这里唯一）
├─ §14 Now Playing Bar
├─ §15 Queue Drawer
├─ §16 Crossfade & Preload
├─ §17 Media Session / Sleep Timer / Stats
├─ §18 Local Music（folder management + scanning + stats）
├─ §19 Favorites & Resume Session
├─ §20 Backend Callbacks（仅这一处定义 onXxx，并把所有 _call_js 目标注册在 window 上）
├─ §21 Audio Element Events
├─ §22 Static Event Bindings（bindStaticEvents）
```

每个章节头部加注释 `/* ========== §X Name ========== */`，方便后续再做物理文件拆分。

---

## 10. 一句话总结

- **歌单混入 bug**：高概率在 `openLocalPlaylist` 直接用 `playlist.tracks` 字段（`get_local_playlists` 全量塞进元数据）；次概率在「源切换不清 `player.visible`」导致 `applyTranslations` 重渲染残留。
- **桌面歌词 bug**：`pushLyricsToDesktop` 没把 lyrics lines 传出去（line 3903 `state.api.update_desktop_lyrics([], lyricIndex)`，`lines` 是空数组），加上 `onOpenDesktopLyrics` 两次定义冲突 + 自反馈循环，整条链路从未把完整歌词送到桌面窗口。
- **歌词主面板 bug**：`applyLyricsEditorChanges` 是空实现，`lyricOffset` 没真正传进 `get_track_lyrics`，dual-line 模式下高亮会同时点亮两行。
- **重构红线**：AGENTS.md 列的 pacing / QR / shuffle 独立 / 分页 200 / 流式播放 / resume only-when-changed 必须逐条保留；合并 8 处重复定义；新 HTML 的 ID 全部已就位，迁移时几乎不需要重命名，但要补回设置面板 tab → playback stats dashboard 的连线。