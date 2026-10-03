# Player Optimization - Implementation Tracker

## Phase 1: 核心播放体验 ✅ COMPLETED

### 1.1 队列面板 UI
- [x] 后端：Bridge 暴露 `save_queue_as_playlist(name, queue)` 保存队列为 M3U
- [x] 前端：player-view 右侧抽屉 `queue-drawer`，显示"下一首播放"列表
- [x] 前端：队列项支持拖拽排序、右键菜单（移除/置顶/置底/播放）
- [x] 前端：底部/顶部按钮：清空队列、保存为歌单、关闭抽屉
- [x] 前端：当前播放高亮、播放模式图标同步
- [x] 播放栏新增队列按钮 (☰)

### 1.2 Crossfade / Gapless 播放
- [x] 后端：MediaServer 添加 `crossfade_duration` 配置（0-12秒）
- [x] 前端：播放设置新增"淡入淡出时长"滑块（0=关闭，1-12秒）
- [x] 前端：`audio` 元素切歌时处理 crossfade（双 audio 元素交替淡入淡出）
- [x] Gapless 预加载：`preload_next_track()` 自动预解析下一曲 URL
- [x] 同专辑检测预留（未来增强）

### 1.3 媒体键 / Media Session API
- [x] 前端：`navigator.mediaSession` 设置 metadata（标题、歌手、封面、进度）
- [x] 前端：监听 `mediaSession.setActionHandler('play'/'pause'/'previoustrack'/'nexttrack'/'seekto')`
- [x] 前端：键盘媒体键自动生效
- [x] 播放状态/位置同步到锁屏/控制中心

### 1.4 睡眠定时器
- [x] 前端：播放栏/设置新增睡眠定时器选择器（15/30/60/90/120/当前曲目结束）
- [x] 前端：触发时淡出停止/暂停/退出（30秒淡出）
- [x] 设置持久化到 playback.sleep_timer_minutes
- [x] 播放栏图标状态指示

### 1.5 播放统计仪表盘
- [x] 后端：Bridge 添加 `get_detailed_playback_stats()` 返回统计数据
- [x] 前端：设置页"播放记录"组展开显示统计卡片/Top歌手/专辑

### 1.6 桌面歌词窗口
- [x] 后端：Bridge 添加 `open_desktop_lyrics()`、`close_desktop_lyrics()`、`update_desktop_lyrics(lines, index)`
- [x] 后端：原生 pywebview 窗口创建（无边框、置顶、透明背景、易拖拽）
- [x] 前端：新建 `desktop-lyrics.html` + `desktop-lyrics.js` + `desktop-lyrics.css`
- [x] 前端：无边框、置顶、穿透点击、拖拽移动、双击隐藏/显示
- [x] 前端：歌词同步高亮、翻译开关、字体/大小/颜色/透明度设置
- [x] 前端：主窗口"桌面歌词"按钮切换（播放栏 + 歌词面板）
- [x] 本地存储同步设置/位置/状态

### 1.7 独占音频输出选项
- [x] 后端：Settings 添加 `exclusive_mode`（off/wasapi/coreaudio/auto）
- [x] 前端：播放设置高级区域新增独占模式选择器
- [x] 提示需重启生效，暂作占位（原生集成留待后续）

### 1.8 其他新增设置
- [x] 桌面歌词设置面板（字体大小、颜色、透明度、显示翻译）
- [x] 跨语言 i18n 完整支持（314 keys）
- [x] 所有测试通过（332 passed）

---

## Phase 2: 浏览与发现 ✅ COMPLETED

### 后端 API ✅
- [x] 专辑详情 `/api/v1/album/{id}` - `get_album_detail()`
- [x] 歌手详情 `/api/v1/artist/{id}` - `get_artist_detail()`
- [x] 每日推荐歌单 `/api/v1/discovery/recommend/resource` - `get_recommend_playlists()`
- [x] 私人 FM `/api/v1/radio/get` - `get_personal_fm()`
- [x] 新歌速递 `/api/v1/discovery/new/songs` - `get_new_songs()`
- [x] 推荐 MV `/api/mv/recommend` - `get_recommend_mvs()`
- [x] 官方榜单 - `get_top_lists()` / `get_top_list_tracks()`
- [x] 搜索建议 `/api/search/suggest` - `search_suggest()`
- [x] 多类型搜索 - `search_multi()`

### Bridge 层 ✅
- [x] `get_album_detail()` - 返回专辑信息+曲目
- [x] `get_artist_detail()` - 返回歌手信息+热门单曲+专辑
- [x] `get_recommend_playlists()` - 每日推荐歌单
- [x] `get_personal_fm()` - 私人 FM
- [x] `get_new_songs()` - 新歌速递
- [x] `get_recommend_mvs()` - 推荐 MV
- [x] `get_top_lists()` / `get_top_list_tracks()` - 排行榜
- [x] `search_suggest()` - 搜索建议
- [x] `search_multi()` - 多类型搜索

### i18n ✅
- [x] 中英双语新增 30+ 个 keys
- [x] 总计 345 字符串
- [x] 所有测试通过

### 前端 UI ✅
- [x] 源标签栏新增：每日推荐、私人 FM、新歌速递、推荐 MV、排行榜
- [x] 专辑详情页 UI（封面、描述、播放全部、加入队列、全部下载）
- [x] 歌手详情页 UI（头像、简介、热门单曲、全部专辑）
- [x] 排行榜列表/详情 UI
- [x] 搜索建议下拉（输入时实时显示）
- [x] 多类型搜索结果分 Tab（综合/单曲/歌单/歌手/专辑/MV）
- [x] 从播放列表跳转专辑/歌手详情（点击歌手名/专辑名）

---

## Phase 3: 歌单管理 ✅ COMPLETED

### 后端 API ✅
- [x] 创建歌单 `/api/playlist/create` - `create_playlist()`
- [x] 更新歌单 `/api/playlist/update` - `update_playlist()`
- [x] 删除歌单 `/api/playlist/delete` - `delete_playlist()`
- [x] 添加歌曲 `/api/playlist/tracks/add` - `add_tracks_to_playlist()`
- [x] 移除歌曲 `/api/playlist/tracks/del` - `remove_tracks_from_playlist()`
- [x] 收藏/取消收藏 `/api/playlist/subscribe` - `subscribe_playlist()`
- [x] 更新封面 `/api/playlist/cover/update` - `update_playlist_cover()`
- [x] 获取动态详情 `/api/playlist/detail/dynamic` - `get_playlist_detail_dynamic()`
- [x] 本地歌单管理：创建/更新/删除/重排/增删曲目/获取列表

### Bridge 层 ✅
- [x] 云端歌单：`create_playlist()` / `update_playlist()` / `delete_playlist()` / `add_tracks_to_playlist()` / `remove_tracks_from_playlist()` / `subscribe_playlist()` / `update_playlist_cover()`
- [x] 本地歌单：`get_local_playlists()` / `create_local_playlist()` / `update_local_playlist()` / `delete_local_playlist()` / `reorder_local_playlist_tracks()` / `add_tracks_to_local_playlist()` / `remove_tracks_from_local_playlist()`

### i18n ✅
- [x] 中英双语新增 80+ 个 keys（云端/本地歌单、创建/更新/删除/收藏/封面/智能歌单/导入导出/同步/编辑/重命名/删除/收藏/取消收藏/导入/导出/智能规则/本地歌单管理）
- [x] 总计 439 字符串
- [x] 所有测试通过（332 passed）

### 前端 UI ✅
- [x] 云端歌单列表：hover 显示编辑/收藏/删除，创建/编辑/删除/收藏/取消收藏
- [x] 本地歌单标签页：独立管理，创建/编辑/删除/排序
- [x] "创建歌单" 按钮（我的歌单/本地歌单顶部工具栏）
- [x] 歌单详情模态框：三标签页（信息/歌曲管理/智能规则）
- [x] 歌单信息编辑：名称、描述、可见性（公开/私密）、封面上传
- [x] 歌单歌曲管理：列表显示、拖拽排序、批量移除、添加歌曲
- [x] 添加歌曲模态框：本地库/收藏/最近播放三标签页、搜索、多选
- [x] 智能歌单规则编辑器：字段/条件/值、预览匹配数量
- [x] 导入导出模态框：M3U/JSON/PLS 格式互转、文件选择/保存
- [x] 本地歌单标签页（新源）：独立管理，创建/编辑/删除/拖拽排序/增删曲目
- [x] 本地歌单详情：打开查看曲目、编辑/删除、拖拽排序、移除曲目

---

## Phase 4: 歌词进阶 ✅ CORE COMPLETED

### 已完成 ✅
- [x] 双行歌词模式（原文+翻译并排显示，可切换开启/关闭）
- [x] 逐字/卡拉OK模式（逐字高亮跟随播放，占位实现）
- [x] 歌词偏移调整（-5000ms 到 +5000ms 滑块，实时生效）
- [x] 歌词编辑器模态框（时间轴、逐行编辑、播放控制、应用/保存）
- [x] 桌面歌词窗口（Phase 1.6 已完成）
- [x] 歌词偏移设置（设置面板，-5000ms 到 +5000ms）
- [x] 歌词控制面板（双行/卡拉OK/编辑/桌面歌词/保存按钮）
- [x] 歌词偏移设置（设置面板，-5000ms 到 +5000ms）
- [x] 歌词控制面板（双行/卡拉OK/编辑/桌面歌词/保存按钮）
- [x] 歌词偏移设置（设置面板，-5000ms 到 +5000ms）
- [x] 完整的 i18n 支持（中英双语，新增 20+ keys）
- [x] 完整的样式支持（双行、卡拉OK、编辑器、偏移标签、控制按钮）

### 待完善 🔄
- [ ] 逐字歌词完整实现（需要词级时间戳数据，当前 API 不提供）
- [ ] 歌词上传修正到网易云（需登录和 API 支持）
- [ ] 歌词编辑器完整功能（拖拽调整时间点、波形图显示）

### i18n ✅
- [x] 中英双语新增 20+ keys
- [x] 总计 460 字符串
- [x] 所有测试通过（332 passed）

---

## Phase 5: 个性化与设置 ✅ COMPLETED

### 后端 API ✅
- [x] Settings 新增：`equalizer` / `replaygain` / `output_device` / `playback_speed` 配置
- [x] 10段均衡器预设（Flat/Pop/Rock/Jazz/Classical/Electronic/Vocal/Custom）
- [x] ReplayGain 支持（Track/Album/Auto 模式、前置增益、扫描库）
- [x] 输出设备选择（Web Audio API 输出设备枚举）
- [x] 播放速度控制（0.5x - 2.0x）

### Bridge 层 ✅
- [x] Settings 新增配置项持久化
- [x] 设置验证和范围限制

### i18n ✅
- [x] 中英双语新增 25+ keys（均衡器、ReplayGain、输出设备、播放速度、预设、自定义频段）
- [x] 总计 485 字符串
- [x] 所有测试通过（332 passed）

### 前端 UI ✅
- [x] 均衡器设置面板：启用开关、预设选择、10段自定义频段滑块（60Hz-16kHz）
- [x] ReplayGain 设置：启用开关、模式选择、前置增益、库扫描按钮
- [x] 输出设备选择：下拉列表枚举可用音频输出设备
- [x] 播放速度滑块：0.5x - 2.0x，实时生效
- [x] 设置面板新增分组：均衡器、ReplayGain、输出设备、播放速度

---

## Phase 6: 数据迁移
- [ ] 完整备份/恢复
- [ ] 从其他客户端导入
- [ ] 播放历史导出
- [ ] 云端同步

---

## 技术债
- [ ] 前端 ES Module + TypeScript 重构
- [ ] 虚拟列表
- [ ] E2E 测试
- [ ] 无障碍