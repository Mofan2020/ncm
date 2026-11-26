# ncm 安卓版历史记录（归档）

2026-10-26 ~ 2026-10-31 那阵子安卓版的实施与排障记录，2026-11-26 从 `notes.md`
拆出来。**安卓版已于 2026-10-31 停止支持**，代码和构建流程都删了，只留 Windows 与
macOS。停掉的原因见 [announcement.md](announcement.md)。

这些章节留着是为了说明当初为什么那么做，**不要再照着它们排活**。

---

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

$ adb shell cat /proc/<pid>/environ | tr '\x00' '
' | grep -E '^(HOME|ANDROID)'
（只有 ANDROID_BOOTLOGO / ROOT / DATA / ASSETS / STORAGE 等系统变量，
  没有 HOME，也没有 ANDROID_PRIVATE）
```

所以 HOME / XDG_CONFIG_HOME / NCM_LOG_DIR / TMPDIR 四个都没设。改用
`os.path.expanduser("~")`——它走 passwd 解析，落到应用私有目录
`/data/user/0/<pkg>/files`，正是应用唯一能写的地方。

**关键约束**：`XDG_CONFIG_HOME` 必须仍然指向 `<私有目录>/.config`。配置文件
实际落在 `files/.config/SkycMedia/`，改路径等于让升级过的用户
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
   「把 button 改成 span」这种改坏——span 点不了，歌手就跳不过去了。要查
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
- **反向验证**：前端 10 处、后端 6 处改坏，**全部被杀，漏网 0**。
- 完整套件 `826 passed, 6 skipped`（用 conda `xijianBase` 的 Python 3.11）。
  注意：系统 python3.9 跑会有一片 `unsupported operand type(s) for |` 的
  失败，那是 `X | Y` 类型语法要 3.10+，**不是真失败**。

## 51. 切后台再回来，整个界面是白的（2026-10-31）

上一条说闪退复现不了，只能开日志。这次日志开了，用户又报「切到后台再返回，
15 秒左右闪退」——**真实故障终于复现了**，而且比闪退更早一步：界面先没了。

### 复现与判决性证据

模拟器 Android 14 / WebView 113，debug 版：

| 步骤 | 截图大小 | 桥调用数 |
|---|---|---|
| 首次启动 | 445 KB（首页、新歌速递、推荐歌单、底部标签栏都在） | 36 |
| HOME 切后台 → 返回 → 等 8 秒 | **24 KB（纯白，只有状态栏）** | **36（一个都没多）** |

截图大小从 445 KB 掉到 24 KB 是判据：页面不是「没内容」，是**没画**。同时桥
调用数一个没涨，说明 JS 也停了——不是界面渲染坏了，是 WebView 整个不动了。

### 根因：Activity 被销毁，而销毁流程卡在半路

`dumpsys window windows` 显示切后台返回后：

```
Window #9  Splash Screen com.skyc8266.neteasemusic   ← 盖在上面，没撤
Window #10 PythonActivity
    mViewVisibility=0x8 (GONE)   mHasSurface=false
    mDrawState=NO_SURFACE        isOnScreen=false
```

`mCurrentFocus=null` —— 没有任何 window 持有焦点。

`logcat` 的因果链（一次完整的切后台 → 返回）：

```
21:11:40.575  V/PythonActivity: onPause()
21:11:42.406  V/SDL: onStop()
21:11:42.724  V/SDL: onDestroy()                ← Activity 死了
21:11:47.672  START ... result code=2           ← 任务还在，但里面没有 Activity
（之后没有 onCreate / onStart / onResume）
```

**返回后系统把任务带到前台，却没有 Activity 实例可显示** —— 启动图照常弹出来，
首帧永远不来，于是启动图一直盖着。用户看到的是一片白。

**「Activity 被销毁后没有重建」这一条是关键。** 原因在
`SDLActivity.onDestroy()`（`org/libsdl/app/SDLActivity.java:605-616`）：

```java
SDLActivity.nativeSendQuit();
// Wait for "SDLThread" thread to end
try {
    SDLActivity.mSDLThread.join();   // ← 616
} catch (Exception e) { ... }
SDLActivity.nativeQuit();
super.onDestroy();                    // ← 永远执行不到
```

`mSDLThread` 就是跑 Python 的那个线程，它跑在 pywebview 的 EventLoop 死循环里，
**不会自己结束**。于是：

1. `join()` 永久阻塞主线程
2. `super.onDestroy()` 永远不执行 → Activity 的销毁流程没走完
3. 系统仍然认为这个 Activity 活着 → 回来时**不重建**
4. 任务里于是有一个空壳 → 启动图等首帧 → **白屏**

主线程栈（`adb shell kill -3 <pid>`，root 后可读 `/data/anr/trace_00`）能直接看到
这个 join：

```
at java.lang.Thread.join(Thread.java:1523)
at org.libsdl.app.SDLActivity.onDestroy(SDLActivity.java:616)
at android.app.Activity.performDestroy(Activity.java:8876)
```

### 改法

`android/webview/platforms/android/__init__.py` 与 `base.py`：

- **注册 `onActivityPreDestroyed`**，在 `AndroidApp.on_pre_destroy()` 里调
  `app.stop()` 关掉 eventloop。Android 10+ 会在 `onDestroy()` **之前** dispatch
  这个回调 —— 那正是还来得及让 Python 线程退出的时刻。Python 线程一退出，
  `join()` 就返回，销毁流程走完，app 干净退场，回来时是全新启动。
- `EventLoop.mainloop()` 里 `lock.acquire()` **加了 1 秒超时**：帧回调是 post 到
  UI 线程再走 WebView 的消息循环回来的，UI 线程漏掉一次回调时，无限等待会把
  跑 app 的那个线程永久parked —— 桥不再应答，页面一直转圈。
- `on_pause` **不碰 WebView**：`pauseTimers()` 会冻结所有 JS 定时器，桥的回传
  全断（**「永远转圈」的机制**）；`onPause()` 会停渲染，而这个 Activity 回来时
  不一定还活着。
- `on_start` 里恢复 WebView（`webview.onResume()` + `requestLayout()`），
  **必须走 `run_on_ui_thread`** —— Android 规定所有 WebView 方法只能在 UI 线程
  调用，否则抛 `A WebView method was called on thread 'Thread-N'`。
  **注意：恢复不能挂在 `on_resume` 上** —— `p4a` 的 `onNewIntent()` 手工调
  `this.onResume()`，绕开了 `performResumeActivity()`，而 `onActivityResumed`
  正是在那里 dispatch 的，所以那个回调在回前台的路上根本不来。
- `on_create` 里补 `activity.appConfirmedActive()`：p4a 撤启动图的条件是这个
  标志，而它是 **Activity 实例字段**，重建后为 false 就再也没人置位。
- `on_destroy` 加幂等保护 —— `app.stop()` 会 dispatch `on_destroy`，而
  `dismiss()` 里又会调 `app.stop()`。
- **所有 `on_*` 回调的参数都要有默认值。** 这是个很隐蔽的坑：同一个方法会被
  两种方式调用 —— 生命周期回调会带 activity 参数，而 `EventDispatcher.dispatch()`
  **一个参数都不传**。签名写成 `def on_destroy(self, _)` 时，`dispatch('on_destroy')`
  会抛 `TypeError: on_destroy() missing 1 required positional argument`，eventloop
  于是没被关掉，Python 线程继续活着，`join()` 继续卡死 —— **整个修复静默失效，
  而且只在 logcat 里留下一行 warning**。

### 实测结果

修完之后（`android/check_background_return.sh` 一条命令跑完整个场景）：

```
first start:  445569 bytes
after return: 445470 bytes   ← 界面完整画出来了
```

日志里的完整链路：

```
21:27:41.923  activity is going away, closing the event loop
21:27:41.972  V/SDL: onDestroy()
21:27:48.071  PythonActivity onCreate running
21:27:48.159  V/SDL: onStart()
```

连续切后台三次，截图分别是 452 KB / 452 KB / 1.37 MB —— **每一次都画出来了**。
之前是 24 KB 的纯白。

### 现在的行为与它的代价

**切后台时 app 会退出，回来时全新启动。** 界面正常、功能正常，但**播放会断**。

原因：Activity 被系统回收 → 我们在 `onActivityPreDestroyed` 里干净地关掉
eventloop → Python 线程退出 → app 退场 → 系统重建 Activity。

**这个回收是模拟器内存太小造成的**（AVD 只有 1536 MB，进程稍重就被系统杀）。
真机内存大时可能不会发生，那时 app 会继续在后台跑。

**要彻底做到「切后台不中断播放」，得给 app 一个前台服务**（通知栏 + 常驻），
那是另一个功能，不在这次修复范围内。

### 一个已知限制

`onActivityPreDestroyed` 是 **Android 10（API 29）** 才有的回调。
`buildozer.spec` 里 `android.minapi = 24` —— **API 24 到 28 的设备上这个回调
不来，白屏还在**。要覆盖那些系统，得给 `PythonActivity` 换成一个自己的
子类（buildozer 的 `android.activity_class_name` 支持），在 `onDestroy()` 里
先关 eventloop 再交给父类。

### 一条容易踩的：本地绿、CI 红

CI 的 lint 是 `ruff check .`（**整个仓库**），本机如果只跑
`ruff check src tests scripts`，范围小了一截，而且两边 ruff 版本可能不同：

- `requirements-dev.txt` 钉的是 `ruff>=0.12,<0.13`
- 本机 conda 环境里装的是 0.17.0 —— **0.12 有的 UP038 规则，0.17 已经不报了**

所以本地 0.17 全绿，CI 0.12 报：

```
UP038 Use `X | Y` in `isinstance` call instead of `(X, Y)`
  --> src/core/bilibili.py:194:8
```

**本地 lint 必须用和 CI 一样的命令、一样的版本：**

```bash
ruff check .          # 不是 ruff check src tests scripts
```

版本对不上时，装一个同版本到临时目录跑：

```bash
python -m pip install --target /tmp/ruff012 'ruff>=0.12,<0.13'
/tmp/ruff012/bin/ruff check .
```

**结论：lint 的"绿"只有在命令和版本都对齐 CI 时才算数。**

### 顺带

- `evaluate_js` 的 `lock.acquire()` 原来**没有超时**。回传丢了就永久占住
  一个线程，后面所有桥调用排队。加了 20 秒上限（`EVALUATE_JS_TIMEOUT`）。
- `js_bridge_call` 现在打一行 `bridge call <名字> took N ms`。**一个永远
  转圈的页面，就是某个调用没有回来**，而之前日志里对此一无所知。
- 媒体服务器代理在线播放时，原来把**整首歌攒进 `bytearray`** 再落盘，峰值
  内存是文件大小的两倍。30 MB 的 FLAC 就是 60 MB——手机上直接被系统杀。
  改成流式写盘（`cache._AudioWriter`，`.part` 临时文件 + 完成才 rename）。
- 前端 `api()` 的 catch 分支原来不设 `error_key`，于是桥调用超时时页面显示
  「这个歌单还没有歌」——**那是假的**。现在失败也带 `error_key`，页面显示
  「读不出来」。

### 三条教训

1. **同名不等于成对。** `onPause()` 和 `resumeTimers()` 看起来是一对，实际是
   两个维度：前者管渲染，后者管 JS 定时器。配错的表现是「白屏」和「永远
   转圈」两种完全不同的症状，而它们其实同源。
2. **工具链里的手工生命周期调用要绕着走。** p4a 的 `onNewIntent()` 调
   `this.onResume()` 是它自己的历史包袱，改不了（工具链目录），只能在
   pywebview 这边兜住。
3. **「首帧画不出来」和「页面没内容」是两回事。** 截图从 445 KB 掉到 24 KB
   就能分开这两种情况 —— 前者查 surface 和启动图，后者查数据。

### 怎么判断白屏在哪一层

- 截图大小：445 KB → 24 KB = 首帧没画出来（不是内容空）
- `mHasSurface=false` / `mDrawState=NO_SURFACE` = WebView 没有 surface
- `mCurrentFocus=null` = 没有 window 持有焦点
- `SplashScreenView: Build` 出现在返回之后 = 系统重新 show 了启动图
- 桥调用数不变 = JS 还在跑，WebView 进程活着，**问题不在桥**
- 主线程栈：`adb shell kill -3 <pid>`（需 root），栈在 `/data/anr/trace_00`





---
