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
- **单文件（muxed）流的主机换过域名。** B站现在给 muxed 流的主机是
  `*.edge.mountaintoys.cn:4483`，不在上面的白名单里。代理会回 403，
  而那个地址**直连是好的**（206）——所以症状是「视频一个都放不了」，
  看起来像播放器坏了，其实是代理拒绝转发。
  macOS 上尤其致命：WKWebView 没有 `MediaSource`，只能吃 muxed 单文件流，
  所以白名单漏了这一个域 = 全部视频都播不了。
  白名单在 `src/core/mediaserver.py` 的 `_BILI_HOST_SUFFIXES`，
  加域名时只加明确后缀，别把回环服务变成开放代理。
- **画质是账号说了算。** `accept_quality` 给出这次登录能看的画质，
  `support_formats` 给出清晰度名称。没有大会员时 1080P 以上拿不到，
  这不是 bug。
- **无损音质在 `dash.flac` 里**（`codecid` 30280 一类），杜比在
  `dash.dolby` 里。两者都只有部分视频有。

## 收藏夹

- 收藏夹列表：`/x/v3/fav/folder/created/list-all?up_mid=<mid>`。
  `up_mid` 取不到就用 cookie 里的 `DedeUserID`。
- 收藏夹内容：`/x/v3/fav/resource/list?media_id=<id>&pn=1&ps=30&platform=web`。
- 写：建夹 `POST /x/v3/fav/folder/add`（`title`/`intro`/`privacy`/`csrf`），
  加视频 `POST /x/v3/fav/resource/deal`（`rid`/`type=2`/`add_media_ids`/`csrf`）。
  `add_media_ids` 的格式是 `"<aid>:2"`，逗号分隔。

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

## 评论

- 查看：`GET /x/v2/reply/main`，要 WBI 签名。翻页用回答里的 `data.cursor.next`
  （**它是 JSON 字符串**，要先 `json.loads` 再当参数传回去）。
- 发表/回复：`POST /x/v2/reply/add`，`parent` 填对方的 rpid 就是回复。
- 点赞评论：`POST /x/v2/reply/action`，`action` 是 **1 或 -1**（不是 1 和 2）。
- 视频评论用的是 **aid**，不是 bvid。

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

- 会话列表：`GET /session_svr/v1/session/notify_fetch`
- 某个会话的消息：`POST /sess_svr/v1/sess/get_messages`
- 发消息：`POST /sess_svr/v1/sess/send_msg`，`content` 是 **JSON 字符串**
  （`{"content": "文字"}`），`client_key` 用 UUID。

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
