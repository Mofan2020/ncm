# B站接入笔记

SkycMedia 里 B站那一半的内部说明。面向维护者，不面向用户。

## 这个文件记的是什么

B站的接口没有公开文档，能用的信息来自对官方前端 bundle 的观察和自己实测。
这里只记**踩过的坑**和**必须遵守的约定**，端点清单在 `AGENTS.md` 的
「Bilibili endpoint map」一节。

## 账号

登录走 `passport.bilibili.com`，和 `api.bilibili.com` 是两套：

- **登录成功后的 cookie 在响应的 `Set-Cookie` 头里，不在 JSON 里。**
  `login_poll()` 返回成功时，要从响应头把 `SESSDATA`、`bili_jct`、`DedeUserID`、
  `DedeUserID__ckMd5`、`sid` 收集起来拼成 cookie 串交给上层。只看 JSON 会得到
  一个「登录成功但其实没登录」的假象。
- **二维码状态码和网易云不一样**：网易云是 800/801/802/803，B站是
  `0`（成功）、`86101`（等待扫码）、`86090`（已扫码待确认）、`86038`（过期）。
  拿错一套，界面会一直显示「等待扫码」。
- **密码和短信登录走 app 端那套路由，不要走 web 端。** 见下面「登录路由」一节。
  web 端（`/x/passport-login/web/login`）强制校验滑块，客户端拿不到结果；
  app 端（`/x/passport-login/oauth2/login`）不校验滑块，密码登录因此完全可用。
- **写接口要 `csrf`**，值就是 cookie 里的 `bili_jct`，少了会回 `-111`。
- cookie 存在配置目录下的 `bilibili.json`（`src/auth/bili_login.py`），
  和网易云的 `settings.yaml` 分开放：两边的格式、有效期、刷新方式都不一样。

## 登录路由

B站有两套登录路由，**差别就是滑块**。2026-11-01 实测（`src/core/bili_app_login.py`）：

| 路由 | 滑块 | 实测结果 |
|------|------|----------|
| `POST /x/passport-login/web/login` | 强制 | 不带滑块一律 `-105`（验证码错误） |
| `POST /x/passport-login/oauth2/login` | 不校验 | 签名对就进凭据校验，回 `-629`（用户名或密码错误） |
| `POST /x/passport-login/web/sms/send` | 强制 | 不带滑块 `-105` |
| `POST /x/passport-login/sms/send` | 不校验 | `code 0`，风控不触发时直接给 `captcha_key` |
| `POST /x/passport-login/login/sms` | 不校验 | 要 `captcha_key`，缺了 `-400`，过期了 `86205` |

**app 端要签名**：参数按 key 字典序 urlencode，拼上 appkey，再拼 appsecret，
MD5 之后填进 `sign`。签名串**必须包含 appkey**，不含会被回 `-3`（签名错误）。
appkey / appsecret 是手机端公开常量，写死在 `src/core/bili_app_login.py` 里。

**密码用 RSA 加密**：`GET /x/passport-login/web/key` 给 `hash`（盐，约 20 秒有效）
和 PEM 公钥，密码明文前面拼盐，再用 RSA/ECB/PKCS1Padding 加密后 base64。
自己实现，别引 `cryptography`——打包体积不允许（见 `src/core/aes.py` 的理由）。

**app 端的 cookie 在 JSON 里**，不在 `Set-Cookie` 头里：`data.cookie_info.cookies[]`
是 `[{name, value}]`。web 端才在响应头。读错地方会拿到「登录成功但其实没登录」。

**短信发码被风控时**回答里没有 `captcha_key`，但有 `recaptcha_url`（B站要滑块的
那个页面）。这时界面必须给出路：打开那个页面，或者改用扫码登录——扫码不要滑块。

**注册已经被 B站下线**。`/web/reg/tel` 和 `/web/sms/general/v2/send` 都还在，
但一律回 `code 20000`（该版本已不支持当前功能）。注册实际上是短信登录顺带完成的
（未注册的手机号登录时会返回 `data.is_new`）。

## 风控

B站对同一 IP 的写操作频控很紧。收藏夹的写入（建夹、加视频）尤其容易触发
`-352`（风控拦截）。所以：

- 写操作**不要在开发时用真实账号跑**。测试全部用假传输层。
- 批量加视频要分批，批与批之间留空隙。
- 一次失败不要重试到天荒地老：`-352` / `-401` 重试多少次都是同一个结果。

## 播放

- **视频和音频是两条流。** 高画质走 DASH（`fnval=4048`），画面在
  `dash.video`、声音在 `dash.audio`；只有整体 mp4 时才有 `dash.durl`。
  前端用 MSE 把两条流拼起来播，初始化段（`segment_base.initialization`，
  形如 `0-821`）要先单独取出来 append 给 `SourceBuffer`。
- **CDN 只认带 `Referer` 的请求**，而且高画质往往需要登录态的 cookie。
  所以网页里的 `<video>` 不能直接连 CDN，要走回环服务器：
  `/m/<token>/bili?u=<地址>`。这个代理会补上 `Referer` 和 cookie、转发
  `Range`，并且**只放行** `hdslb.com` / `bilivideo.com` / `bilibili.com`
  这几个域名——不限制的话，回环服务器就成了一个开放代理。
- **代理只放行 B站自己的域名。** 白名单在 `src/core/mediaserver.py` 的
  `_BILI_HOST_SUFFIXES`（含 `*.mountaintoys.cn`——合并流现在在那儿）。
  加域名时只加明确后缀，别把回环服务变成开放代理。
- **`baseUrl` 里的节点不一定是最快的。** 2026-11-01 实测同一台机器取 6 MB：
  `upos-sz-estgoss.bilivideo.com` 8211 kB/s、`upos-sz-mirrorhwo1` 1097 kB/s、
  `*.edge.mountaintoys.cn:4483` 331 kB/s、`*.mcdn.bilivideo.cn:8082` 211 kB/s
  （还两次读超时）。B站把 `mcdn` 节点排在 `baseUrl` 第一位，那是给官方客户端
  用的 P2P 加速节点，对第三方客户端经常连得上却不发数据。
  所以：**按主机排个序（`upos-*` → `mountaintoys` → `mcdn`），并且把
  `backupUrl` 一起带着，代理在第一台不发数据时换下一台。**
  代理要记住哪台能用，同一个文件后面的 Range 请求直接用它。
- **画质是账号说了算。** `accept_quality` 给出这次登录能看的画质，
  `support_formats` 给出清晰度名称。没有大会员时 1080P 以上拿不到，
  这不是 bug。
- **无损音质在 `dash.flac` 里**（`codecid` 30280 一类），杜比在
  `dash.dolby` 里。两者都只有部分视频有。
- **同一个画质会给好几条**（码率和主机都不同）。挑的时候要按码率最高的那条，
  按顺序拿第一条会拿到 750 kB/s 的流，而 2.6 MB/s 的那条就在旁边。
- **合并流（`fnval=1` 的 `durl`）最高只有 720P**，不管 `qn` 要多少。它是
  WKWebView 里唯一能随意 seek 的形态（普通 mp4 文件）。
- **WKWebView 有没有 `MediaSource`，要在 http 来源的页面上测。**
  `pywebview` 的 `html=` 内联页面不是 http 来源，WebKit 会把 MSE 直接掐掉
  （`video.error.code = 4`），拿这种页面测出来的「不支持」是假的。
  2026-11-01 在真实回环地址上测：MSE 可用，H.264/H.265/AAC 都支持。
- **MSE 不能从分片中间起播。** 把 fragment 切开喂进 `SourceBuffer`，
  WebKit 回的是 decode error，整个 MediaSource 就废了。压缩画面里到处是
  假的 `moof` 标记，靠扫字节对齐 box 边界不可靠。要跳到没缓冲的地方，
  只能换成合并流让浏览器自己 seek。
- **`SourceBuffer.remove()` 是异步的**，必须等 `updateend`。没等就
  `appendBuffer()`，WebKit 回 `InvalidStateError`，画面同样会废。

## 收藏夹

- 收藏夹列表：`/x/v3/fav/folder/created/list-all?up_mid=<mid>`。
  `up_mid` 取不到就用 cookie 里的 `DedeUserID`。
- 收藏夹内容：`/x/v3/fav/resource/list?media_id=<id>&pn=1&ps=30&platform=web`。
- 写：建夹 `POST /x/v3/fav/folder/add`（`title`/`intro`/`privacy`/`csrf`），
  改名和改公开私密 `POST /x/v3/fav/folder/edit`（`media_id`/`title`/`intro`/`privacy`/`csrf`），
  删夹 `POST /x/v3/fav/folder/del`（`media_id`/`csrf`），
  加视频 `POST /x/v3/fav/resource/deal`（`rid`/`type=2`/`add_media_ids`/`csrf`）。
  `add_media_ids` 的格式是 `"<aid>:2"`，逗号分隔。
- **公开/私密看 `attr` 的第 0 位**：`attr & 1` 是 1 就是私密。实测默认收藏夹是
  `attr=1`（私密），公开的夹是 `attr=22`，私密的夹是 `attr=23`——
  22 和 23 只差最低位，正好对得上。用匿名读验证过：`attr=22` 能读，
  `attr=23` 和 `attr=1` 都是 `-403`。

## 互动

- **点赞视频**：`POST /x/web-interface/archive/like`，表单 `bvid`、`aid`、`like`（1 赞 / 2 取消）、`csrf`。
  **未登录时这个接口会回 `code 0` 但其实没生效**——所以发请求前必须自己挡掉未登录的情况，
  否则界面会显示「点赞成功」而账号那边什么也没变。
- **投币**：`POST /x/web-interface/coin/add`，表单 `multiply`（1 或 2）、`select_like`（顺便点赞）。
  余额走 `GET /x/web-interface/coin/number`。投币的风控比点赞严，还有每天的次数上限。
- **收藏**：`POST /x/v3/fav/resource/deal`，收藏用 `add_media_ids="<aid>:2"`，取消用 `del_media_ids`。
  收藏整个收藏夹是 `POST /x/v3/fav/folder/fav`（取消用 `/x/v3/fav/folder/unfav`）。
- **关注**：`POST /x/relation/modify`，`act` 是 1（关注）/ 2（取关）——**同一个路由两个方向**，
  不是两个路由。关注状态读 `GET /x/relation?fid=<mid>` 的 `data.attribute`（2 或 3 都是已关注）。
- **关注列表 / 粉丝列表**：`GET /x/relation/followings?vmid=<mid>&pn=1&ps=50&order=desc`
  和 `GET /x/relation/followers?...`，两个都要 WBI 签名。回答在 `data.list`，
  总数在 `data.total`。条目里有 `mid`、`uname`、`face`、`sign`、
  `official_verify`（认证名）和 `vip`——**没有等级**，别去读 `level_info`。
  `vmid` 不给就是当前账号，所以「我关注的」和「我的粉丝」都能用同一个方法。

## 评论

- 查看：`GET /x/v2/reply/main`，要 WBI 签名。
  - **翻页用的是 offset 协议**：下一页带 `pagination_str=json.dumps({"offset": <next_offset>})`，
    `<next_offset>` 在 **`data.cursor.pagination_reply.next_offset`**。
  - **`data.cursor.next` 不是能发回去的令牌**，它只是个页码计数
    （mode 2 是 464、441 之类的数，mode 3 是 2、3、4 这样的序号）。
    把它字符串化发回去会得到 `code -400`。
  - `cursor.is_end` **不可信**：实测 `all_count=459/460` 而只回了 3 条时它也是 `true`。
    判断还有没有下一页要看 `pagination_reply.next_offset` 在不在。
  - `mode`：2 是按热度，3 是按时间。
- **楼中楼**：`GET /x/v2/reply/reply?oid=<aid>&type=1&root=<主楼rpid>&pn=1&ps=20`。
  这个路由还是老的 `pn`/`ps` 分页，回答在 `data.replies`，总数在 `data.page.count`。
  主楼评论的 `replies` 里只内联 3 条，剩下的要靠这条路由拉。
- **发表/回复**：`POST /x/v2/reply/add`，`parent` 填对方的 rpid 就是回复。
  - 楼中楼要**同时**填 `root`：`parent` 是直接被回的那条，`root` 是主楼那条。
    只填 `parent` 不填 `root`，回复会挂到错的地方。
  - 两条都是字符串，`"0"` 表示没有。
- 点赞评论：`POST /x/v2/reply/action`，`action` 是 **1 或 -1**（不是 1 和 2）。
- 视频评论用的是 **aid**，不是 bvid。
- **评论的点赞状态读不出来**：回答里没有「当前用户赞过没」的字段
  （`action` 恒为 0，`up_action` 是 UP 主自己的动作），所以赞没赞只能前端自己记。

## 弹幕

- 读：`GET /x/v2/dm/web/seg.so?type=1&oid=<cid>&segment_index=<n>`，**回的是 protobuf**。
  顶层是 `repeated DmElem elems = 1`，字段号：`id=1 progress=2 mode=3 fontsize=4 color=5
  midHash=6 content=7 ctime=8 weight=9 action=10 pool=11 idStr=12 attr=13`。
  解析器在 `src/core/bili_danmaku.py`，是手写的（不引依赖）。
- 发：`POST /x/v2/dm/post`，表单 `type=1`、`oid`（cid）、`bvid`、`message`、`color`、
  `fontsize`、`mode`、`pool=0`、`rnd`、`ct`、`csrf`、`buvid3`。
  缺 `csrf` 回 `-111`；缺 `buvid3` 会被当成没有身份的客户端，很容易被拒。
- 段落数从 `GET /x/v2/dm/web/view?oid=<cid>` 的 `data.segs` 拿。

## 观看历史

`GET /x/web-interface/history/cursor`（WBI 签名），参数 `max`（第一页 `"0"`，之后用
`data.cursor.max`）、`view_at`（当前秒级时间戳）、`type=archive`。
`progress` 是百分比，接口给的 `-1` 表示看完了，要换算成 100。

## 个人主页

- 资料：`GET /x/space/wbi/acc/info`（WBI 签名，参数 `mid`、`token`、`platform=web`、
  `web_location=1550101`）。
- 投稿列表：`GET /x/space/wbi/arc/search`（WBI 签名，`mid`、`pn`、`ps`、`order=pubdate`）。
- 粉丝/关注/投稿数：`GET /x/space/upstat?mid=<mid>`——**这个接口不要签名**。
- **`acc/info` 对第三方客户端一律回 `-352`（风控校验失败）。** 拿不到资料的时候
  用 `GET /x/web-interface/card?mid=<mid>` 兜底，它不要签名，能给出头像、昵称、
  签名和等级。少了这个回落，别人的主页就是一片空白。

## 私信

私信走的是**另一个网关** `api.vc.bilibili.com`，不走 WBI 签名：

- 会话列表：`GET /session_svr/v1/session_svr/get_sessions`
- 某个会话的消息：`GET /svr_sync/v1/svr_sync/fetch_session_msgs`
  （`session_type=1`、`talker_id`、`session_id`、`size`、`build=0`、
  `mobi_app=web`、`platform=web`）
- 发消息：`POST /session_svr/v1/session_svr/send_msg`，`content` 是 **JSON 字符串**
  （`{"content": "文字"}`），`client_key` 用 UUID。
  （这条路由代码里是这个写法，旧笔记记的是 `/sess_svr/v1/sess/send_msg`；
  发消息是写操作，本轮没有拿真实账号试，两条都留着，谁先试通改另一条。）

2026-11-01 实测踩到的：

- **读消息是 GET，而且网关前缀是 `svr_sync`。**
  `/session_svr/v1/session_svr/fetch_session_msgs` 回 404（整页 HTML）。
  POST 到 `svr_sync` 那条回 405——路径对，方法不对。
- **会话列表只给「关注过的人」带 `account_info`**（名字 + 头像）。
  没关注过的会话这一项是空的，界面只剩一串 uid。补的办法是
  `GET /account/v1/user/cards?uids=1,2,3`——一次能问 50 个，
  网页端私信列表也是这么做的。
- **`content` 是 JSON 字符串**，里面再包一层 `{"content": "文字"}`。
  系统通知（登录提醒一类）没有 `content`，正文在 `text`、标题在 `title`。

## 投稿

投稿在 `member.bilibili.com`，跟主站 API 完全是两套，而且是**三步**：

1. `GET /x/vuweb/pre` → 拿到 `upload_id`、`endpoint`、`biz_id`、`complete_token`；
2. 把文件 multipart 上传到上一步给的 `endpoint`；
3. `POST /x/vuweb/add` 提交表单。

**上传地址和字段名都从预上传的回答里读**，不要写死——B 站改得很勤。封面要单独走一次
预上传再上传图片。

## 缓存

B 站的读接口在 `src/core/bili_cache.py` 里按类型分开缓存，寿命不一样：

| 内容 | 寿命 |
|------|------|
| 弹幕 | 24 小时 |
| 视频信息、播放地址 | 60 分钟 / 10 分钟 |
| 个人主页、主页投稿 | 30 分钟 / 15 分钟 |
| 搜索、榜单、收藏夹、统计数据 | 5–10 分钟 |
| 评论 | 5 分钟 |

**账号状态、观看历史、私信和硬币余额不缓存**——它们跟着账号变，不跟着时间变，
缓存了反而会给出过期的答案。写操作（点赞、投币、收藏、发评论）之后，
对应的缓存条目会立刻作废。

缓存键是 `"{bvid}|{页码}|{排序}"` 这种带分隔符的形式，作废的时候要按**前缀**
扔（`"{bvid}|"`），不能只扔裸 bvid——`BV10…` 开头的视频号会互相误伤。

## 分区

- **分区榜比搜索靠谱。** `GET /x/web-interface/ranking/region?rid=<rid>&day=3`
  对 rid 1、3、4、24、28、29、33、36、160 都有数据，字段跟热门视频一致。
  按关键词搜索再过滤 `tids` 的做法，在纪录片、番剧这些分区只回 0～1 条——
  分区页于是空着。
- **番剧（rid 2）是 PGC 内容**，没有 `bvid`，当前播放器用不了。分区榜对 rid 2
  也是空的。兜底办法：拿同一个关键词、不带分区过滤再搜一次 UGC 视频。
  不要把 PGC 条目硬塞进按 `bvid` 播放的模型。

## 备份

`.ncmbk` 的第 4 版多了 `bilibili` 一节，装着账号信息和收藏夹。规则见
`AGENTS.md` 的「The `.ncmbk` backup file」一节：导出只要登录任意一边，
导入时缺哪边就跳过哪边，不因为缺一部分而拒绝整个文件。

## 收藏夹的多选、批量改、合集、选集

这几条路由的参数形状都试过才写死的，装反了或者少给一项，B 站 只回一句
「请求错误」，界面上看不出哪里错了。

- **一个视频进多个收藏夹还是同一条路由。** `POST /x/v3/fav/resource/deal`，
  `rid` 是稿件 aid、`type=2`，收藏夹 id 用逗号拼在 `add_media_ids`（新勾上的）
  和 `del_media_ids`（取消勾选的）里。网页播放器就是这么发的：一张勾选表，
  一次请求改完，不是循环发 N 次。
- **勾选表要知道哪些收藏夹里已经有它。** `GET /x/v3/fav/folder/created/list-all`
  平时只给收藏夹列表，**必须带 `rid=<aid>` 和 `type=2`** 才在每条里给
  `fav_state`（1 = 已在此夹）。不带这两个参数，所有收藏夹都没有 `fav_state`，
  界面会把它们全画成未收藏。
- **批量移动 / 复制** `POST /x/v3/fav/resource/move`（或 `/copy`），参数是
  `resources`、`src_media_id`、`tar_media_id`、`mid`、`platform`。`resources`
  的每一段都是 **`<aid>:<type>`**，类型是条目的一部分，不是单独的参数——
  只给 aid 会被判成参数错误。
- **批量移出** `POST /x/v3/fav/resource/batch-del`，参数 `resources`、
  `media_id`、`platform`。
- **合集里的视频** `GET /x/polymer/web-space/seasons_archives_list`，要
  `mid` + `season_id` + `page_num` + `page_size`。合集的 id 只有配上 UP 主 的
  mid 才有意义，单独一个 season_id 什么都拿不到。条目里的封面叫 `pic`、
  时长是秒数，和收藏夹那套（`cover`、字符串时长）不是一套字段。
- **番剧选集** `GET /pgc/view/web/season?season_id=` 给 `episodes`，每集有
  `id`（ep_id）、`aid`、`cid`、`title`、`long_title`。追番列表只给最新一集的
  ep_id，想从第一集看只能问这一条，播放还是走 `bangumi_streams`。

## 批量下载的限速

会被 B 站 当成刷接口的是**取流**（`/x/player/wbi/playurl`），不是搬字节。
一次排几十个视频，几十条取流请求同时打过去就回风控错误；文件传输本身不会。

所以 `BiliDownloader` 把取流这一步排成队：一把锁 + 上一次发请求的时刻，两次
之间至少隔 `interval` 秒（默认 1.5，下载页可调，上限 10）。字节搬运不在锁里，
并发数照旧，下载速度不受影响。`interval=0` 表示不排队，测试用它验「关掉就
真的没等」。

`track_payload` 多带了一个 `aid`：收藏夹的批量接口要 aid 列表，只有 bvid 拼不出
`resources`。

## 聚合搜索要搜 PGC：番剧 / 影视 单独一条搜索

主站搜索（`search_type=video`）只回 UGC 稿件。搜「进击的巨人」得到一堆二创，
正片一条没有——所以「能播番剧」和「能搜到番剧」是两件事，别只做前者。

PGC 在两个搜索类型后面，都要问，按 `season_id` 去重（同一部剧两边都可能出现）：

| search_type | 内容 |
|---|---|
| `media_bangumi` | 番剧 / 国创 / 动画 |
| `media_ft` | 影视：电影、剧集、纪录片、综艺 |

走的是同一个 `/x/web-interface/wbi/search/type`，一样要 wbi 签名。条目里的字段：

- `title` 带 `<em class="keyword">` 高亮标签，**必须剥掉**，否则列表上显示 HTML 源码；
- `season_id` 才是播放用的 id（`bangumi_streams` 要它），`media_id` 是另一套编号；
- 评分在 `media_score.score` / `media_score.user_count`，不在顶层；
- `index_show`（"全15话"）、`season_type_name`（"国创"）、`styles`、`areas` 用来做副标题；
- 没有 `season_id` 的条目播放不了，直接丢掉。

播放入口：搜索结果点开先把剧集列表拉出来（`pgc/view/web/season` 的 `episodes`），
选哪集播哪集；不要盲点就播最新一集。

**漫画（manga）不在这条线上。** `manga.bilibili.com` 是独立站点、独立接口
（`twirp/comic.v1.Comic/Search`），裸请求回 `code 99`（要它自己的设备凭据），
而且应用里没有漫画阅读器，就算搜到也只能丢给浏览器。所以聚合搜索里没有漫画。

## 播放画质：请求的、实际在播的，要分开

`video_streams` / `bangumi_streams` 现在都回 `requested_quality` 和
`picked_quality`。前者是调用方要的，后者是 `_pick_video` 真正选中的那一条
（≤ 请求值里最高的）。

界面拿它做两件事：请求 1080P 而视频最高 720P 时，下拉框显示 720P 而不是
「自动」；只有真的请求了 0（自动）才显示「自动」。

还有一处更早的错：设置里的 `bili.quality` 只被下载读过，播放的三个入口
（内联播放器、独立窗口、番剧）全部写死传 0，所以设成什么都不生效、
看起来像没保存。三个入口现在都走同一个 `defaultBiliQuality()`。
