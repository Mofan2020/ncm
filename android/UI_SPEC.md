# Android 视觉规格（Material 3 + 液态玻璃）

适用：`android/ncmkivy/`，Kivy 2.3.1（已完成 APK 的实际版本）。
配套：`android/ARCHITECTURE.md` 第 4 节（尺寸分档与硬性规则）。本文件是那份的视觉细化，冲突时以本文件的具体数值为准。

**这份规格只给数值和画法，不给实现代码。** 所有尺寸 dp()、字号 sp()、所有颜色十六进制或 rgba，所有圆角/阴影/描边都写明用哪条 canvas 指令。

---

## 0. 硬约束（Kivy 2.3.1 实测能力）

| 项 | 能用 | 不能用 | 后果 |
|---|---|---|---|
| 圆角 | `RoundedRectangle(radius=[...])`、`SmoothRectangle` | — | 半径必须 dp()，四角顺时针 TL/TR/BR/BL |
| 描边 | 外扩 1dp 的 `RoundedRectangle` 压在填充下面 | CSS 式 `border` | 见 §5.3 画法 |
| 阴影 | `BoxShadow(blur_radius=, offset=, spread_radius=, border_radius=)` | CSS `box-shadow` 列表 | 每条 BoxShadow 一个 FBO，同屏 ≤4 条 |
| 背景模糊 | `EffectWidget` + `HorizontalBlurEffect`/`VerticalBlurEffect`（只对子树生效） | `backdrop-filter` | 玻璃层**没有**真背景模糊，见 §5.2 |
| 渐变 | `Texture.create(size=(1,N))` + `blit_buffer()` 贴到 `Rectangle(texture=)` | CSS `linear-gradient` | 纹理必须在 Window 建立后创建 |
| 图标 | `Line(points=, width=, cap='round', joint='round')`、`Triangle`、`Ellipse` | 图标字体、SVG | `Line.width` 单位是**像素**，不是 dp |
| 字重 | `Label(bold=True)` 两档 | 100–900 九档 | 只有 regular / bold |
| 字距 | 无 | `letter-spacing` | 表里的 tracking 值只在拉丁文大标题上手动逐字排 |
| 行高 | `Label(line_height=倍数)` | — | 倍数 × 字号 |

依赖面：只用 `kivy` 本体。不引入 KivyMD、Pillow、iconfont。

---

## 1. 设计原则

1. **和桌面同一套语言，不是同一套布局。** 桌面是「玻璃面板浮在壁纸上」；手机没有壁纸，玻璃浮在**内容**上——所以手机的玻璃只用在三处会遮住内容的地方：迷你播放条、底部导航、底部抽屉。列表和正文永远是实色 surface，否则滚动时读不下去。
2. **玻璃靠三个可画的层叠出来**：半透明填充 + 1dp 顶部高光 + 柔和投影。不追求真模糊（Kivy 做不到低成本背面采样）。
3. **信息层级靠字号和色阶，不靠装饰。** 每屏最多三级文字（title / body / label），每级都有固定的 sp 与颜色 token，见 §3、§2.4。
4. **颜色向品牌收敛。** 桌面品牌色 `#EC4141` 在浅色底上只有 3.89:1，不够正文用；Android 的 M3 primary 取加深后的 `#C4302B`（白字 5.52:1），`#EC4141` 只作为 `brand` token 出现在大面积图形与选中高亮上。

---

## 2. 色彩 token

命名与 `theme.py` 的 `Palette` 字段对齐；带 ★ 的是本规格新增、`Palette` 目前没有的字段。

### 2.1 浅色

| token | 值 | 用途 |
|---|---|---|
| `primary` | `#C4302B` | 主按钮底、进度条、选中图标、链接文字 |
| `on_primary` | `#FFFFFF` | 主按钮文字 |
| `primary_container` | `#FFDAD6` | 主色浅底（选中、徽章底） |
| `on_primary_container` | `#410E0B` | 主色浅底上的文字 |
| `secondary` | `#775651` | 次要强调（少用） |
| `on_secondary` | `#FFFFFF` | |
| `secondary_container` | `#F5DDD9` | **导航选中胶囊底**、进度条轨道底 |
| `on_secondary_container` | `#2C1512` | 选中图标/文字 |
| ★ `tertiary` | `#00639B` | 信息类强调（下载中、扫码状态） |
| ★ `on_tertiary` | `#FFFFFF` | |
| ★ `tertiary_container` | `#CFE5FF` | 信息胶囊底 |
| ★ `on_tertiary_container` | `#001D33` | |
| `error` | `#8C1D18` | 错误文字、错误图标（**不用纯红**：和 primary 撞色） |
| `on_error` | `#FFFFFF` | |
| ★ `error_container` | `#FFDAD6` | 错误横幅底 |
| ★ `on_error_container` | `#410E0B` | |
| ★ `success` | `#0F7B4A` | 下载完成 |
| ★ `on_success` | `#FFFFFF` | |
| ★ `success_container` | `#A8F2C8` | |
| ★ `on_success_container` | `#002114` | |
| ★ `warning` | `#7A5900` | 需登录、离线 |
| ★ `on_warning` | `#FFFFFF` | |
| ★ `warning_container` | `#FFDF9B` | |
| ★ `on_warning_container` | `#261A00` | |
| `background` | `#FFF8F7` | 窗口底色（`Window.clearcolor`） |
| `on_background` | `#201A19` | |
| `surface` | `#FFF8F7` | 页面底色，与 background 同值 |
| `on_surface` | `#201A19` | 一级文字 |
| `surface_variant` | `#F5DED9` | 输入框底、未选中 chip |
| `on_surface_variant` | `#534341` | 二级文字、未选中图标 |
| ★ `text_tertiary` | `#6F5C58` | 三级文字（时间、计数、脚注）→ on_surface 5.98:1 |
| ★ `surface_container_lowest` | `#FFFFFF` | 全屏播放页卡片底 |
| ★ `surface_container_low` | `#FFF1EF` | 抽屉底（不透明档） |
| `surface_container` | `#FCEAE7` | 卡片底、列表分组底 |
| `surface_container_high` | `#F7E4E1` | 按下的行、空状态图标圈 |
| ★ `surface_container_highest` | `#F1DFDC` | 最高层容器（嵌套卡片的卡片） |
| ★ `surface_tint` | `rgba(196,48,43,0.05)` | 需要额外提亮的容器，叠在 container 上 |
| `outline` | `#857370` | 描边按钮边、分割线（强） |
| `outline_variant` | `#D8C2BE` | 列表分割线、输入框默认边（弱） |
| ★ `inverse_surface` | `#362F2E` | Snackbar/Toast 底 |
| ★ `inverse_on_surface` | `#FBEEEB` | Toast 文字 |
| ★ `inverse_primary` | `#FFB4AB` | |
| `scrim` | `#000000` @ 32% | 抽屉后面的遮罩 |
| ★ `brand` | `#EC4141` | 仅大面积图形、选中高光、封面占位渐变；**不当文字色** |

### 2.2 深色

| token | 值 | token | 值 |
|---|---|---|---|
| `primary` | `#FFB4AB` | `surface` | `#181211` |
| `on_primary` | `#690005` | `on_surface` | `#EDE0DE` |
| `primary_container` | `#93000A` | `surface_variant` | `#534341` |
| `on_primary_container` | `#FFDAD6` | `on_surface_variant` | `#D8C2BE` |
| `secondary` | `#E7BDB7` | ★ `text_tertiary` | `#A89A96`（6.82:1） |
| `on_secondary` | `#442925` | ★ `surface_container_lowest` | `#0E0908` |
| `secondary_container` | `#4E3A37` | ★ `surface_container_low` | `#201A19` |
| `on_secondary_container` | `#FFDAD5` | `surface_container` | `#251E1D` |
| ★ `tertiary` | `#99CCFF` | `surface_container_high` | `#302826` |
| ★ `on_tertiary` | `#003352` | ★ `surface_container_highest` | `#3B3331` |
| ★ `tertiary_container` | `#004A77` | ★ `surface_tint` | `rgba(255,180,171,0.05)` |
| ★ `on_tertiary_container` | `#CFE5FF` | `outline` | `#A08C89` |
| `error` | `#FFB4AB` | `outline_variant` | `#534341` |
| `on_error` | `#690005` | ★ `inverse_surface` | `#EDE0DE` |
| ★ `error_container` | `#93000A` | ★ `inverse_on_surface` | `#362F2E` |
| ★ `on_error_container` | `#FFDAD6` | ★ `inverse_primary` | `#C4302B` |
| ★ `success` | `#8AD5A5` | `background` | `#181211` |
| ★ `on_success` | `#00391D` | `on_background` | `#EDE0DE` |
| ★ `success_container` | `#00522C` | `scrim` | `#000000` @ 55% |
| ★ `on_success_container` | `#A8F2C8` | ★ `brand` | `#EC4141` |
| ★ `warning` | `#F5BF48` | | |
| ★ `on_warning` | `#412D00` | | |
| ★ `warning_container` | `#5E4200` | | |
| ★ `on_warning_container` | `#FFDF9B` | | |

### 2.3 表面色调 tint（surface container 四级的关系）

M3 的 container 层级 = surface + primary tint 递增。规格已把 tint 直接烘进上面的色值，实现时**不要**再叠一层，只有两种情况额外叠 `surface_tint`：

| 层级 | 浅色 | 深色 | tint 等效量 | 用在哪 |
|---|---|---|---|---|
| lowest | `#FFFFFF` | `#0E0908` | 0% | 抽屉内部卡片 |
| low | `#FFF1EF` | `#201A19` | 3% | 底部抽屉主体、全屏播放页底部信息区 |
| container | `#FCEAE7` | `#251E1D` | 5% | 常规卡片、空状态底、列表分组 |
| high | `#F7E4E1` | `#302826` | 8% | 按下的行、图标底衬圈 |
| highest | `#F1DFDC` | `#3B3331` | 11% | 最高层级容器（抽屉里的卡片） |

### 2.4 玻璃 token（半透明表面 + 描边 + 高光）

| token | 浅色 | 深色 | 用途 |
|---|---|---|---|
| ★ `glass_fill` | `rgba(255,255,255,0.72)` | `rgba(34,28,27,0.74)` | 迷你播放条、底部导航、玻璃 chip |
| ★ `glass_fill_strong` | `rgba(255,255,255,0.92)` | `rgba(34,28,27,0.94)` | 底部抽屉、浮层（要盖住下面所有内容） |
| ★ `glass_hairline` | `rgba(120,100,95,0.12)` | `rgba(255,255,255,0.10)` | 玻璃外沿 1dp 细边 |
| ★ `glass_highlight` | `rgba(255,255,255,0.92)` | `rgba(255,255,255,0.16)` | 顶部 1dp 高光线（玻璃的"受光边"） |
| ★ `glass_shadow` | `rgba(16,24,40,0.10)` | `rgba(0,0,0,0.45)` | 玻璃层投影 |
| ★ `glass_border` | `rgba(255,255,255,0.55)` | `rgba(255,255,255,0.14)` | 抽屉顶部抓手的底 |

规则：玻璃面上的文字一律用 `on_surface` / `on_surface_variant`，**不能**在玻璃面上用 `brand` 或 `primary` 小字（0.72 透明白底会把红字洗成对比 3:1 以下）。

### 2.5 背景层（玻璃必须有东西可透）

纯平底色上放半透明白 = 灰，玻璃感全无。窗口底色用两档垂直渐变，纹理画法见 §5.4：

| | 顶部 (y=1.0) | 底部 (y=0.0) | 说明 |
|---|---|---|---|
| 浅色 | `#FFEDE9` | `#FFF8F7` | 顶部带 primary 的 tint，往下收干净 |
| 深色 | `#241A18` | `#121010` | 顶部微亮，底部压暗 |

`Window.clearcolor` 用底部色兜底，渐变层只是盖在最上面的一个全屏 `Rectangle`。

### 2.6 对比度实测（WCAG 2.1，sRGB）

| 组合 | 浅色 | 深色 |
|---|---|---|
| `on_surface` / `surface` | 16.37 | 14.40 |
| `on_surface_variant` / `surface` | 8.91 | 10.92 |
| `text_tertiary` / `surface` | 5.98 | 6.82 |
| `on_surface_variant` / `surface_container_high` | 7.63 | 8.50 |
| `on_primary` / `primary` | 5.52 | 7.72 |
| `on_primary_container` / `primary_container` | 12.57 | 7.24 |
| `on_secondary_container` / `secondary_container` | 13.27 | 8.18 |
| `error` / `surface` | 8.69 | 10.91 |
| `success` / `surface` | 5.07 | 10.71 |
| `warning` / `surface` | 6.15 | 10.97 |
| `outline` / `surface`（非文字，≥3:1 即可） | 4.27 | 5.83 |
| `outline_variant` / `surface`（纯装饰分割线，豁免） | 1.62 | 1.98 |

全部正文级组合 ≥ 4.5:1。

---

## 3. 字体层级

字体：`NotoSansCJK`（`app.py` 已把系统 CJK 字体注册成 `Roboto`）。字重只有两档：`regular` / `bold=True`。M3 的 medium(500) 用 **bold + 比下一档小 0dp** 近似；不要用 500 的视觉预期去调，CJK 在 bold 下已经比拉丁文重。

行高用 `Label(line_height=倍数)`，倍数 = 行高 dp / 字号 sp。

| role | sp | 行高(dp) | line_height | 字重 | tracking | 用在哪 |
|---|---|---|---|---|---|---|
| `display_large` | 52 | 60 | 1.15 | bold | -0.5 | 全屏播放页备用大标题，暂不使用 |
| ★ `headline_large` | 32 | 40 | 1.25 | bold | -0.25 | 登录页主标题 |
| `headline_medium` | 26 | 34 | 1.30 | bold | -0.25 | 全屏播放页歌名 |
| ★ `headline_small` | 24 | 32 | 1.33 | bold | -0.25 | 页面主标题（"音乐库""搜索"） |
| `title_large` | 20 | 28 | 1.40 | regular | 0 | 抽屉标题、歌单详情页歌单名 |
| `title_medium` | 16 | 24 | 1.50 | bold | 0 | 列表行标题、迷你条标题、空状态标题 |
| ★ `title_small` | 14 | 20 | 1.43 | bold | 0 | 分组标题、chip 文字 |
| `body_large` | 16 | 24 | 1.50 | regular | 0 | 空状态说明、长段落 |
| `body_medium` | 14 | 20 | 1.43 | regular | 0 | 正文、输入框文字、按钮以外的说明 |
| `body_small` | 12 | 16 | 1.33 | regular | 0 | 列表行副标题、辅助信息 |
| ★ `label_large` | 14 | 20 | 1.43 | bold | 0 | 按钮文字 |
| ★ `label_medium` | 12 | 16 | 1.33 | bold | 0 | 底部导航标签、chip |
| `label_small` | 11 | 16 | 1.45 | bold | 0 | 时间戳、进度时间、极次信息 |

规则：

- 一个屏幕内 title 级不超过 2 种、body 级不超过 2 种。
- 正文最小 12sp；11sp 只允许出现在时间戳和计数。
- 任意文本块必须 `shorten=True, shorten_from='right', text_size=(width, None)`，**不设 height**（`widgets.py` 已有的注释是对的，别改）。
- 用户 fontscale 1.3 时，所有靠 `texture_size` 算高的地方自动变高；固定高度的容器见 §10。

---

## 4. 间距刻度

只有六档，全部 dp()：`4 / 8 / 12 / 16 / 24 / 32`。

| 值 | 用在哪 | 禁止用在哪 |
|---|---|---|
| 4 | 图标与文字同一行的微调、两行文本行内 gap、chip 上下内边距 | 区块之间 |
| 8 | 导航项内 icon 与 label、行内两个图标按钮之间、卡片内小分段 | 页面边距 |
| 12 | 列表行封面↔文本、迷你条元素间距、按钮组内间距 | |
| 16 | **页面左右边距（唯一值）**、卡片内边距、列表行左右内边距、迷你条左右内边距 | |
| 24 | 区块之间、抽屉内边距、全屏播放页主要纵向节奏、按钮上下的间隔 | |
| 32 | 大区块分隔（空状态图标↔文字、全屏播放页顶部↔封面）、抽屉顶栏↔内容 | 列表行内 |

派生常量：

- 页面内容区宽度 = `窗口宽 - 32`（左右各 16）。
- 列表分割线左缩进 = `16 + 48 + 12 = 76dp`（封面右沿 + 12）。
- 可点元素之间的最小间隔 8dp；相邻两个 48dp 触控目标中心距 ≥ 56dp。

---

## 5. 圆角与阴影

### 5.1 圆角刻度

| token | dp | 用在 |
|---|---|---|
| `radius_xs` | 4 | 进度条、骨架占位条、徽章 |
| `radius_s` | 8 | 列表行封面（48）、迷你条封面（44）、chip、Toast |
| `radius_m` | 12 | 输入框、搜索框、按下的行、内嵌卡片 |
| `radius_l` | 16 | 卡片、对话框、二维码白卡、抽屉内卡片 |
| `radius_xl` | 24 | 全屏播放页封面、**按钮（48 高 → 胶囊）**、导航选中胶囊（32 高的一半） |
| `radius_sheet` | 28 | 底部抽屉顶部两角（底部两角 0） |
| `radius_full` | 高/2 | 图标按钮的按下圆、圆形封面（歌手头像 80 → 40） |

`theme.py` 现在只有 `radius_s/m/l`，需要补 `radius_xs/xl/sheet/full`。

### 5.2 玻璃层画法（按顺序入 canvas）

一个玻璃面 = 4 条指令，自下而上：

1. `BoxShadow(pos, size, blur_radius=dp(8), offset=(0, -dp(2)), spread_radius=(0,0), border_radius=[r,r,r,r])` —— 投影
2. `Color(*rgba(glass_fill))` + `RoundedRectangle(pos, size, radius=[r,r,r,r])` —— 填充
3. `Color(*rgba(glass_hairline))` + 外扩 dp(1) 的 `RoundedRectangle`，**插在填充之前**（这样只在边上露出一圈）—— 外沿细边
4. `Color(*rgba(glass_highlight))` + `Line(points=[x+r, y+h, x+w-r, y+h], width=dp(1), cap='square')` —— 顶部受光边

不要再叠第二层半透明（浅色玻璃上叠浅色玻璃 = 可读性塌陷）。

### 5.3 描边画法（无 `border`）

外扩法：先画 `RoundedRectangle(pos=(x-1, y-1), size=(w+2, h+2), radius=[r+1,...])` 填描边色，再在上面画填充色、半径 r 的同尺寸矩形。1dp 边就这么来，不会出现 `Line` 拐角接缝。

### 5.4 渐变画法

`Texture.create(size=(1, 64), colorfmt='rgba')` + `blit_buffer(64×4 字节的 RGBA 序列)`，贴给全屏 `Rectangle(texture=, size=窗口)`，`mag_filter='linear'`。**窗口建立之后再创建**，mock/无 GL 环境下会段错误。

用途：§2.5 背景层、全屏播放页底部压暗罩、骨架屏 shimmer。

### 5.5 阴影三档

| 档 | blur_radius | offset | 用在哪 |
|---|---|---|---|
| `elev_1` | 8dp | (0, -2dp) | 迷你播放条、底部导航、玻璃 chip |
| `elev_2` | 24dp | (0, -6dp) | 底部抽屉、对话框、Toast |
| `elev_3` | 40dp | (0, -12dp) | 全屏播放页封面 |

列表行、卡片**不用阴影**，用 container 色阶区分层级。同屏 BoxShadow 实例 ≤4，列表里一律不要。

---

## 6. 组件规格

### 6.1 页面骨架

```
┌──────────────────────────────┐ ← 状态栏（系统，safe top）
│ TopAppBar 56dp               │  标题 24sp bold / 返回 48 / 右侧动作 48
├──────────────────────────────┤
│                              │
│ ScrollView 内容区            │  左右 16dp，底部留 16+safe
│                              │
├──────────────────────────────┤
│ MiniPlayer 64dp（玻璃，可选） │  仅在播放时出现
├──────────────────────────────┤
│ NavigationBar 64dp（玻璃）    │  5 项
└──────────────────────────────┘ ← 手势条 safe bottom
```

- TopAppBar：高 56dp，左内边距 16dp，标题 `headline_small`；有返回时左侧 `IconButton(back)` 48dp，标题起始 x = 16+48 = 64dp；右侧动作按钮 48dp，末位按钮右沿距屏幕边 8dp。底色透明（页面背景透上来），**不加分割线**——滚动时用 1dp `outline_variant` 渐显（`elev_1` 阴影 + 透明度动画，见 §7）。
- 内容区底部额外 16dp + `safe.bottom`，避免最后一行被迷你条压住。
- 迷你条与导航栏同时存在时，二者间距 0（视觉上一条连续的底部玻璃，中间一条 1dp `glass_hairline`）。

### 6.2 底部导航栏（NavigationBar）

现状问题：只有图标、无标签、无选中态、无分隔、64dp 里居中排。全部按下面改。

```
┌────────────────────────────────────────────────┐
│  ▁▁▁▁▁▁▁                                       │ ← 1dp glass_highlight（顶部受光边）
│  ╭──────╮                                      │
│  │  ⌂   │    ⌕      ▤      ↓      ●           │ ← 选中胶囊 64×32 r16
│  ╰──────╯                                      │
│  首页    搜索    曲库    下载    我的            │ ← 12sp label_medium
└────────────────────────────────────────────────┘
  64dp（内容不缩放时）；玻璃底
```

| 项 | 值 |
|---|---|
| 总高 | `dp(64)`；fontscale > 1.15 时按 `dp(6+32+2+16*fs+8)` 展开，最小 64 |
| 项宽 | `可用宽 / 5`（360dp 屏 = 72dp） |
| 导航项 | 纵向：top 6dp → 胶囊 32 → gap 2 → 标签 16 → bottom 8 |
| 图标 | 24dp，居中在胶囊里 |
| 选中胶囊 | 64×32dp，`radius_full`(16)，底 `secondary_container` |
| 选中图标 | `on_secondary_container` |
| 未选中图标 | `on_surface_variant` |
| 标签 | `label_medium` 12sp，选中 `on_surface`，未选中 `on_surface_variant` |
| 分隔 | 顶部 1dp `glass_highlight` + 整条 `elev_1` 阴影；**不要**再加实线 |
| 按压 | 整项 72×64 区域，按下时胶囊底换成 `surface_container_highest` @ 60% alpha，回弹见 §7 |
| 触控 | 每项 72×64 ≥ 48dp ✔ |
| 位移 | 胶囊横向移动、图标颜色交叉淡入，200ms |

标签文案固定：`首页 / 搜索 / 曲库 / 下载 / 我的`（对应 `SECTIONS` 的 home/search/library/downloads/me），`player.home` 等 i18n key 挂到现有 catalogue。带角标时（下载数 > 0）在"下载"图标右上角放 16dp 圆点，底 `error`，白字 `label_small` 9sp 居中——数字 > 99 显示 `99+`。

### 6.3 列表行（TrackRow / PlaylistRow）

```
 ←16→┌──────┐←12→┌────────────────────┐      ┌────┐←8→
     │ 48   │     │ 标题 16sp bold     │      │ ⋮  │
     │ r8   │     │ 副标题 12sp        │      │ 48 │
     └──────┘     └────────────────────┘      └────┘
     行高 64dp；分割线从 x=76 到右边缘 1dp
```

| 项 | 值 |
|---|---|
| 行高 | 64dp（封面 48 + 上下 8） |
| 左内边距 | 16dp；右内边距 8dp（给 48dp 动作按钮留位） |
| 封面 | 48×48，`radius_s`(8)，底 `surface_container_high` 占位 |
| 封面↔文本 | 12dp |
| 文本块 | 两行：标题 `title_medium` 16sp bold `on_surface`，单行省略；副标题 `body_small` 12sp `on_surface_variant`，单行省略 |
| 行内文本 gap | 2dp |
| 文本可用宽 | 360dp 屏 = 228dp（见 §10） |
| 右侧动作 | 48×48 触控，图标 24dp `on_surface_variant`；每行最多 1 个 |
| 分割线 | 1dp `outline_variant`，左起 76dp，右到屏幕边；最后一行不画 |
| 按压 | 整行填 `surface_container_high`，半径 12dp，左右各内缩 4dp；90ms 淡入 |
| 正在播放 | 标题色换 `primary`，封面右沿加 2dp 宽、24dp 高的 `primary` 竖条（距封面 4dp，圆角 1dp） |
| 无封面变体 | 行高 56dp，左内边距 16dp，图标位 24dp + 12dp gap |
| 下载状态 | 行尾 chip：高 24dp，`radius_s`，横向 padding 8dp，`label_small` 11sp；色对见 §6.10 |

**不要**在行里放下载进度条——进度统一进"下载"页，行内只给状态 chip。

### 6.4 按钮

三种变体 + 图标按钮。全部 48dp 高（触控 ≥48），文字 `label_large` 14sp bold。

| 变体 | 高 | 圆角 | 横向 padding | 底色 | 文字 | 最小宽 |
|---|---|---|---|---|---|---|
| Filled | 48 | 24 | 24 | `primary` | `on_primary` | 88 |
| Outlined | 48 | 24 | 24 | 透明 + 1dp `outline` 边 | `primary` | 88 |
| Text | 48 | 24 | 12 | 透明 | `primary` | 64 |
| Icon | 48×48 | 24（按压圆 24） | — | 透明 | 图标 24dp | 48 |

状态：

| 状态 | Filled | Outlined | Text | Icon |
|---|---|---|---|---|
| default | `primary` | 透明 / 边 `outline` | 透明 | 图标 `on_surface_variant` |
| pressed | 叠加 `on_primary` @ 12% | 叠加 `primary` @ 8% + 边 `primary` | 叠加 `primary` @ 8% | 圆底 `on_surface` @ 8% |
| disabled | 底 `on_surface` @ 12%，字 `on_surface` @ 38% | 边 `on_surface` @ 12%，字 @ 38% | 同左 | 图标 @ 38% |
| loading | 文字换成 16dp 环形指示器（`on_primary`，2dp 粗），按钮宽不变 | 同色环 `primary` | 同左 | 图标换环 |
| focused（外接键盘） | 2dp `primary` 外描边，offset 2dp | 同 | 同 | 同 |

按压反馈：`scale 0.96`，90ms `in_quad`；释放 140ms `out_quad` 回 1.0。坐标轴心 = 按钮中心。

宽度：单按钮 `width = min(328dp, 文本宽 + 48)`；并排两个按钮时各占 `(328-12)/2 = 158dp`，间隔 12dp。

### 6.5 迷你播放条（MiniPlayer）

```
┌──────────────────────────────────────────────────┐ ← 2dp 进度条（primary / track 12%）
│ ←12→┌────┐←12→┌──────────────┐  ┌───┐ ┌───┐ ←12→│
│      │44  │     │ 歌名 16b     │  │ ▶ │ │ ⏭ │      │
│      │r8  │     │ 歌手 12sp    │  │48 │ │48 │      │
│      └────┘     └──────────────┘  └───┘ └───┘      │
└──────────────────────────────────────────────────┘  高 64dp，玻璃
```

| 项 | 值 |
|---|---|
| 高 | 64dp（`show_track` 时为 64，无播放时 0，高度动画 200ms） |
| 底 | 玻璃 4 层画法（§5.2），`radius` 0（贴边） |
| 顶部进度 | 2dp 高，全宽；已完成 `primary`，未完成 `on_surface` @ 12%；**贴容器最顶边**，不要内缩 |
| 封面 | 44×44，`radius_s`(8) |
| 文本 | 标题 `title_medium`，副标题 `body_small` `on_surface_variant`；两行 gap 2dp；可用宽 = `360-12-44-12-48-4-48-12 = 180dp` |
| 播放按钮 | 48 触控，视觉 40 圆，底 `primary`，图标 `on_primary` 20dp |
| 下一首 | 48 触控，图标 24dp `on_surface_variant`；两按钮间 gap 4dp |
| 按压 | 图标按钮走 §6.4 Icon 态；整条空白区按压 = 展开全屏播放页 |
| 展开 | 点非按钮区域 → 全屏播放页，320ms（§7） |
| 状态 | 加载中：播放按钮内换成 16dp 环；暂停：图标 `play`；错误：副标题换 `error` 色，文案"播放失败，点此重试" |

### 6.6 全屏播放页

```
┌──────────────────────────────┐ ← safe top
│ ←48            队列 48       │ 56dp 顶栏，透明
│                              │
│      ╭────────────────╮      │ ← 封面 = min(w-48, 320) = 312dp @360
│      │                │      │   radius 24，elev_3
│      │     COVER      │      │
│      ╰────────────────╯      │
│                              │
│         歌名 26sp bold        │ 24dp 间距
│         歌手 14sp on_variant  │ 8dp
│                              │
│  0:42 ──────●──────── 3:58   │ 4dp 轨道 + 12dp 拇指；时间 11sp
│                              │
│   ⤮    ⏮    (▶)   ⏭    ⟲     │ 48/48/64/48/48，gap 12
│                              │
│   ♡    ↓    💬    ⋯          │ 48 ×4，gap 24
└──────────────────────────────┘
```

| 项 | 值 |
|---|---|
| 背景 | 封面铺满（`fit_mode='cover'`，放大 1.2 倍）→ 上面压两层：中间色遮罩 `surface` @ 58%（深色）/ 62%（浅色），再压 §5.4 的上下渐变（顶部 0%、底部 40% `background` 不透明） |
| 顶栏 | 56dp，返回 48 左 8dp，队列 48 右 8dp，无标题，无底色 |
| 封面 | 边长 `min(窗口宽 - 48, 320dp)`（360 屏 = 312dp），`radius_xl`(24)，`elev_3`，距顶栏 24dp |
| 歌名 | `headline_medium` 26sp bold，单行省略，左右 24dp |
| 歌手 | `body_medium` 14sp `on_surface_variant`，距歌名 8dp，单行省略 |
| 进度条区 | 距歌词块 24dp；左侧时间 40dp 宽右对齐，右侧 40dp 左对齐，`label_small` `text_tertiary`，等宽数字（Kivy 无法设 tabular-nums，用固定宽度容器对齐） |
| 轨道 | 4dp 高，`radius_xs`(4)；已完成 `primary`，未完成 `on_surface` @ 18%；可拖动区高 32dp（上下各扩 14dp 透明热区） |
| 拇指 | 12dp 圆，`primary`，仅拖动时显示（出现在轨道左端起点） |
| 控制行 | 5 个：随机 48 / 上一首 48 / **播放 64 圆 `primary`** / 下一首 48 / 循环 48；间距 12dp；整行宽 312 恰好占满 360-48 |
| 播放按钮图标 | 28dp `on_primary`；加载时换 20dp 环 |
| 次级行 | 喜欢 / 下载 / 评论 / 更多，各 48，间距 24dp，图标 `on_surface_variant`，已喜欢用 `primary` 实心 |
| 底部 | 24dp + `safe.bottom` |
| 进出 | 从迷你条位置竖向展开（320ms）；返回手势/返回键下滑收起，动画可打断（跟手） |

不做歌词（本版没有歌词页），中间那块留给封面与信息，从上到下由 24/24/8/24/24/24 的间距撑开，垂直居中对齐剩余空间。

### 6.7 空状态

现状问题：一行灰字飘在中间，没有图形、没有层级、没有操作入口。

```
        ╭────────╮
        │   ⌕    │   96dp 圆，surface_container_high，图标 32dp
        ╰────────╯
        还没有搜索内容        16sp bold on_surface，距图标 32dp
   换个关键词，或从歌单里找找   14sp on_surface_variant，距标题 8dp，居中，最多 2 行
        ╭──────────╮
        │  去搜索   │         Filled 按钮，距说明 24dp
        ╰──────────╯
```

| 项 | 值 |
|---|---|
| 整块 | 垂直居中于内容区，左右 32dp，`size_hint_y=None` + `minimum_height` |
| 图标圈 | 96dp 圆（`radius_full`），底 `surface_container_high`，图标 32dp，色 `on_surface_variant` @ 65% |
| 标题 | `title_medium` 16sp bold `on_surface`，居中，距图标 32dp |
| 说明 | `body_medium` 14sp `on_surface_variant`，居中，`max_lines=2`，距标题 8dp |
| 按钮 | 有操作时 Filled，宽 160dp，距说明 24dp；无操作时不放按钮 |
| 变体 | 未登录（图标 person + 说明"登录后同步歌单和喜欢的音乐" + 按钮"去登录"）、搜索无结果、歌单空、下载队列空、网络错误（图标用 `error` 色 40%） |

### 6.8 加载状态

| 场景 | 表现 |
|---|---|
| 首屏加载 | 环形指示器 40dp（`Line` 描圆弧，3dp 粗，`primary`，270° 缺口，900ms `linear` 无限旋转）+ 下方 16dp 处 `body_medium` `on_surface_variant` 文案，整块垂直居中 |
| 列表加载 | 骨架屏：3 行，每行 64dp，结构 = 48dp 圆角块（`surface_container_high`）+ 14dp 高标题条（60% 宽）+ 12dp 高副标题条（40% 宽）；条状 `radius_xs`；1200ms shimmer（透明度 1.0↔0.55，`linear` 循环） |
| 局部刷新 | 顶栏右侧 48dp 位置放 24dp 环，不遮挡标题 |
| 加载更多 | 列表尾部 64dp 高容器，居中 24dp 环 |
| 失败 | 换 `error` 色图标 + "重新加载" Text 按钮；**不要**只给一行红字 |

`kivy.uix.spinner.Spinner` 不是加载指示器（它是个下拉选择器），现在 `LoadingView` 用它当转圈是错的——规格要求换成自绘圆弧。

### 6.9 底部抽屉（设置、扫码登录、手机号登录）

`kivy.uix.popup.Popup` 的默认外观（居中白框 + 标题栏 + 关闭按钮）不符合本规格，改成从底部升起的抽屉。

```
┌──────────────────────────────┐ ← scrim 32%（浅）/55%（深）
│  ╭────────────────────────╮  │
│  │         ▁▁▁▁           │  │ 抓手 32×4 r2，距顶 8dp
│  │  扫码登录          ✕   │  │ 标题 20sp，✕ 48dp
│  │                        │  │
│  │      ┌──────────┐      │  │ 内容左右 24dp
│  │      │  QR 200  │      │  │ 白卡 200×200 r16
│  │      └──────────┘      │  │
│  │   请用网易云音乐 App 扫码 │  │ 14sp
│  ╰────────────────────────╯  │
│         ← safe bottom + 24   │
└──────────────────────────────┘
```

| 项 | 值 |
|---|---|
| 位置 | 贴底，宽 100%，最大高 88% 屏高；内容超出时内部 ScrollView |
| 圆角 | 上两角 `radius_sheet`(28)，下两角 0 |
| 底 | `glass_fill_strong`（0.92/0.94，几乎不透明，保证内容可读）+ `elev_2` |
| 抓手 | 32×4dp，`radius_xs`(2)，`glass_border`，水平居中，距顶 8dp |
| 顶栏 | 高 56dp：标题 `title_large` 20sp，左 24dp；关闭 `IconButton` 48dp，右沿 8dp |
| 内容 | 左右 24dp，顶栏下 8dp 开始，底部 `24 + safe.bottom` |
| 关闭 | 抓手下方整体可下滑关闭；点 scrim 关闭；返回键关闭；下滑 >30% 高度或速度 > 800dp/s 即关闭 |
| 升起 | 280ms `out_quart` `translateY 100%→0`；scrim 180ms `linear` 0→目标透明度 |
| 收起 | 220ms `in_quart` |
| 扫码内容 | QR 白卡 200×200 + `radius_l`(16) + 内白边 8dp（quiet zone）；下方 16dp 状态文字 `body_medium`（`tertiary` 表示等待、`success` 表示成功、`error` 表示过期）；过期时白卡上压 70% 白罩 + "二维码已过期" + Outlined"刷新"按钮 |
| 手机号登录 | 两个输入框（高 56dp，§6.11）+ 12dp gap + Filled"发送验证码" + 12dp + Filled"登录"；错误文案 `body_small` `error` 出现在输入框下方 8dp |

### 6.10 状态 chip / 徽章

| 语义 | 底 | 字 | 例子 |
|---|---|---|---|
| 进行中 | `primary_container` | `on_primary_container` | 下载中、解析中 |
| 完成 | `success_container` | `on_success_container` | 已完成 |
| 失败 | `error_container` | `on_error_container` | 失败 |
| 等待 | `surface_variant` | `on_surface_variant` | 排队中、跳过 |
| 信息 | `tertiary_container` | `on_tertiary_container` | 无损、SQ |

尺寸：高 24dp，`radius_s`(8)，横向 padding 8dp，`label_small` 11sp。**状态不能只靠颜色**：文字本身就是状态名，颜色是加速识别。

### 6.11 输入框 / 搜索框

| 项 | 值 |
|---|---|
| 高 | 56dp |
| 圆角 | `radius_m`(12) |
| 底 | `surface_variant`（浅）/`surface_container_low`（深） |
| 边 | 默认 1dp `outline_variant`；聚焦 2dp `primary` |
| 文字 | `body_large` 16sp `on_surface`，左内边距 16dp |
| placeholder | 同字号，`on_surface_variant` |
| 前置图标 | 24dp，距左 16dp，文字左移 16+24+12 = 52dp |
| 清除按钮 | 24dp，距右 12dp，触控 48 |
| 错误态 | 边 2dp `error`，下方 8dp `body_small` `error` 文案 |

### 6.12 Toast / Snackbar

高 48dp，`radius_s`(8)，底 `inverse_surface`，字 `inverse_on_surface` `body_medium`，左右 16dp；距底 = `迷你条高 + 导航高 + 16dp`；`elev_2`；进 160ms 淡入 + 上移 8dp，停留 2400ms，出 180ms。现在走的是系统 `Toast`，可以保留（系统 Toast 有原生无障碍支持），规格只约束自绘版。

---

## 7. 动效

Kivy 没有 cubic-bezier，用下列 `transition` 近似 M3 曲线：`out_quart` ≈ emphasized-decelerate，`in_quart` ≈ emphasized-accelerate，`in_out_cubic` ≈ standard，`linear`（进度、旋转、shimmer）。均通过 `Animation(...)` 实现，`Animation.cancel_all(widget)` 做打断。

| 转场 | 时长 | 曲线 | reduced motion |
|---|---|---|---|
| 底部导航切换 section | 内容 150ms 淡出 → 新内容 200ms 淡入 + 8dp 上移归位 | 淡出 `in_quad`，淡入 `out_quart` | 只保留 120ms 交叉淡入，无位移 |
| 导航选中胶囊移动 | 200ms | `out_quart` | 0ms 直接到位 |
| 按钮按下 | 90ms scale→0.96 | `in_quad` | 只改色，不改尺寸 |
| 按钮释放 | 140ms scale→1.0 | `out_quad` | 同上 |
| 列表行按压 | 90ms 背景淡入 | `linear` | 同 |
| 迷你条出现/收起 | 200ms 高度 0↔64 + 40ms 延迟淡入 | `out_quart` | 0ms 高度切换 + 100ms 淡入 |
| 迷你条 → 全屏播放页 | 320ms 竖向展开，封面同时从 44 放大到 312 | `out_quart` | 120ms 淡入，无展开 |
| 底部抽屉升起 | 280ms；scrim 180ms | `out_quart` / `linear` | 0ms 位移 + 120ms 淡入 |
| 底部抽屉收起 | 220ms | `in_quart` | 0ms |
| 封面加载完成 | 200ms opacity 0→1（占位块同时 150ms 淡出） | `linear` | 同（透明度动画保留） |
| 进度条推进 | 100ms 线性插值到新位置 | `linear` | 同 |
| play ↔ pause 图标 | 120ms 交叉淡入 | `linear` | 同 |
| Toast 进 / 出 | 160 / 180ms | `out_quad` / `in_quad` | 120ms 淡入淡出 |
| 骨架 shimmer | 1200ms 循环 | `linear` | 停止循环，静态 0.7 不透明度 |
| 主题切换（浅↔深） | 220ms 所有颜色 token 线性插值 | `linear` | 0ms 直接切 |
| 顶栏分隔线随滚动出现 | 120ms 透明度 | `linear` | 同 |

可打断性：

- 抽屉、全屏播放页、导航胶囊：可打断，新动画从**当前值**起步（读 `widget.y` / `widget.opacity` 再起 `Animation`，不要从目标值反推）。
- 抽屉下滑关闭必须跟手：拖动期间 `y = 起始y + 手势位移`，松手用 `project(velocity)` 判断去留，再交回 `Animation`，初速度传给动画。
- 进度条、shimmer、环形旋转不可打断（它们是状态指示，不是交互反馈）。

reduced motion 开关：读 Android `Settings.Global.ANIMATOR_DURATION_SCALE`，值为 0 时按上表"reduced motion"列执行，把全局时长系数设为 0，仅保留 ≤120ms 的透明度过渡。

---

## 8. 图标

### 8.1 网格与画笔

- 画布 24×24dp，四周留 2dp，**实际图形占 20×20**。
- 描边式图标：`Line(width=dp(2), cap='round', joint='round')`，端点落在半像素上避免发糊。
- `Line` 的 `width` 单位是**像素**：必须 `dp(2)`，写成 `width=2` 在 3x 屏上是 0.67dp，等于没画。现在 `widgets.py` 里 `arrow_down`、`cross`、`plus`、`check`、`arrows`、`chevron_left` 都是裸 `2` / `2.2` / `2.4`，全部要换 `dp()`。
- 图标尺寸只能跟 24dp 网格走，**不能**跟按钮尺寸走。现在 `_paint()` 里 `size = min(self.width, self.height)` 再乘 0.12~0.22，导致 48dp 按钮里画出 17dp 的图标、44dp 按钮里画出 16dp 的图标，视觉重量不一致。
- 端点触边时向内收 `dp(1)`（圆头会多探出半个线宽）。

### 8.2 逐个图标的修正

坐标以 24 网格左下角为原点（Kivy 坐标），y 向上。

| 图标 | 现状问题 | 规格坐标 |
|---|---|---|
| `play` | 三角偏心（`cx-0.6*half` 起步），视觉重心偏左 | 实心三角 `(8,4)-(8,20)-(20,12)`，闭合；填充 `on_primary` 或当前色 |
| `pause` | 两条 0.12 宽竖条、间距 0.1，太挤 | 两条 4×16 圆角条：`x=7..11` 与 `x=13..17`，`y=4..20`，各自 `radius 2` |
| `next` | 第二个三角形与第一个不共基线 | 三角 `(5,4)-(5,20)-(13,12)` + 竖条 `x=15.5..18.5`，`y=4..20` |
| `prev` | 镜像不自洽 | `prev` = `next` 水平镜像：竖条 `x=5.5..8.5`，三角 `(19,4)-(19,20)-(11,12)` |
| `more` | 横排三点，Material 的溢出是竖排 | 竖排 3 点，r=2，圆心 `(12,6)/(12,12)/(12,18)`——`more_vert` |
| `search` | 圆太小（r≈0.15*size）、柄太短 | 圆 r=6.5 圆心 `(10.5,13.5)`，柄 `(15.2,8.8)→(19.5,4.5)`，2dp，`cap='round'` |
| `home` | 屋顶三角 + 无门的矩形，24dp 下读成"向上箭头" | 屋顶 `(3,11)-(12,3.5)-(21,11)` 2dp 描边；屋身 `(5.5,11)-(5.5,20)-(18.5,20)-(18.5,11)`，描边，`radius 1.5`；门 `(10,20)-(10,15)-(14,15)-(14,20)` 2dp |
| `library`（stack） | 三个实心横条，像汉堡菜单 | 改成"叠层唱片"：底 `(4,4)-(20,4)` 2dp 横线；上两片 `(6,9)-(18,9)`、`(8,14)-(16,14)` 各 2dp；顶端圆点 r=1.5 在 `(12,19)` |
| `download` | 杆长与箭头宽不匹配 | 杆 `(12,20)-(12,9)`；箭头 `(7.5,12.5)-(12,8)-(16.5,12.5)`；底部托盘 `(5,4.5)-(19,4.5)` 2dp |
| `person` | 实心圆 + 实心方块，像图钉 | 头 r=3.5 圆心 `(12,16.5)` 描边；肩 `(5,4.5) 弧`——用 `Line(points=[5,5, 5,8, 19,8, 19,5], width=2, joint='round')` + 头圆 |
| `back` | 折线几乎水平（dx 只有 0.18）| `(15,5)-(9,12)-(15,19)`，2dp，`joint='round'` |
| `close` | 线太细 | `(6.5,6.5)-(17.5,17.5)` 与 `(6.5,17.5)-(17.5,6.5)`，2dp |
| `plus` | 线长不足 | `(12,4.5)-(12,19.5)`、`(4.5,12)-(19.5,12)`，2dp |
| `check` | 拐点偏 | `(5,12.5)-(10,17.5)-(19,7)`，2.5dp |
| `heart` | 两个椭圆 + 三角，24dp 下糊成一团 | 圆 r=4.2，圆心 `(8.2,14.6)` 与 `(15.8,14.6)`；底尖 `(4.2,13.5)-(19.8,13.5)-(12,3.4)`；实心 = 填充，空 = 2dp 描边 |
| `note` | 符头与符干比例失衡 | 符头 r=3 圆心 `(9,6)`；符干 `(12,6)-(12,19)` 2dp；符尾 `(12,19) 弧到 (17,17)` |
| ★ `queue` | 新增 | 三条 12dp 横线 `x=4..16`，y=18/13/8，2dp；右端 `▶` 小三角 |
| ★ `shuffle` | 新增 | 两条交叉 2dp 折线 + 右端两个 3dp 箭头，占 20×14 居中 |
| ★ `repeat` | 新增 | 20×14 圆角矩形描边 + 顶部一个箭头 |
| ★ `lyrics` | 新增 | 三条不等长横线（如 `8..20`/`6..16`/`10..18`）+ 左侧一个 4dp 竖线 |
| ★ `share` | 新增 | 竖杆 `(12,20)-(12,8)` + 上箭头 + 底部托盘 |

图标绘制入口要保持一个"按 kind 画 24 网格"的函数，`IconButton(size=48)` 只负责触控目标；视觉尺寸固定 `dp(24)`，`IconButton(size=40)` 时视觉仍 24（只有热区变小）。

---

## 9. 360dp 宽度预算（不能溢出）

| 组件 | 计算 | 结果 |
|---|---|---|
| 页面内容宽 | 360 − 16 − 16 | 328 |
| 列表行文本宽 | 360 − 16(左) − 48(封面) − 12(gap) − 48(动作) − 8(右) | **228** |
| 无封面行文本宽 | 360 − 16 − 24 − 12 − 48 − 8 | 252 |
| 迷你条文本宽 | 360 − 12 − 44 − 12 − 48 − 4 − 48 − 12 | **180** |
| 底部导航每项 | 360 / 5 | **72**（≥48 ✔） |
| 全屏封面边长 | min(360 − 48, 320) | **312** |
| 全屏控制行 | 48+12+48+12+64+12+48+12+48 | **312** = 可用宽 ✔ |
| 全屏次级行 | 48×4 + 24×3 | 264 ≤ 312 ✔ |
| 按钮单列最大宽 | 360 − 32 | 328 |
| 两个并排按钮 | (328 − 12) / 2 | 158 |
| 抽屉内容宽 | 360 − 24 − 24 | 312 |
| 空状态可用宽 | 360 − 32 − 32 | 296（说明最多 2 行，24 个汉字/行） |
| 导航标签（2 字，12sp） | 24dp | 居中于 72 ✔ |
| 导航标签（3 字，"音乐库"） | 36dp | 居中于 72 ✔ |

所有会超的文本一律 `shorten=True`；**任何**横向 `BoxLayout` 里不得同时放两个 `size_hint_x=None` 且宽度之和大于容器宽度的子项。

---

## 10. 无障碍与动态字体

- **对比度**：§2.6 全表 ≥4.5:1（文字），装饰线条豁免。玻璃面上的文字用 `on_surface`，不要用 `primary`（见 §2.4 规则）。
- **触控热区**：全部 ≥48×48dp。迷你条的播放/下一首现在是 44——违规格，改 48。图标视觉仍是 24dp。
- **动态字体**：全部字号 `sp()`，行高随字号；以下容器高度必须按 fontscale 展开而不是固定：
  - 底部导航：`dp(6+32+2+16*fs+8)`，fs = `Metrics.fontscale`
  - 列表行：`max(dp(64), dp(8+24*fs+2+16*fs+8))`；超过则文本行数上限仍为 1，用 `shorten`
  - 迷你条：`max(dp(64), dp(8+24*fs+2+16*fs+8))`
  - 按钮：`max(dp(48), dp(20+20*fs))`
  最大按 fontscale 1.3 验算：导航 6+32+2+20.8+8 = 68.8 → 69dp，不裁字。
- **TalkBack**：Kivy 的控件树不进入 Android 无障碍树（SDL2 只有一个 SurfaceView），自绘界面在系统层面是"一个图像"。可行的补偿只有两条，二选一交给用户拍板：
  1. 关键控件（播放/暂停/上一首/下一首）通过原生 MediaSession / 通知栏暴露，耳机与读屏用户可用；
  2. 不做，明确写进 README 的已知限制。
  规格里给每个 `IconButton` 都预留 `a11y_label` 属性（"播放""暂停""下一首""更多操作"），将来接任何桥都用得上。
- **不以颜色单独传达信息**：下载状态 chip 带文字（§6.10）；"正在播放"除颜色外加竖条标记（§6.3）；错误除红色外带图标与文案。
- **不做纯手势操作**：抽屉可下滑关闭，也必须有 ✕ 按钮；全屏播放页可下滑收起，也必须有返回按钮。

---

## 11. 状态矩阵

每个组件至少覆盖以下行；表内"√"表示该状态必须显式设计，不允许"没想就落到默认"。

| 组件 | default | pressed | selected | disabled | loading | empty | error |
|---|---|---|---|---|---|---|---|
| 底部导航项 | √ | √ | √ | — | — | — | —（角标另算） |
| 列表行 | √ | √ | √（正在播放） | √（版权不可播） | — | — | √（解析失败） |
| Filled 按钮 | √ | √ | — | √ | √ | — | — |
| Outlined 按钮 | √ | √ | √（已关注/已喜欢） | √ | √ | — | — |
| Icon 按钮 | √ | √ | √（喜欢/循环开） | √ | √ | — | — |
| 迷你播放条 | √（播放中） | √ | — | — | √（缓冲） | — | √（播放失败） |
| 全屏播放页 | √（播放中） | √（拖动进度） | — | — | √（缓冲） | —（无曲目） | √ |
| 空状态 | √ | — | — | — | — | √ | √（错误变体） |
| 列表容器 | √ | — | — | — | √（骨架） | √ | √ |
| 底部抽屉 | √ | √（拖动中） | — | — | √（二维码生成中） | — | √（二维码过期） |
| 输入框 | √ | — | √（聚焦） | √ | — | √（空提示） | √ |
| 状态 chip | √ | — | — | — | √（进行中） | — | √ |
| Toast | √ | — | — | — | — | — | √ |

---

## 12. 实现注意（会踩的坑）

1. **`Line(width=...)` 是像素**，所有描边必须 `dp()`。现有的裸 `2` 在 3x 屏上是 0.67dp。
2. **`RoundedRectangle.radius` 不能超过短边一半**，超过后 Kivy 画出来是圆变方，不报错。
3. **`Label` 有 `text_size` 才能 `halign`**，且不要同时设 `height`（会压成一条线，`widgets.py` 里已有注释说明，别推翻）。
4. **`AsyncImage` 加载完成前 `texture` 是占位纹理**，淡入要绑 `on_load` 而不是 `on_source`。
5. **`BoxShadow` 有 FBO 开销**，列表行里用会掉帧；只用在 §5.5 那几处。
6. **渐变纹理必须在 `Window` 之后创建**，否则无 GL 上下文时段错误（本机 mock 窗口实测崩过）。
7. **`Window.clearcolor` 只吃 RGBA 浮点**，用 `get_color_from_hex()` 转换；它不认 `rgba()` 字符串，token 表里的 `rgba(...)` 要拆成 4 个浮点。
8. **玻璃层 `opacity` 动画会连子控件一起淡**，迷你条收起动画要动 `height`，不要动 `opacity` 到 0 再改 height（会闪）。
9. **`ScrollView` 里的 `BoxLayout` 必须 `size_hint_y=None` + `minimum_height`**，否则列表不出滚动。
10. **抽屉关闭时 `Popup` 会立即 `dismiss`**，自定义抽屉不要用 `Popup`，用 `ModalView` 或 `Widget` 叠层 + 自己控制 `y` 动画。

---

## 13. 和现有代码的差距（给实现的清单）

| 文件 | 现状 | 规格要求 |
|---|---|---|
| `theme.py` | M3 baseline 紫（`#6750A4`），缺 tertiary / success / warning / container 高两级 / glass | 换成 §2 全表；`Palette` 补 ★ 字段 |
| `theme.py` | 只有 `radius_s/m/l`、`space_xs/s/m/l/xl` | 补 `radius_xs/xl/sheet/full`、`space_xl=32` 语义、`elev_1/2/3` 常量 |
| `theme.py` | 字号 8 档，缺 label_large/medium、headline_small/large、title_small | 按 §3 扩到 12 档 |
| `app.py` | 底部导航 64dp、纯图标、无选中态、spacing 4/padding(8,4) | §6.2：胶囊 + 标签 + 玻璃 |
| `app.py` | 无顶栏规范（每个 screen 自建 56dp header，标题 `title_large` 20sp） | 统一 `headline_small` 24sp，抽公共顶栏 |
| `widgets.py` `IconButton` | 图标随按钮尺寸缩放；线宽裸 px；`more` 横排三点 | §8：固定 24dp 网格 + `dp()` 线宽 + 竖排 more |
| `widgets.py` `CoverImage` | 占位圆角矩形 + 无加载淡入 | §6.3 + 200ms 淡入 |
| `widgets.py` `TrackRow` | 行高 64、无分割线、无按压态、无播放中标记 | §6.3 全量 |
| `widgets.py` `EmptyState` | 一行灰字 + 可选按钮 | §6.7 图标圈 + 三级文字 + 按钮 |
| `widgets.py` `LoadingView` | 用 `Spinner`（下拉框）当转圈 | §6.8 自绘圆弧 + 骨架屏 |
| `widgets.py` `_TextButton` | 40dp 高、圆角 20、宽度按字数估（CJK 估不准） | §6.4：48dp、`radius 24`、宽度用 `texture_size` |
| `uix.py` `PhoneLoginDialog` | `Popup` 居中白框 | §6.9 底部抽屉 |
| `uix.py` `QrCodeView` | QR 260 + 无白卡、无状态 | §6.9 200 白卡 + 状态文字 + 过期遮罩 |
| 无 | 迷你条、全屏播放页 | §6.5 / §6.6 从零建 |

---

## 14. 需要拍板

1. **主色**：把 Android 的 primary 从 M3 baseline 紫改成品牌红系（`#C4302B`）。不拍板就保留紫，但那样两端视觉不在一个品牌里。
2. **底部导航是否带标签**：本规格要求带（5 个图标里 `stack`/`arrow_down` 单独看有歧义）。若要极简无标签，导航降至 48dp 且必须补选中胶囊。
3. **无障碍**：Kivy 无法把控件树交给 TalkBack，要么接原生 MediaSession 做播放控制，要么写进已知限制。二选一。
