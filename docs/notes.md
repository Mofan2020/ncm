# 实施与验证笔记

记录本轮（2026-09-30）做的事、**实测数据**，以及"没做 / 做不到的事和原因"。
接口清单与开发约定见 [`../AGENTS.md`](../AGENTS.md)。

---

## 1. 起点：不是"没做完"，而是核心功能全坏

盘点时用真实请求验证了代码里在用的每一个端点：

| 代码里用的端点 | 实测结果 | 影响 |
|----------------|----------|------|
| `/api/song/url/v1` | **HTTP 404 接口未找到** | 每首歌都拿不到下载地址 → 所有下载必然失败 |
| `/api/login/status` | **404** | `is_logged_in` 恒为 False → 登录永远不可能成功 |
| `/api/sent/verificationcode` | **404** | 手机验证码永远发不出去 |
| `/api/login/cellphone` | 401 `无权限访问. ENC` | 需要加密参数，非加密调用一律被拒 |
| `/api/login/qrcode/unikey` | 200 ✅ | 唯一还能用的登录端点 |
| `/weapi/*` | **HTTP 200 + 空响应体**（连故意写坏的参数也一样） | 该通道在本机网络下被 CDN 黑洞，无法当回退 |

另外二维码登录的状态码语义写反了（真实语义 `800=过期 / 801=等待扫码 / 802=已扫码 /
803=成功`，代码里按 `800=等待 / 803=过期` 处理），所以即使端点可用也登不进去。

`cryptography` 一直在依赖里却从未被 import —— 说明加密层当初压根没写。

---

## 2. 换用的可用通道（全部实测通过）

请求走官方桌面端同款 **eapi**（AES-ECB + MD5 摘要签名，实现见 `src/core/crypto.py`）：

* 播放地址 `eapi /api/song/enhance/player/url/v1` → 免费曲目返回 200 + 真实 URL（128kbps/320kbps/FLAC 均可）
* 歌单详情 `eapi /api/v6/playlist/detail` → 热歌榜 200 首完整返回
* 曲目详情 `eapi /api/v3/song/detail` → 用于补齐 `trackCount > len(tracks)` 的大歌单
* 账号状态 `/api/w/nuser/account/get` → 未登录 `account: null`，登录后返回 account + profile
* 二维码登录 `/api/login/qrcode/unikey` + `client/login` → 返回 `801 等待扫码`
* 验证码 `/api/sms/captcha/sent`（参数名是 **`cellphone`**）、登录 `/api/w/login/cellphone`
* 退出登录 `/api/logout` → 200

踩坑记录：

* eapi 的签名摘要对**参数顺序敏感**（服务端会按自己的结构体顺序重算），因此
  `build_client_header()` 生成的 `header` 必须排在常规参数之后，且每个请求要用新的
  `requestId`，复用同一 requestId 会被拒。
* 请求发太快会返回 `code 400`（看起来像签名错误），必须限速 + 退避重试；
  `NeteaseAPI.MIN_REQUEST_INTERVAL` / `MAX_RETRIES` 负责这件事。

---

## 3. 本轮实测数据（真机、真下载）

| 项目 | 结果 |
|------|------|
| 歌单 `3778678`（热歌榜） | 200 首，名称/创建者/曲数正确 |
| 歌曲地址解析 | 免费曲目返回真实 URL；`fee>0` 且需登录的返回 `-110` → 归类为 `copyright_unavailable` |
| 单文件下载 | 4.1 MB，实测 **5.34 MiB/s** |
| 顺序下载 1 并发 | 4 首 / 12.8 MB / 2.06 s（≈6.2 MiB/s 合计） |
| 并列 3 并发 | 4 首 / 12.8 MB / 2.49 s → **0.83×（没有提速）** |
| 跳过已存在 | 第二次运行 4 首全部 `skipped`，未重复下载 |
| 打包产物 | `NeteaseMusicDownloader.app` 50 MB / arm64 / bundle id `com.skyc8266.neteasemusicdownloader` |
| 打包后自检 | 9 项（资源、翻译、eapi 签名、下载器、设置、桥接、pywebview、qrcode）**全部 PASS** |
| 单元测试 | 131 passed / 1 skipped（真实接口用例需 `NCM_LIVE=1`） |
| ruff | All checks passed |

关于并发：**并发不等于更快**。实测这台机器的链路总带宽已经被占满，1 个和 3 个并行下载的
总吞吐一样（甚至略低）。并发选项是按需求保留的（用户可选 1–3），它真正有用的场景是
单文件被服务端限速、或大量小文件时摊薄握手开销；UI 上不做"提速"承诺。

---

## 4. 本次改动清单

* `src/core/crypto.py`（新）：eapi 签名（AES-ECB + MD5 + 新 requestId/次）
* `src/core/api.py`（重写）：可用端点 + legacy 回退 + 限速/退避/域名轮换 + 质量逐级降级
  + 歌单 TTL 缓存；`get_song_url_info()` 返回 `type/size/br/level`（扩展名不再靠猜）
* `src/auth/login.py`（重写）：修正 QR 状态码语义、`cellphone` 参数、`w/login/cellphone`、
  账号状态缓存（不再每次调用都发网络请求）、cookie 导入/退出
* `src/core/downloader.py`（重写）：URL 解析移入工作线程、并发 1–3、`.part` + 原子改名、
  字节数校验、暂停/继续/取消、失败清单、每任务独立统计
* `src/gui/bridge.py`（重写）：补齐 `set_theme` / `reset_settings` / `get_system_theme` 等，
  选中集合由前端显式传入（此前 JS 与 Python 的选择状态不同步，导致"点了开始没反应"）
* `src/gui/theme.py`（新）：深浅色同步到原生窗口（macOS `NSAppearance`、Windows 深色标题栏）
* `src/resources.py`（新）：打包后资源定位（此前用 `__file__`，冻结后必然找不到 HTML）
* `src/version.py`（新）：版本/名称/包名唯一来源（原 spec 里写死版本号）
* `main.py`：去掉 Windows 强制 `gui='cef'`（未打包 cefpython3 会直接启动失败）、
  窗口尺寸按屏幕裁剪并居中、按主题设置窗口底色、URL 带 `theme/mode/lang` 消除首帧闪白、
  新增 `--self-test` / `--report` 供 CI 冒烟
* 前端：**i18n 全覆盖**（151 个键，含 `data-i18n*` 与全部 JS 文案）、进度按 `song_id` 映射、
  响应式布局（780×560 起）、深浅色 + 跟随系统（含原生标题栏）、主题快捷切换按钮、
  失败原因本地化、自绘窗口按钮移除（改用系统原生标题栏）
* `main.spec`：图标、bundle id、版本、`argv_emulation=False`、macOS 关闭 UPX、
  只打包目标平台的 GUI 后端、排除 Qt/GTK/cef
* `.github/workflows/build.yml`：macOS 双架构（arm64=`macos-15`，x64=`macos-15-intel`；
  原先的 `architecture` 参数在 macOS 上无效，实际产出两个 arm64 包）、新增 test job、
  DMG 改用 `hdiutil`（不再 `|| true` 吞错）、产物大小校验、打包产物跑 `--self-test`、
  Release 用 gh CLI 并校验资产齐全
* 仓库规范：`LICENSE`（MIT）、`pyproject.toml`（ruff + pytest 配置）、
  `requirements-dev.txt`、`.gitignore`、应用图标（`scripts/make_icon.py`）、
  README 重写、AGENTS.md 重写、旧 CLI 移入 `legacy/`

---

## 5. 没做 / 做不到的，以及原因

1. **登录后的非公开歌单与会员歌曲，没有用真实账号验证。**
   接口、cookie 复用、`/api/w/nuser/account/get` 状态确认都已实现，但需要真实账号
   （扫码或 MUSIC_U cookie）才能端到端验证。用户自行测试；QR/手机/Cookie 三条链路都
   有本地单测覆盖（除真实网络交互）。
2. **macOS x64（Intel）产物只有 CI 能验证。** 本地是 Apple M2，只能构建 arm64。
3. **Windows 的深色标题栏无法在本地验证。** 代码是 best-effort（属性 20 → 19 逐级尝试，
   全部失败只记日志不报错），CI 只跑打包产物的 `--self-test`，不启动 GUI。
4. **没有代码签名/公证。** 没有 Apple 开发者账号，也未购买 Windows 代码签名证书：
   macOS 首次打开需右键 → 打开（或 `xattr -dr com.apple.quarantine`），
   Windows SmartScreen 会提示"未知发布者"。这是费用/账号问题，不是代码问题。
5. **没有实现 weapi 回退。** 本机网络下 weapi 一律返回空响应（连坏参数也一样），
   写成回退只会在排障时误导人。若换到 weapi 可用的网络，可以按 `docs/notes.md` 第 1 节
   的结论自行加回。
6. **没有断点续传（HTTP Range）。** 播放地址是短时效签名 URL，续传要先重新取地址，
   收益有限；当前策略是"失败即重试整首"。如需，可在 `_download()` 里加 Range + 校验。
7. **专辑封面仍未落盘**（v2.2.0 起歌词已落盘，见第 8 节）。封面只在界面显示，不写文件。
8. **Linux 不支持。** pywebview 在 Linux 需要 GTK/Qt 依赖，与"Windows + macOS"的范围不符。
9. **并发数上限固定 3。** 这是需求明确要求的范围，刻意没做更高并发（风控 + 收益低）。

---

## 6. 复核方式（不需要图形界面）

```bash
ruff check .                                  # 静态检查
pytest -q                                     # 131 个单元测试，不联网
NCM_LIVE=1 pytest tests/test_live_api.py -q    # 真实接口/真实下载冒烟
python3 main.py --self-test                    # 资源与依赖自检
pyinstaller --clean --noconfirm main.spec      # 打包
dist/NeteaseMusicDownloader.app/Contents/MacOS/NeteaseMusicDownloader --self-test
```

## 7. CI 首轮失败与修复（首次推送后由"打包产物自检"抓出）

首轮 CI（run `36704638395`）lint + 单测通过，两个平台构建失败 —— 都是真问题：

1. **Windows：`UnicodeEncodeError: 'charmap' codec can't encode characters`**
   应用名是中文，而 windowed 打包产物的 stdout 是 cp1252/cp936 控制台，
   自检在打印报告时直接抛异常。
   → 修复：`_self_test()` 先把 `sys.stdout/stderr` 重设为 UTF-8（`errors="replace"`）。
   同时把 PowerShell 冒烟改成 `Start-Process -Wait -PassThru` 取 `ExitCode`：
   GUI 子系统的 .exe 不会阻塞 PowerShell 也不设置 `$LASTEXITCODE`，用 `& $exe` 会误判失败。

2. **macOS x64：`ImportError: Symbol not found: _SSL_get0_group_name`**
   `src/core/crypto.py` 依赖 `cryptography`，其原生扩展在 Intel runner 上加载到了
   被打包进来的**版本不匹配的 `libssl.3.dylib`**（arm64 恰好自洽，所以只在 x64 暴露）。
   → 修复：eapi 只用到 AES-128-ECB，改为**纯 Python 实现** `src/core/aes.py`，
   从 `requirements.txt` 移除 `cryptography`，并在 `main.spec` 的 `excludes` 里排除它
   （pywebview 仅在其可选 `ssl=True` 服务器路径里才 import 它，本项目不使用）。
   顺带把 bundle 从 50 MB 降到 **38 MB**，并彻底消除各平台 OpenSSL 错配的可能。

正确性保证：`tests/test_aes.py` 同时验证 FIPS-197 官方向量（AES-128：
`000102…0f` / `001122…ff` → `69c4e0d86a7b0430d8cdb78070b4c55a`）**与**
对同一批输入逐字节比对 `cryptography` 的输出，两者完全一致；真实接口冒烟
（`NCM_LIVE=1 pytest tests/test_live_api.py`）在换用纯 Python AES 后依然全绿。

---

## 8. 歌词下载（v2.2.0 新增）

### 接口选择（2026-09-30 实测）

| 端点 | 实测结果 |
|------|----------|
| `eapi /api/song/lyric`（POST，`id`/`lv=-1`/`kv=-1`/`tv=-1`） | **code 200**，热歌榜 40/40 首都有 `lrc.lyric` ✅ 采用 |
| `eapi /api/song/lyric/v1` | code 400，不可用 |
| `GET /api/song/lyric`（legacy） | 已死（该网络下 legacy 只剩登录流程的几条） |

响应里 `tlyric`（翻译）字段存在但中文歌为空；`klyric`/`yrc`（逐字歌词）为空，
且**没有** `romalrc`（罗马音）字段，所以这三类不做（见第 5 节未做清单）。

### 行为

* 音频落地后在**同目录**写 `<歌手 - 歌名>.lrc`，同样走 `.part` + 原子改名。
* 翻译按**时间戳就地合并**：原句一行、翻译一行、同一个时间戳（绝大多数播放器都会两行都显示）。
  多时间戳行（`[00:10][01:20]副歌`）的翻译会在每个时间戳下都出现。
* 已存在的 `.lrc` 默认不动（`lyrics_status=exists`）；打开「覆盖已存在文件」才重写。
* 已下载过音频的歌再跑一次会**补歌词**（音频跳过的分支也会取歌词）。
* 取不到歌词、或歌词请求报错，**绝不**影响歌曲本身的成功状态，只记在任务上
  （`lyrics_status = none | failed`，附 `lyrics_error`）。
* 统计里单列 `lyrics_saved` / `lyrics_missing`，界面在下载完成后提示。

### 真机验证（真网络、真文件）

* 下载「芮恩 - 讨厌」：音频 4,126,555 字节 + `.lrc` 2,407 字节 / 79 行，含真实时间戳；
  关闭歌词时同批任务不产生任何 `.lrc`；清掉 `.lrc` 只留音频再跑，音频 `skipped` 且歌词补回。
* 翻译合并用真实外文歌验证通过，例如 Michael Jackson《Whatever Happens》：
  `[00:20.730]He gives another smile tries to understand her side`
  紧接 `[00:20.730]他再次微笑，试图站在她的角度去理解她`。
* 单元测试 `tests/test_lyrics.py`（解析/合并/原子写/边界）、`tests/test_api_lyrics.py`
  （端点与回退）、`tests/test_downloader.py`（落盘、回填、失败不影响歌曲、统计）全覆盖；
  `NCM_LIVE=1` 的真实用例包含歌词取回与「下载即带歌词」。

### 代价与取舍

* 每首歌多一次 API 请求（受 `MIN_REQUEST_INTERVAL=0.3s` 限速），200 首歌大约多花 1 分钟
  的接口时间，和下载并行进行。不想要可以在界面关掉。
* 不做逐字（yrc/klyric）与罗马音：接口没给可用数据，硬做只能自己造轮子。
* 不往音频文件里写 ID3/FLAC 内嵌歌词：那会改动音频字节与校验（见第 5 节）。

---

## 9. 登录问题排查（v2.2.1）

用户报告：① 发短信验证码提示"频繁"不能发；② 二维码扫码并授权后一直显示
"已扫码，请在手机上授权"，不推进到成功。

### 实测（2026-09-30，大陆网络）

| 探测 | 结果 |
|------|------|
| `GET /api/login/qrcode/unikey?type=1` | `{"code":200,"unikey":"..."}`，每次都是新的（无缓存） |
| `GET /api/login/qrcode/client/login?key=&type=1` | `{"code":801,"message":"等待扫码"}`；**响应头 `cache-control: no-cache, no-store`**，CDN 不缓存 |
| 轮询换成 `unikey=` / 去掉 `type` | `400 参数错误` → 现有参数名与组合是正确的 |
| **`POST /eapi/api/login/qrcode/client/login`（eapi 签名）** | 同样返回 `{"code":801,...}` ✅ **可作第二条通道** |
| `/weapi/...` | HTTP 200 + 空 body —— 确认被 CDN 黑洞，官方加密通道在本网络不可用 |
| `POST /api/sms/captcha/sent`（**非法号码**） | `{"code":200,"data":true}` —— 该接口对任何号码都回"已受理"，所以 `code==200` 本身不能当作发送成功 |

### 找到并修掉的三个真问题

1. **登录成功也会被显示成"卡在已扫码"**（最可能的直接原因）：二维码返回 `803` + cookie
   后，我们立刻查 `/api/w/nuser/account/get` 校验；cookie 在服务端生效有几百毫秒延迟，
   校验失败 → 状态置为 `failed`，而**前端 `onLoginStatusChange` 没有 `failed` 分支**，
   文字就停在上一句"已扫码，请在手机上授权"。
   现在：校验失败会重试 3 次（间隔 0.8s）；前端补齐 `failed` 分支；
   未知状态码/服务端原文也会显示在二维码下方，不再静默。
2. **重复打开登录面板会重新申请 unikey**，把手机上正在确认的那次扫码作废。
   现在 `login_qrcode()` 对仍在等待（pending/scanned）的二维码**复用**，
   只有显式「刷新二维码」才申请新的（`force=True`）——有单测钉住"只发一次 unikey 请求"。
3. **验证码按钮失败后可以立刻再点**：每多一次请求就把网易云的风控锁得更久
   （这就是"频繁"越点越严重的原因）。
   现在：请求进行中禁用按钮；失败后也进入倒计时（普通失败 60s，被限流 180s）；
   `频繁` 会走专门的提示文案并建议改用扫码。

顺带修的：
* 轮询改为**双通道交替**（普通 `/api` ↔ eapi 签名），并支持 `code 200 + cookie`、
  嵌套 `data.code` 等多种成功形态；轮询间隔 1.5s（原来 2s）。
* 会话带上 PC 客户端身份 cookie（`os=pc` / `appver` / `channel` / `_ntes_nuid`），
  与真实客户端一致。
* 新增 `login-debug.log`（配置目录内，256KB 滚动，手机号打码）：记录 unikey、
  每次状态变化的状态码与原文、cookie 是否到手、校验结果 —— 用户遇到问题可以直接把这个
  文件发出来，不用再靠猜。

### 没验证到的部分（说清楚）

* **没有真机扫码验证过 803 全链路**：本地无法完成"手机扫一扫 + 确认"这一步，所以
  只验证到 801 与双通道可用、以及上述状态的单元测试。若仍卡住，`login-debug.log`
  会直接显示服务器在那一步给的原文（例如是否根本没出现 803）。
* **短信验证码无法端到端验证**：不拿真实号码发短信（会打扰陌生人）。已验证的是：
  接口可达、请求形状被接受、限流/失败的分类与倒计时逻辑（有单测）。
  网易云的限流是按号码（+IP）判定的，被锁后只能等待；本版能保证的是**不再越点越糟**。

## 10. 播放器（v3.0.0：下载 + 播放两个标签页）

### 范围

顶部拆成「下载 / 播放」两个标签页，下载侧保持原样。播放侧：账号歌单与「我喜欢的」、
本地目录、最近播放、在线搜索五个数据源；单曲循环 / 列表循环 / 随机；滚动歌词；
收藏；一键下载；播放次数；关机续播；自定义背景图。

### 关键设计决定

| 决定 | 原因 |
|------|------|
| 音频统一走 `127.0.0.1` 回环 HTTP 媒体服务（`src/core/mediaserver.py`） | 网易云 CDN 直链需要桌面端 `Referer`，WebKit 对 `file://` 和自定义请求头都不可控；本地文件走同一个出口还能白拿 Range 拖动。绑定 127.0.0.1 + 随机 token，只服务白名单根目录内的文件 |
| 标签解析自己写（`src/core/localmusic.py`） | mutagen 是 GPL-2.0，会污染 MIT 项目。自写 ID3v2 / FLAC Vorbis / MP4 atom / WAV / OGG，读不到就从文件名兜底 |
| 播放次数以本地 `library.json` 为准，云端上报可选（默认关闭） | `/api/scrobble`、`/api/v1/play/record`、`/api/radio/like` 都不可用（-460 / -2 / 404）；`/api/feedback/weblog` 可用，但默认不发，只在设置里打开且已登录时才发，失败不影响播放。设置里可一键清空本地次数 |
| 随机播放是均匀随机 | 用户明确要求「不要根据播放次数改变概率」。实现里不读播放次数，只避开「刚播过的那一首」 |
| 背景图复制到配置目录 | 原图被删/被移走之后壁纸不该失效 |

### 端点（2026-10-01 实测）

| 能力 | 端点 | 结果 |
|------|------|------|
| 搜索 | eapi `/api/cloudsearch/pc` | code 200，返回 `ar` / `al` / `dt` |
| 账号歌单 | eapi `/api/user/playlist` | code 200，每页 100 + `more` 翻页 |
| 收藏 / 取消 | eapi `/api/song/like` | 301（未登录）；登录后可用 |
| 我喜欢的 | eapi `/api/song/like/get` | 同上 |
| 播放上报 | eapi `/api/feedback/weblog` | code 200 |
| ~~`/api/radio/like`、`/api/scrobble`、`/api/v1/play/record`~~ | | -460 / 404 / -2，不用 |

### 实测数据（本机 macOS，真文件、真 HTTP）

| 项目 | 数值 |
|------|------|
| 进程常驻（含 pywebview 后端与媒体服务） | 约 46 MB |
| 索引 3000 个本地文件 | +13 MB，首次 0.27s，二次 0.07s |
| 每个本地文件的常驻开销 | 约 3 KB（曲目负载 + 标签缓存） |
| 一次下发给前端的本地分页 | 200 首 / 64 KB |
| 过滤一次 3300 首 | 约 3.5 ms |
| 60 MB 文件：取 1 MB Range | 16 ms；整曲流式读完只增加约 1 MB 常驻 |
| 封面缓存预算 | 12 MB（单图上限 4 MB） |
| 2000 首的续播存档 | 254 KB |
| 随机播放 | 6 首队列跑 24 万次，每首偏离均值 < 1%，与播放次数无关 |

### 本次改动清单

* 新增 `src/core/library.py`：播放次数、收藏、最近播放、续播存档，落 `library.json`
  （临时文件 + 原子改名，损坏文件降级不崩；收藏/最近/次数都有上限）
* 新增 `src/core/localmusic.py`：目录扫描 + 零依赖标签 / 内嵌歌词 / 封面解析，
  索引缓存到 `local-index.json`（按 mtime + size 判断是否需要重解析）
* 新增 `src/core/mediaserver.py`：回环媒体服务，Range、HEAD、封面、背景图、
  在线代理（带 Referer）、URL 缓存与封面缓存
* `src/core/api.py`：搜索、账号歌单、收藏、播放上报端点与 `get_song_url_info`（含降级）
* `src/core/lyrics.py`：`parse_lrc_timed` / `build_timed_lyrics`（多时间戳展开、翻译配对容差 800ms）
* `src/config/settings.py`：`PlaybackSettings`（优先级 / 音质 / 模式 / 音量 / 续播 /
  上报开关 / 歌词翻译 / 本地目录）与背景图存取
* `src/gui/bridge.py`：播放器侧全部接口（解析、歌词三来源、收藏、计数、续播、
  五个数据源、快捷下载、背景图、`reveal_path`）；网络调用统一经 `_safe()` 兜底
* `main.py`：启动/关闭媒体服务，`--self-test` 增加播放器检查项
* `src/gui/assets/`：`index.html` / `style.css` / `app.js` 重写，液态玻璃 + 壁纸
* `scripts/check-player-js.mjs`：随机播放的统计校验（node 运行，不进 pytest）
* 测试新增 5 个文件：`test_library` / `test_localmusic` / `test_mediaserver` /
  `test_lyrics_timed` / `test_player_bridge`，配套 `tests/audio_fixtures.py`
  逐字节合成 MP3(ID3v2) 与 FLAC

### 写的过程中发现并修掉的问题

* **扫描是 O(n²) 的**：`sidecar_lyrics_path()` 在找不到 `.lrc` 时会把所在目录整个列一遍，
  而它每个文件都会被调用一次。1500 个文件扫描要 12s，目录列表还被反复重建。
  改成按目录缓存（按 mtime 失效）的 `stem -> .lrc` 表之后，同样的 1500 个文件 0.12s，
  重扫 0.03s。
* **标签缓存从来没生效过**：`refresh()` 收尾时拿「按路径索引的缓存」去和
  「按曲目 key（`local:/path`）索引的曲目表」比对，于是每次扫描都把缓存清空。
  原本那版测试用 `Path.read_bytes` 计数，而解析器走的是 `open()`，所以测不出问题；
  现在改成替换 `read_metadata` 计数，重扫时调用次数必须是 0。
* **死锁**：在线 URL 解析里嵌套了两次 `with self._cache_lock:`（`threading.Lock` 不可重入），
  第一次解析在线歌曲就会把媒体服务线程挂死。测试里的超时抓到了这个。
* **播放次数上限会吃掉新曲目**：次数表按上限裁剪时把「刚播的那首」也裁掉了，
  于是每首歌每次都从 1 重新开始。现在裁剪一定保留刚播放的 key。
* **续播存档每 5 秒重写一遍整个队列**：加了变更检测，队列/序号/模式没变、位置只挪了
  不到 1 秒就直接返回，不落盘。
* **USLT 内嵌歌词**：描述字段（descriptor）之前被当成歌词的一部分带出来，
  显示成 `desc [00:01.00]...`。合成样本用的是规范布局，问题在测试夹具上，
  已按 `encoding + language + descriptor\0 + text` 修正。
* **翻译行会渲染出空行歌词**：翻译里对不上原文的时间轴会生成一条没有正文的行。
  现在这类孤行直接丢弃，不再显示成幽灵行。

### 没做 / 做不到的，以及原因

* **没有手测界面**：按约定只保证「功能理论上可用 + 自测通过」，点界面、真实播放、
  原生文件对话框、扫码登录这些都要用户自己跑一遍。
* **滚动歌词用的是 `scrollTop`**：不是逐帧动画，滚动平滑度依赖浏览器默认行为。
* **在线歌曲的歌词每次播放都会请求一次**：接口有节流，没有再做一层歌词缓存。
* **本地文件只做只读索引**：不改标签、不写回文件，也没有重命名/整理功能。
* **不支援 Live2D / 在线音源扩展**：与本次需求无关。
* **播放上报的语义**：网易云那边只接受 weblog，不是完整的听歌记录（没有播放完成度），
  所以「云端次数」和本地次数不会完全一致。
* **应用名与 bundle id 没改**：现在它不只是下载器，但改名会挪配置目录、让已装的版本失效，
  所以仓库名、产物名、`com.skyc8266.neteasemusicdownloader` 都保持原样。
* **`/api/song/like` 的返回没有再次校验**：只按 code 判定成功，没有回查一次收藏列表。

### 复核方式

```bash
ruff check .                                   # 静态检查
pytest -q                                      # 全部单测（不联网）
node scripts/check-player-js.mjs               # 随机播放的统计校验
python3 main.py --self-test                    # 打包前的资源/依赖自检
```

---

## 4. 播放页 Bug 修复 + 媒体缓存（2026-10-19）

### 改了什么

| 模块 | 改动 |
|---|---|
| `src/gui/assets/app.js` | `showView('player')` 改为无条件 `selectSource(player.sourceTab)`;`backToPlaylists` 清空 `#track-list` 与 `player.visible`;`mountVirtualTrackList` 在 `clientHeight<=0` 时 rAF 重试,并加 ResizeObserver;新增 `coverSrc`/`setCoverImg`/`collectArtworkUrls` 走封面缓存代理;album/artist 详情模板的封面 `<img>` 改为 `setCoverImg` |
| `src/core/cache.py`（新） | `MediaCache` 单例,`cache/audio/<song_id>/<quality>.<ext>` + `cache/cover/<url_hash>.<ext>`;按字节预算 LRU 驱逐;线程安全;`.part` 原子重命名 |
| `src/core/mediaserver.py` | `_online` 命中磁盘走本地 `_stream_file`;`_proxy` 仅在「非 Range + 200」时把字节 tee 到内存,响应结束后写磁盘;`_cover(url)` 路由:先查磁盘,未命中拉 CDN 写缓存再返回;`forget_online` 同时清磁盘 |
| `src/gui/bridge.py` | `resolve_cover_url(track)` 与 `cache_stats()` |
| `tests/test_cache.py`（新） | 14 个测试:基本存取、quality 隔离、LRU 驱逐、并发写入、path traversal sanitisation、singleton、etc. |
| `tests/conftest.py` | `_reset_stores` 增 reset MediaCache 单例 |
| `tests/test_frontend_assets.py` | `_JS_GLOBALS` 增 `ResizeObserver` |

### 实测数据

- `pytest tests/` → 341 passed, 6 skipped(327 之前 + 14 新增 cache 测试)
- `ruff check .` → All checks passed
- `node -c src/gui/assets/app.js` → 无错误

### Bug 根因

1. **首次进入播放页不刷新**:`showView('player')` 的旧条件 `!player.queue.length && !player.current` 在恢复 session 后会跳过 `selectSource`,所以 `#playlist-list` 永远是空的。`selectSource` 自身有幂等保护,无条件调一次最稳。
2. **退出歌单后歌曲残留在「收藏」**:`#playlist-list` 和 `#track-list` 是同一个 `.list-area` 内的两个 flex 子元素,`backToPlaylists` 只重渲染前者、漏清后者,导致两者并列出现,看上去像「歌单歌曲出现在收藏里」。
3. **13 首歌后剩余空白但占位**:virtual scroll 在 `container.clientHeight === 0`(容器未 layout)时只渲染 1 行 + 大量 placeholder;窗口/侧边栏变化后窗口大小不会重算。

### 没做 / 留着的

- 真实启动 app 看效果 — 按星空喵偏好,机器不实际跑界面测试,由他本人手测。
- 缓存的回收上限目前是 audio 2 GB + cover 256 MB(写死在 `cache.py`),UI 还没暴露调整入口。
- 锁屏/控制中心的 mediaSession artwork 同时给本地代理 URL 和原 URL 兜底;只在 `coverUrlCache` 命中代理时去掉原 URL。

---

## 5. Windows 启动即报错 + 桌面歌词缺失（2026-10-20）

### 现象

Windows 上一启动就是 WebView2 的提示页「文件可能已删除或者已移动」。Win10、Win11 两台机器同样报错。macOS 正常。

### 排查过程

先排除掉几个看似最像的:

| 猜测 | 结论 | 怎么排除的 |
|---|---|---|
| URL 里的 `?theme=…` 被 WebView2 拒绝 | **不是** | 在真实 Chromium 里加载 `file:///…index.html?theme=dark`,DOM 正常渲染 |
| `Path.as_uri()` 在 Windows 生成非法 URI | **不是** | 空格、中文用户名、8.3 短路径(`RUNNER~1`,CI 里就是这种)都生成合法 URI |
| 资源没打进包 | **不是** | CI 日志 `[PASS] ui assets: …\_MEI00000ec42\src\gui\assets` |
| pywebview 后端选错 | **不是** | `guilib.py` 在 Windows 只会选 winforms,是对的 |
| **onefile 临时目录(_MEIPASS)不可读** | **是** | 目录一旦消失,WebView2 立刻渲染那一句提示;已在浏览器里复现 |

macOS 没事是因为它是 onedir(`.app/Contents/Frameworks`),根本没有 `_MEI` 这回事——所以这类问题只可能出现在 Windows。

### 为什么 CI 一直是绿的

`--self-test` 只断言「文件在磁盘上」,而且**开窗前就 return 了**,从不验证浏览器能不能加载那个 URL。onefile 临时目录被清理/被安全软件隔离这类事只发生在真实机器上,CI 复现不了。

### 改了什么

| 模块 | 改动 |
|---|---|
| `src/resources.py` | 新增 `extracted_resources_dir()` / `ensure_resources()`。仅在 **frozen + win32** 时,把 zip 解到 exe 同级的 `_resources/`;其余情况全部走原来的 `bundle_root()` 逻辑,一行没动 |
| `main.spec` | Windows 分支把 `src/gui/assets` + `src/i18n` 打成 `ncm-resources.zip`;`datas` 改成**整目录**打包(顺手补上漏掉的桌面歌词) |
| `main.py` | 启动时 `ensure_resources()`;`--self-test` 增加 `resource unpack` 和 `ui url` 两项 |
| `tests/test_resources.py`(新) | 13 个测试:解包位置、URL 不含 `_MEI`、幂等、损坏修复、缺包降级、zip 路径穿越防护、以及「非 Windows 平台钩子必须为空」 |
| `tests/test_spec.py`(新) | 8 个测试:**真跑 spec 源码**并在 win32/darwin 下分别断言 datas 构造 |
| `.github/workflows/build.yml` | Windows 增加一步:断言 `dist\_resources\` 及各资源文件真的存在 |

解包是原子的:先解到 `_resources.new/` 再整体换入,中途崩溃不会留下半个目录;zip 里的绝对路径和 `..` 一律拒绝。

### 开发中被测试抓出来的三个真 bug

1. `ensure_resources()` 一开始从 `bundle_root()` 找 zip——但 `_resources` 一旦存在,`bundle_root()` 就指向它,于是永远找不到 zip,**残缺目录永远修不好**。改成固定从 `_MEIPASS` 读。
2. spec 里写成 `SPEC_DIR`,实际变量是 `_SPEC_DIR`——只在 Windows 分支执行,macOS 本地构建完全测不到。
3. spec 里 `import shutil as _shutil` 写在 `if` 块内,块外用会 `NameError`。两条都是 `test_spec.py` 跑真实 spec 源码时炸出来的。

### 实测数据

- `pytest -q` → 362 passed, 6 skipped
- `ruff check .` → All checks passed
- macOS `PyInstaller` 构建 → 成功;`--self-test` 全绿,新增两项显示 `resource unpack: not used on this build` / `ui url: scheme=file query=theme=dark&mode=dark&lang=zh_cn`
- macOS 打包前后对比:资源目录多了 `desktop-lyrics.html` / `desktop-lyrics.js`(之前完全没进包);`Contents/Frameworks/src/` 路径不变
- Windows 分支:用真实 spec 源码在 `sys.platform='win32'` 下执行,产出 8 个成员的 zip,与源码目录逐一比对一致
- 最终 URL 实测:`file:///…/app/_resources/src/gui/assets/index.html?theme=dark&mode=dark&lang=zh_cn`,在真实浏览器里加载出完整界面

### 没做 / 留着的

- **没有在真实 Windows 上跑过**。星空喵主要用 Mac,Win10/Win11 上没条件复测。CI 的 Windows runner 会跑到新的解包断言,但"用户机器上 Defender 是否还会拦"只能等他方便时手测一次。
- 这个改动的依据是"文件不在 `_MEIPASS` 里 WebView2 就报错"这个已在浏览器里复现的机制 + 你在两台机器上同样报错;**具体是哪个进程删的 `_MEI` 目录仍未定位**(候选:Defender 实时扫描、onefile 退出清理、其他安全软件)。
- `libssl.3.dylib` 仍在 macOS 包里(虽已 exclude `cryptography`)。这是**本次改动之前就有**的,CI 日志里的历史包同样有,没动。
- 桌面歌词在 macOS 上本来就没法用(需要 pywebview 第二个窗口);这次只补齐了缺失的资源文件,**没有**验证它在打包版里真能弹出窗口。

---

## 6. Windows 仍然打不开:真因是中文路径（2026-10-21）

### 上一版为什么没修好

3.2.1 把资源从 `_MEIPASS` 挪到 exe 同级目录,CI 全绿、Windows runner 上 `[PASS] ui assets: D:\a\ncm\ncm\dist\_resources\src\gui\assets`——**路径确实换了,文件确实在**,但用户那边一字不差的同一个报错。

错的不是路径位置,是**这条路径本身**。用户把 zip 解压到了中文目录(实测确认),于是 URL 长这样:

```
file:///D:/下载/NCM/_resources/src/gui/assets/index.html?theme=dark&...
```

**WebView2 打不开含非 ASCII 字符的 `file://` URL。** 这一条解释了所有此前的矛盾:

| 现象 | 解释 |
|---|---|
| CI 全绿 | runner 路径是 `D:\a\ncm\ncm`、`C:\Users\RUNNER~1`,纯 ASCII |
| macOS 没事 | `.app` 是 onedir,不走 `file://` 的这套逻辑 |
| Win10 / Win11 一样错 | 两台都是中文路径 |
| 换路径位置没用 | 问题不在 `_MEIPASS` vs `_resources`,在「非 ASCII」本身 |

**连续两次靠推测定位都错了**,教训记在 `docs/notes.md` 和 skill 里:排除 URL 理论必须用真实引擎跑,不能只推 URL 长得对不对。

### 改了什么:界面改走 HTTP,彻底不用 file://

播放器本来就有一个 127.0.0.1 的本地服务(`src/core/mediaserver`),直接给它加一条 `/ui` 路由来发界面文件:

| 模块 | 改动 |
|---|---|
| `src/core/mediaserver.py` | 新增 `/ui/<路径>` 路由、`set_ui_root()`、`ui_url()`、`resolve_ui_asset()`;`_content_type_for` 显式钉死 `.js`/`.css`/`.html`(Windows 的 mimetypes 注册表会把 `.js` 映射成奇怪的值,浏览器就不执行了) |
| `main.py` | 界面改用 `media.ui_url(...)`;服务没起来时退回 `file://`;新增 `ncm-startup.log` 诊断文件 |
| `src/gui/bridge.py` | 桌面歌词同样改走 HTTP |
| `tests/test_ui_server.py`(新) | 15 个测试,**核心那条就是「中文路径下 URL 必须是纯 ASCII」** |
| `.github/workflows/build.yml` | 新增一步:把 exe 复制到 `dist\下载测试\` 这种中文目录里跑一遍,并断言 `ncm-startup.log` 里的 `ui_url` 是 `http://127.0.0.1:*` 且不含非 ASCII |

关键在于生成的 URL **只含 ASCII**,中文目录被百分号编码掉了:

```
http://127.0.0.1:49336/m/mx0-QDM0BOwzSd2qykYYww/ui/index.html?theme=dark&...
```

安全上和原来的媒体服务一致:token 保护 + 只允许 `_resources` 内的文件,`..` 和绝对路径一律 404(测试覆盖)。

### 诊断文件

启动时在 exe 同级写 `ncm-startup.log`(JSON):平台、frozen 状态、exe 目录、资源根、`ui_url`、各资源文件大小;窗口加载完再追加一次,把 webview 当时的 `location.href` 和页面文字记下来。以后再出「打开是错误页」的问题,这个文件能直接告诉我们真实 URL 和页面内容,不用再猜。

### 实测数据

- `pytest` → 377 passed, 6 skipped
- `ruff check .` → All checks passed
- **中文目录下的真实浏览器实测**:`/tmp/下载/NCM/_resources/...` 起服务,URL 纯 ASCII,浏览器加载出完整界面(`data-theme=dark` 生效、`app.js` 加载、`style.css` 生效)
- HTTP 路由实测:index.html 59026 B / style.css 68254 B / app.js 210054 B 全部 200,Content-Type 正确;`../../../../etc/passwd`、`..%2f..`、错误 token 全部 404
- macOS 重新构建 + `--self-test` 全绿,新增 `[PASS] ui over http: 200 text/html 59026B, ascii_url=True`

### 没做 / 留着的

- **仍然没在真实 Windows 上跑过**。但这次的验证不再是「文件在不在」,而是「URL 是不是纯 ASCII」和「服务是否真的返回了界面」,这两点都已在 CI 上用中文目录实测。
- 具体是 WebView2 哪一段拒绝非 ASCII `file://`,没有深挖到浏览器内部;机制层面的依据是「非 ASCII 路径 + 两台中文目录机器同样报错 + CI 纯 ASCII 路径正常」这三条观测。
- 桌面歌词仍未在真实 Windows 上验证过能否弹出窗口。

---

## 6. 手机号注册 + 免责声明 + 三项旧 bug（2026-10-22）

### 手机号注册

网易云没有公开注册接口，普通 `/api/*` 路由一律返回 `400 手机号或ursToken不能为空`，必须走 **eapi 签名路由**：

| 用途 | 路由 |
|---|---|
| 号码是否已注册 | `/eapi/cellphone/existence/check` |
| 注册 | `/eapi/w/register/cellphone` |

实测语义（脚本见会话记录，全程只用未注册号码与错误验证码）：

- `exist` 返回 `1`（有密码）/ `2`（无密码或第三方登录）/ `-1`（未注册）。同一号码跨 session 两次查询结果一致，可以当本地判据用。
- 注册响应：`200 注册成功`、`503 验证码错误`、`408` 频率限制。密码按 **MD5** 传。
- ⚠️ **该接口对已注册号码等同「重置密码」**。所以 `register_phone()` 之前必须先 `check_phone_registered()`，已注册的直接在本地拒绝，绝不发请求。这条是整个功能里唯一有破坏性的地方，测试 `tests/test_register.py` 里专门锁死。
- 短信验证码接口对未注册号码也可用，所以不能靠「发码失败」判断号码是否已注册，必须先查存在性再分流。
- 注册成功后服务端不一定下发会话 cookie，`register_phone()` 会用同一验证码补一次 `verify_login(force=True)`；失败时提示改用验证码或扫码登录。

前端：登录弹窗加了第 4 个 tab「注册账号」，流程是「查号码 → 发验证码 → 设密码 + 昵称 → 注册并登录」。`registerState.canRegister` 只有在 `registered === false && known === true` 时才为真，查询失败（`known === false`）不允许继续。

### 免责声明

首次启动弹一次，勾选后才能点「同意并继续」，否则只能退出。不响应遮罩点击、没有关闭按钮 —— 只能是同意或退出。

- 文案在 `src/i18n/{zh_cn,en_us}.json` 的 `disclaimer` 段，正文是一个字符串数组。
- 记录的是 `ui.disclaimer_accepted_version`，与 `src/version.py::DISCLAIMER_VERSION` 比较。**改了文案就把 `DISCLAIMER_VERSION` 加一**，老用户升级后会重新看到。
- `flatten_translations()` 现在会把数组摊平成 `disclaimer.body.1`、`disclaimer.body.2`…；前端按数字序收集。之前它只认 dict，数组会被原样塞进 `flat`，`test_i18n.py` 的「值是字符串」断言会挂。

### 三项旧 bug

1. **大歌单打开后空白**（`mountVirtualTrackList`）：虚拟滚动量的是 `container.clientHeight` / `container.scrollTop`，但 `#track-list` 是 `height: auto`，真正滚动的是父级 `.list-area`。于是高度和 `scrollTop` 恒为 0，可视窗口塌成 0 行 —— 超过 200 首的歌单渲染出来就是一片空白。改成量 `container.closest('.list-area')`，滚动监听和 `ResizeObserver` 一并挂到它上面。
2. **桌面歌词关不掉**：`toggleDesktopLyrics()` 判断的是 `player.desktopLyricsWindow`，但这个句柄只在浏览器兜底路径里赋值。打包后的窗口由 Python 持有，JS 永远看不到 → 每次点击都走「打开」分支，`open_desktop_lyrics()` 发现窗口已存在就只调 `restore()`，所以关不掉。新增 `desktop_lyrics_open()` 让前端问后端，`toggleDesktopLyrics` / `closeDesktopLyrics` 改走桥接。
3. **桌面歌词始终为空**：`pushLyricsToDesktop()` 只在窗口打开 500 ms 后和 `onDesktopLyricsWindowReady` 里各推一次。如果打开时还没在放歌，那一次推的是空数组；之后歌词加载完了也没人再推 → 一直是空的。现在歌词加载/清空时 `pushLyricsToDesktop(true)` 强制全量推，`updateLyricHighlight()` 每换行推一次索引；`force` 参数用来绕过 33 ms 去抖（去抖本来会吞掉「换了首歌」这种关键推送）。
4. **双行歌词高亮错位**：双行模式每行是两个 `.lyric-line`（原文 + 翻译），扁平节点数比 `player.lyrics` 多一倍，`lines[found]` 指错。现在按 `player.dualLine` 分别取 `.lyric-line-wrapper[found]` 或 `.lyric-line[found]`。

### 实测数据

- `pytest` → 405 passed, 5 skipped（含新增 `test_disclaimer.py` 5 项、`test_register.py` 18 项）
- `ruff check .` → All checks passed
- `node -c` → app.js / icons.js / desktop-lyrics.js 均无错误
- 免责声明在真实 Chromium 里跑过：6 段正文、标题、勾选框、按钮禁用态都正确，勾选后按钮解禁、点击后弹窗关闭
- 未机器验证（按偏好交本人手测）：真机开桌面歌词的关闭路径、大歌单在真实 pywebview 里的滚动、注册的真实发码

---

## 7. 自动更新检测 + 账号面板（2026-10-22 续）

### 自动更新

`src/core/updates.py` 读 GitHub releases API，和 `__version__` 比数字版本（`3.2.10` > `3.2.9`，字符串比较会反过来）。只提示、不下载、不安装：命中新版本时右下角弹一张可关闭的卡片，点「查看」跳 release 页。启动后异步查一次，结果缓存 6 小时；**失败不缓存** —— 否则一次断网会让「没有更新」一直被记住。

预发布版也算：本项目功能版常标 pre-release，跑在 pre-release 上的用户也应该收到提示。草稿（draft）跳过。

### 账号面板

点头像/昵称打开，展示服务端返回的真实资料：头像、昵称、签名、用户 ID、等级、会员、关注/粉丝、歌单数、累计听歌、注册时间。

**昵称与签名无法在第三方客户端修改。** 实测结论：

- `/api/user/profile/update` 这个路由**存在**（匿名访问返回 `301`，已登录返回 `250 用户资料初始化或修改失败`，都不是 `404 接口未找到`）。
- 参数名和字段与开源参考实现（`user_update.js` 的 `nickname/gender/signature/province/city/birthday`）完全一致；我用**当前昵称原样回传**做过幂等测试（不发新值、不可能改动账号），各种组合都是 `code 250`：
  - 只传 nickname → 250
  - nickname + `csrf_token` → 250
  - 全字段回传 → 先 `400 操作过于频繁`，等待重试后仍是 `250`
  - 换 `os=android` / `os=ios` / `os=pc 3.0.0` 的 eapi header → 250 / 250 / 400
- 所以面板做成**只读**，并在底部写明原因。放一个必然失败的输入框只会骗人。

`update_nickname()` 仍然留在 `LoginManager` 里（含空值、超长、未登录、原值相同的本地拦截），将来路由放开可以直接接上，但 UI 不暴露。

### 实测数据

- `pytest` → 全部通过, 5 skipped（新增 `test_updates.py` 17 项、`test_account.py` 8 项、`test_desktop_lyrics.py` 7 项、`test_lyrics_pairing.py` 7 项）
- `ruff check .` → All checks passed
- 账号面板在真实 Chromium 里跑过：9 个字段、真实资料值、提示文案全部正确
- 更新卡片在真实 Chromium 里跑过：标题、当前版本、按钮文案正确
- 未机器验证：真机 pywebview 下的浮层关闭、真实大歌单滚动（交本人手测）；GitHub API 实时请求在本机被 hosts 加速器的自签证书挡住，逻辑用 `gh api` 拿到的真实 release 数据验证过

---

## 8. 性能与本地占用（2026-10-22 续）

### 实测到的

- 启动不是 import 瓶颈：`-X importtime` 显示 `import main` 共 64 ms，其中 `yaml` 11 ms、`logging` 17 ms。慢的是别的（首屏要等网盘接口），砍 import 收益极小，没动。
- 本机缓存实测：音频 0 个，封面 22 个 / 10.4 MB。缓存上限写死 2 GB 音频 + 256 MB 封面（`src/core/cache.py`），UI 里没有调整入口。

### 做了什么

- **设置 → 高级 → 本地缓存**：显示音频 / 封面 / 合计占用，三个按钮分别清空。清空是安全的 —— 缓存里的东西全都能重新下。
- 顺手修了一个真 bug：`MediaCache.forget_audio()` 开头 `if not self._available: return 0`。这个标志只有 `_ensure_root()` 建过目录才为 True，所以「磁盘上有缓存、但本次会话没写过」的情况下点「清空音频」**静默什么都不做**。改成「不可用且目录不存在才返回 0」。`tests/test_cache_panel.py` 锁死了这个行为。
- 大歌单的虚拟滚动本来就是省内存的（200 首以上只渲染可视窗口的行），这轮又修了它的滚动位置丢失，滚动时的重建次数反而更少。

### 没做 / 留着的

- 没动缓存上限的默认值（2 GB / 256 MB）。要暴露成设置项就得存状态、还得让用户自己填数字，收益不大；清空按钮已经够用了。
- 没做首屏预加载、没做图片懒加载之外的渲染优化 —— 那是另一个量级的改动，风险高于收益。
- 打包体积没动：`main.spec` 把 `src/gui/assets` 整目录打进去，`icons.js` 增加了约 30 KB（37 个图标），换掉 emoji 之后总体是持平的。

### 实测数据

- `pytest` → 全部通过, 5 skipped（新增 `test_cache_panel.py` 5 项）
- `ruff check .` → All checks passed
- `node scripts/check-player-js.mjs` → all player checks passed

---

## 9. v3.3.0 发布（2026-10-22）

`v3.2.2` 已转正式版；本次发 `v3.3.0`，CI 通过，产物 6 个（Windows exe + zip、macOS arm64/x64 的 zip + dmg）。

### 发布流程里踩到的两件事

1. **CI 建的 release 是正式版，不是 prerelease。** `build.yml` 的 release job 里 `gh release create` 只带了 `--title` / `--generate-notes` / `--latest`，**没有 `--prerelease`**。结果 `gh release list` 显示 v3.3.0 的 `isPrerelease: false`，`/releases/latest` 会指向它 —— 和「先发 prerelease、等实测再转正式」的流程直接冲突。发完必须 `gh release edit v3.3.0 --prerelease` 改回来。
   → 下次改 `build.yml`：建 release 时加 `--prerelease`，或者按 tag 决定。别依赖事后手改。
2. **`--generate-notes` 生成的说明没法用。** 自动 notes 是 commit 列表，里面是「feat:」「fix:」这类开发信息，不该出现在公开产物里。改成自己写 notes 文件，用 `gh release edit --notes-file` 覆盖。

### 发布前自检（这次实际跑过的）

| 检查 | 结果 |
|---|---|
| `pytest` | 444 passed, 6 skipped |
| `ruff check .` | All checks passed |
| `node -c` × 3 | app.js / icons.js / desktop-lyrics.js 均无错误 |
| `node scripts/check-player-js.mjs` | all player checks passed |
| JS 调用的 86 个 bridge 方法 | 全部有实现（`MISSING: none`） |
| HTML 里 25 个 `data-icon` | 全部在 icons.js 里（`MISSING: none`） |
| 四个 UI 文件的 emoji | 0 |
| 打包目录 | 只有 icons.js / FEATHER-LICENSE.txt 是新增，无测试残留 |
| 浏览器实测 | 注册页 / 免责声明 / 账号面板 / 更新卡片 / 缓存占用 / 登录徽章 全部正常 |

CI 那边还跑了一个额外的验证：`Smoke test the .exe` + `Run the app from a non-ASCII path` —— Windows 产物在非 ASCII 路径下真的能启动。

---

## 10. 已知问题：Windows 端播放页偶现排版异常（2026-10-22）

### 现象

Windows 上多次启动后，**播放页面**的排版突然异常。窗口是最大化的。macOS 端暂未确认复现。

### 现状

**偶现，无法给出稳定的复现步骤。** 星空喵判断它对正常使用影响不强，决定先转正式版、后续再修。因此 v3.3.0 已按正式版发布（`gh release edit v3.3.0 --prerelease=false`），这个问题**没有**在 release notes 里写 —— 它是未验证、未定位的偶发问题，写进公开说明等于给用户一个没有答案的疑问。

### 已知线索（还没验证）

「多次启动后开始出现」这句话很关键，指向**跨次启动残留的状态**，而不是随机的布局抖动：

- `settings.yaml` 里持久化的字段（窗口尺寸、主题、壁纸、玻璃效果、播放器状态）会跨启动保留。最大化窗口 + 某次保存的尺寸组合，可能在下次启动时和当前的 CSS 布局前提冲突。
- 播放器状态走 `src/core/library.py` 的 session resume：退出时记住队列、当前曲目、进度。下次启动 `restoreSession()` 会把队列灌回来 —— 如果灌回来的行数和虚拟滚动的窗口计算不一致，排版就会错位。
- 虚拟滚动（`mountVirtualTrackList`）依赖 `.list-area` 的 `clientHeight` / `offsetTop`。窗口最大化时它的值和未最大化时不同，缓存下来的某个尺寸一旦被复用，就会算出错误的窗口。
- `ResizeObserver` 只在 `mountVirtualTrackList` 里挂了，且绑在 `scroller` 上。窗口尺寸变化会触发它，但如果此时 `renderTrackList()` 正在跑，就会重入。

### 下一步怎么查

需要一个能复现的抓手，否则改什么都是猜。三个方向，按性价比排：

1. **先分辨是哪一类排版异常。** 文字重叠？行高塌陷？列宽错位？还是整个播放页布局散架？截图比描述有用得多 —— 让星空喵下次遇到时截一张。
2. **跨启动状态的嫌疑最大** → 让用户试一次：删掉 config 目录（`~/Library/Application Support/NeteaseMusicDownloader` 或 Windows 上的对应路径）再启动。如果不再出现，就是持久化状态的问题，不是渲染的问题。这个测试成本最低、结论最硬。
3. **如果和 session resume 有关** → 启动后立刻切换到别的 tab 再切回播放页。如果复现，就和 restoreSession 的时序有关（队列灌回来和虚拟列表 mount 的先后）。

在拿到 (1) 或 (2) 之前不动代码 —— 这个项目刚发完正式版，没有复现路径的改动就是在猜。

---

## 11. 性能实测（2026-10-22）—— **已作废，见第 12 节**

> 这一节的数据是错的：只测了孤立的 Python 进程，漏掉了 WebView 的子进程。
> 正确数据在第 12 节（跑真实编译产物 + 进程树 diff）。本节仅留作对照。


不是估计，是在这台机器上跑出来的数。环境：macOS 27.2，M2 / 8GB，Python 3.14.7（项目自己的 `.venv`）。

### 启动

| 阶段 | 耗时 |
|---|---|
| `import main`（全部 156 个模块） | **41 ms** |
| 读 settings | 28 ms |
| import `src.gui.bridge` | 79–214 ms（首次 214 ms，含 `requests` / `webview` / `yaml`） |
| 媒体服务器起端口 | **2.9 ms** |
| 首屏 UI 资源取回（5 个文件，387 KB） | **19.7 ms**（index.html 12.7 / app.js 2.3 / icons.js 1.4 / style.css 1.5） |

**结论：启动不是 import 瓶颈。** 最大的单项是 `requests`（含 `urllib3` + `chardet` + `charset_normalizer`），但它本来就要用；`yaml` 7 ms。把这 41 ms 砍到 20 ms 对一个要联网、要起 WebView 的桌面应用没有意义。真正决定「打开多久能用」的是网络请求，不是 import。

import 之后那 214 ms 里有一次明显的分叉：bridge 的 import 链把 `requests`、`webview`、`yaml` 全拉进来。第一版 PyInstaller 打包时它们就都在包里，所以**运行时不付这个钱** —— 付钱的是打包体积。

### 内存

- Python 进程常驻：**52.5 MB**（已拉起媒体服务器 + 缓存完一个 200 首的歌单）
- 纯 import 完（还没建 bridge）：41.1 MB
- 也就是说**整个后端的常驻内存不到 55 MB**。

WebView 是另一个进程（WKWebView / WebView2），不在这 52.5 MB 里。

### 磁盘

| 位置 | 实测 | 说明 |
|---|---|---|
| `src/`（打包进 app 的全部代码 + UI） | **2.4 MB** | 其中 UI 资源 387 KB |
| 打包产物 | macOS dmg 17–20 MB / Windows exe 19 MB | 含 Python 运行时 |
| 本地配置目录 | 34 MB | **其中壁纸 23 MB** |
| 本地缓存 | 11 MB | 22 张封面 10.4 MB |
| 日志 | 1.5 MB 上限 | `RotatingFileHandler` 512 KB × 3 |

### 这轮实际改掉的：壁纸

**23 MB 的 PNG 存进了配置目录**，而这个图在应用里是要模糊 + 压暗 + 垫在毛玻璃后面的 —— 多出来的像素一点用都没有。3600×2400 的 RGBA PNG 就是这个体积。

改成存 JPEG：同样 3600×2400，**22.7 MB → 3.09 MB**（JPEG q=88，progressive），编码耗时 352 ms（一次性，只在换壁纸时）。超过 3840 px 的会等比缩小。

老用户怎么办？`Settings.migrate_background_image()` 在启动时跑一次：发现存的是超过 2 MB 的 PNG 就原地改写成 JPEG，删掉旧的，改 `settings.yaml` 里的文件名。已经是 JPEG 的、或体积不大的，不动。

`tests/test_background_image.py` 10 项锁住这个行为。

### 没动的（和理由）

- **缓存上限**（2 GB 音频 / 256 MB 封面）：本机实测只用了 11 MB，没碰到上限。UI 里能一键清空了（上一轮做的），比让用户填数字实际。
- **`app.js` 232 KB / 5983 行**：单文件 IIFE，没有组件拆分。这是**结构问题不是性能问题** —— 真要拆是重写，不是优化。而且它压缩后只有 ~60 KB，走本地 HTTP server，19.7 ms 取完全部 UI 资源。
- **WebView 侧的 DOM 节点数**：200 首以上的歌单走虚拟滚动，只渲染可视窗口那十几行。已经在做了。
- **import 优化**：41 ms，不值得。
- **启动时预取歌单 / 图片懒加载**：会改变首屏行为（先看到空列表再看到内容），风险高于收益，没做。

### Android 要接着做的话

已有的基础：`style.css` 有一整套 `max-width: 720px` 的移动端样式 —— token 覆盖（更大的圆角、更软的玻璃）、header 收窄、侧栏变抽屉（带 `env(safe-area-inset-*)`）、全屏 now-playing 视图。`app.js` 里有 `initMobileLayout()` 和 `npm-*` 那套全屏播放控件。

**但那套是「桌面窗口缩窄」的响应式，不是「手机应用」的原生体验。** 真要做 Android，还差：触摸手势（滑动切歌、长按菜单）、底部导航栏替代顶部 tab、安全区适配刘海屏、后台播放与通知栏控制、离线状态。**不能直接把 pywebview 打包成 APK** —— pywebview 只有桌面后端（WinForms / GTK / Cocoa / Qt），没有 Android 后端。

两条路：
1. **Capacitor / Tauri 壳 + 复用现有前端**：`src/gui/assets/` 的 HTML/CSS/JS 本身是响应式的，能复用大部分；Python 后端要么改成 HTTP API 让壳去调（但那会动到整个 bridge），要么用 WebView + 本地服务。改动集中，但要重新设计前后端边界。
2. **Kotlin / Compose 原生重写**：Android 体验最好，但等于重写一遍，Python 的 eapi 签名、歌词解析、媒体缓存逻辑全要翻成 Kotlin。

等星空喵定方向再动。

---

## 12. 性能实测（重做：跑真实编译产物，2026-10-23）

第 11 节那版数据**是错的**，作废。那次测的是 `.venv` 里跑起来的孤立 Python 进程，
只算了 `resource.getrusage(RUSAGE_SELF)`，既没走 PyInstaller 冻结后的代码路径，
也没算 WebView 的子进程 —— 实际使用中占大头的恰恰是那些子进程。这次重做。

### 怎么测的

- 先 `pyinstaller --clean --noconfirm main.spec` 重新打包（`dist/NeteaseMusicDownloader.app`，38 MB），
  `--self-test` 16 项全 PASS。
- 启动的是**打包后的 `.app`**，不是源码。
- **进程树怎么找**：macOS 会把 WebKit 的辅助进程 re-parent 给 launchd（ppid 变成 1），
  所以 `pgrep -P` 走父子链只能看到 Python 那一个进程，什么都找不到。
  正确做法是**启动前后做 `ps` 快照 diff** —— 新出现的 PID 就是这个 app 造成的
  （Python 进程 + WebContent + GPU + Networking + 音频沙箱助手）。
- **「起来了」的判据**是 app 自己的窗口出现（`CGWindowListCopyWindowInfo` 查
  on-screen 窗口里 owner 是本 app 的），不是进程存在 —— 进程比窗口早得多。
- 内存同时看 `ps` 的 RSS 和 `footprint -p <pid>` 的物理占用（RSS 会重复计共享页）。

### 结果

单个实例（`dist/NeteaseMusicDownloader.app`，本机 M2 / 8GB）：

| 指标 | 实测 |
|---|---|
| 进程启动 → 窗口出现 | **565 ms**（另一次 13.7 s，是因为 Little Snitch 弹了授权框，见下） |
| 启动过程内存峰值 | **288 MB**（整棵树） |
| 稳态（跑 40 s） | **115 – 155 MB**，均值 151 MB，末值 117 MB |
| 稳态时 Python 进程 | **19 – 50 MB**（启动后从 108 MB 回落到 19 MB，macOS 内存压力回收） |
| `footprint` 物理占用（Python 进程） | **65 MB** |
| 空闲 CPU | **0.0%**（30 s 采样全程 0） |
| 打包后 app 体积 | **38 MB**（dmg 17–20 MB） |

各进程拆解（稳态）：

| 进程 | 内存 |
|---|---|
| NeteaseMusicDownloader（Python） | 19–50 MB |
| com.apple.WebKit.WebContent | 40–49 MB |
| com.apple.WebKit.GPU | 22–33 MB |
| com.apple.WebKit.Networking | 6–12 MB |
| com.apple.audio.SandboxHelper | 6 MB |
| 合计 | 115–155 MB |

**所以：整棵进程树约 150 MB，其中 Python 只占三分之一，两块是 WebView 的渲染进程。**
我上一版报的「52.5 MB」只算到了 Python 那一块，实际是它的 3 倍。

### 启动那 13.7 s 是什么

Little Snitch 弹出了「Terminal via NeteaseMusicDownloader wants to connect to
music.163.com」的授权框，等它超时了 5 s 才继续。**这是本机的网络拦截，不是应用慢。**
正常的机器上窗口 565 ms 就出来了。

顺带发现：日志里 `account query failed ... ConnectTimeoutError` 每隔十几秒一条，
是 Little Snitch 挡的必然结果，不是应用的重试风暴 —— 观察 30 s 日志行数 **+0**，
说明应用没有在反复打这个接口。

### 打包体积

38 MB 里最大的一块是 **PIL 11 MB** —— 它是 `qrcode[pil]` 的传递依赖，
我们只用来做两件事：渲染登录二维码、把壁纸缩放成 JPEG。
其余：Python.framework 5.2 MB + python3__dot__14 5.2 MB + libcrypto 4.6 MB。

**没动它**：去掉 PIL 意味着二维码得自己画（要写 QR 编码器）或改成 SVG 交给前端渲染，
壁纸缩放则要退回原样拷贝 —— 都是拿功能换体积。而且 PIL 只影响安装包大小，
**不影响运行时的内存占用**（那 65 MB 里 PIL 只占 2 MB）。

### 这一轮真正改掉的

- **壁纸 22.7 MB 的 PNG → 3.09 MB 的 JPEG**，超 3840 px 的等比缩小。
  迁移逻辑在真实安装上验证过：本机配置目录 **34 MB → 15 MB**，
  `settings.yaml` 里的文件名跟着改成 `background.jpg`，旧 PNG 已删。
- 顺手发现并修了一个 UI 侧的浪费：`MediaCache.forget_audio()` 用 `_available` 当闸，
  而该标志只在本次会话建过目录时为 True —— 磁盘上有缓存时点「清空音频」静默无效。

### 结论：性能不是当前的问题

| 维度 | 状态 |
|---|---|
| 启动到能用 | 565 ms —— 够快 |
| 空闲 CPU | 0% —— 不空转 |
| 内存 | 150 MB，其中 100 MB 是 WebView 的 —— 正常范围 |
| 磁盘 | 本地 15 MB（含 11 MB 缓存）—— 够小 |
| 打包体积 | 38 MB —— 对 pywebview 应用正常 |

**没有值得动的地方。** 真要省，唯一的空间在 PIL（11 MB 安装体积）和
WebView 的 WebContent 进程（那不是我们能控制的）。

### 教训（下次别再犯）

1. **不要用 `resource.getrusage` 报内存。** 它只算当前进程，漏掉 WebView 的子进程，
   数字会差 3 倍。要么用 `ps` 快照 diff + 进程树，要么用 `footprint`。
2. **macOS 上不能靠父子链找 WebKit 辅助进程** —— 它们被 re-parent 给 launchd。
   必须用启动前后的进程快照 diff。
3. **「进程存在」不等于「应用可用」。** 窗口出现才是。
4. **测编译后的产物，不测源码。** PyInstaller 的 import 行为和源码跑不一样
   （第一版 PyInstaller 打包把 `requests` / `webview` / `yaml` 都收进去了，
   运行时不付 import 的钱，付的是包体积）。
5. **本机 Little Snitch 会挡出站请求**，会污染启动时间和网络相关的测量 ——
   测启动前先看一眼有没有它的弹窗。

---

## 13. 缓存、桌面歌词与性能（2026-10-23）

### 内存：实测结论是「没什么可省」

用**编译版**（`dist/NeteaseMusicDownloader.app`）跑完整进程树，交错 A/B 三轮：

| 变体 | 峰值中位数 | 稳定态中位数 |
|---|---|---|
| 带壁纸 | 251.1 MB | 146.8 MB |
| 无壁纸 | 231.5 MB | 110.1 MB |

Chromium 单实例对照（同一 profile、3 个进程，可比数字）：

| 页面 | RSS 合计 |
|---|---|
| 空白页 | 245.9 MB |
| 应用真实 index.html | 246.8 MB |

**结论：页面内容几乎不占内存（+0.9 MB），`backdrop-filter` 约 +10 MB，
其余全是 WebView 引擎基线。** 壁纸净占约 20 MB 峰值 / 37 MB 稳定态。

已落地且保留：壁纸存 JPEG（22.7 MB PNG → 3.09 MB）、启动时迁移旧 PNG、
预生成 1920 px 显示副本（浏览器按原图尺寸解码，3600×2400 会变成 ~34 MB
RGBA 像素）。

**教训（第一次测错了）**：壁纸 A/B 最初无效 —— 所谓「完整图」请求仍被应用
按需缩放，测的不是全尺寸解码。改成绕过应用、直接加载两张文件对比才拿到真值。

### 缓存（`src/core/metacache.py`）

磁盘 JSON 缓存，三类 TTL：歌曲元数据 7 天 / 歌词 30 天 / 歌单列表 10 分钟。
单条 4 MB 上限，每类 2000 条 LRU，临时文件 + `os.replace` 原子写入，
损坏条目丢弃而不是抛错。

接入点：`get_songs_detail`（按单曲粒度，只请求缺失的 id）、
`get_lyrics`、`get_playlist_songs`、`get_playlist_info`（服务器不可达时
用缓存兜底返回名称和封面）、`get_user_playlists`。

**实测效果**（独立进程、共享配置目录，模拟真实重启）：

```
第 1 次启动: 2 次网络请求
第 2 次启动: 0 次
第 3 次启动: 0 次
```

歌曲文件缓存本来就按 `song_id/quality` 分键（`audio_path(song_id, quality, ext)`、
URL 缓存键 `(song_id, quality)`），所以**改音质天然切到另一份文件**。

**踩的坑**：元数据缓存是模块级单例，`tests/conftest.py` 的 `_reset_stores()`
没重置它 —— 歌词测试因为「上一个测试的歌词从磁盘缓存里返回了」而全红，
看起来像是代码坏了，其实是测试隔离漏了。另外 `MetaCache.root` 原来只在
第一次访问时算一次，改成每次从当前 settings 解析，否则换配置目录的测试
会写进旧位置。

### 强制刷新

设置里加「强制刷新」按钮：`bridge.force_refresh()` 清掉元数据缓存、媒体缓存
（音频 + 封面）、内存里的歌单/响应缓存和已解析的 URL 缓存，然后重启应用。

重启用 `subprocess.Popen` 起新进程、旧进程 2 秒后 `os._exit(0)` ——
媒体服务器绑的是随机端口（port 0），新旧两个实例不会抢端口。

### 桌面歌词

**偏移的根因**：原生浮窗固定 `height=120`，而内容随字号增长
（18 px → 112 px，24 px → 140 px，28 px → 159 px，36 px → 196 px）。
`reportContentHeight()` 原来**只在 `applyStyles()` 里调用一次**（启动时、
歌词到达之前），歌词渲染后再没人要求加高窗口，于是文字被裁掉。

修法：`renderLyricLines()` 末尾重新上报高度，且测量包在 `requestAnimationFrame`
里 —— 同一 tick 读到的是旧高度，窗口会慢一帧。

浏览器实测（带 pywebview stub）：长歌词请求 125 px，短歌词 82 px，
`resize_desktop_lyrics` 的调用值和实测高度一致。

**顺带修掉一个真 bug**：桌面歌词的设置（字号/颜色/透明度/背景）**从来没有被保存过** ——
`bridge.update_settings` 的 ui 白名单里没有这些字段，全被丢掉了。用户改了字号，
界面变了，下次启动又回到默认。

### 歌单预览图

不是磁盘缓存的问题（`/cover` 已经发 `Cache-Control: public, max-age=604800`，
字节也落盘）。真正的原因是**每张封面都要一次 Python IPC 往返**：
`proxyCoverUrl` 只能问后端要 loopback URL，而 `base_url` 从未暴露给前端。

修法：`get_app_info` 增加 `media_base_url`，前端用 `buildCoverUrl()` 直接拼
`/cover?u=...&ext=...`（URL 完全确定），不再往返、不再等最后一个响应才出图。

---

## 14. 歌词区域：居中、空状态、双行与逐字（2026-10-23）

### 歌词整体偏下的根因

两条独立原因叠在一起：

1. **`padding: 40% 18px`** —— CSS 的百分比 padding 是相对**宽度**解析的，
   不是高度。面板宽 1128 px 时上下各 451 px，恰好等于面板高度的一半，
   于是「看起来居中」。窗口一变成宽而矮（比如最大化播放页），40% 的宽度
   就远大于高度的一半，歌词被推到底部甚至推到可视区外。
2. **`offsetTop` 量错了参照物** —— `.lyrics` 没有 `position`，所以最近的
   定位祖先是 `.lyrics-panel`，而它包含标题栏。`offsetTop` 于是多了标题栏
   的高度，滚动目标每次都差 47 px，高亮行停在中心偏上。

修法：竖向 padding 改为脚本按 `clientHeight / 2` 写进 `--lyrics-pad`；
滚动一律用 `getBoundingClientRect()` 算（与 offsetParent 无关）；
`.lyrics` 加 `position: relative` 让语义也对上。

实测（多窗口尺寸）高亮行离中心偏移从 **-47 px** 变成 **0 ~ -0.5 px**。

### 「暂无歌词」看不见

同一个 `padding: 40%` 也作用在空状态上：提示语被顶到 464 px 处，面板一矮
就整个落到可视区外。现在空状态切 `.lyrics.is-empty`（flex 居中、padding 归零），
提示语恒在区域正中（实测偏移 0），手机端的全屏播放页同样处理。

### 双行 / 逐字「用不了」

- **双行**：切模式会重建 DOM，把 `scrollTop` 归零、`lyricIndex` 重置为 -1，
  而重新高亮只发生在下一次 `timeupdate`。暂停时切换，列表跳回第一行、
  一行都不高亮 —— 看起来就是功能坏了。现在 `renderLyricLines()` 结尾直接
  按当前播放位置重新高亮并居中。
- **逐字**：`renderKaraokeLines()` 原本只加了个 class，靠 CSS `::before`
  在整行激活时把宽度拉满 —— 那是整行块高亮，不是逐字。而且那段 CSS 写了两遍。
  现在按字符切 span（中文逐字、英文逐词），用「本行起始 → 下一行起始」的
  时间区间线性推进，每个播放 tick 更新 `is-sung`。

### 滚动动画不能只靠浏览器

调试时发现 `behavior: 'smooth'` 在 headless WebView 里完全不生效，
`scrollTop = x` 也无效，只有 `scrollTo({behavior:'auto'})` 可靠。
更糟的是 `requestAnimationFrame` 在某些环境下根本不回调（遮罩/无头窗口），
而原来「按内容高度调窗口」和「滚动到目标」都只挂在 rAF 上。

现在滚动由自己的定时器缓动（`smoothScrollTo`，16 ms 步进、easeOutCubic、
新目标取消旧动画），桌面歌词的高度上报同时挂 rAF 和 setTimeout。
这条对**所有**平台都更稳：reduced-motion、无头、掉帧都不会让歌词停在原地。

---

## 15. v3.4.0 发布（2026-10-23）

版本 `3.3.0` → `3.4.0`（新增缓存面板、强制刷新、桌面歌词文字样式，属功能增量，
按 minor 走）。tag `v3.4.0` 触发 CI，五个 job 全绿，产物 6 个，**按 prerelease 发布**，
等实测确认后再 `gh release edit v3.4.0 --prerelease=false`。

release notes 由手工撰写后 `--notes-file` 写入，不用 `--generate-notes`。

**这一版踩到的一点：** `gh release view --json ...` 偶发返回 `release not found`，
紧接着 `gh release list` 能正常列出同一个 tag，`gh run view` 中途也报过 HTTP 502 —— 都是
GitHub API 的瞬时抖动，隔几秒重试即恢复。判断发布是否成功以 `gh release list` 为准，
不要因为一次 `view` 失败就以为 release 没建出来。

---

## 16. 收藏同步、歌单写操作与歌单权限（2026-10-23 续）

### 收藏同步失败：不是代码问题，是服务端拒绝

用户在搜索结果里点收藏，本地成功、网易云失败。逐层排查后定位到
**`/api/song/like` 返回 `code 524 当前环境异常，已取消喜欢`**。

实测排除了所有客户端侧原因：

- 同一账号、同一时刻、同一 payload，部分歌曲 200、部分 524
- 换 eapi host（interface / interface3 / music.163.com）无效
- 换客户端身份（os=pc/android/ios/linux，四种 appver）无效
- 带 `csrf_token`、换 `alg` 值、`like` 传字符串或布尔，都无效
- 换参考实现用的 `/api/radio/like` 路由，同样 524
- 换 weapi 通道无效 —— 详见下一节

524 有明显的**连续 id 段特征**：

```
3440655476 -> 200  万相由心何妨问天机
3440655477 -> 200  月声尽，心灯明
3440655478 -> 524  一宵同尘，灯月长圆
3440655479 -> 524  万相由心何妨问天机 (Instrumental)
3440655480 -> 524  月声尽，心灯明（instrumental）
3440655481 -> 524  一宵同尘，灯月长圆（Instrumental）
3440655482 -> 524  熬油.wav
```

而且 `unlike` 返回 200（能取消）、紧接着 `like` 仍是 524 —— 说明这批歌曲在
网易云侧有服务端标记，客户端改不掉。

**结论**：这是网易云对特定曲目/专辑的风控限制，客户端无法绕过。
能做的是把不同的失败区分开，让用户知道发生了什么：

| cloud | 含义 | 界面提示 |
|---|---|---|
| `ok` | 服务端已接受 | 收藏成功 |
| `unsupported` | 这首歌不被网易云接受（524） | 该歌曲暂不支持云端收藏，已保存在本地 |
| `rate_limited` | 请求太密（405），稍后重试即可 | 操作太频繁，稍后再试一次即可同步 |
| `not_logged_in` | 会话失效（301） | 需要重新登录 |
| `login_required` | 未登录 | 仅保存在本地 |
| `failed` | 其他（网络错误等） | 同步失败 |

改之前 `set_song_like` 返回 bool，`code != 200` 一律算 False，三种完全不同的
原因被压成同一句「同步失败」。

### weapi 通道在这台机器上是死的

按参考文档复刻了完整的 weapi 加密（AES-128-CBC 双层 + RSA PKCS#1 v1.5，
`secKey` 先反转再 RSA，密钥 `0CoJUm6Qyw8W8jud` / IV `0102030405060708`），
并用项目自己的纯 Python AES 实现实现。

结果：**每个 weapi 请求都返回 HTTP 200 + 空 body**。故意发垃圾 payload 也是
空 body —— 说明不是签名错，是通道被吞掉了。这与 2026-09-30 的记录一致，
weapi 在这条网络上不可用。参考文档主要讲 weapi，对本项目没有帮助。

### 歌单写操作的路由全部换了

代码里原来用的四个端点，实测：

| 操作 | 旧路由 | 实测 | 新路由 |
|---|---|---|---|
| 重命名 | `/api/playlist/update` | 404 | `/api/playlist/update/name` |
| 增删歌曲 | `/api/playlist/tracks/add` `/del` | 404 | `/api/playlist/manipulate/tracks` |
| 删除歌单 | `/api/playlist/delete` | 400 | `/api/playlist/remove` |

参考实现（`xgxdmx/NeteaseMusic-API` 的 `module/*.js`）给出了正确的 payload：

```python
# 增删歌曲：tracks 是 [{"type":3,"id":"<song id>"}] 的 JSON 字符串
{"op": "add"|"del", "pid": pid, "trackIds": "[id]", "imme": "true"}
# 重命名：走 /api/batch，一次改多个字段
{"/api/playlist/update/name": '{"id":pid,"name":name}'}
# 删除歌单
{"ids": "[pid]"}
```

实测：重命名 200、删除 200、增删歌曲 200（增删歌曲在**别人**的歌单上返回
401「无权限操作歌单」，在**自己**的歌单上返回 200 并真的生效）。

### 歌单权限模型

权限在**后端**判定（`_playlist_payload`），前端只按后端给的标志渲染按钮 ——
前端从名字或标志位猜权限，猜错就是用户在别人的歌单上看到「删除」。

| 歌单类型 | 取消收藏 | 重命名 | 增删歌曲 | 删除 |
|---|---|---|---|---|
| 别人创建的 | 允许 | 禁止 | 禁止 | 禁止 |
| 自己创建的 | 禁止 | 允许 | 允许 | 允许 |
| 「我喜欢的音乐」(specialType 5) | 禁止 | 禁止 | 禁止 | 禁止 |

后端在 `update_playlist` / `delete_playlist` / `add_tracks_to_playlist` /
`remove_tracks_from_playlist` 里再查一遍账号的歌单列表做校验 —— 前端按钮
只是提示，真正的拦截在服务端。

### 「我喜欢的音乐」里的红心

账号的网易云收藏和本地收藏是两个列表，之前红心只看本地，所以「我喜欢的音乐」
歌单里的歌全是空心。现在 `get_play_stats` 返回的是**合并后**的列表
（本地收藏 ∪ 云端收藏，云端用 `online:<song id>` 键，和本地同格式）。

云端列表在收藏/取消收藏之后要**绕过响应缓存**重新读 —— 缓存里存的是
点击前的答案，不绕过的话红心不会跟着点击变。

---

## 17. v3.4.1 发布（2026-10-23 续）

v3.4.0 已经发出去了，所以这一版是 **v3.4.1** —— tag 不可复用（v3.4.0 的
产物里没有这批修复）。CI 五个 job 全绿，产物 6 个，prerelease 发布。

release notes 手工撰写（`--notes-file`），按「收藏 / 歌单 / 修复」分块。

**这一版顺带确认的一件事：** `gh release view` 又返回过一次 `release not found`，
隔几秒 `gh release list` 就能正常列出同一个 tag。GitHub API 的瞬时抖动，
判断发布是否成功以 `gh release list` 为准，不要因为一次 view 失败就重发。

---

## 18. 云端收藏 524 的真正原因：登录时伪造的 PC 客户端身份 cookie（2026-10-24）

### 结论

`code 524 当前环境异常，已取消喜欢` 的原因是**本项目自己装的假 PC 客户端
cookie**：

```python
# src/auth/login.py
CLIENT_COOKIES = {"os": "pc", "appver": "8.9.70", "channel": "netease"}
```

这三个 cookie 让会话"自称"是官方 PC 客户端，但服务器并没有把它发给这个
客户端。网易云的风控看到这种身份不符的会话，就拒绝把歌加进"我喜欢的音乐"。

**把它们去掉，524 就消失了。**

### 怎么测出来的（这一步绕了很多弯路）

一开始的排查全部指向"服务端对某些歌做了限制"：换 host、换 os/appver 请求头、
带 csrf_token、换 `alg`、换 `/api/radio/like` 路由、复刻完整的 weapi 加密，
全部无效。而且 524 有明显的**连续 id 段**特征：

```
3440655476 -> 200
3440655477 -> 200
3440655478 -> 524
3440655479 -> 524
3440655480 -> 524
```

看起来像"这批歌被服务器拉黑了"。**但这个结论是错的。**

关键在于**测法有污染**：一旦某首歌被 like 成功过一次，之后再对同一首歌 like
就是幂等的，永远返回 200 —— 所以"后来全好了"根本不是"限制解除"，而是
"那首歌已经在收藏列表里了"。

正确的测法必须是：**每个变体用一首从没被 like 过、已知会 524 的歌**，
测完立刻 unlike 还原。用这个测法重跑，cookie 变体的差异就出来了：

```
完整 cookie 集（现状）      -> 524, 524, 524, 524, 524, 524, 524, 524
去掉 os                      -> 200
去掉 appver                  -> 200
去掉 channel                 -> 524
去掉 os+appver+channel       -> 200
去掉 NMTID                   -> 524
去掉 _ntes_nuid              -> 524
去掉反爬三件套                -> 524
只留 MUSIC_U + __csrf        -> 200
```

`os` 和 `appver` 就是元凶。最后做了**同歌 A/B 反复对照**确认（每轮先确保
那首歌没被收藏，再 full/thin 各测一次）：8 首全部 full=524、thin=200，
8/8 一致。

### 修法

- `CLIENT_COOKIES` 清空（保留常量并写清原因，避免以后有人又加回去）
- `apply_cookie_string()` 在读入时丢弃 `os`/`appver`/`channel`
  （旧版本存下来的 cookie 里还有它们）
- `_cookie_string()` 写回时不再带上它们

### 验证

- 通过**应用自己的代码路径**（`LoginManager` → `NeteaseAPI` → `set_song_like`）
  实测：那首一直失败的歌现在 `set_song_like` 返回 `ok`，并且真的出现在云端
  收藏列表里；测完 unlike 还原，账号前后一致
- 其余端点在两种 cookie 集下**完全一致**（账号、收藏 id、歌单列表、歌曲
  详情、搜索、歌单详情、歌词、播放链接全部 code 200 且返回相同数据）
- **二维码登录不受影响**：去掉 cookie 后 unikey 正常拿到（code 200），
  plain 和 eapi 两条轮询通道都正常返回 801「等待扫码」

### 官方 App 的逆向（顺带）

`~/Documents/temp/网易云音乐.app`（3.1.9，Cef + 原生）用 IDA 9.5 headless
分析：

- 二进制里**没有** eapi 用的盐值 `36cd479b6b5`，但**有** weapi 的 IV
  `0102030405060708` —— 说明官方客户端这条链路走的是 weapi
- 官方客户端访问的域名里有 `interfacepc.music.163.com`（PC 专用），
  本项目原来只用 `interface` / `interface3`
- 所有 API 路径都是加密存放的（`resources.pack` 是加密包），明文字符串里
  只有 8 个 `/api/` 开头的

不过这些都没有直接用上 —— 真正的原因是自家 cookie，IDA 这一趟确认了
"不是客户端协议版本不对"这个方向。

### 教训

**测写接口时，每首歌只能测一次。** like 是幂等的，一旦成功，后续所有
"像失败"的结论都会被污染。测试脚本必须记录哪些 id 被碰过，并用一首
全新的歌去测下一个变体。

---

## 19. v3.4.2 发布（2026-10-24）

524 的真正原因找到并修好了（见第 18 节）。这一版只改了一处根因：
登录管理器不再伪造 PC 客户端身份 cookie。

CI 五个 job 全绿，产物 6 个，prerelease 发布。

**发布后账号状态核对：** 调试期间我一共往账号里加了 14 首（探针歌 + 之前
误删的 `3440655478`）。全部清掉后，「我喜欢的音乐」回到 **202 首**，
与 2026-10-23 会话开始前用 `at` 时间戳判定的原始集合完全一致
（`session leftovers: []`，ids 端点也是 202）。

---

## 20. v3.4.3 缓存失效（2026-10-24）

用户反馈：**收藏了新歌，歌单内容不刷新**。

### 根因

缓存本身没坏（它的作用是别把服务器打爆），坏的是**写操作不失效缓存**。
一共 7 层缓存，写操作一次都没碰过它们：

| 层 | TTL | 存什么 | 写操作后失效？ |
|---|---|---|---|
| `_response_cache`（内存） | 60s | 每个 API 响应 | ❌ |
| `_playlist_cache`（内存） | 300s | `get_playlist_info` —— **整个歌单对象，含歌曲列表** | ❌ |
| disk `meta` `song:*` | 7d | 歌曲元数据 | ✅ 内容不变 |
| disk `meta` `playlist:*` | 7d | 歌单名称/封面 | ❌ 改名后旧名 |
| disk `playlist` `tracks:*` | 10min | 歌单歌曲列表 | ❌ |
| disk `playlist` `userplaylists:*` | 10min | 账号歌单列表（含曲目数） | ❌ |
| disk `lyric` | 30d | 歌词 | ✅ 内容不变 |

具体到用户的场景：点红心 → `set_song_like`（写，不走缓存）→
`get_liked_song_ids` 命中 60 秒响应缓存 → 「我喜欢的音乐」不显示新歌。
打开歌单时更糟：300 秒内存缓存里存的是**整个歌单对象**（含歌曲列表），
所以哪怕磁盘上 10 分钟的列表过期了，内存里那份 5 分钟前的快照还在，
旧列表又被端上来。

### 改法

**作用域（scope）**：每个缓存条目记住它**依赖什么**，这个依赖从请求本身
推导（`liked:<uid>` / `userplaylists:<uid>` / `playlist:<pid>`），不靠调用方
传参 —— 调用方不可能忘记传。

**失效**：`invalidate_liked()` / `invalidate_playlist()` / 
`invalidate_user_playlists()` 精确丢掉被写操作改坏的那部分。
所有写接口接上了：

| 写操作 | 失效什么 |
|---|---|
| 红心 / 取消红心 | liked + 「我喜欢的音乐」歌单 + 账号歌单列表 |
| 新建歌单 | 账号歌单列表 |
| 改名 / 改封面 | 该歌单 + 账号歌单列表 |
| 删除歌单 | 该歌单 + 账号歌单列表 |
| 加歌 / 删歌 | 该歌单（含内存快照） |
| 订阅 / 退订 | 该歌单 + 账号歌单列表 |

**内存缓存存整个歌单对象**这件事本身是坑：`get_playlist_info` 把
`tracks` 一起存了 300 秒。改成 `_fetch_playlist_detail`（只问服务器）
与 `get_playlist_info` / `get_playlist_songs`（各自缓存）分开，
歌曲列表不再从元数据缓存里读 —— 否则会把旧列表盖上**新**的时间戳，
让陈旧内容再活一个 TTL。

**手动刷新**：歌单列表标题右边加了一个刷新按钮，重新读当前列表，
跳过所有缓存。另外红心成功后立刻刷新屏幕上的列表（`liked_dirty`）。

### 验证

- **真实接口**（自己的账号）：点红心 → id 列表和「我喜欢的音乐」的
  歌曲列表**立刻**都出现新歌；测完还原，账号前后完全一致（204 → 204）
- **负向测试**：把修复在临时副本上拆掉，18 个新测试里 **11 个失败**
  —— 说明测试真的咬得住
- 全量：**585 passed, 6 skipped**，ruff 干净，`node -c` 干净
- **浏览器实测**（真实 index.html + stub 掉 pywebview 桥）：
  - 收藏后 → `set_favorite` 然后 `get_liked_tracks(true)`
  - 刷新按钮（歌单列表页）→ `get_playlists(true)`
  - 刷新按钮（我喜欢的音乐页）→ `get_liked_tracks(true)`
  - 刷新按钮（打开某个歌单）→ `get_playlist_tracks("42", true)`

### 发布

CI 五个 job 全绿（lint+tests / macOS arm64 / macOS x64 / Windows x64 /
Publish release），产物 6 个，prerelease 发布。

release notes 手工撰写（`--notes-file`），两块：修复 + 其它。

### 顺带清掉的一个坑

调试时我往真实 `src/gui/assets/index.html` 注入过浏览器测试用的 stub，
`git checkout` 把刷新按钮一起revert了，后来手动补回。**教训**：
浏览器测试的桩只放 scratch 目录的副本里，不要碰仓库里的真文件。

## 21. 收藏/退订失败：illegal request!（2026-10-25）

### 现象

收藏和**退订**歌单失败，提示「收藏失败：illegal request!」。退订别人的
歌单必现。仓库里搜不到 `illegal request!` 这个字符串 —— 是服务器返回的。

### 两个原因叠在一起

**原因一：路由形状错了。** 老代码一个路由走两个方向：

```python
# 改之前
PLAYLIST_SUBSCRIBE_ENDPOINT = "/api/playlist/subscribe"
payload = {"id": pid, "t": 1 if subscribe else 2}
```

实测（自己的账号，真歌单）：

| 形状 | 答案 |
|---|---|
| `/api/playlist/subscribe` + `t=1` | 200 |
| `/api/playlist/subscribe` + `t=2` | 403 illegal request! |
| `/api/playlist/unsubscribe` | 200 |

参考实现（`module/playlist_subscribe.js`）也是**按方向选路由**的：
`const path = query.t == 1 ? 'subscribe' : 'unsubscribe'`，payload 只有
`{id}`，没有 `t`。方向在路由里，不在参数里。

**原因二：写接口被风控门控。** 光把路由改对还不够 —— 同一个
`/api/playlist/unsubscribe`，不带身份还是 403。带 `os=pc` 就过了。

### 门控矩阵（2026-10-25 实测，每个都是真实请求）

| 路由 | 无身份 | 只带 os | os+appver+channel |
|---|---|---|---|
| `/api/playlist/subscribe` | **403** | 404（过了门） | 404 |
| `/api/playlist/unsubscribe` | **403** | 404（过了门） | 404 |
| `/api/playlist/update/name` | **403** | 404（过了门） | 404 |
| `/api/playlist/create` | **403** | 200 | 200 |
| `/api/batch`（内层 rename） | **内层 403** | 内层 404 | 内层 404 |
| `/api/playlist/remove` | 200 | 200 | — |
| `/api/playlist/manipulate/tracks` | 404 | **-462 风控** | 404 |
| `/api/playlist/cover/update` | 400 | 400 | 400 |

三条结论：

1. **门控是按路由的**，不是所有写接口都要身份。
2. **只有 `os` 能开门**：`appver` 单独带还是 403。
3. **身份要写完整**：`os` 单独带在 `manipulate/tracks` 上得到
   `-462 检测到您的网络环境存在风险`，什么都不带反而正常
   （404 = 过了门，只是歌单不存在）。

第 3 条后来在真歌单上又测了一遍（探针 20），结果更清楚：

| `manipulate/tracks`（真歌单） | 答案 |
|---|---|
| 只带 `os` | **-462 风控** |
| os+appver+channel | 200 |
| 完全不带身份 | 200 |

为什么？本项目的 eapi **header** 里带的是 `os=pc` **和**
`appver=8.9.70`。cookie 只写 `os` 就是在说「我是 pc 客户端，但我不说
版本」—— 和 header 对不上，看起来像伪造了一半的身份，风控就拦了。
写完整（和 header 说同一个客户端）就没事。

**这跟官方客户端的做法是同一回事**：它把整个 eapi header 当 cookie 发
（`createHeaderCookie(header)`）。本项目取 header 里和身份有关的那几项。

所以身份**只发给被门控的那几条路由**，而且**要写完整**。

### 官方客户端是怎么做的

`~/Documents/temp/NeteaseMusic.app`（3.1.9，Cef）的逆向报告
（`~/Documents/temp/Netease/API.md`）给出的答案和实测一致：

- 官方客户端**把整个 eapi header 当 cookie 发**
  （`createHeaderCookie(header)`），header 里就有 `os` / `appver` /
  `osver` / `channel` / `deviceId` / `requestId` / `__csrf`
- 写操作走 **weapi**，需要 `__csrf` 和登录 `MUSIC_U`
- eapi body 顶层强制追加 `os="OSX"` / `verifyId=1` / `deviceId`

参考实现（NeteaseMusic-API）也是同一套：`createHeaderCookie(header)` 把
header 逐字段拼成 cookie。本项目只取其中**能开门的最小那块**。

### /api/batch 的外层 200 是个谎

`/api/batch` 的外层 `code` **永远是 200**，里面的路由成没成都一样。
所以老代码的改名一直是坏的，只是没人发现：

```
outer code=200  inner code=403  inner msg=illegal request!
```

界面报「改名成功」，服务器上歌单名字没变。

### 改法

**身份按路由决定**，不靠每个调用点记得传：

```python
# src/core/api.py
IDENTITY_REQUIRED_ROUTES: frozenset[str] = frozenset({
    PLAYLIST_CREATE_ENDPOINT,
    PLAYLIST_UPDATE_ENDPOINT,
    PLAYLIST_SUBSCRIBE_ENDPOINT,
    PLAYLIST_UNSUBSCRIBE_ENDPOINT,
    "/api/batch",
})
```

`_eapi_once()` 里查这个集合决定要不要加 cookie。这样**新写方法不可能
忘记带身份**，也**不可能给不该带的路由乱带**。

身份写完整（`os` / `appver` / `channel`），和 eapi header 说同一个
客户端；而且**只在这一个请求上带**（`requests` 的 `cookies=` 参数，
合并进请求头但不写回 session jar）：

- 第 18 节说「session 里带 os+appver，like 接口会回 524」—— 那是
  **session jar** 的问题。现在身份是 per-request 的，like 路由不在集合
  里，永远不带身份，所以 524 的修复完好
- 登录时读进来的旧 cookie 里的 `os`/`appver`/`channel` 依然被丢掉
  （`apply_cookie_string()`），session jar 一直是干净的

**退订走自己的路由**，payload 只有 id，没有 `t`：

```python
if subscribe:
    endpoint = PLAYLIST_SUBSCRIBE_ENDPOINT
else:
    endpoint = PLAYLIST_UNSUBSCRIBE_ENDPOINT
payload = {"id": playlist_id}
```

**batch 看内层结果**：`_batch_result()` 把内层第一个失败挑出来当答案，
这样改名失败会真的报出来。

**文案分开**：退订失败不再显示「收藏失败」。i18n 补
`player.unsubscribe_failed`（中英各一份）。

### 验证

**真实账号闭环**（`8425278693`，别人创建的歌单，正是用户的操作），
身份带全套：

```
S1  订阅前:      subscribed
S2  订阅:        200（探针 17 用只带 os 也 200）
S3  订阅后:      subscribed
S4  退订:        code=200          ← 用户的那个操作
S5  退订后:      absent            ← 状态真的翻了
S6  再订阅:      （撞上 405 限流，见下）
S7  最终:        subscribed         ← 账号恢复原状
```

**账号最终状态没变**：`8425278693` 回到 subscribed（探针开始前就是
subscribed），收藏列表 183 → 184 → 183 没多没少，歌单总数 19 没变，
探针建的临时歌单已删干净。

**batch 内层**：真实歌单上实测 `outer 200 / inner 200`（带身份），
`outer 200 / inner 403`（不带身份）。

**create**：不带身份 403，带身份 200。

**like 没被带坏**：写操作带身份之后，like 路由（不带身份）仍然是 200，
而且那首「524 组」的歌**真的进了收藏列表**（183 → 184），unlike 之后
又真的出去了（184 → 183）。这是最尖的一次测试 —— 第 18 节那批歌就是
会 524 的。

**风险控制没被踩到**：真歌单上 `manipulate/tracks` 带全套身份 200，
不带身份 200，带半个身份 -462。所以那个路由不在集合里。

### 顺带发现的另一个坏：写接口的限流

探针 9 退订了 `8425278693` 之后，恢复一直撞 405
（`操作过于频繁，请稍后再试`），那个歌单在账号上少了将近一天。
写接口的 405 比读接口频繁得多 —— 探针间隔从 40 秒放到 80 秒才稳，
探针 22 打十几个写请求之后更是连着 405，退订成功之后**补订阅的那一步
差点没做上**（探针的退避最多到 450 秒，不够）。

**教训**：测写接口要留足退避，并且**每个探针都要能把账号还原回去**，
哪怕是靠另一个脚本兜底。

### 教训

- **`/api/batch` 的外层 code 不能信**。改批量接口时先看内层。
- **风控门控是按路由的**。给不需要身份的接口乱加身份，可能把
  「正常」变成「-462 风控」——比不加更糟。
- **测写接口要考虑幂等**：like 过一次的歌再 like 永远 200（第 18 节）。
  测完要还原。
- **身份 cookie 要和 eapi header 说同一个客户端**，不能只写一半。
  半截身份在 `manipulate/tracks` 上直接吃 -462。
- **写接口的限流比读接口狠**。连打十几个写请求就会被限流到 405，
  探针的退避要留够，而且必须留一条「把账号还原回去」的路。

## 22. 客户端身份与写请求节奏

### 起因

第 21 节修好了「收藏/退订报 illegal request!」，但 2026-10-25 下午
排查 405 的时候把用户的账号打进了风控：登录态失效、公网不可见、所有
设备被踢下线。

**先说清楚结论：405 不是身份形状不对，是打的次数太多。**

证据在同一批 `/api/batch` 内层路由、同一个身份、同一时刻：

```
batch rename   → outer 200  inner 200
batch desc     → outer 200  inner 405   ← 只有它被限流
batch privacy  → outer 200  inner 200
```

身份错了三个都得过不去。按路由计数才对 —— 探针打 `subscribe` 的次数
远多于 `unsubscribe`，所以 `subscribe` 405 而 `unsubscribe` 200。

**所以「把请求伪装得更像」不是防封的关键，节奏才是。** 身份该修还是修
（自相矛盾的身份是不必要的风险），但真正保命的是写请求别连发。

### 身份：按官方客户端改

用 IDA Professional 9.5 反编译官方客户端（`网易云音乐.app`
`Contents/MacOS/NeteaseMusic`），核对了报告里写的几处。地址与伪代码：

| 函数 | 地址 | 结论 |
|---|---|---|
| `+[YYYSystemInfoHelper systemInfo]` | `0x10018EC7C` | header 字典只有 7 个键 |
| `+[YYYSystemInfoHelper getUserAgent]` | `0x10018EE20` | UA 格式 |
| `+[CloudMusicCoSDK internalGenerateSerial:jsonDict:type:]` | `0x1006A00B0` | body 顶层追加三个字段 |
| `+[YYYRequestSecureHelper _secureParamStringWithURI:params:]` | `0x1001ACD20` | requestId 加在哪 |

**header 字典**（`systemInfo`）：`deviceId` / `osver` / `appver` /
`buildver` / `mobilename` / `os` / `brand`，其中 `os="osx"`、
`brand="Apple"` 是硬编码常量。

**body 顶层**（`internalGenerateSerial`）强制追加三个字段：
`os="OSX"`（大写，和 header 里的 `osx` 不是一回事）、
`verifyId=1`（NSNumber）、`deviceId`（CoSDK 的，和 header 里那个
**不是同一个值**，报告特别警告别混用）。

**requestId**：报告 §6.2 漏了它。反编译显示它是在
`_secureParamStringWithURI` 里加到 `systemInfo` 的**副本**上，所以最终
header 是 8 个键。值是每次请求新生成的 UUID。

**UA**：`... Chrome/120.0.0.0 Safari/537.36 NeteaseMusicDesktop/%@`，
参数是 `appver.buildver`。系统版本和 Chrome 版本在官方客户端里也是
硬编码的，跟真实机器无关。

**改之前的样子**：`os=pc` 配 `appver=8.9.70` —— **8.9.70 是安卓客户端
的版本号**，一个自称 Windows PC 的请求带着安卓发行号。它没炸（网易云
收下了），但那是个白送的矛盾。现在换成官方桌面客户端的身份：
`os=osx` / `appver=3.1.9` / `buildver=3364` / `brand=Apple`，
`mobilename` 读 `hw.model`（`Mac14,7`），UA 结尾同步。

**cookie 和 body header 同源**：`build_client_identity(header)` 直接从
那份 header 生成，所以两个地方不可能对不上。以前 cookie 是手写的
三个字段常量，跟 header 各写各的。

### 节奏：写请求串行 + 405 自动退避

`WRITE_ROUTES` 列出会改账号的路由（`/api/song/like`、`/api/playlist/*`
的写接口、`/api/batch` 等）。注意 `/api/song/like/get` 是**读**，不在
里面 —— 搞错了会让每次读收藏列表都排在写队列后面。

- **写请求排队**：`_write_lock` 保证同一时刻只有一个写在飞。第二个人
  的写请求排队等着，连退避等待也在队列里。
- **写请求之间有间隔**：`WRITE_MIN_INTERVAL`（0.4 秒）+ 抖动。不做成
  几秒级，因为写操作是用户点的，等太久体验就差了。
- **405 自动退避重试**：`RATE_LIMIT_BACKOFF` 是 3 → 8 → 15 → 30 → 60
  秒的阶梯，每挨一次拒绝对应一级，最多试 6 次。挨完就放弃，把 405
  原样返回给调用方。
- **退避是共享的**：一次 405 抬起的冷却对所有写请求生效，所以一个路由
  被拒了，其它路由也会慢下来。
- **读请求不受影响**：读照常走自己的节流（0.15 秒 + 抖动），不会被写
  的冷却挡住。读被限流了也只在自己这条路上退避，不牵连写。

### 验证

全部离线，没有碰任何账号：

- `tests/test_request_pacing.py`（新）：写请求不重叠（用假 session
  统计在飞数量）、读请求不被写的冷却挡住、405 会重试、退避阶梯逐级
  变长、共享冷却生效、阶梯走完会放弃、`403 illegal request!` 不被误判
  成限流、身份 cookie 仍与 header 一致。
- `tests/test_crypto.py`：header 的字段集、`os` 大小写两处不同、两个
  deviceId 不是同一个值、requestId 每次都新。
- `tests/test_playlist_write_routes.py`：身份 cookie 与 header 同源、
  身份不自相矛盾、身份按路由决定、session jar 保持干净。

`647 passed, 6 skipped`，Ruff 干净。

**测试自己也要验**：光看测试全绿不算数，得确认测试真的能抓住回归。
用「改坏代码，看测试会不会红」的方式逐个试过：

| 改坏的地方 | 被谁抓住 |
|---|---|
| 写请求不拿锁（会重叠） | `test_two_writes_never_overlap` |
| 405 不抬冷却 | `test_a_rate_limited_write_is_retried_after_a_wait` |
| 写请求的间隔没了 | `test_writes_are_spaced_out` |
| 冷却不起作用 | `test_the_next_write_waits_for_the_cooldown` |
| 读请求也抬写冷却 | `test_a_read_does_not_raise_the_write_cooldown` |
| 把 403 当成限流 | `test_another_refusal_is_not_a_rate_limit` |
| 写请求无限重试 | `test_a_refusal_is_not_retried_forever` |
| 任何 code 都当限流 | 5 个测试 |
| `appver` 换回安卓号 | `test_the_identity_does_not_contradict_itself` |
| `os` 换回 pc | 4 个测试 |
| body 的 `os` 变小写 | 2 个测试 |
| 少掉 `verifyId` | `test_the_header_and_the_body_state_the_same_client` |
| 两个 deviceId 变成一个 | `test_the_two_device_ids_are_different` |
| 丢掉 `brand` | `test_client_header_defaults_and_overrides` |

备份功能那边也照样试过：去掉「没决定就不写」的闸门、去掉「已有的收藏
不重复加」、去掉「别人建的歌单跳过」、去掉同名冲突检测、去掉分批 ——
五处全被测试抓住。

**第一版的节流测试是废的**：它只断言「等过」，而通用节流（每请求
0.15 秒）自己就满足了这个断言，所以把写队列整个拆掉测试还是绿的。
现在的测试断言的是**具体的那次等待**（退避阶梯的哪一级、写间隔、
共享冷却），并且把通用节流也 stub 掉，逼着等待只能来自被测的那段
代码。

教训：**测试绿了不代表它在测东西。** 改坏一次看看它会不会红。

### 教训

- **探针打的是真账号，代价是真的。** 这次把账号打进了风控，所有设备
  被踢下线。以后测写接口只用假 session，真实账号只留给用户自己手动
  验。
- **先读完逆向文档再设计探针。** 报告 §5.4/§6.2 早写清楚了 body 顶层
  追加三个字段这件事，我先探了三个小时才发现。
- **报告也会有不准确的地方**，用 IDA 核一遍比信报告快。§6.2 漏了
  requestId 就是这么发现的。
- **「同一个错误码」不一定同一个原因。** 405 有两种来源：打太快，和
  身份不对。分清它们靠的是「同一批请求里只有一路被拒」这个细节。
- **退避要留够，而且必须留一条「把账号还原回去」的路。** 探针 9 退订了
  用户的歌单，恢复脚本撞 405 差点没还原回去。


## 23. 歌词自动滚动：居中、手动接管、空闲回弹（2026-10-25）

### 原来的毛病

`app.js` 里旧的做法是这样：

```js
['wheel', 'touchstart', 'mousedown'].forEach((name) => {
    lyricsBox.addEventListener(name, () => {
        player.autoScroll = false;
        player.lastScrollAt = Date.now();
    });
});
lyricsBox.addEventListener('scroll', () => {
    if (!player.autoScroll && Date.now() - player.lastScrollAt > 4000) {
        player.autoScroll = true;
    }
});
```

空闲判断写在 `scroll` 回调里。`scroll` 事件只在**列表正在动**的时候触发，用户
停手之后就不会再有事件，那句 `Date.now() - player.lastScrollAt > 4000` 永远得不到
重新求值 —— 所以「自动回弹」在旧代码里实际上是失效的：手动滚走之后，歌词再也回
不到正在唱的那一行。`player.autoScroll` 也就此变成一个只能被置 false 的单向开关。

另外旧代码绑的是 `mousedown`：在歌词面板里点一下鼠标（哪怕只是想拖选一段歌词）
也会把自动跟随关掉。

### 改法

把「谁在驱动列表」这件事变成显式的状态机，每个歌词列表一份：

| 字段 | 意思 |
|------|------|
| `following` | 列表是否跟着歌曲走 |
| `idleTimer` | 待执行的「回到正在播放的那一行」定时器 |
| `autoScrollUntil` | 这段时间内的 `scroll` 事件是面板自己造成的 |
| `refocus` | 把正在播放的那一行放回中间 |

- **接管**：`wheel` / `touchmove` / 用户造成的 `scroll` → `following = false`，
  同时挂一个 `setTimeout`。
- **回弹**：定时器到期 → `following = true` → 调用 `refocus()` 重新定位。**这个
  定时器是回弹唯一的驱动力**，不依赖任何 `scroll` 事件，所以用户停手之后照样
  会回弹。
- **不误判**：面板自己滚动前先 `markLyricsAutoScroll(box)`，记一个 120 ms 的
  窗口，窗口内的 `scroll` 事件不算用户操作。
- **动画让位**：`smoothScrollTo` 的每一步都检查 `following`，用户中途接手，正在
  跑的滑动立刻停。
- **重建后复位**：歌词重新渲染（`renderLyricLines` / `initMobileLyricsRenderer`）
  会清掉旧的 DOM，滚动位置随之消失，所以这时要 `resetLyricsList(box)` 让列表
  重新跟随歌曲。

两个歌词面板（播放器旁边的 `lyrics-scroll` 和窄窗口下全屏的 `npm-lyrics`）各
有一份独立状态，互不干扰。

### 设置项

`playback.lyrics_scroll_idle_seconds`，单位秒，默认 4，取值 1–30。走的是现成的
链路：`Settings` → `GuiBridge.get_settings()` → `state.settings` → 设置面板
→ `update_settings('playback', ...)`，没有另起一套。

| 位置 | 内容 |
|------|------|
| `src/config/settings.py` | `PlaybackSettings.lyrics_scroll_idle_seconds`，`_normalize()` 里夹到 1–30 |
| `src/gui/bridge.py` | `get_settings()` 带上它，`_clean_playback()` 放行它 |
| `src/gui/assets/app.js` | `player.lyricsScrollIdleSeconds` 运行时值、`lyricsIdleMs()`、设置面板的滑块 |
| `src/gui/assets/index.html` | 设置 → 播放 → 歌词滚动 → 回弹等待 |
| `src/i18n/zh_cn.json` / `en_us.json` | `settings.lyrics_scroll` / `_idle` / `_idle_hint` |

界面上是 1–30 秒的滑块，旁边显示当前秒数；改完立刻生效（正在等待回弹的列表会
按新的间隔重新计时），保存后写进 `settings.yaml`。

### 验证

- `node scripts/check-lyrics-scroll.mjs` —— 15 项全过。这个脚本用假时钟加载
  真的 `app.js`，把「用户滚动 → 接管 → 定时器回弹」整条路走一遍。
- **变异测试**（改了实现看测试会不会红），6 处变异全部被抓住：

  | 变异 | 被哪条抓住 |
  |------|-----------|
  | 回弹改回由 `scroll` 事件驱动 | the panel returns to the playing line on its own |
  | 用户滚动后歌曲仍然把列表拉回去 | the song does not pull the panel back while the user reads |
  | 认不出面板自己的滚动 | the panel's own scroll is not read as a takeover |
  | 空闲间隔写死成 4 秒 | the configured delay is used |
  | 改设置不重新计时 | changing the setting re-arms the pending return |
  | 不标记面板自己的滚动 | the panel's own scroll is not read as a takeover |

- `pytest` 659 passed, 6 skipped（新增 `tests/test_lyrics_layout.py` 7 条、
  `tests/test_settings.py` 3 条、`tests/test_bridge.py` 2 条）。
- `ruff check .` All checks passed；`python3 main.py --self-test` result: OK。

### 没做的事

- **没有动 `src/gui/assets/desktop-lyrics.js`**：那是独立的桌面歌词浮窗，没有
  自己的滚动状态机（`grep -n "scroll\|autoScroll\|lastScrollAt\|4000"` 只命中
  一句说明文字），改它没有意义。
- **没有加「0 = 永不回弹」**：回弹是这次需求的核心，滑块下限取 1 秒。
- **`npm-lyrics` 的回弹只在窄窗口下生效**：全屏播放视图只在移动端布局出现，
  桌面端那个容器是空的。

## 24. 歌手主页与关注（2026-10-26）

### 顺手修掉的一个旧 bug

歌手页一直显示「热门单曲 0 首」。原因不在前端，在接口形状：
`/api/v1/artist/{id}` 的返回是

```
{ "artist": {...}, "hotSongs": [...50], "code": 200, "more": true }
```

`hotSongs` 是 `artist` 的**兄弟节点**，不是它的字段。旧代码读的是
`artist.get("hotSongs")`，永远拿到空列表 —— 不报错，只是什么都没有。
`get_artist_detail()` 现在把歌曲折回 `artist` 里再返回，调用方不用再关心这个
分层。

### 新增的接口（全部 2026-10-26 实测）

| 用途 | 路由 | 要点 |
|------|------|------|
| 歌手自己的全部歌曲 | `/api/v1/artist/songs` | `id`/`limit`/`offset`/`total=true`，返回 `total` + `more`；周杰伦 total=580 |
| 歌手专辑 | `/api/artist/albums/<id>` | 路径式；扁平的 `/api/artist/album` 恒返回 `400 参数错误` |
| 关注状态 | `/api/artist/follow/count/get` | `isFollow` / `fansCnt` / `followDayCnt` |
| 关注 / 取关 | `/api/artist/sub`、`/api/artist/unsub` | 两个方向是**两条路由**，不是 `t` 字段 |
| 已关注歌手 | `/api/artist/sublist` | `data` / `count` / `hasMore`；未登录返回 `code 301` |

### 两个容易踩的点

**参数名是 `artistId`，不是 `id`。** 传 `id` 得到的是 `400 参数错误`，看起来像请求
格式坏了，实际上只是字段名不对。匿名探测时 `artistId` 会得到 `301 未登录` ——
这才说明路由和参数都对，只差登录态。

**`/api/v1/artist/songs` 的歌曲字段名跟别处不一样。** 歌单和搜索接口用
`ar` / `al` / `dt`，这个接口用 `artists` / `album` / `duration`。所以
`_track_payload()` 和新加的 `Controller._track()` 都同时认两种拼法，否则歌手页
的每一行都会是「未知歌手」。

### 写请求没有自动验证

关注/取关是**写操作**。之前用真实账号跑自动写探测，把账号打进了风控状态（第 22
节），所以这次的规矩是：写路由只做匿名探测确认「路由存在、参数名正确」，真正的
关注/取关由人手动点一次确认。单元测试用假的传输层覆盖成功、限流、未登录、异常
四种返回。

匿名探测结果（不带 MUSIC_U，不可能改动任何数据）：

```
/api/artist/sub    -> code=301 未登录
/api/artist/unsub  -> code=301 未登录
```

### 关注状态实测

拿已关注列表里的歌手和未关注的歌手各验一次，确认按钮不会永远显示「关注」：

```
win7mus            follow_state=True   artist_page.followed=True    fans=279
封茗囧菌            follow_state=True   artist_page.followed=True    fans=2599437
周杰伦（未关注）     follow_state=False  artist_page.followed=False   fans=19139626
```

## 25. Android 版（2026-10-26）

### 为什么不是套壳

桌面端是 pywebview：Python 后端 + HTML/CSS/JS 前端，靠 `window.pywebview.api`
对话。Android 上没有 pywebview，把整个 Python 运行时塞进 WebView 壳的代价是
APK 40MB 起、内存翻倍，而且 HTML 布局是照着鼠标和 1080px 宽写的，手机上要重做
一遍才勉强能用。

实际做法是**共用业务核心、分开表现层**：

- `src/core`、`src/auth`、`src/config`、`src/i18n` 共约 12400 行，**零 webview
  依赖**（核对过：没有 `import webview`，没有 `from src.gui`），原样跑在
  Android 的 Python 3.11 上；
- 表现层是 Kivy 原生控件树，`android/ncmkivy/`；
- `controller.py` 是唯一的分界线：Kivy 永远不直接碰 `NeteaseAPI`。

完整设计见 `android/ARCHITECTURE.md`。

### 音频怎么播

Kivy 自带的 SDL2_mixer 播不了 HTTP 流，而网易云 CDN 要 `Referer`。所以：

- `MediaServer` 原样复用（它本来就在 `127.0.0.1` 上提供带 Range 的代理）；
- `android.media.MediaPlayer`（pyjnius 调）直接吃那个 loopback URL，不经过
  WebView，所以没有 CORS、`Referer`、沙箱 origin 这些问题。

### 尺寸分档

按**可用宽度**（dp）分三档，不按设备型号，因为手机横屏会跨档、折叠屏会变宽：

| 档 | 宽度 | 布局 |
|----|------|------|
| compact | < 600dp | 单栏，底部导航 |
| medium | 600–839dp | 底部导航，列表与详情并排 |
| expanded | ≥ 840dp | 侧边导航 + 列表 + 详情 |

`check_screens.py` 把 8 个屏幕在 5 种宽度（360/411/600/840/1280dp）下各建一遍，
逐个检查子控件有没有横向溢出的父容器——**这是「不要有显示不下的 UI」的机器判据**，
不靠肉眼看某台机器。

### 没做完 / 没验证的

- **APK 已经打出来并在模拟器上跑过**，见第 27 节；真机播放和锁屏控制仍然没有实测
  数据。
- **Android 的本地音乐要走 MediaStore**，`LocalMusicLibrary` 的目录扫描在
  Android 上不能直接用（scoped storage），需要一条适配分支。
- **KivyMD 没用。** 它依赖 pillow，跟「去掉 PIL 瘦身」的目标直接冲突；所以
  Material 3 的主题层是手写的，图标是 canvas 画的简笔，不是 Material 图标库。
- **界面在模拟器上逐屏看过，但没有在真机小屏上验证过**（模拟器是 Pixel 6 模板，
  411dp 宽）。

## 26. 卸掉 Pillow：安装包 38 MB → 26 MB（2026-10-26）

### 起因

`requirements.txt` 里 `qrcode[pil]` 的 `[pil]` 把 Pillow 拖进了运行时依赖。
Pillow 自己不说，它还带进来一堆 dylib（libavif / libbrotli* / libharfbuzz /
libjpeg / libpng / libwebp* / libfreetype ...）。PIL 在运行时只干三件事：

| 位置 | 用途 |
|------|------|
| `src/auth/login.py` `get_qrcode_image()` | 把登录二维码 URL 画成 PNG |
| `src/config/settings.py` `_write_background()` | 壁纸缩到 ≤3840 px 存成 JPEG |
| `src/core/mediaserver.py` `scaled_background()` | 壁纸再缩到 ≤1920 px 给 WebView |

### 改法

- **二维码：自己写 PNG。** `qrcode` 的 `QRCode.get_matrix()` 直接给布尔矩阵，
  `src/core/png.py` 用 `zlib` + `struct` 拼 IHDR/IDAT/IEND 输出 8-bit 灰度 PNG
  （约 90 行，纯标准库）。`get_qrcode_image()` 的返回类型、`data:image/png;base64,`
  这条契约、前端 `<img>` 一行都没动。每个模块给同样的像素数（不再像以前那样
  LANCZOS 缩到 256），扫码反而更清楚。
- **壁纸：交给系统自带的工具。** 新增 `src/core/imaging.py`：
  - macOS → `/usr/bin/sips`（`-z` 精确重采样 + `-s format jpeg`）；
  - Windows → Windows PowerShell 5.1 + `System.Drawing`（脚本写临时 .ps1 再 `-File`
    跑，避开任意路径的引号地狱）；
  - 都没有 / 工具失败 → `downscale_to_jpeg()` 返回 False，调用方沿用原来的降级：
    存原文件、`scaled_background` 返回 None（`/bg` 直接发原图）。
  **注意 `sips -Z` 会把小图放大**（800x600 会被撑到 1920x1440），所以尺寸先在
  `target_size()` 里算好，只传 `-z`，且只缩不放。
- `requirements.txt`：`qrcode[pil]` → `qrcode`。`pillow` 留在 requirements-dev
  （`scripts/make_icon.py` 和壁纸测试的 fixture 要用），它不进包。
- `main.spec`：hiddenimports 去掉 `PIL` / `PIL.Image` / `qrcode.image.pil`，
  并把 `PIL`、`qrcode.image.pil`、`qrcode.image.styles.moduledrawers.pil` 等写进
  `excludes`。**这一步是必须的**：`qrcode/image/styles/moduledrawers/__init__.py`
  在 try/except 里 import 了 `.pil`，PyInstaller 静态分析照样会收进去；光把 Pillow
  从环境里卸掉不够。
- `main.py --self-test`：qrcode 那一项改成真的画一张 PNG（不再只是 `import qrcode`），
  并新增 `image scaling` 一项——冻结包里实际跑一次平台缩放，macOS runner 验 sips、
  Windows runner 验 PowerShell，两边各自证明。

### 验证

- `du -sh dist/NeteaseMusicDownloader.app`：**38 MB → 26 MB**（−12 MB，−32%）。
  包里 `find -iname "*PIL*"` 为空；libjpeg/libwebp/libavif/libfreetype 也都不在了。
- 体积是在 **venv 里装着 pillow** 的情况下量出来的（CI 就是这条路径：requirements-dev
  装了 pillow 再打包），说明 main.spec 的 excludes 真拦住了，不是靠卸包凑出来的。
- 冻结产物 `--self-test`：`qrcode: 288B png, no imaging library`、
  `image scaling: 5155B jpeg`，`result: OK`。
- 新二维码不是"看着像"：用 macOS CoreImage 的 `CIDetectorTypeQRCode` 解回来，
  拿到的正是原 URL（`tests/test_qrcode_image.py::test_the_qr_code_decodes_back_to_the_url`）。
- `pytest -q` 全绿（含新增 `tests/test_qrcode_image.py`、`tests/test_imaging.py`、
  `tests/test_runtime_deps.py`）；把 pillow 从 venv 卸掉后再跑一遍，同样全绿。

### 没做的 / 需要知道的

- **libcrypto.3.dylib 4.6 MB + libssl.3.dylib 848 KB 还在包里。** 它们不是 Pillow
  独有的：`requests` → `urllib3` → `ssl` 这条链要 `_ssl` 扩展，`_ssl` 就链着 OpenSSL。
  除非把 HTTPS 换成别的实现，这部分动不了。（`main.spec` 里对 `cryptography` 的
  exclude 仍然有效——那是 pywebview 可选的 HTTPS server 路径。）
- **Windows 那条 PowerShell 路径本机没法实测**（只有 macOS）。它被设计成失败即降级
  （存原图、壁纸照常显示），并且 `--self-test` 的 `image scaling` 会在 Windows CI
  上真跑一次；但真机上第一次缩放值得手动看一眼。
- **两边认的格式不一样。** macOS 的 sips 本机实测能读 png/jpg/webp/bmp（HEIC 没测）；
  Windows 的 `System.Drawing` 走 GDI+，只认 BMP/GIF/JPEG/PNG/TIFF，WebP/HEIC 壁纸
  会走降级：原文件照原样复制成 `background.jpg`（浏览器按内容嗅探，仍然能显示），
  不吃缩放这层优化。

## 27. Android 上真机（模拟器）跑起来之后（2026-10-27）

APK 打出来了：`neteasemusic-3.5.1-arm64-v8a-debug.apk`，22.3 MB，装在 Android 14
arm64 模拟器（Pixel 6 模板，411dp 宽）上，首页、搜索、歌手主页、播放条、扫码登录
都逐个点过。这一节记的是几个不看源码就会重踩的坑。

### 原生 Toast：两个坑叠在一起

Kivy 2.3 里已经没有 `kivy.uix.toast`，所以走 jnius 调 `android.widget.Toast`。
第一版报 `No static methods called makeText in android/widget/Toast matching your
arguments`：jnius 是按参数类型打分的，Python 的 `str` 只认 `java/lang/String`，
而 `makeText` 要的是 `CharSequence`，于是直接被判 -1，一个重载都不剩。改成传
`java.lang.String` 对象就过了（JavaObject 对任何 `L...;` 形参都接受）。

第二版报 `Can't toast on a thread that has not called Looper.prepare()`：
`makeText` 自己也要 Looper，所以不能只把 `show()` 扔到 UI 线程，**建对象和显示
都得在 UI 线程**。

### 封面：模拟器直连 CDN 会超时

模拟器访问 `p2.music.126.net` 会读超时。封面现在统一走本机媒体服务器代理
（`/m/<token>/cover?u=...`），代理自己下载时在 p1/p2/p3 之间轮换重试，缓存键仍
用原始 URL。请求头带 `Referer: https://music.163.com/`，Kivy 直连是拿不到的。

### 播放条

`MiniPlayer` 默认高度 0，播起来才撑到 64dp。加在 root 上，位置在底部导航之上。

调试时走了弯路：低分辨率截图里它看着像没渲染，其实几何完全正确（root 的三个
子控件高度 168 / 168 / 1873，都在窗口 2400 之内）。**Kivy 的 px 在 Android 上
等于设备 px，`dp()` 要乘 density（模拟器 2.625），拿窗口高度除密度会算错一大截。**

### 没做的 / 还没验证的

- **只有 debug APK，没有正式签名**，CI 也没有 Android 的 release 资产流程。
  签名密钥怎么管还没定。
- **真机播放没验过。** 模拟器上未登录时部分歌拿不到播放地址
  （`copyright_unavailable` / `song_not_found`），登录后的真实解码、音频路由、
  后台播放、锁屏控制都还是空白。
- **本地音乐在 Android 上没通**：scoped storage 下 `LocalMusicLibrary` 的目录扫描
  直接用不了，要走 MediaStore。
- **字体缺字。** 系统字体对生僻字符会出豆腐块（歌手名里的 `JA□-Z`）。

## 28. 备份补全：剩下的收藏面和云端播放次数（2026-10-27）

`.ncmbk` 升到 3 版，把账号能迁移的东西补齐。

2 版已经有的：账号资料、收藏的歌曲、所有歌单（自己建的 + 订阅的）和歌单里的歌、
关注的歌手、本机的播放次数 / 收藏 / 最近播放。

这次补的是**剩下的收藏面**，之前一节都没有：

| 一节 | 接口 | 备份里的键 |
|------|------|-----------|
| 收藏的专辑 | `/api/album/sublist` | `liked_albums` |
| 订阅的电台 | `/api/djradio/get/subed` | `subscribed_djradios` |
| 收藏的 MV | `/api/cloudvideo/allvideo/sublist` | `liked_mvs` |
| 云端听歌排行 | `/api/v1/play/record` | `play_record` |

都是 eapi 路由，真实账号实测（2026-10-27）：专辑 2 张、电台 3 个、MV 1 个、
听歌排行 100 条；整次导出 4.4 秒，`errors` 为空。歌单 9 个 / 1749 首、
收藏 184 首、关注的歌手 20 位照旧。

### 三个容易踩的地方

- **MV 的 id 和名字不叫 id/name**，是 `vid` 和 `title`。导出时认错键，
  这一节就会变成 0 条还不报错。
- **听歌排行只有前 100 首**。接口只收 `{uid, type}`，没有翻页参数，
  也没有 `more`，100 条就是全部。
- **云端的播放次数只写本地**：键是 `online:<歌曲 id>`（和本机计数器一样），
  导入时两数取大的那个，**不会向网易云报告**。云端本来就有这份数据，
  本地记一份只是为了让「播放次数」换机之后也还在。

### 没做的 / 不在范围内的

- **云盘（自己上传的歌）不在备份里。** `.ncmbk` 是 JSON，带不了音频；
  重新上传属于写操作，也不该由备份导入悄悄做掉。
- **动态、评论、评论点赞不在备份里**，这些内容没法迁到别的账号。
- **导入是写操作**：收藏专辑、订阅电台、收藏 MV 都会真的动账号。
  备份模块只提供方法，由用户在界面里点确认，程序自己不会去碰。

### 测的时候踩的一个坑

实测时先用了错的那个 uid（8425278693），歌单接口返回 `code 200` 但列表是空的，
看着像代码坏了。这个号有 184 首收藏、20 位关注的歌手，但没有歌单；
真正在用的号是 9641982263，9 个歌单都在它下面。**两个号指向同一个人的数据**，
但只有后者能读到歌单。

## 29. APK 的版本号跟 src/version.py（2026-10-27）

buildozer spec 里也写了一份版本号，两份会漂：APK 文件名还叫 3.5.1，app 自己
报的是 3.5.4，manifest 的 versionName（系统里「应用信息」显示的那个）也是
3.5.1——三处三个答案。

现在 `android/build_apk.sh` 从 `src/version.py` 读版本号，用
`buildozer --version` 传进去，spec 里那份只是留个底。改版本只改一处。

## 30. 播放条没播东西时也是隐形的（2026-10-27）

播放条高度为 0 的时候，**Kivy 照样会画它的子控件**：两个图标按钮是固定
44dp，容器又不裁剪，于是没播任何东西的时候，底部导航上方就浮着两个孤零零
的 ▶ ▶▶。

改成播放条一开始 `opacity = 0`，真的开始播才置回 1。

顺带记一笔：`buildozer --version` 不是设置应用版本号，是打印 buildozer 自己的
版本然后退出。脚本原来拿它当版本号传，结果构建**静默空跑**：退出码 0，什么
都没构建。现在版本从 `src/version.py` 读出来写进 spec，并且构建完必须看到
对应版本的 APK 才算成功。

## 31. 小屏幕的排版，和 Windows 换版本后残留的旧资源（2026-10-27）

两件事，都是在 Windows 上试出来的。

### 窄窗口把左边那一栏顶到上面

原来的 `@media (max-width: 1024px)` 把下载页和播放页的两栏折成一栏，
音源列表（左边那一栏）被顶到内容上面，变成从上到下的纵向排列。
1366×768 配 150% 缩放的机器，逻辑屏只有 911×512，正好落进这个区间，
界面于是看着像坏了：显示不全，播放条也够不着。

两栏自己就放得下（280 + 320 = 600），所以把折行留给手机档（≤720px），
桌面档一直保持两栏。顺带补了三处 `min-width: 0`：顶栏、`.app-main`、
`.view` 都是 flex item，默认的 `min-width: auto` 等于内容宽度；
顶栏内容一宽（用户名 + 按钮 221px），整个 body 就被撑宽，右边的按钮跑出屏幕。

改完在 1280 / 1024 / 911 / 860 / 800 / 721 / 720 / 640 / 560 / 480
十个宽度上量过：scrollWidth 都不超过视口，721px 以上两栏保持，
720px 以下才进手机布局。

### 窗口本身比屏幕还大

`MIN_SIZE = (780, 560)` 是硬下限，而 `_initial_geometry` 写的是
`max(MIN_SIZE[0], min(width, screen * 0.92))` —— 屏幕不够大的时候，
下限反而把窗口撑到屏幕外：911×512 的逻辑屏上，窗口最小 560 高，
直接顶出屏幕，底部播放条被切掉。

改成屏幕说了算：`_fit_to_screen()` 先按屏幕夹，夹完再对下限取下限；
`min_size` 也按屏幕收（`_window_min_size()`）。屏幕实在太小，
窗口下限跟着降到 `FLOOR_SIZE`（360×300）。

### 换版本后 exe 换了，旁边的资源还是旧的

Windows 的包是 onefile：界面打成一个 zip 塞在 exe 里，启动时
`ensure_resources()` 把它解到 exe 旁边的 `_resources/`（WebView2 不能从
onefile 的临时目录读文件）。原来只检查「文件在不在」就复用，
所以换新 exe 之后，旁边那套旧 CSS/JS 还在 —— 新 Python 配旧界面。

现在按**内容摘要**判断：解包时把资源 zip 的 sha256 写进 `_resources/`
里的 `.ncm-resources.digest`，每次启动拿当前 exe 自带的 zip 摘要比对，
不一致就重新解包（整个目录换掉，不是合并）。没有摘要文件的旧安装
（也就是所有老版本）第一次启动会重解一遍。

判据用摘要而不是版本号：版本号要人肉维护，而且同一份源码重新构建一次
就会看起来像「更新了」。

## 32. Android 版的视觉层重做（2026-10-27）

之前的 Android 界面是「能跑」的骨架：平涂底色、没有卡片层级、底部导航
只有一排图标没有选中态、字号只有一个档位、空状态飘着一行字。功能链路都在，
视觉层等于没做。

这次按 Material 3 + 液态玻璃重做，气质跟桌面对齐：

- **表面层**：加了 surface_container_lowest / low / highest 三级表面色，
  外加一组玻璃色（半透明面板 + 亮边）。卡片是「色调表面 + 一根发丝描边」，
  不再是平涂。
- **组件**：新增 `SurfaceCard`（圆角面板）、`NavItem`（带胶囊指示器和
  文字标签的导航项）、`AppButton`（填充 / tonal / 描边 / 文字四种变体）。
  `IconButton` 有了按压状态层（12% 的标签色叠加）和 tonal / filled 底衬，
  还能按比例放大字形；`TrackRow` 有了按压态和行间分隔线；`EmptyState`
  变成「图标底衬 + 标题 + 说明 + 一个按钮」。
- **排版**：页面标题 headline_medium（26sp），分区标题 title_large（20sp），
  列表主标题 title_medium（16sp），次要文字 body_small（12sp），
  导航标签 label_small（11sp）。
- **导航**：底栏 64dp → 72dp，选中项有胶囊指示器，图标和标签一起加深。

踩到的坑：

1. **Kivy 里颜色不是图形对象的属性。** `RoundedRectangle` 没有 `.color`，
   颜色是单独的 `Color` 指令。改颜色只能重画（`canvas.before.clear()`
   之后重新画一遍），不能就地改属性。
2. **类名会撞 Kivy 自带的控件。** 原来叫 `ActionButton`，Kivy 自己就有一个
   `ActionButton`，它的 KV 样式按类名匹配，一命名就抛 `BuilderException`。
   改名 `AppButton`。
3. **构造期就会重绘。** `IconButton` 的 `on_size` 可能在 `__init__` 结束前
   就触发，所以 `variant` / `glyph_scale` / `_pressed` 都得有类级默认值，
   否则 `AttributeError`。
4. **ScrollView 只能有一个子控件。** 歌单页把 `LoadingView` 直接加到
   `TrackList`（一个 ScrollView）上，真机上会直接崩。改成
   `TrackList.show_loading()`，占位进到行列表所在的那个 box 里。

顺带修了两个既有问题：`check_screens.py` 给每个工厂都传了 artist_id 和
playlist_id（Kivy 拒绝未知属性），以及上面第 4 条那个崩溃。

无头布局检查：5 个宽度 × 8 个页面，无横向溢出。

## 33. Android 的本地音乐走 MediaStore（2026-10-27）

Android 版「本地音乐」一直是空的，原因不是权限也不是分区存储，是接口
对不上：`controller.local_tracks()` 调 `self.local_music.get_tracks(...)`，
而 `LocalMusicLibrary` 根本没有这个方法，异常被 `except Exception` 吞掉，
页面就永远拿到空列表。

顺带一个更根本的问题：Android 10 以后应用不能直接读 /sdcard/Music，
桌面的目录扫描在手机上本来也找不到东西。所以 Android 侧改用 MediaStore ——
系统自己维护的媒体索引，按内容 URI 读。

`android/ncmkivy/mediastore.py`：`MediaStoreLibrary` 提供跟桌面
`LocalMusicLibrary` 相同的三个方法（`tracks()` / `track()` / `get_tracks()`），
控制器按平台选一个，上层不用关心用的是哪个。

查询分两截：`_query()` 负责碰 Java（jnius + activity 的 content resolver），
`_read()` 只吃 resolver / audio / ContentUris 三个对象，用假 cursor 就能测。
没有权限或者查询失败时返回空列表并记一条 warning，页面显示空状态，不崩。

`tests/test_android_media.py` 7 项：字段映射、空库、查询打到 audio 集合、
按标题/歌手/专辑搜索、分页窗口、按 path 和 key 找单曲、桌面不是 Android。

真机上的 MediaStore 查询没验过（模拟器已关，用户明天自己测）。

## 34. Android 的配色从 M3 默认紫换成品牌红（2026-10-27）

`theme.py` 的 `PALETTES` 一直是 Material 3 的默认基线（`#6750A4` 紫），
所以 Android 版看起来不像网易云。设计师的规格（`android/UI_SPEC.md`）
给了一套向品牌收敛的配色，两套方案都算了对比度：

- **primary 用加深的 `#C4302B`**，不是桌面那个 `#EC4141`。白字压在原品牌红上
  只有 3.89:1，不够正文用；加深后 5.52:1，过 AA。`#EC4141` 保留成 `brand`
  token，只给大面积图形、选中高光、封面占位用，不当文字色。
- **error 不用纯红** `#8C1D18`，否则跟 primary 撞色、分不清哪个是错误。
- 补了 surface_container_lowest / low / highest 三级表面（卡片要有地方待），
  以及 inverse_* 三个（Toast 用）。

规格文件里带了 `brand` 这个新字段，已经加进 `Palette`。

对比度是规格那边用 Python 逐对算过的（`#C4302B` 白字 5.52、`#201A19` 压
`#FFF8F7` 16.37 这类），我这边没有重新算，只按值填进去、跑测试和布局检查。

## 35. 声明了权限不等于拿到了权限（2026-10-27）

最后一块：`READ_MEDIA_AUDIO` 在 `buildozer.spec` 的 permissions 里声明了，
APK 的 AndroidManifest 也确实带着它，但代码里没有任何地方在运行时申请。
Android 6 以后声明和授予是两回事 —— 没授予时 MediaStore 只返回应用自己
创建的文件，手机里塞满歌，本地音乐页还是空的。

`android/ncmkivy/permissions.py`：

- `audio_permission()` 按 `Build.VERSION.SDK_INT` 选。33（Android 13）起
  `READ_EXTERNAL_STORAGE` 不再覆盖音频，改名 `READ_MEDIA_AUDIO`；拿旧名字
  去申请在 13+ 上是白申请。
- `request_audio_permission(callback)` 先 `check_permission`，已经给了就直接
  回调；否则 `request_permissions` 并等结果。回调在 Java 线程上来，所以
  `app.py` 里用 `Clock.schedule_once` 跳回 Kivy 线程再刷新媒体库。
- 非 Android 环境三个函数都是 no-op，桌面跑测试不会因为缺 jnius 炸。

`tests/test_android_media.py` 三顶：两档 SDK 各自对应正确的权限名、非 Android
申请返回 UNAVAILABLE、非 Android 检查恒 False。

## 36. 图标改成固定 24dp 网格，描边宽度改回 dp（2026-10-27）

视觉规格里有两条是算出来的，不是审美问题：

1. **`Line(width=)` 的单位是像素，不是 dp。** 原来的图标有一半描边写的是
   `width=2` / `width=2.2`，在 2.625 倍密度的屏上就是 0.76dp——比发丝还细。
   全部改成 `dp(2)` / `dp(2.2)` / `dp(2.4)`。
2. **图标尺寸原本跟着按钮缩放**（`min(按钮宽, 按钮高) * glyph_scale`）。
   48dp 的按钮画出来的图标只有 17dp，不同图标之间视觉重量还不一样。
   现在图标固定在 24dp 网格上：按钮只负责触控热区，图标大小跟按钮无关。

空状态那个 64dp 的圆形底衬原来靠 `glyph_scale=0.42` 压小图标，换成固定网格
之后这个比例没意义了，改成 0.9（21.6dp）。

## 37. 设置里能导出/复制登录 Cookie（2026-10-27）

需求：在设置里除了导出账号数据，还要能把当前登录的 cookie 导出来，这样在
另一台设备上可以免密临时登录。

这个 cookie 就是账号的会话凭据——拿到它的人就是这个账号，所以：

- 桌面：设置 → 账号，两个按钮。「导出登录 Cookie」弹原生保存对话框写成
  `.txt`，文件权限 0600（只有本人能读）；「复制登录 Cookie」走系统剪贴板
  （macOS `pbcopy`、Windows `clip`）。两个按钮都会把内容显示在下面的文本框
  里，剪贴板或者文件哪条路都不通的时候还能手动复制。
- Android：账号页一个「复制登录 Cookie」按钮，走 ClipboardManager。
  Android 那边不落盘——剪贴板是用户能立刻用掉再擦掉的地方。

`LoginManager.cookie_string()` 是新加的公开方法（原来只有私有的
`_cookie_string()`）。剪贴板和文件两条路都不经过日志。

`tests/test_login_cookie.py` 五项：导出写文件 + 0600 权限、取消对话框不算
错误、复制报告剪贴板接没接到、没登录时两条路都拒绝、cookie 不进日志。
剪贴板在非 Android 上必然失败那一项放在 `tests/test_android_ui.py`（要 Kivy）。

i18n 两份语言同步加了 6 个 backup.* key 和 3 个 android.* key。

## 38. toast 停留时间变长，写操作可撤销（2026-10-27）

**停留时间**：桌面默认 3200ms → 4800ms（原来那条还没看清就没了）；带撤销
按钮的用 8000ms，因为它是控件不是通知——得让人看见、读完、够得着。
Android 从 `Toast.LENGTH_SHORT`（2 秒）换成 `LENGTH_LONG`（3.5 秒），这是
平台给的最长档，再长就得自定义 view 了。

**撤销**：`showToast(type, message, timeoutMs, options)` 多了第四个参数，
`{ undoLabel, onUndo }` 两样都在才画按钮，少一样就退化成普通 toast——死按钮
比没按钮更糟。按钮点一次就 disable 并立刻关掉，再点不会把请求发第二遍
（撤销发的是写请求，不是幂等的）。

挂上撤销的，都是本来就在弹 toast 的写操作：

- 喜欢/取消喜欢：五个分支全挂。云端不支持、限流、失败、只在本地生效这几条
  也都改了本地状态，所以都能撤。撤销用 `!wanted`，回到点之前的状态。
- 关注/取关歌手。
- 新建歌单（云端 + 本地）→ 撤销就是删掉刚建的那个（云端返回 `playlist_id`，
  本地返回 `playlist.id`）。
- 歌单改名（云端 + 本地）→ 撤销改回旧名字。改名前后 `playlist.name` 会被就地
  改掉，所以旧名字必须在发请求之前存下来。
- 订阅/取消订阅歌单。
- 往歌单加歌 → 撤销把这几首移出去。
- 从歌单移出单曲（云端 + 本地）→ 撤销加回去；本地的还要把行插回内存里那个
  数组再重渲染，因为本地列表是从数组渲染的，不是从磁盘。
- 从本地歌单移出单曲同上。

**故意没挂的**：

- 删除歌单（云端 + 本地）：两边接口都没有恢复能力，歌单和里面的歌删掉就没了。
  给它一个按钮等于骗人。
- 缓存清理、设置保存：本地维护动作，不是账号写操作。
- 下载队列：取消下载不等于撤销，可能已经写了半个文件。
- 歌曲排序：云端没有排序接口。

**Android 没有撤销按钮**：Android 现有的 6 处 toast 全在只读路径上（播放
失败、需要登录、Cookie 复制结果），写操作（关注、退出登录）本来就不弹 toast。
按「本来不弹 toast 的写操作不要加」的规矩，Android 只改了时长。

`tests/test_toast_undo.py` 13 项：时长三级关系、撤销按钮必须两样齐全、只能点
一次、消息和按钮共存、10 个可撤销写操作都挂了 onUndo、撤销调的是反向接口、
两个删除操作不许有 onUndo、两个语言都有撤销文案、Android 用 LENGTH_LONG。

真实浏览器验过：普通 toast 4.8 秒消失、带撤销的 8 秒还在；点一次触发一次、
按钮立刻禁用；420px 窄屏下长标题换行、按钮不挤出 toast。

## 39. Android 启动闪退：四层原因，逐层剥

安卓版从 v3.5.9 的原生 Kivy UI 换成 pywebview 之后，第一次启动必崩。四个
原因叠在一起，每一个都是前一个修好之后才暴露出来的。

**第一层：缺 Java 类。** pywebview 的 Android backend 用 jnius 调
`com/pywebview/` 下的七个 Java 类，PyPI 包里只有 Python 端的类名字符串，
没有实现。报错是 `ClassNotFoundError`，但因为在 Java 层抛的，Python 的
traceback 里看不到，logcat 里也只有一行。实现在上游仓库的
`webview/lib/pywebview-android.jar` 里，14,789 字节，SHA-256
`d5715b9c07ff2858c854bda872191c581dbbe101acabeff0076fc69edfc612a8`。
`android.add_jars` 引进来，构建脚本在构建前校验七个类都在，缺任一个就停。

**第二层：proxy_tools 找不到。** `webview/util.py` 在 import 阶段就
`import proxy_tools`。之前把 vendored 文件放成了 `android/proxytools/`
目录——模块名是 `proxy_tools`，目录形态永远 import 不到，报错还指向一个
明明就在旁边的名字。改成顶层 `android/proxy_tools.py`，构建脚本里加了一条
断言：出现 `proxytools/` 目录直接失败。

**第三层：缺 bottle。** `webview/http.py` 在 import 阶段 import bottle，同一
个坑。我们其实不用 pywebview 自带的 server（页面是桌面的 MediaServer 提供
的），但 import 已经发生了，躲不掉。一并 vendored。

**第四层：WebView 拒绝明文 HTTP。** 页面由 loopback media server 提供，
是 `http://127.0.0.1:<port>/`。Android 9 起默认禁明文，WebView 直接报
`net::ERR_CLEARTEXT_NOT_PERMITTED`，页面全空。官方文档里 localhost 的豁免
要到 Android 17 (API 37) 才有，所以要显式配 network security config：

```xml
<network-security-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="false">127.0.0.1</domain>
        <domain includeSubdomains="false">localhost</domain>
    </domain-config>
</network-security-config>
```

只放行回环地址，API 和 CDN 继续走 HTTPS 受系统信任库管。

两个坑：

- `android.add_resources` 的目标路径是**相对 res/ 目录**的。写
  `res/xml/xxx.xml:res/xml/xxx.xml` 会被放到 `res/res/xml/`，AAPT 报资源
  找不到，manifest 里那个属性看起来完全正确。
- 这份 XML 的注释里不能出现 `--`，AAPT 报解析错误但不指出行号。构建脚本
  现在先用 `xmllint --noout` 校验，省掉一次四分钟的失败。

**顺带两处环境问题。** Android 的 `HOME` 是 `/data`，不可写。原来的
`setdefault` 会保留这个已经存在的值——恰恰是问题所在，改成无条件覆盖。
而且 `_prepare_home()` 必须提到 `_configure_logging()` 之前，否则日志先落到
只读的 `/tmp`。

**崩溃日志要能进 logcat。** release 构建会把 traceback 收进 app 私有目录，
没人看得见。日志同时写 stderr，p4a 会转发到 logcat，这样一次启动失败就是
一条 `adb logcat -d | grep python` 的事，不用猜。

**模拟器实测（Pixel 6 / Android 14）**：安装后启动，进程存活，logcat 无
FATAL，页面正常渲染，底部标签栏点击可切换、选中态跟随。数据为空是模拟器
没登录网易云账号，不是接口坏了。

### 构建脚本现在会拦的

- jar 里七个 Java 类缺任一个
- `proxytools/` 目录形态
- 缺 proxy_tools.py / bottle.py / typing_extensions.py
- network security config 不是合法 XML

## 40. 桌面歌词卡片宽度：用 px 不用 em

卡片宽度本来写的是 `26em`。em 是相对于元素自身的 font-size 算的，而 JS 会把
用户设定的字号写到卡片的 font-size 上（`content.style.fontSize = s.fontSize +
'px'`）。于是 em 跟着字号一起变：18px 时卡片 468px，28px 时 `min(26em, 100vw
- 40px)` 里的 26em 已经超过视口宽，卡片被夹成视口宽度减边距。

结果就是宽度不恒定了——这正是这次要消除的那个抽动，只不过从「跟着歌词长度
变」变成了「跟着字号变」。

改成固定 `468px`。字号只影响高度，而高度本来就由 JS 测出来动态报给原生
窗口。

**原生浮窗宽度是 600px**，所以卡片左右各留 66px 空。这是原来就有的（胶囊
也是居中的），不算更差。窄视口下 `min(468px, 100vw - 40px)` 仍然兜底。

**顺带把原生窗口的高度上限从 400 提到 600。** 卡片定宽之后长句在 36px 字号
下换行成多行，内容高 381px，400 的上限会把最后一行裁掉。

浏览器实测（600px 视口，即原生浮窗的真实宽度）：18/28/36 三种字号下卡片宽
度恒为 468px，左右各 66px，横向不溢出，高度 145/266/381px。

### 拖拽把手曾经被卡片盖住

把手是 `position: absolute` + `z-index: auto`，卡片在 DOM 里排在它后面且背景
不透明。绘制顺序上后画的盖先画的，所以那根横杠根本看不见——截图上只有一
张卡片，没有任何拖拽提示。

加 `z-index: 2`，位置从 `top: 8px` 挪到 `top: 11px`（避开第一行文字）。

**教训**：绝对定位的元素叠在有背景的元素上面时，`z-index: auto` 不等于「在
上面」。DOM 顺序决定绘制顺序，`position: absolute` 只改变定位基准，不改变
层级。

## 41. 安卓的整个 JS 桥是断的，而且不报错（2026-10-29）

安卓版登录弹窗报 "method error"，首页的榜单和推荐 MV 全是空。电脑端一切正
常，所以先怀疑的是登录逻辑。

真因在更下面：`android/libs/pywebview-android.jar` 是从上游 master 拿的，
Java 侧 `PyJavascriptInterface.call(func, params, id, token)` 收**四个**参数；
随包的 `android/webview/` 是 PyPI 6.2.1，JS 侧
`window.external.call(funcName, params, id)` 传**三个**。

Android 按参数个数精确解析 `@JavascriptInterface`，个数对不上就抛
`Method not found`——不抛别的错，也不写 Python 日志。于是每一个 `api()` 调用
都失败，整个前端像死了一样，但进程活着、界面也渲染。

换成上游 6.2.1 tag 的 jar（`3f9d3ee…`，14309 字节）就对了。

**教训**：jar 和随包的 Python 包必须同一个版本，以后取 jar 一律带
`?ref=6.2.1`，别取 master。构建脚本现在会用 `javap` 读出 jar 里 `call()` 的
参数个数，跟 `webview/js/api.js` 实际传的个数对比，不一致直接 fail build。

**为什么这个坑这么贵**：症状（登录二维码不出来、首页空）离真因（jar 版本）
隔了三层，而且真因不产生任何日志。找不到线索时，先去看版本对不对得上。

## 42. 二维码图片只走了一条路（2026-10-29）

桥修通之后，安卓的二维码还是裂的，底下写着「二维码取不到，稍后重试」。

桌面端拿二维码图片靠 `onQrcodeUpdate` 事件推送，安卓端是轮询——调
`login_qrcode`，然后直接用 `result.qr_code` 画图。而 `login_qrcode` 只返回
`{success, error_key}`，图片从来没有进过返回值。

安卓没有 `onQrcodeUpdate` 这个监听函数，push 也送不到它那儿。

改成 `login_qrcode` 同步返回图片（data URL）。桌面端那条推送照旧，两边不用
互相迁就。

顺带修了一个边界：key 请求被拒时不返回旧码。失败后 `_qrcode_key` 还是上次的
值，把它画出来等于给一个没人轮询的二维码。

## 43. 隐藏之后点不回来（2026-10-29）

桌面歌词双击会隐藏整个浮窗——这是个会被误触的手势，而且隐藏后卡片
`pointer-events: none`，**再也点不回来**，只能关掉重开。

删掉双击绑定，隐藏只留在右键菜单里。再把拖拽把手改成隐藏态下依然可见可点，
点一下就恢复。

**教训**：一个「隐藏」的动作必须自带恢复入口，否则它就是单向门。淡出 + 禁用
指针事件是最容易写出单向门的地方。

## 44. 浮窗外面那圈白底（2026-10-29）

桌面歌词浮在一块纯白不透明的圆角矩形里，深色卡片压在它中间。

白底是**原生窗口的背景色**，不是网页画的：pywebview 默认 `background_color`
是不透明的 `#FFFFFF`，macOS 把无边框窗口画成那个形状。

10 月 3 日那次提交（`3bc1d81`）把 `transparent=True` 和
`background_color="#00000000"` 删掉了，理由写的是「for better
compatibility」——于是白底回来了。

恢复透明，另加 `vibrancy=True`（系统材质）；卡片本身从深色渐变换成主窗口同款
的液态玻璃：半透明中性灰 + backdrop-filter + 高光描边，文字另加一层阴影兜底
（底色半透明，壁纸会透上来）。

## 45. 安卓版不是「有几个 bug」，是根本没接上（2026-10-30）

用户反馈安卓版「简陋」：验证码没有冷却、toast 看不见、UID 是 0、用户名写死
「已登录」。这四条看着像四个独立的小毛病，实际是**同一个根因**：安卓前端是
照着界面写的，不是照着 bridge 写的。

逐条查下去：

- **UID 0 / 「已登录」**——`get_login_status` 把用户放在 `status.user` 里，
  安卓读的是 `status.nickname`。字段名对不上，于是昵称空、UID 空，
  代码兜底成「已登录」。
- **toast 看不见**——`.toast-host` 的 `z-index` 是 80，登录 sheet 是 90。
  弹窗一盖，提示就在下面。
- **验证码没有冷却**——桌面端有 `startCountdown()`，安卓端**压根没写**。
- **失败提示只有「下载没能开始」**——安卓前端读 `result.message`，但 bridge
  失败时给的是 `error_key`（`player.download_local_only` 这种）。安卓端没有
  翻译表，`error_key` 是纯给机器看的，翻译成句子需要词典——而词典在
  `get_translations()` 里躺着，安卓从来没调过。

方法名层面完全对得上：安卓调的 32 个方法 bridge 全有（`comm -23` 为空）。
**所以只比对方法名是查不出这类问题的**，要看的是字段路径、层级、状态提示
和调用时机。

同一个根因还有两处：

- **下载统计永远是 0**——安卓读 `stats.completed` / `stats.downloading`，
  bridge 发的是 `stats.finished` / `stats.total`。
- **主题设置不生效**——安卓读 `info.theme`，`get_app_info` 里没有这个字段
  （主题在 `get_settings().ui.theme`）。

**教训**：前端和后端之间的契约，光比对「调了哪些方法」是不够的。要比的是
**返回结构里的字段路径**、**CSS 层级**、**状态机的每一步**。安卓前端和桌面
前端是两套代码，各自跑得好好的，接口对不上的时候没有任何一边会报错——
UI 照常渲染，只是内容是空的或者错的。

另外，安卓的 `error_key` 一律配上翻译词典：新增 `t()` 与 `errorText()`，
`boot()` 里先拉 `get_translations()` 再渲染，失败提示统一走
`errorText(result, fallback)`。

## 46. 内容区撑破了整个界面（2026-10-30）

安卓的底部菜单栏跟着内容高度跑，不是贴在屏幕底部。

三处 CSS 一起造成这个：

- `.app` 用了 `min-height: 100dvh`——**min** 不是 **max**，内容比屏幕高的
  时候整个壳会被撑大。
- `.views` 是 flex item，没写 `min-height: 0`。flex item 的自动最小尺寸是
  它的内容高度，所以它拒绝收缩。
- `.view` 用了 `height: 100%`——**百分比高度依赖父元素的高度**，而父元素
  的高度正是这条规则想建立的。循环依赖，结果是按内容算。

改成：`.app { height: 100dvh; min-height: 0; overflow: hidden }`，
`.views { min-height: 0 }`，`.view { position: absolute; inset: 0;
overflow-y: auto }`。页面在中间那块里自己滚，菜单栏永远贴底。

连带两处：滚动监听原来挂在 `#views` 上——scroll 事件不冒泡，而容器现在
`overflow: hidden`，那个监听永远不触发（顶栏的阴影因此一直是错的）。改成给
每个 `.view` 单独挂。切页时的 `scrollTop = 0` 同样要重置**活动页**的，不是
容器的。

**教训**：`min-height` 和 `height` 在「填满视口」这件事上是两回事；flex
item 想收缩就得显式 `min-height: 0`；百分比高度在高度未定的容器里是循环
依赖。

## 47. 桌面歌词窗口根本没建起来（2026-10-30）

用户点「打开桌面歌词」完全没反应。日志里连着二十多行同一个错：

```
WARNING ncm.bridge: Failed to create desktop lyrics window:
  #00000000 is not a valid hex triplet color
```

第 44 节把浮窗改成透明时，`background_color` 写成了八位的 `#00000000`。
pywebview 的校验是 `^#(?:[0-9a-fA-F]{3}){1,2}$`——**只认 3 位或 6 位**，
8 位 RGBA 会被 `create_window` 直接拒掉，窗口从来没建过。

`transparent=True` 时 Cocoa 后端自己把 alpha 设成 0
（`nscolor_from_hex(window.background_color, 0)`），颜色写 `#000000` 就够。

**教训**：改 pywebview 的窗口参数时，`background_color` 的**格式**本身就是
契约的一部分。测试里之前断言的是 `#00000000`——把非法值固化成了「正确」，
所以 CI 全绿也拦不住。断言要对着 pywebview 的正则写。

## 48. 测试不能读注释（2026-10-30）

给安卓前端补契约测试时踩的坑：我把 `min-height: 100dvh` 从 `.app` 里删掉了，
测试却报「规则里还有这个值」——因为**我自己在注释里写了这句话**。

同一个坑踩了三次：CSS 规则体里的注释、JS 函数体里的注释（「用户住在
`status.user` 里」）、以及查 `'"throttled"'` 时匹配到了
`result.get("throttled")` 而不是返回字典的键。

**教训**：断言要读**解析后的结构**，不是源码文本。CSS 剥注释、JS 剥注释、
Python 用 `ast` 拿字典的键。写完还得做变异测试——把实现改坏一次，看测试
会不会红。这次 12 个变异全部被杀才算数。

## 49. 安卓界面在旧 WebView 上整个塌掉（2026-10-31）

用户报：安卓版打开只有首页大标题和底部菜单栏，菜单栏紧跟着标题而不是在
屏幕底部，切换功能只换标题、没有任何内容。

**根因**：`100dvh` 是 Chrome 108+（2022）。旧版 Android System WebView 不认
这个单位，**整条声明被丢掉**——不是回退到 `vh`，是这条高度直接没了。
于是 `.app` 从「视口高」退化成「内容高」：

```
old-css (dvh ok)   app height 891, views 783, tabbar 贴在底部（间距 0）
old-css (no dvh)   app height 108, views 0,    tabbar 贴在标题下面（离底 783px）
```

`views` 高度 0 → `.view` 虽然 `position: absolute` 也撑不出内容 → 页面空白；
`.tabbar` 是 flex item，被顶到标题下面。**切换只换标题**是因为 `go()` 照常
执行（`appbar-title` 的 `textContent` 变了），只是下面的页面没有高度。

**修法**：不依赖任何新单位。`.app` 用 `position: fixed` + 四边 `0` 撑满
视口，`height: 100%` 兜底、`height: 100dvh` 只作为支持它的引擎上的细化。
同批改掉的还有 `inset` 简写（Chrome 87+）、`aspect-ratio`（Chrome 88+）、
`min()`/`max()` 函数、`vh` 兜底。

**踩到的坑**：

1. **不能用 padding 百分比做正方形封面**。`.player-cover` 的宽度是
   `min(78vw, 340px)`，比父容器窄；`padding-bottom: 100%` 是按**父容器**
   的宽度算的，做出来的不是正方形。宽度和高度必须用同一个表达式。
   `.card-cover` 是 `width: 100%`，和父容器同宽，才能用 padding 百分比。
2. **模拟旧引擎不能只删 `dvh`**。`inset` / `aspect-ratio` / `min()` 得一起
   删掉才接近真实。而且**光删属性不够**：`@supports not (...)` 那条在真
   浏览器里是成立的，所以模拟时要把属性本身删掉才会走 fallback 分支。
3. **Playwright 里没有 pywebview 桥，页面看起来是死的**。`waitForApi()`
   8 秒超时后返回 null → `wire()` 根本不执行 → 标签栏没有 click handler。
   第一版 harness 因此报「所有标签页都不可见」，那是 harness 的问题不是
   代码的问题。加 `addInitScript` 塞一个假桥之后才是真的在测。
4. **断言要读解析后的结构**。第 48 节的教训在这里又应验一次：测
   `inset` 时不能只 `assert "inset: 0" in view`——新写法是四个偏移，
   但注释里提到 `inset` 简写也会命中。要按属性名匹配。

**验证方式**：`scripts/check-android-layout.mjs` 用 Playwright 加载真实的
`android/web/index.html`，量出 `.app` / `.views` / `.tabbar` 的几何，
并逐个标签页点一遍确认内容真的显示。同一套脚本喂**修复前的** CSS 必须
**红**（3 项失败：壳没撑满、内容区没高度、菜单栏不在底部）——不红就说明
这套断言没在测东西。

**遗留**：模拟器上是 WebView 113（支持 `dvh`），复现不出用户的故障；真机上
还得让用户自己装一次确认。


## 50. 安卓版八处问题：闪退、登录丢失、关注歌手大头像、没歌词、没设置、没缓存、歌单卡住、下载转圈（2026-10-31）

用户一次报了八条。前七条能定位能修，**闪退定位不了**——模拟器上复现不
出来，只能先把日志打开（下文）。

### 前端六条

**1. 下载列表永久转圈**：`loadDownloads()` 里 `showSpinner(queueHost)` 之后
**再没有任何代码重新渲染 `queueHost`**。没有下载任务时转圈是对的（任务在
跑），但任务数为 0 时也一样转，就成了永远转。改成 `renderDownloadQueue()`，
按 `state.downloadStats.total` 分流：0 就显示空状态，非 0 显示进度。

**2. 关注歌手是大卡片网格**：`openFollowedArtists()` 调 `cardGrid()`，一个
关注多的人得滑很久。改成 `artistRow()` 列表行，头像 40px 圆形和名字同行。

**3. 播放器没有歌词**：bridge 早就有 `get_track_lyrics`，安卓前端**从来没
调用过**。现在大封面点击切歌词、再点切回，歌词按时间戳高亮当前行并跟随
滚动。返回结构跟桌面一样（`lines[].time/text/translation`），所以两个前端
读的是同一份数据。

**4. 歌曲信息太粗、歌手跳不过去**：`openTrackMenu()` 只有三个动作，没有详情
页。`_track_payload` 里本来就带 `artist_list`（含 id），现在用它渲染可点的
歌手名。

**5. 没有设置页**：bridge 有 `get_settings` / `update_settings`，安卓前端
一个都没调。

**6. 歌单加载没反馈**：`get_playlist_info` 默认 `max_tracks=1000`，大歌单要
分批补全，前端只有一个转圈。现在先显示「正在读取歌曲…」，读不出来会说明
原因。

### 后端三条

**7. 登录不持久化**：`AuthSettings.remember_login` 默认 `False`，
`login.py:935` 的 `if ...: self._save_cookie()` 于是永远不执行——登录成功
也不落盘，重启必然退出登录。安卓版又没有设置页，用户根本开不了这个开关。

改成默认 `True`。**同时要处理「关掉它」**：光把开关存成 False 只是不再写
新 cookie，旧的 `saved_cookie` 还在盘上，`load_saved_cookie()` 下次启动照
样读回来——开关形同虚设。所以 `bridge.update_settings` 的 auth 分支里，
关掉时一并 `update_auth(saved_cookie="")`。

**8. 日志被静音**：`_configure_logging()` 把 file handler 和 stream handler
都设成 `WARNING`（`debug` 才用 DEBUG），`log.info(...)` 全被丢弃。实测模拟器
上 `files/logs/ncm.log` 是 **0 字节**。两个 handler 都提到 `INFO`。stream
尤其重要：p4a 把 stdout/stderr 接到 logcat，而 logcat 是手机上唯一能拿到
崩溃上下文的地方。

**9. `_prepare_home()` 整个空转**：读 `os.environ.get("ANDROID_PRIVATE")`，
取不到就 `return`。**p4a 根本不设这个变量**——

```
$ grep -rn ANDROID_PRIVATE /Users/mofan/android-toolchain/bv/lib/python3.11/site-packages/pythonforandroid/
（0 命中）

$ adb shell cat /proc/<pid>/environ | tr ' ' '
' | grep -E '^(HOME|ANDROID)'
（只有 ANDROID_BOOTLOGO / ROOT / DATA / ASSETS / STORAGE 等系统变量，
  没有 HOME，也没有 ANDROID_PRIVATE）
```

所以 HOME / XDG_CONFIG_HOME / NCM_LOG_DIR / TMPDIR 四个都没设。改用
`os.path.expanduser("~")`——它走 passwd 解析，落到应用私有目录
`/data/user/0/<pkg>/files`，正是应用唯一能写的地方。

**关键约束**：`XDG_CONFIG_HOME` 必须仍然指向 `<私有目录>/.config`。配置文件
实际落在 `files/.config/NeteaseMusicDownloader/`，改路径等于让升级过的用户
「登录态和缓存消失」。

### 踩到的坑

1. **bridge 的 `get_settings` 是分组的**，不是扁平键。实际结构是
   `download` / `playback` / `ui` / `auth` 四个组，`remember_login` 在
   `auth` 里。第一版按扁平键写，`Boolean(settings['remember_login'])` 永远
   false。
2. **`update_settings` 的签名是 `(category, data: dict)`**，不是
   `(key, value)`。传 `(key, value)` 不会报错，只是静默不生效。
3. **`get_cache_stats` 的字段是嵌套的**：`audio.bytes` / `cover.bytes` /
   `meta_bytes`，没有 `total_size`。
4. **`get_artist_detail` 把 `hot_songs` 放在顶层，不在 `artist` 里面**。
   `openDetail()` 原来读 `artist.hot_songs`，所以**每个歌手页都显示「这个
   歌手还没有热门歌曲」**。album 的 `tracks` 同理。改对之后歌手页才真的有
   歌。
5. **断言要读解析后的结构**（第 48 节的教训第三次应验）。契约测试查
   `el('button', 'artist-link', ...)` 时，只查 `"artist-link" in links` 漏掉了
   「把 button 改成 span」这个变异——span 点不了，歌手就跳不过去了。要查
   `'artist-link'` 这个**带引号的类名**才算数。

### 真机上又抓到一个：时长单位

模拟器和 Playwright 全绿，装到模拟器上一看，歌曲信息里的时长写的是
**`3747:22`**——一首歌不可能这么长。

`_track_payload` 的 `duration` 来自网易云的 `dt` 字段，**单位是毫秒**
（`bridge.py:891`；bridge 自己也知道，`bridge.py:1230` 那里写着
`seconds = float(track.get("duration") or 0) / 1000.0`）。安卓前端直接把它
喂给 `formatTime()`，而 `formatTime()` 假设入参是秒。

桌面端是对的：`Math.floor(track.duration / 1000)`。安卓这边漏了。

修法是 `formatTime(track.duration / 1000)`。**注意别改错地方**——播放器
进度条上的 `formatTime(audio.duration)` 用的是 HTML5 Audio 元素的时长，
那个本来就是秒，除 1000 就错了。

**教训**：Playwright 的桩数据是我自己写的，`duration: 245`，单位是秒，
所以测试全绿。真机上的数据是 245000 毫秒。**桩数据的单位必须跟真实
bridge 一致**，否则测的是一个不存在的世界。改完桩数据要重跑。

### 闪退：没修，只能等日志

用户报「打开一会儿后崩溃闪退」。模拟器 Android 14 / WebView 113 上跑
20 秒：无 FATAL、crash buffer 为空、进程 PID 不变。**复现不了**。

这一版能做的只有把日志从 0 字节变成有内容（第 8 条）。下次闪退时，
`adb logcat -s python` 或应用私有目录的 `ncm.log` 会有线索。**没有堆栈就
定位不了根因**，所以这一版不宣称修了闪退。

### 验证

- `scripts/check-android-features.mjs`（新）：Playwright 加载真实页面，
  stub 一个符合 bridge 契约的假桥，点封面、切歌词、点歌手、开设置、
  看下载列表。**20 项全过**。
- pytest 契约测试 7 条 + `tests/test_android_main.py` 6 条。
- **变异测试**：前端 10 个变异、后端 6 个变异，**全部被杀，漏网 0**。
- 完整套件 `826 passed, 6 skipped`（用 conda `xijianBase` 的 Python 3.11）。
  注意：系统 python3.9 跑会有一片 `unsupported operand type(s) for |` 的
  失败，那是 `X | Y` 类型语法要 3.10+，**不是真失败**。



