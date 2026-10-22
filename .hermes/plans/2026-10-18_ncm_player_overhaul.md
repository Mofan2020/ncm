# NCM 播放器修复计划（修订版，2026-10-18）

## 上下文
- HEAD 上 index.html = 955 行完整旧三栏布局（已含 sidebar-nav/player-sidebar，是混合版）
- HEAD 上 app.js = 4829 行（保留原状，未改）
- 之前未提交的 wiper 已 stash 起来后丢弃，**好改动已取回工作树**（app.js 55 行歌词编辑器增强 / desktop-lyrics.* / bridge.py 桌面歌词优化 / i18n / TODO.md）
- 测试 baseline: **327 passed, 6 skipped**

## 用户报告 5 件事
1. 播放页 UI 不美观
2. 首次进入不立即获取信息，切别的 tab 才有数据
3. 内存占用尽量优化
4. 歌单末尾混入 200+ 首其他歌单的曲子
5. 歌词功能不太好用，桌面歌词用不了

## 修复策略

### Phase 1: 跑 baseline 确认现状
```
pytest tests/ -q
python3 main.py --self-test
```

### Phase 2: 修 app.js 4 个具体 bug（不整体重写）
- **2a 启动自动加载默认 source**：bindStaticEvents 末尾调用 `selectSource('recommend')`（不需登录）
- **2b track-list 重置**：所有 source loader 进入时 `track-list.innerHTML = ''`
- **2c 歌词偏移**：updateLyricHighlight 应用 lyricOffset
- **2d offset slider 不重新加载**：仅更新 player.lyricOffset

### Phase 3: 重写 desktop-lyrics.js
- 收到 full lyrics 重渲染 DOM（current + prev + next 三行）
- 缓存 line DOM 引用

### Phase 4: UI 美观
- 重写 player 区域 CSS（半透明 + 圆角 + 大封面）
- 不引入新框架

### Phase 5: 内存优化
- 曲目列表虚拟化（>200 行）
- 歌词面板虚拟化
- rAF 替代 setInterval
- image lazy/async
- 切换 source 清空可见数据
- toast 上限 5

### Phase 6: 测试保持 327+
### Phase 7: 单个 commit + TODO 更新

## 不做
- 不整体重写 app.js
- 不重写 download view
- 不引入新依赖
- 不改 bridge.py RPC 接口