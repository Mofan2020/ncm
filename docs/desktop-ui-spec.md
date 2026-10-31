# 桌面端界面规格：首页分区 · 聚合搜索 · B站分区

适用：`src/gui/assets/`（pywebview 窗口，HTML/CSS/JS）。
本文只定义界面，不含业务代码。实现归 engineer。
视觉语言沿用现有液态玻璃（`.glass` + `--glass-*`），不新起一套。

---

## 0. 前提与约束

| 项 | 值 | 出处 |
|---|---|---|
| 窗口最小尺寸 | 780 × 560 | `main.py:31` `MIN_SIZE = (780, 560)` |
| 现有视图 | `#view-download` / `#view-player`，靠 `.view.is-active` 切换 | `index.html:63` `:175` |
| 顶部一级导航 | `.top-tabs` / `.top-tab[data-view]` | `index.html:43` |
| 播放器侧栏项 | `#sidebar-nav .sidebar-item` | `app.js:2091` |
| 图标 | `icons.js` 37 个 Feather 名，`data-icon` + `setIcon()` | 已核 |
| i18n | 18 个命名空间，`ns.key` | `src/i18n/zh_cn.json` |

### 0.1 可用图标（37 个，只能用这些）

```
align-center  align-left  arrow-down  arrow-up  back  chevron-down  chevron-right
clock  close  download  edit  file-text  folder  heart  list  menu  monitor  moon
more  music  next  pause  play  prev  refresh  repeat  repeat-one  reveal  settings
shuffle  smartphone  star  sun  trash  user-plus  volume  volume-muted
```

实际情况与派单描述有两处出入，按文件为准：

1. 静音图标叫 **`volume-muted`**，不是 `volume-x`。
2. **没有** `x` / `search` / `home` / `grid` / `filter` / `chevron-up`。关闭用 `close`，返回用 `back`，搜索用 `music` 或 `list` 兜底，筛选一律用原生 `<select>`（见 2.3）。

### 0.2 三个新界面的落点

| 界面 | 容器 | 一级位置 |
|---|---|---|
| 首页分区 | `#view-home`（新增 `.view`） | 新增 `.top-tab[data-view="home"]`，默认激活 |
| 聚合搜索 | `#view-search`（新增 `.view`） | 首页卡片进入，顶部「首页」tab 保持高亮 |
| B站分区 | `#view-bili`（新增 `.view`） | 首页卡片进入，顶部「首页」tab 保持高亮 |

「设置」**不新建视图**，直接打开已有的 `#settings-modal`（`index.html:547`）。理由：设置本来就是模态，做成页面等于把已有的 12 个分页签搬一遍，纯返工。

「网易云」不新建视图，进入时切到 `#view-player` 并把 source 设为 `playlists`（复用 `selectSource('playlists')`，`app.js:1988` 那套）。理由：歌单/排行榜/推荐已经在侧栏里，重做一份等于让两份状态互相打架。

顶部 tab 从 2 个变 3 个：**首页 / 下载 / 播放**，`tabs.home` 新增一个 key。子页（聚合搜索、B站）不改顶部 tab 高亮，只有「首页」亮着——顶部指示的是导航树的一级位置，子页不该让指示器来回跳。

---

## 1. 首页分区选择

### 1.1 布局：2×2 卡片网格

```
┌──────────────────────────────────────────────────────┐
│  选择分区                                             │
│  从一个入口开始                                       │
│                                                      │
│  ┌──────────────────┐  ┌──────────────────┐          │
│  │ [music]          │  │ [play]           │          │
│  │ 网易云            │  │ B站               │          │
│  │ 歌单、排行榜、推荐 │  │ 热门榜与分类视频  │          │
│  │ 我的歌单 12       │  │ 热门 100          │          │
│  └──────────────────┘  └──────────────────┘          │
│  ┌──────────────────┐  ┌──────────────────┐          │
│  │ [list]           │  │ [settings]       │          │
│  │ 聚合搜索          │  │ 设置              │          │
│  │ 一次搜索，两个源  │  │ 播放、下载、外观  │          │
│  │ 2 个来源          │  │                  │          │
│  └──────────────────┘  └──────────────────┘          │
└──────────────────────────────────────────────────────┘
```

选网格不选列表：只有 4 个入口，网格一屏看完，列表要滚；网格的视觉重量也更像「分区」而不是「菜单」。

不选「顶部 4 个 tab」：4 个一级 tab 会和现有 2 个一级 tab 叠成两级 tab，导航层级说不清。

### 1.2 每张卡片

| 卡片 | 图标 | 标题 key | 一句话 key | 计数行 |
|---|---|---|---|---|
| 网易云 | `music` | `home.entry_netease` | `home.entry_netease_desc` | 已登录：`home.count_playlists`「我的歌单 {count}」；未登录：`home.count_signed_out`「未登录」 |
| B站 | `play` | `home.entry_bili` | `home.entry_bili_desc` | `home.count_trending`「热门 {count}」 |
| 聚合搜索 | `list` | `home.entry_search` | `home.entry_search_desc` | `home.count_sources`「2 个来源」，固定值 |
| 设置 | `settings` | `home.entry_settings` | `home.entry_settings_desc` | 无 |

用 `play` 而不是 `music` 表示 B站：`music` 已经被「网易云」占了，两个音乐类图标并排会分不清。`play` 是三角，跟「视频」的心理模型对得上。

计数是弱信息，用 `.home-card-count`（`--text-3`，11.5px，`tabular-nums`），永远在卡片最后一行。数据没到之前显示 `--`，请求失败**不显示错误**，直接把这一行藏掉（`.is-empty` → `display:none`）。理由：分区页是导航，一个计数请求失败不该污染整页。

### 1.3 结构

```html
<section class="view is-active" id="view-home">
  <div class="home-wrap">
    <header class="home-head">
      <h1 class="home-title" id="home-title" data-i18n="home.title"></h1>
      <p class="home-sub muted" data-i18n="home.subtitle"></p>
    </header>
    <div class="home-grid" id="home-grid" aria-labelledby="home-title">
      <button class="home-card is-current" type="button" data-entry="netease" aria-current="true">…</button>
      <button class="home-card" type="button" data-entry="bili">…</button>
      <button class="home-card" type="button" data-entry="search">…</button>
      <button class="home-card" type="button" data-entry="settings">…</button>
    </div>
  </div>
</section>
```

卡片内部：

```html
<span class="home-card-icon" data-icon="music" aria-hidden="true"></span>
<span class="home-card-title" data-i18n="home.entry_netease"></span>
<span class="home-card-desc" data-i18n="home.entry_netease_desc"></span>
<span class="home-card-count" data-i18n="home.count_playlists" data-i18n-args='{"count":12}'></span>
```

`data-i18n-args` 得看现有 `app.js` 的插值函数叫什么，实现时对齐现有的（`{count}` 占位符风格与 `status.downloading` 一致）。

### 1.4 当前分区高亮

| 状态 | 表现 |
|---|---|
| 默认 | 玻璃底 + `--glass-border` 描边 + `inset 0 1px 0 var(--glass-edge)` 高光 |
| 从某个分区返回 | 该卡加 `.is-current` + `aria-current="true"`：左侧 3px `--accent` 竖条（`top/bottom: 14px`），描边换成 `--brand-soft` |
| 首次进入 | 无高亮 |

竖条样式直接照抄 `.sidebar-item.is-active::before`（`style.css:1640`）。理由：同一个「你在这儿」的语义，全应用应只有一个视觉表达。

### 1.5 返回

- 子页左上角 `back` 图标按钮（`.btn.btn-icon`，`data-icon="back"`，`aria-label` 取 `home.back_home`），点击回首页。
- `Esc` 回首页。仅在**无弹窗打开、焦点不在输入框内**时生效；输入框内 `Esc` 只清空输入框。
- `Alt+Left`（Windows）/ `Cmd+[`（macOS）也回首页，走同一条路径。
- 回首页后焦点落在刚才离开的那张卡上。理由：键盘用户回来能直接换分区，不用重新 Tab 四遍。

### 1.6 键盘与焦点顺序

Tab 顺序 = DOM 顺序：网易云 → B站 → 聚合搜索 → 设置。

方向键（网格内）：

| 键 | 行为 |
|---|---|
| `→` / `←` | 同一行内左右移动，到行尾不环绕 |
| `↓` / `↑` | 下一行 / 上一行同列 |
| `Home` / `End` | 第一张 / 最后一张 |
| `Enter` / `Space` | 进入 |

理由：2×2 一共 4 个格子，`↓` 比 Tab 直观；不环绕是为了和 macOS 键盘导航的默认行为一致，环绕会让人以为到底了还能继续。

焦点环：`:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }`，鼠标点击不显示。热区整卡可点（卡片本身就是 `<button>`），最小 176 × 320，远超 44pt。

### 1.7 窄窗口退化

窗口本身卡在 780 宽，所以这是防御性规则：

| 容器宽 | 表现 |
|---|---|
| ≥ 860 | 2 列，卡片宽自动分配，容器 `max-width: 720px` 居中 |
| 760 – 860 | 2 列，`padding` 收到 12px，卡片 `min-height: 156px` |
| < 760 | 1 列，卡片 `min-height: 128px`，图标与文字改为横向排（图标左、文字右），整页可滚 |

单列时永远不出现横向滚动条：`grid-template-columns: minmax(0, 1fr)`，卡片内所有文字 `min-width: 0` + 省略。

### 1.8 首页状态

| 状态 | 表现 |
|---|---|
| 加载（计数未回） | 计数行显示 `--`，卡片其余部分立即可用，不做骨架。理由：导航不该被数据阻塞 |
| 空（计数接口失败） | 计数行隐藏，卡片可点 |
| 错误 | 不报错。若整页数据源不可达，底部出现一行 `.home-foot`（`--warn` 文字 + `refresh` 图标的文字按钮）「部分数据不可用 · 重试」 |
| 离线 | 顶部 `.offline-bar`（见 4.5），卡片仍可点；B站卡计数隐藏，网易云卡可点但进去会走播放器自己的离线态 |

---

## 2. 聚合搜索结果页

### 2.1 整体结构

```
┌────────────────────────────────────────────────────────────┐
│ [←]  [ 🔍 搜索歌曲、歌单、视频            ]  [ 搜索 ]        │
│ ┌ 全部 │ 网易云 │ B站 ┐  [分类▾]  [排序▾]  [时长▾]  [清除] │
│  已筛选: 网易云 ✕   1-5 分钟 ✕                              │
│ 找到 236 条 · 网易云 120 · B站 116                          │
│ ┌────────────────────────────────────────────────────────┐ │
│ │▎[封面] 标题文字…            [网易云]              4:12  │ │
│ │        歌手 · 专辑                                      │ │
│ ├────────────────────────────────────────────────────────┤ │
│ │▎[封面] 视频标题…              [B站]             12:06  │ │
│ │        UP主 · 12.3万播放 · 482 弹幕                     │ │
│ └────────────────────────────────────────────────────────┘ │
│ [                    加载更多                    ]          │
└────────────────────────────────────────────────────────────┘
```

整页滚动（`.search-page { overflow-y: auto }`），不做嵌套滚动区。理由：最小窗口高 560，再套一个内层滚动列表只剩 200px 可用，滚两次才看到第三条。

顶部搜索区**不吸顶**。理由：吸顶带玻璃背景要额外处理滚动模糊，560 高时粘性工具栏会吃掉三分之一屏。

### 2.2 结果项信息层级

三行信息按视觉权重排：标题 > 来源 > 副信息 > 时长。

| 元素 | 规格 |
|---|---|
| 左侧色条 `.result-row::before` | 3 × 26px，圆角 2，左偏移 4px，`--brand` / `--bili`，常显 `opacity: .55`，悬停 `1` |
| 封面 | 40 × 40，`--cover-radius`，`object-fit: cover`，无封面时 `.is-empty` 用 `--sunken` 底 + `music` 图标（`--text-3`） |
| 标题 | 13.5px / 600 / `--text`，单行 `ellipsis`，`min-width: 0` |
| 来源标签 `.source-badge` | 高 18px，圆角 5，10.5px / 600，紧跟在标题右侧 `gap: 6px`，`flex: 0 0 auto` |
| 副信息 | 11.5px / `--text-2`，单行 `ellipsis`。网易云：`歌手 · 专辑`；B站：`UP主 · {播放} 播放 · {弹幕} 弹幕` |
| 时长 | 12px / `--text-3` / `tabular-nums`，右列固定 `min-width: 46px`，右对齐 |

来源标签色：网易云 `--brand-soft` 底 + `--brand` 字；B站 `--bili-soft` 底 + `--bili` 字（`#fb7299`，B站品牌粉）。
标签用色块不用纯文字：长列表里色块比文字先被扫到，且不占行宽。

色条和标签同时存在是刻意的冗余：色条负责「扫」，标签负责「确认」。

### 2.3 筛选器

四个维度，一行放完，`flex-wrap: wrap` 兜底。

| 维度 | 控件 | 选项 | 位置 |
|---|---|---|---|
| 来源 | 分段控件（复用 `.top-tabs` + `.top-tab.is-compact`） | `全部` / `网易云` / `B站` | 最左，最常用 |
| 分类 | 原生 `<select class="filter-select">` | `全部` / `歌曲` / `歌单` / `视频` | 中段 |
| 排序 | 原生 `<select class="filter-select">` | `相关度` / `播放量` / `最新` / `时长` | 中段 |
| 时长 | 原生 `<select class="filter-select">` | `全部` / `1 分钟以内` / `1–5 分钟` / `5–15 分钟` / `15 分钟以上` | 最右 |

来源用分段控件因为它最常切、只有 3 个值，多一次点击不值得；分类/排序/时长值多且不常改，用原生 `<select>`。

一律用原生 `<select>`，不自造下拉弹层：项目里到处都是原生 select（`.sort-select`、`#quality-select`），自造弹层在 WKWebView 里跟 NSMenu 的观感对不上，还要自己管外点关闭、键盘、滚动。

宽度预算（780 窗口，内容区 744）：分段 3 × 64 + 内边距 ≈ 204，三个 select 各 110 = 330，间距 5 × 8 = 40，合计 574，装得下。放不下时 `flex-wrap: wrap`，第二行左对齐，**不出现横向滚动**。

`filter-select` 在非默认值时：边框 `--brand`、文字 `--text`、字重 600。理由：收起状态下也得一眼看出筛了什么。

### 2.4 筛选状态可见性

三层可见性，缺一层都会有人搞不清为什么结果这么少：

1. **控件本身**：非默认值的 select 变强调色（见 2.3）。
2. **chips 行** `.filter-chips`：只在存在非默认筛选时出现，每个维度一个 chip（`--brand-soft` 底 + `close` 小图标），点击 `×` 单独清掉该维度。末尾一个 `.btn-link`「清除筛选」清空全部。
3. **统计行** `.result-meta`：始终显示「找到 236 条 · 网易云 120 · B站 116」，数字用 `tabular-nums`，变化时 160ms 透明度交叉淡入（不做滚动计数，滚动计数在短时高频更新下会抖）。

chips 行出现/消失用高度过渡（`max-height` 0 → 32px，160ms）。折起来不留空位。

### 2.5 三个非正常状态

**加载中**：不做全屏转圈。结果区放 5 行骨架 `.skeleton-row`（高 60px，内部三块灰条：40px 方块 + 两条不等宽横条），列表位置和真实结果完全一致。搜索按钮进 `is-loading`（文字换成 `search.loading`，`disabled`，不换图标——没有 spinner 图标）。

- 换关键词重新搜索时：旧结果先降到 `opacity: .4`，新结果回来后整块替换。不先清空，理由：清空会让列表高度塌陷再弹回，视觉上像闪了一下。

**空结果**：结果区居中一块（上下 `padding: 48px 16px`）。

```
[list]  ← 40px 图标，--text-3
没有找到结果            ← 15px / 600
换个关键词，或把来源切到「全部」   ← 12.5px / --text-2
[ 清除筛选 ]            ← .btn.btn-sm.btn-secondary，仅当有筛选时出现
```

空态必须给下一步动作，否则用户只能盲试。

**出错**：按源降级，不做整页错误。

| 情况 | 表现 |
|---|---|
| 一个源失败、另一个成功 | 列表照常显示成功的那部分；列表顶部一条 `.inline-error`：「B站 搜索失败：{message}」+ `重试` 文字按钮；统计行改为 `search.result_partial`「网易云 120 · B站 未返回」 |
| 两个源都失败 | 结果区整块换成错误态：`close` 图标 + 标题「搜索失败」+ 具体原因 + `重试` 主按钮 |
| 网络失败（`error.network`） | 同上，文案用现成的 `error.network`「网络错误，请检查连接」 |

聚合搜索最怕一个源挂了整页白屏。降级是硬要求，不是优化。

**离线**：进入页面时 `navigator.onLine === false` → 顶部 `.offline-bar` + 搜索按钮 `disabled` + 结果区显示离线空态（`volume-muted`? 不用，用 `close`）。已有结果不清空，只读展示。

### 2.6 分页

- 每源每页 20 条，合并后一屏最多 40 条。
- 「加载更多」放在列表末尾，整宽 `.btn.btn-sm.btn-block`（照 `#btn-list-more` 的形态），文案 `search.load_more`。
- 加载中：文案换 `common.loading`，`disabled`，保持按钮高度不变（不塌）。
- 没有更多：按钮换成一整行 `.result-end`（`--text-3`，12px，居中）「没有更多结果了」。
- 累计上限 200 行。到上限时「加载更多」换成 `.result-end` 文案 `search.cap_hint`「结果过多，用筛选缩小范围」。

不做虚拟化：200 行静态 DOM 在 pywebview 里滚动没有压力，虚拟化要接管滚动位置、焦点和 `aria-activedescendant`，收益不抵成本。分页 + 上限已经覆盖了「不能卡」。

排序作用于**已加载的合并结果**，切换排序不重新请求；「加载更多」按当前排序追加后重排。理由：每切一次排序打两个接口，在 NetEase 的 pacing 限制下（`MIN_REQUEST_INTERVAL`）会明显卡。

### 2.7 搜索交互

- 输入框 `#aggr-input` 用 `.search-wrapper`，回车即搜索。
- 从首页进入时焦点自动进输入框（用键盘进入时），鼠标进入则不抢焦点。
- 空关键词时搜索按钮 `disabled`。
- 搜索中再次回车 = 取消上一次请求并重发（请求序号比对，旧响应丢弃）。

---

## 3. B站分区页

### 3.1 结构

```
┌──────────────────────────────────────────────────────────────┐
│ [←]  B站                                        [refresh]    │
│ [热门] 全部  音乐  舞蹈  游戏  知识  科技  生活  影视  美食    │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐                          │
│ │ ▢ 16:9  │ │ ▢ 16:9  │ │ ▢ 16:9  │                          │
│ │ 标题两行 │ │         │ │         │                          │
│ │ UP·12万 │ │         │ │         │                          │
│ └─────────┘ └─────────┘ └─────────┘                          │
│ [                      加载更多                     ]        │
└──────────────────────────────────────────────────────────────┘
```

页面容器 `.bili-page`（`overflow-y: auto`），头部 `.page-head` 一行：返回按钮 + 标题 + 右侧 `refresh` 图标按钮。

### 3.2 热门榜与分类 chips

同一个 chip 行，第一个是「热门」，后面是分类。不分成两行两个控件。

理由：热门和分类是同一维度的互斥选择（选了「音乐」就不在热门榜里），拆成两个控件会让人以为能同时成立。

| 项 | 值 |
|---|---|
| 行容器 | `.chip-row`，`display: flex; flex-wrap: wrap; gap: 6px;` |
| chip | 高 30px，`padding: 0 12px`，圆角 999，`--glass-soft` 底，`1px --glass-border` 描边，12.5px / 500 / `--text-2` |
| 悬停 | 底 `--hover`，字 `--text` |
| 按下 | `scale(.97)`，120ms |
| 选中 | 底 `--bili-soft`，字 `--bili`，600，无描边 |
| 分类数量 | 8 个（全部 / 音乐 / 舞蹈 / 游戏 / 知识 / 科技 / 生活 / 影视） |

`wrap` 不滚动：780 宽时 9 个 chip 会折成两行，折行比隐藏好——藏起来的分类等于不存在，横向滚动条在桌面端没人发现。

选中色用 B站粉而不是 `--brand`：`#view-bili { --chip-active-bg: var(--bili-soft); --chip-active-fg: var(--bili); }`，chip 用 `var(--chip-active-bg, var(--brand-soft))`。一个控件一处变量覆盖，不写第二套类。

### 3.3 网格与封面

| 项 | 值 |
|---|---|
| 网格 | `grid-template-columns: repeat(auto-fill, minmax(180px, 1fr))`，列间距 `clamp(10px, 1.2vw, 16px)`，行间距 18px |
| 780 宽实际列数 | 3 列（每列 ≈ 238px） |
| 封面比例 | 16 : 9（视频封面是 16:9，套 1:1 会裁掉标题区） |
| 封面圆角 | `--cover-radius`（10px） |
| 封面悬停 | 内部 `img` `scale(1.035)`，280ms `cubic-bezier(.22,1,.36,1)` |
| 时长角标 | 右下角 6/6px 内缩，`rgba(0,0,0,.62)` 底，白字 11px，圆角 5px，`tabular-nums` |

### 3.4 文字层级：UP主和播放量不能抢标题

```
标题（2 行截断，13.5px / 600 / --text / min-height: 2 × 1.35em）
UP主 ········································· 12.3万播放
      ↑ --text-3 / 11.5px / 省略             ↑ --text-3 / tabular-nums / 不省略
```

- UP主**永远不加粗、不用 `--text` 色**，跟播放量同色同级，都是 `--text-3`。
- 两者同一行，`justify-content: space-between`；UP主 `min-width: 0` + 省略，播放量 `flex: 0 0 auto`。播放量不能被挤掉，它是选片的主要依据之一。
- 标题固定两行高度（`min-height` 而不是 `height`），避免单行标题把整行卡片顶歪。

### 3.5 状态

| 状态 | 表现 |
|---|---|
| 加载中 | 6 个骨架卡：16:9 灰块 + 两行文字条，`shimmer` 1.6s；chips 仍可点 |
| 空 | `list` 图标 + 「这个分类今天没有内容」+ `刷新` 按钮 |
| 错误 | 网格区内联错误块：`close` 图标 + 原因 + `重试`。chips 保留，切换分类即重新请求 |
| 切换分类 | 旧网格降到 `opacity: .35`，新数据回来整块替换 |
| 离线 | `.offline-bar` + 网格区离线空态 + 刷新按钮 `disabled` |
| 加载更多 | 网格下方整宽按钮，行为同 2.6 |

---

## 4. 通用

### 4.1 页面头 `.page-head`

聚合搜索和 B站共用：

```
display: flex; align-items: center; gap: 8px;
padding: 0 0 2px; min-height: 40px;
```

返回按钮 `hidden` 由视图决定；标题 15px / 600；右侧动作靠 `margin-left: auto`。

### 4.2 图标用法

全部走 `data-icon` + 现有 `setIcon()`，不写 `<img>`、不写 emoji。

| 场景 | 图标 |
|---|---|
| 返回首页 | `back` |
| 刷新列表 | `refresh` |
| 关闭 / 清除 chip | `close` |
| 网易云分区 | `music` |
| B站分区 | `play` |
| 聚合搜索 | `list` |
| 设置 | `settings` |
| 无封面占位 | `music` |
| 空态（通用） | `list` |
| 错误态 | `close` |

### 4.3 离线条 `.offline-bar`

```
高度 28px，整宽，--warn 12% 底，--warn 文字，12px，居中，
左侧 12px 内边距，图标不用（28px 里塞图标会挤）
```

出现时把内容区整体下推 28px（不浮在上面盖住第一行）。理由：浮层会挡住第一张卡的标题，而这个条可能一直存在。

### 4.4 动效

| 元素 | 规格 | 可打断 | reduced-motion |
|---|---|---|---|
| 首页卡片悬停 | `translateY(-2px)` + 阴影，180ms `cubic-bezier(.22,1,.36,1)` | 是 | 只留阴影，不位移 |
| 首页卡片按下 | `scale(.985)`，120ms | 是 | 保留（压缩感是反馈，不是装饰） |
| 视图切换 | `opacity` 0→1 + `translateY(6px)`→0，220ms | 否（一次性） | 只留透明度，120ms |
| chip 选中 | 底色/文字 140ms | 是 | 保留 |
| 骨架 shimmer | 1.6s linear infinite | — | 静态 `--sunken` 块，不动 |
| chips 行展开 | `max-height` 0→32px + `opacity`，160ms | 是 | 直接出现 |
| 统计数字 | 透明度交叉 160ms | 是 | 直接替换 |
| 封面放大 | `scale(1.035)`，280ms | 是 | 取消，只留遮罩变深 |

统一走 `@media (prefers-reduced-motion: reduce)`，在文件末尾追加一次覆盖，不给每个组件单独写。

### 4.5 响应式

窗口最小 780 × 560，以下按容器宽度（不是视口，用 `@media` 即可，pywebview 视口=窗口）。

| 宽度 | 首页 | 聚合搜索 | B站 |
|---|---|---|---|
| ≥ 1200 | 2 列，容器 720 居中 | 列表满宽，副信息可显示专辑 | 5 列 |
| 900 – 1200 | 2 列 | 满宽 | 4 列 |
| 780 – 900 | 2 列，内边距 12 | 满宽，筛选条可能折两行 | 3 列 |
| < 780（防御） | 1 列，图标横排 | 筛选条折行；副信息只留歌手/UP主 | 2 列 |

高度 < 600 时：区块间距 16 → 12，卡片内边距 18 → 14，首页卡片 `min-height` 176 → 156。

任何宽度下都不出现横向滚动：所有文字容器 `min-width: 0`，图片 `max-width: 100%`，网格用 `minmax(0, 1fr)` 或 `auto-fill`。

### 4.6 无障碍

| 项 | 要求 |
|---|---|
| 对比度 | 正文 `--text` on `--surface` ≥ 7:1；次要文字 `--text-2` ≥ 4.5:1；`--text-3` 只用于 ≥ 11.5px 的非关键信息。`--bili` `#fb7299` on `--bili-soft` 在浅色下约 3.4:1 —— **`--bili` 只用于 10.5px 加粗标签和 3px 色条，不承载正文** |
| 焦点 | 所有新交互元素 `:focus-visible` 有 2px `--accent` 外描边；焦点永不丢失（切换视图后焦点落到页面标题 `tabindex="-1"`） |
| 动态字体 | 所有字号用固定 px（webview 无 `dynamic-type` 通道）。跟随缩放的兜底：`html { font-size: 14px }` 不变，但所有新组件用 `rem` 以外的一致 px，避免和现有样式打架。**这一项和现有代码库一致，不单独引入 rem 体系** |
| VoiceOver | 卡片 `aria-label` = 标题 + 一句话；结果行 `aria-label` = 标题 + 来源 + 副信息 + 时长；chip 用 `aria-pressed`；分段控件 `role="tablist"` + `aria-selected`；筛选用 `<label>` 包裹 select，`<label>` 文字用 `.sr-only` |
| 热区 | 所有可点元素 ≥ 32 × 32；chip 30px 高 + 上下 `padding` 让实际热区到 34；返回按钮 `.btn-icon` 补齐到 36 × 36 |
| 图标 | `setIcon()` 已输出 `aria-hidden="true"` 与 `focusable="false"`，不要覆盖 |

---

## 5. Token 复用与新增

### 5.1 复用现有 CSS 变量（不改值）

| 变量 | 值 | 用在哪 |
|---|---|---|
| `--brand` | `#ec4141` | 网易云来源色、主按钮、选中竖条 |
| `--brand-hover` | `#d63b3b` | 主按钮悬停 |
| `--brand-soft` | `rgba(236,65,65,.14)` | 来源标签底、chip 选中底、`is-current` 底 |
| `--glass` / `--glass-strong` / `--glass-soft` | 见 `style.css:29-31` | 卡片、chip、select 底 |
| `--glass-border` / `--glass-edge` | 见 `:33` | 描边与顶部高光 |
| `--glass-blur` / `--glass-saturate` | `26px` / `180%` | 卡片与 chip 的 `backdrop-filter` |
| `--surface-strong` | `rgba(255,255,255,.78)` | 次要按钮底 |
| `--text` / `--text-2` / `--text-3` | 见 `:18-20` | 三级文字 |
| `--hover` | `rgba(255,255,255,.55)` | 行悬停、chip 悬停 |
| `--sunken` | `rgba(120,130,150,.12)` | 封面色块、骨架底、分段控件槽 |
| `--divider` | `rgba(60,70,90,.12)` | 结果行分隔 |
| `--shadow-sm` / `--shadow` | 见 `:40-41` | 卡片静止 / 强调 |
| `--radius` / `--radius-sm` / `--radius-xs` | `20 / 12 / 9px` | 卡片 / 行 / 小标签 |
| `--warn` | `#d99a1f` | 离线条 |
| `--danger` | `#d64545` | 错误文字 |
| `--accent` | `var(--brand)` | 焦点环 |
| `--header-h` / `--status-h` / `--playerbar-h` | `62 / 30 / 84px` | 布局高度计算 |

### 5.2 复用现有 class（不改定义）

| class | 用途 |
|---|---|
| `.view` / `.view.is-active` | 三个新视图的显隐 |
| `.glass` | 卡片、chip 行容器 |
| `.card` / `.card-head` / `.card-body` | 结果列表外层（可选，列表本身不加卡片边） |
| `.top-tabs` / `.top-tab` / `.top-tab.is-active` | 顶部一级导航 + 来源分段控件 |
| `.btn` / `.btn-primary` / `.btn-secondary` / `.btn-ghost` / `.btn-link` / `.btn-icon` / `.btn-sm` / `.btn-block` | 所有按钮 |
| `.search-wrapper` / `.search-suggest` | 聚合搜索输入框 |
| `.inline-form` | 头部「输入框 + 按钮」 |
| `.list-area` / `.playlist-list` / `.track-list` | 结果列表容器 |
| `.source-head` / `.list-title` / `.source-foot` | 页面头与底部 |
| `.sidebar-item` / `.sidebar-item.is-active` | 高亮竖条的样式来源（照抄 `::before`） |
| `.muted` | 所有次要文字 |
| `.empty` | 空态容器 |
| `.pill` | 时长角标的地基（覆盖背景色） |
| `.sort-select` | 筛选 `<select>` 的基础样式 |
| `.sr-only` | 筛选 `<label>` 的隐藏文字 |
| `.progress-track` | 骨架条（覆盖背景） |
| `.tabs` | B站分区内部的备选分段（若来源分段换用） |

### 5.3 新增 token

| 名字 | 默认值 | 用途 | 可调 | 范围 |
|---|---|---|---|---|
| `--bili` | `#fb7299` | B站来源色、B站 chip 选中字色 | 是 | 任意色，需保证 on `--bili-soft` ≥ 3:1 |
| `--bili-soft` | `rgba(251,114,153,.14)` | B站标签底、B站 chip 选中底 | 是 | 与 `--bili` 同色相的 10–18% 透明 |
| `--cover-radius` | `10px` | 视频封面与结果缩略图圆角 | 是 | 6–16px |
| `--card-lift` | `0 2px 6px rgba(16,24,40,.08), 0 18px 40px rgba(16,24,40,.16)` | 首页卡片悬停阴影 | 是 | — |
| `--grid-gap` | `clamp(10px, 1.2vw, 16px)` | 所有新网格的列间距 | 是 | 8–20px |
| `--home-card-h` | `176px` | 首页卡片最小高度 | 是 | 140–220px |

深色主题追加（`[data-theme="dark"]`）：

```css
--bili-soft: rgba(251, 114, 153, 0.2);
--card-lift: 0 2px 8px rgba(0, 0, 0, .45), 0 20px 44px rgba(0, 0, 0, .5);
```

紧凑/无玻璃主题（`style.css:89-113` 那段会把 `--glass-blur` 归零）无需额外覆盖，新组件全部走 `--glass-*`，会自动跟着变实心。

### 5.4 新增 class（给值与用途，实现照抄）

```css
/* 首页 */
.home-wrap  { width:100%; max-width:720px; margin:0 auto; padding:clamp(16px,2vw,28px);
              display:flex; flex-direction:column; gap:clamp(14px,1.6vw,22px);
              overflow-y:auto; min-height:0; }
.home-title { font-size:clamp(19px,1.4vw+13px,24px); font-weight:700; letter-spacing:-.02em; }
.home-sub   { margin:0; }
.home-grid  { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
              gap:clamp(10px,1.2vw,16px); }
.home-card  { display:flex; flex-direction:column; align-items:flex-start; gap:8px;
              min-height:var(--home-card-h); padding:18px; text-align:left;
              border:1px solid var(--glass-border); border-radius:var(--radius);
              background:var(--glass); color:var(--text); font:inherit; cursor:pointer;
              position:relative; overflow:hidden;
              -webkit-backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-saturate));
              backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-saturate));
              box-shadow:inset 0 1px 0 var(--glass-edge), var(--shadow-sm);
              transition:transform .18s cubic-bezier(.22,1,.36,1), box-shadow .18s; }
.home-card:hover  { transform:translateY(-2px);
                    box-shadow:inset 0 1px 0 var(--glass-edge), var(--card-lift); }
.home-card:active { transform:scale(.985); }
.home-card:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.home-card.is-current { border-color:var(--brand-soft); }
.home-card.is-current::before { content:''; position:absolute; left:0; top:14px; bottom:14px;
                                width:3px; border-radius:2px; background:var(--accent); }
.home-card-icon  { width:44px; height:44px; border-radius:12px; display:grid; place-items:center;
                   background:var(--brand-soft); color:var(--brand); flex:0 0 auto; }
.home-card-icon .icon { width:22px; height:22px; }
.home-card-icon[data-entry="bili"]     { background:var(--bili-soft); color:var(--bili); }
.home-card-icon[data-entry="search"]   { background:var(--sunken); color:var(--text-2); }
.home-card-icon[data-entry="settings"] { background:var(--sunken); color:var(--text-2); }
.home-card-title { font-size:15px; font-weight:600; }
.home-card-desc  { font-size:12.5px; color:var(--text-2); }
.home-card-count { margin-top:auto; font-size:11.5px; color:var(--text-3);
                   font-variant-numeric:tabular-nums; }
.home-card-count.is-empty { display:none; }

/* 通用页面骨架 */
.search-page, .bili-page { display:flex; flex-direction:column; gap:12px;
                           padding:clamp(12px,1.4vw,18px); width:100%;
                           min-height:0; overflow-y:auto; overscroll-behavior:contain; }
.page-head { display:flex; align-items:center; gap:8px; min-height:40px; }
.page-title { font-size:15px; font-weight:600; }
.page-head .btn-icon { width:36px; height:36px; display:grid; place-items:center; }
.page-head .page-actions { margin-left:auto; display:flex; gap:6px; }

/* 聚合搜索 */
.filter-seg  { flex:0 0 auto; }
.top-tab.is-compact { padding:5px 12px; font-size:13px; }
.filter-bar  { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.filter-select { height:32px; max-width:132px; min-width:96px; padding:0 8px;
                 border-radius:var(--radius-xs); border:1px solid var(--glass-border);
                 background:var(--glass-soft); color:var(--text-2); font:inherit; font-size:12.5px; }
.filter-select.is-set { border-color:var(--brand); color:var(--text); font-weight:600; }
.filter-chips { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.chip { display:inline-flex; align-items:center; gap:5px; height:30px; padding:0 12px;
        border-radius:999px; border:1px solid var(--glass-border);
        background:var(--chip-bg,var(--glass-soft)); color:var(--chip-fg,var(--text-2));
        font:inherit; font-size:12.5px; font-weight:500; white-space:nowrap; cursor:pointer;
        transition:background .14s, color .14s, transform .12s; }
.chip:hover  { background:var(--hover); color:var(--text); }
.chip:active { transform:scale(.97); }
.chip.is-active { background:var(--chip-active-bg,var(--brand-soft));
                  color:var(--chip-active-fg,var(--brand));
                  border-color:transparent; font-weight:600; }
.chip .icon { width:12px; height:12px; }
#view-bili { --chip-active-bg:var(--bili-soft); --chip-active-fg:var(--bili); }

.result-meta { display:flex; gap:8px; align-items:baseline; font-size:12px;
               color:var(--text-3); font-variant-numeric:tabular-nums; }
.result-list { display:flex; flex-direction:column; gap:2px; transition:opacity .16s; }
.result-list.is-stale { opacity:.4; pointer-events:none; }
.result-row  { display:grid; grid-template-columns:40px minmax(0,1fr) auto; gap:10px;
               align-items:center; padding:9px 12px 9px 14px; min-height:60px;
               border-radius:8px; position:relative; cursor:pointer;
               transition:background .16s; }
.result-row:hover { background:var(--hover); }
.result-row::before { content:''; position:absolute; left:4px; top:50%; transform:translateY(-50%);
                      width:3px; height:26px; border-radius:2px;
                      background:var(--brand); opacity:.55; transition:opacity .16s; }
.result-row:hover::before { opacity:1; }
.result-row[data-src="bili"]::before { background:var(--bili); }
.result-thumb { width:40px; height:40px; border-radius:var(--cover-radius);
                object-fit:cover; background:var(--sunken); display:block; }
.result-main .result-name-line { display:flex; align-items:center; gap:6px; min-width:0; }
.result-name { font-size:13.5px; font-weight:600; overflow:hidden; text-overflow:ellipsis;
               white-space:nowrap; }
.result-sub  { font-size:11.5px; color:var(--text-2); overflow:hidden;
               text-overflow:ellipsis; white-space:nowrap; }
.result-dur  { font-size:12px; color:var(--text-3); font-variant-numeric:tabular-nums;
               min-width:46px; text-align:right; }
.source-badge { display:inline-flex; align-items:center; height:18px; padding:0 6px;
                border-radius:5px; font-size:10.5px; font-weight:600; flex:0 0 auto; }
.source-badge.is-netease { background:var(--brand-soft); color:var(--brand); }
.source-badge.is-bili    { background:var(--bili-soft);  color:var(--bili); }

/* B站 */
.chip-row { display:flex; flex-wrap:wrap; gap:6px; }
.bili-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
             column-gap:var(--grid-gap); row-gap:18px; }
.video-card { display:flex; flex-direction:column; gap:8px; min-width:0;
              border:none; background:none; padding:0; cursor:pointer;
              text-align:left; color:var(--text); font:inherit; }
.video-card .vc-thumb { position:relative; aspect-ratio:16/9; border-radius:var(--cover-radius);
                        overflow:hidden; background:var(--sunken); }
.video-card .vc-thumb img { width:100%; height:100%; object-fit:cover; display:block;
                            transition:transform .28s cubic-bezier(.22,1,.36,1); }
.video-card:hover .vc-thumb img { transform:scale(1.035); }
.video-card .vc-dur { position:absolute; right:6px; bottom:6px; padding:1px 5px;
                      border-radius:5px; background:rgba(0,0,0,.62); color:#fff;
                      font-size:11px; font-variant-numeric:tabular-nums; }
.video-card .vc-title { font-size:13.5px; font-weight:600; line-height:1.35;
                        min-height:calc(2 * 1.35em); overflow:hidden;
                        display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
.video-card .vc-sub { display:flex; align-items:baseline; justify-content:space-between;
                      gap:8px; font-size:11.5px; color:var(--text-3); }
.video-card .vc-up { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.video-card .vc-plays { flex:0 0 auto; font-variant-numeric:tabular-nums; }

/* 状态 */
.skeleton, .skeleton-row { background:linear-gradient(90deg,var(--sunken) 0%,var(--hover) 50%,var(--sunken) 100%);
                           background-size:200% 100%; border-radius:var(--radius-xs);
                           animation:sk-sweep 1.6s linear infinite; }
.skeleton-row { height:60px; margin-bottom:2px; }
.skeleton-card { display:flex; flex-direction:column; gap:8px; }
.skeleton-card .sk-thumb { aspect-ratio:16/9; }
.skeleton-card .sk-line  { height:12px; }
.skeleton-card .sk-line.is-short { width:60%; }
@keyframes sk-sweep { from { background-position:200% 0 } to { background-position:-60% 0 } }

.state-block { display:flex; flex-direction:column; align-items:center; gap:8px;
               padding:48px 16px; text-align:center; color:var(--text-3); }
.state-block .state-icon { width:40px; height:40px; display:grid; place-items:center; }
.state-block .state-icon .icon { width:32px; height:32px; }
.state-block .state-title { font-size:15px; font-weight:600; color:var(--text); }
.state-block .state-hint  { font-size:12.5px; color:var(--text-2); max-width:34ch; }

.inline-error { display:flex; align-items:center; gap:8px; padding:8px 12px;
                border-radius:var(--radius-xs); background:rgba(214,69,69,.1);
                color:var(--danger); font-size:12px; }
.offline-bar { height:28px; display:flex; align-items:center; justify-content:center;
               background:rgba(217,154,31,.12); color:var(--warn); font-size:12px;
               padding:0 12px; flex:0 0 auto; }
.result-end { text-align:center; padding:14px 0; font-size:12px; color:var(--text-3); }

@media (prefers-reduced-motion: reduce) {
  .home-card, .home-card:hover, .video-card:hover .vc-thumb img,
  .result-row, .chip, .filter-select { transition-duration:.01ms; transform:none; }
  .skeleton, .skeleton-row { animation:none; background:var(--sunken); }
}
```

---

## 6. i18n key 清单

新增 3 个命名空间：`home`、`search`、`bili`；`tabs` 加 1 个 key。
`zh_cn.json` 与 `en_us.json` 必须同步（`tests/test_i18n.py` 会查 key 对齐与占位符一致）。

### 6.1 `tabs`

| key | 中文 | English |
|---|---|---|
| `tabs.home` | 首页 | Home |

### 6.2 `home`

| key | 中文 | English |
|---|---|---|
| `home.title` | 选择分区 | Choose a section |
| `home.subtitle` | 从一个入口开始 | Start from one place |
| `home.entry_netease` | 网易云 | NetEase |
| `home.entry_netease_desc` | 歌单、排行榜、每日推荐 | Playlists, charts, daily picks |
| `home.entry_bili` | B站 | Bilibili |
| `home.entry_bili_desc` | 热门榜与分类视频 | Trending and category videos |
| `home.entry_search` | 聚合搜索 | Combined search |
| `home.entry_search_desc` | 一次搜索，两个来源 | One query, both sources |
| `home.entry_settings` | 设置 | Settings |
| `home.entry_settings_desc` | 播放、下载、外观 | Playback, download, appearance |
| `home.count_playlists` | 我的歌单 {count} | {count} playlists |
| `home.count_trending` | 热门 {count} | {count} trending |
| `home.count_sources` | 2 个来源 | 2 sources |
| `home.count_signed_out` | 未登录 | Not signed in |
| `home.back_home` | 返回首页 | Back to home |
| `home.partial_unavailable` | 部分数据不可用 | Some data unavailable |
| `home.offline` | 当前离线 | You are offline |

### 6.3 `search`

| key | 中文 | English |
|---|---|---|
| `search.title` | 聚合搜索 | Combined search |
| `search.placeholder` | 搜索歌曲、歌单、视频 | Search songs, playlists, videos |
| `search.btn` | 搜索 | Search |
| `search.loading` | 正在搜索 | Searching |
| `search.filter_source` | 来源 | Source |
| `search.filter_category` | 分类 | Category |
| `search.filter_sort` | 排序 | Sort |
| `search.filter_duration` | 时长 | Duration |
| `search.source_all` | 全部 | All |
| `search.source_netease` | 网易云 | NetEase |
| `search.source_bili` | B站 | Bilibili |
| `search.category_all` | 全部 | All |
| `search.category_song` | 歌曲 | Songs |
| `search.category_playlist` | 歌单 | Playlists |
| `search.category_video` | 视频 | Videos |
| `search.sort_relevance` | 相关度 | Relevance |
| `search.sort_plays` | 播放量 | Plays |
| `search.sort_newest` | 最新 | Newest |
| `search.sort_duration` | 时长 | Duration |
| `search.duration_all` | 全部时长 | Any length |
| `search.duration_short` | 1 分钟以内 | Under 1 min |
| `search.duration_mid` | 1-5 分钟 | 1-5 min |
| `search.duration_long` | 5-15 分钟 | 5-15 min |
| `search.duration_xlong` | 15 分钟以上 | Over 15 min |
| `search.result_count` | 找到 {count} 条 | {count} results |
| `search.result_split` | 网易云 {netease} · B站 {bili} | NetEase {netease} · Bilibili {bili} |
| `search.result_partial` | {ok} · {failed} 未返回 | {ok} · {failed} failed |
| `search.active_filters` | 已筛选 | Filtered |
| `search.clear_filters` | 清除筛选 | Clear filters |
| `search.empty_title` | 没有找到结果 | No results |
| `search.empty_hint` | 换个关键词，或把来源切到「全部」 | Try another keyword, or switch the source to All |
| `search.error_title` | 搜索失败 | Search failed |
| `search.error_source` | {source} 搜索失败：{message} | {source} search failed: {message} |
| `search.load_more` | 加载更多 | Load more |
| `search.no_more` | 没有更多结果了 | No more results |
| `search.cap_hint` | 结果过多，用筛选缩小范围 | Too many results, narrow the filters |
| `search.offline_title` | 当前离线 | You are offline |
| `search.offline_hint` | 联网后再搜索 | Search again once you are back online |
| `search.sub_bili` | {uploader} · {plays} 播放 | {uploader} · {plays} plays |
| `search.sub_netease` | {artist} · {album} | {artist} · {album} |

### 6.4 `bili`

| key | 中文 | English |
|---|---|---|
| `bili.title` | B站 | Bilibili |
| `bili.chip_trending` | 热门 | Trending |
| `bili.chip_all` | 全部 | All |
| `bili.chip_music` | 音乐 | Music |
| `bili.chip_dance` | 舞蹈 | Dance |
| `bili.chip_game` | 游戏 | Gaming |
| `bili.chip_knowledge` | 知识 | Knowledge |
| `bili.chip_tech` | 科技 | Tech |
| `bili.chip_life` | 生活 | Life |
| `bili.chip_film` | 影视 | Film |
| `bili.plays` | {count} 播放 | {count} plays |
| `bili.by` | UP主 {name} | by {name} |
| `bili.empty_title` | 这个分类今天没有内容 | Nothing in this category today |
| `bili.empty_hint` | 换一个分类，或稍后再来 | Try another category, or come back later |
| `bili.error_title` | 加载失败 | Could not load |
| `bili.error_hint` | {message} | {message} |
| `bili.load_more` | 加载更多 | Load more |
| `bili.no_more` | 没有更多了 | No more |
| `bili.refresh` | 刷新 | Refresh |
| `bili.offline_title` | 当前离线 | You are offline |
| `bili.offline_hint` | 联网后刷新 | Refresh once you are back online |

复用现有、不新增的文案：`common.retry` / `common.refresh` / `common.loading` / `common.close` / `error.network` / `player.load_more` 的位置已有 `#btn-list-more` 可作形态参考。

静态标记走 `data-i18n` / `data-i18n-placeholder` / `data-i18n-title`，与现有写法一致；动态生成的列表项由 JS 调 `t()`，不写死中文。

---

## 7. 实现要点（给 engineer 的排序）

1. `#view-home` + 顶部第三个 tab + `home.*` 文案 + 卡片点击路由。这一块自己就能跑，先落地。
2. `#view-bili`：只有网格和一个请求，比聚合搜索简单，先做能验证网格与封面的比例。
3. `#view-search`：分段 + 三个 select + 合并排序 + 降级错误。降级逻辑（2.5）不要省。
4. 最后统一补状态态（骨架/空/错/离线）与 `prefers-reduced-motion` 覆盖。

视图切换沿用现有 `data-view` 那套，不要新写一个路由。子页返回 = 切回 `#view-home` 并把焦点给 `is-current` 那张卡。

---

## 8. 需要拍板的点

1. **B站卡片的点击行为**：播视频（要后端解析播放地址）、跳浏览器、还是只展示列表？规格默认第三种（卡片可点但只高亮），因为这决定了要不要接 B站播放链路。
2. **分类与时长是本地过滤还是请求参数**：规格假设「来源」是请求参数，「分类/时长」在已加载结果上本地过滤（NetEase `cloudsearch` 没有时长参数，B站搜索也没有分类维度）。如果后端要按分类分别请求，筛选交互得改成「切换即重搜」。
3. **B站是否需要登录态**：规格按免登录设计（热门榜与分类公开可取）。要接收藏/关注的话，首页 B站卡的计数行和右侧动作都要加。
4. **聚合搜索的排序是否跨源可比**：「播放量」在网易云是歌曲播放数、B站是视频播放数，跨源混排没有真实可比性。规格按「同一排序键各源内排完再交替合并」处理。如果要求严格可比，得在 UI 上标出「排序仅在各来源内有效」。
5. **`--bili` 的对比度**：`#fb7299` 在浅色玻璃上约 3.4:1，规格把它限制在 10.5px 加粗标签和 3px 色条。如果设计要求它承载正文（比如 chip 选中文字 12.5px），需要换深一档的 `#e85a80` 或加粗到 700。
6. **首页是否记住上次分区**：规格没有做「自动跳回上次分区」，每次启动都停在分区页。要的话是在 `settings.yaml` 加一个 `last_section`，属于产品决策。
