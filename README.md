# 网易云音乐歌单下载器 / NetEase Music Playlist Downloader

![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

基于 **pywebview** 的桌面应用：粘贴歌单 ID（或链接）→ 勾选歌曲 → 多线程下载。
同一套代码同时适配 **Windows** 与 **macOS**（Apple 芯片 / Intel 都有构建产物），界面支持 **中文 / English** 实时切换。

> 与旧版本的区别：v2.0 之前的命令行用法（以及一个 macOS 专用版本）已经废弃，
> 现在只有这一个 GUI 版本，同时支持 Windows 和 macOS。旧脚本保留在 `legacy/` 目录，仅供参考，不再维护。

## 功能

- **歌单获取**：直接粘贴 ID 或歌单链接；超过 1000 首的歌单会自动分批补全曲目
- **音质选择**：标准 / 较高 / 极高 / 无损 / Hi-Res，不可用时自动降级到可用音质
- **并发下载**：可同时下载 1–3 个文件（默认 2），实时显示每首进度、速度与总体统计
- **歌词下载**：与音频同目录保存 `.lrc`（原歌词 + 翻译合并，同一个时间戳两行）；
   已存在的歌词不会覆盖，关掉再开可给旧歌补歌词
- **用户登录**：扫码登录 / 手机验证码 / 导入 Cookie；登录后可下载**非公开歌单**与会员（VIP）歌曲
- **多语言**：简体中文 / English，应用内切换，无需重启
- **断点安全**：先写 `.part` 临时文件，校验完整（字节数/接口声明大小）后再原子改名；失败歌曲单独列出
- **下载控制**：暂停 / 继续 / 取消，跳过已存在文件，可覆盖重下
- **界面**：深色 / 浅色 / 跟随系统主题，窗口从 780×560 到 4K 自适应

## 下载安装

到 [Releases](https://github.com/Mofan2020/ncm/releases/latest) 下载对应系统的产物：

**macOS**

| 机型 | 文件 |
|------|------|
| Apple 芯片（M 系列） | `NeteaseMusicDownloader-macOS-arm64.dmg` / `.zip` |
| Intel 芯片 | `NeteaseMusicDownloader-macOS-x64.dmg` / `.zip` |

构建产物**未做代码签名**（没有 Apple 开发者账号），首次打开请**右键 → 打开**，或执行：

```bash
xattr -dr com.apple.quarantine /Applications/NeteaseMusicDownloader.app
```

**Windows**

| 文件 | 说明 |
|------|------|
| `NeteaseMusicDownloader.exe` | 单文件免安装，直接运行 |
| `NeteaseMusicDownloader-Windows-x64.zip` | 同上，压缩包形式 |

Windows 需要 **WebView2 运行时**（Windows 11 与较新的 Windows 10 已自带；老系统请到
[微软官网](https://developer.microsoft.com/microsoft-edge/webview2/) 安装）。

## 从源码运行

需要 Python 3.9+（CI 使用 3.11 构建）：

```bash
git clone https://github.com/Mofan2020/ncm.git
cd ncm
pip3 install -r requirements.txt
python3 main.py
```

`pywebview` 会自动带上平台后端（Windows: pythonnet + WebView2；macOS: pyobjc + WebKit）。

## 使用方法

1. 在顶部输入框粘贴歌单 ID 或歌单链接（如 `3778678` 或 `https://music.163.com/playlist?id=3778678`），点「获取歌单」
2. 选择音质、同时下载数与是否下载歌词，勾选想要的歌曲（支持全选 / 取消全选）
3. 点「开始下载」，右侧队列显示每首进度；完成后点「打开文件夹」
4. 需要非公开歌单或会员歌曲时，先点右上角「登录」：扫码 / 手机验证码 / 导入 Cookie 三种方式

默认下载目录为 `~/Music/Downloads`（Windows 为 `%USERPROFILE%\Music\Downloads`），可在设置里修改。
配置与日志位于 `~/Library/Application Support/NeteaseMusicDownloader/`（macOS）
或 `%APPDATA%\NeteaseMusicDownloader\`（Windows）。

## 自行打包

```bash
pip3 install -r requirements-dev.txt
pyinstaller --clean --noconfirm main.spec
# macOS: dist/NeteaseMusicDownloader.app
# Windows: dist/NeteaseMusicDownloader.exe

# 冒烟自检（不需要图形界面，检查打包后资源是否可用）
dist/NeteaseMusicDownloader.app/Contents/MacOS/NeteaseMusicDownloader --self-test
```

图标由 `python3 scripts/make_icon.py` 生成（`assets/icon.{png,ico,icns}`）。

## 开发与测试

```bash
pip3 install -r requirements-dev.txt

ruff check .        # 静态检查
pytest -q           # 单元测试（完全不联网，使用本地 HTTP 服务器）

# 可选：真实接口冒烟测试（会访问网易云 API）
NCM_LIVE=1 pytest tests/test_live_api.py -q
```

CI（`.github/workflows/build.yml`）在每次提交跑 lint + 测试，打 tag 时构建
Windows x64、macOS arm64、macOS x64 三份产物并自动发布 Release。

## 常见问题

### 登录没反应，或者一直停在「已扫码，请在手机上授权」？

1. 确认手机弹窗里点了「确认登录」；二维码约 2 分钟不确认就会过期，点「刷新二维码」重扫一次。
2. 应用会把每一次登录交互写进配置目录里的 `login-debug.log`
   （macOS：`~/Library/Application Support/NeteaseMusicDownloader/login-debug.log`，
   Windows：`%APPDATA%\NeteaseMusicDownloader\login-debug.log`）。
   里面有轮询到的**服务器状态码与原文**，卡住时把这个文件发出来就能定位。
3. 「验证码发送过于频繁」是网易云自己的限制（同一号码每分钟一条），**不要连点**：
   发送按钮会进入倒计时锁定；被限制后请等几分钟，或直接用扫码登录。

| 现象 | 原因 / 处理 |
|------|-------------|
| 部分歌曲下载失败（提示「版权不可用」） | 该曲目对未登录用户不开放，登录后重试；会员歌曲需要账号有对应权益 |
| 歌单获取失败 | 非公开歌单必须登录；也请确认歌单 ID 正确 |
| 下载速度没有更快 | 同时下载数提高的是「并发」；若本机带宽已被占满，多个文件会分摊同样的总速度 |
| macOS 提示「无法验证开发者」 | 未签名构建，右键 → 打开，或见上方 `xattr` 命令 |
| Windows 打不开 / 白屏 | 缺少 WebView2 运行时，按上方链接安装 |
| 想要命令行版本 | v2.0 起已移除，见 `legacy/`（不再维护） |

## 技术说明

- 请求走网易云 **eapi** 通道（AES-ECB + MD5 签名，官方桌面端同款）。`weapi` 在部分网络下
  被 CDN 黑洞（任何请求都返回空响应），因此本项目不使用它
- 接口清单、登录状态码语义、限速策略与踩过的坑都记录在 [`docs/notes.md`](docs/notes.md)
- 目录结构、开发约定见 [`AGENTS.md`](AGENTS.md)

## 免责声明

本项目仅用于学习与个人备份，请勿用于商业用途。所有音乐版权归网易云音乐及其权利人所有，
请遵守相关法律法规与平台条款。

## 许可证

[MIT](LICENSE) © 2026 Skyc8266
