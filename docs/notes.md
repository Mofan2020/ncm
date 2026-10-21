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
