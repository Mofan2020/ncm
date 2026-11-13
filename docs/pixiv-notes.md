# Pixiv 接口笔记

全部结论都在本机实测过（2026-11-12），除非段落里另写了「文档说」。走的是
**网页版** `www.pixiv.net/ajax/*`，不是 App API：App API 要一个只有手机端
能签出来的 OAuth refresh token，第三方拿不到。

## 会话

- 登录态就是 `PHPSESSID`。另外还有 `device_token`、`p_ab_id` 等等，跟它一起
  存：pixiv 拿这些认设备，少了它每次重启都被当成新机器。
- 登录由**网页自己完成**（`accounts.pixiv.net/login`），程序只从 webview 的
  cookie store 里把它取出来。`document.cookie` 拿不到——它是 HttpOnly，
  必须走 `window.get_cookies()`（pywebview 在 macOS 走 `WKHTTPCookieStore`，
  Windows 走 WebView2 的 `CookieManager`，两边都含 HttpOnly）。
- 注册在同一个窗口里（页面上有入口），因为注册全程有验证码，第三方替不了。
- **写操作要 CSRF**：`X-CSRF-TOKEN`，值在首页 HTML 里，
  `__NEXT_DATA__` → `props.pageProps.serverSerializedPreloadedState`（它本身
  是一段**被转义过的 JSON 字符串**）→ `api.token`。老页面是
  `<meta name="global-data" content='...'>`，账号字段还是平铺的。
- 同一个 preload 里还有 `userData.self`，账号面板要的名字/头像/会员都在那儿：
  一次请求拿全，比再调三个接口稳。

## 包络

`/ajax/*` 一律是 `{error, message, body}`。**失败是 HTTP 200 + `error: true`**，
而且 `body` 这时可能是空数组而不是对象。`/ranking.php` 和两条老 PHP 路由
（`/bookmark_add.php`、`/rpc_group_setting.php`）**没有包络**，字段是
下划线风格（`illust_id` / `user_id` / `illust_type`）。

## 读接口（实测 200）

| 用途 | 路由 | 备注 |
|------|------|------|
| 搜索 | `/ajax/search/artworks/{word}` | `mode=safe/all/r18`、`s_mode`、`order`、`ai_type=1` 隐藏 AI |
| 作品详情 | `/ajax/illust/{id}` | `urls` 齐全、`bookmarkData.id` 是取消收藏要的号 |
| 多页 | `/ajax/illust/{id}/pages` | 单页作品也回一条 |
| 动图 | `/ajax/illust/{id}/ugoira_meta` | 静止图回 **404 + 「您所指定的ID不是动图」**，这是正常答案 |
| 用户 | `/ajax/user/{uid}?full=1` | **没有作品数**，两个字段都是 null |
| 用户全部作品 id | `/ajax/user/{uid}/profile/all` | 只有 id，键是 id 值也是 id |
| 按 id 取作品 | `/ajax/user/{uid}/illusts?ids[]=…&work_category=illustManga` | 按 id 作键回答，**顺序不可信** |
| 收藏 | `/ajax/user/{uid}/illusts/bookmarks` | `tag` / `offset` / `limit` / `rest=show|hide|''` |
| 关注列表 | `/ajax/user/{uid}/following` | `total` + `users` |
| 关注动态 | `/ajax/follow_latest/illust` | 要登录 |
| 排行 | `/ranking.php?format=json&mode=daily&content=illust&p=1` | 下划线字段 |
| 发现 | `/ajax/discovery/artworks`（要登录）/ `/ajax/illust/discovery`（免登录，最多 18 条） | 前者回 `thumbnails.illust` |

## 写接口（未用真实账号打过全链路）

| 动作 | 路由 | 形状 |
|------|------|------|
| 收藏 | `POST /ajax/illusts/bookmarks/add` | JSON `{illust_id, restrict, comment, tags}`，`restrict=1` 是私密 |
| 取消收藏 | `POST /ajax/illusts/bookmarks/delete` | **表单** `bookmark_id=…`；发 JSON 回 400 |
| 点赞 | `POST /ajax/illusts/like` | JSON `{"illust_id": "…"}`；**没有取消赞的接口** |
| 关注 | `POST /bookmark_add.php` | 表单 `mode=add&type=user&user_id=…&tag=&restrict=0&format=json` |
| 取关 | `POST /rpc_group_setting.php` | 表单 `mode=del&type=bookuser&id=…` |

**取消收藏要的是 `bookmarkData.id`，不是作品 id**——两个号不一样，传错对端
回 400 且不解释。点赞不可撤销，所以界面上先问一句。

## 图片

`i.pximg.net` 对没有 `Referer: https://www.pixiv.net/` 的请求一律 403，而
`<img>` 设不了请求头。所以图片全部走媒体服务器的 `/pixiv` 路由（主机白名单
`i.pximg.net` / `s.pximg.net`，小图落盘缓存）。

尺寸可以从任何一个已知地址推出来：去掉 `/c/<size>/` 前缀，`img-master` ↔
`img-original` 互换，文件名后缀 `_master1200` / `_square1200` / `_custom1200`
去掉即原图。**画师设了自定义缩略图时路径段是 `custom-thumb`**，直接替换会拼出
403 的地址；`image_url_for()` 处理了这个。原图的扩展名可能是 `.png`，推出来的
`.jpg` 会 404，调用方要能退回 `regular`。

## 成人内容

`xRestrict`（0 安全 / 1 R-18 / 2 R-18G）是权威字段，`sl >= 6` 和标签
（`R-18`、`R-18G`、`18禁`…）是补漏，三个判据都留着——有的作品带标签没 flag，
有的两个都没有只有 `sl`。

服务端也会筛（`mode=safe`），但那只对「账号自己开了 R-18」的用户生效，所以
**客户端要再筛一遍**，否则设了「不显示」搜索里照样冒出来。
