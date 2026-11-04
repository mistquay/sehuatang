# x1080x 新版采集运行说明

## 采集范围

新版站点使用 `/boards/{board_id}` 和 `/resources/{resource_id}`。更新 `config/config.example.yaml` 中的 `x1080x` 与 `crawler.sources.x1080x` 配置；不要只替换域名而继续请求 Discuz URL。

先提取首帖正文的磁链；没有磁链时，只领取价格、实付积分和消耗配额都明确为零的 `.torrent`。VIP 下载区、非零价格、配额消耗和异常预览不会领取。正文直出磁链可能无需登录，附件需要有效账号。

默认保留八个 BT 分类的业务名称，另配置两个独立免费板块。`categories: {}` 可关闭 BT 分类，只保留 `boards`。旧 `typeids` 数字仅作为分类配置别名；旧数字帖子失败台账保留并跳过，不猜测新版 ID。页码检查点使用新版范围，旧检查点不会沿用。

## 登录与 Cookie

推荐通过进程环境注入：

```text
CRAWLER_X1080X_USERNAME
CRAWLER_X1080X_PASSWORD
CRAWLER_X1080X_FLARESOLVERR_URL
```

第三项设置为现有 Byparr 的 `/v1` 地址。凭据不写示例配置、日志或提交。可选 `CRAWLER_X1080X_COOKIE` 导入完整 Cookie Header；使用前仍检查服务端登录态。

`x1080x.auth.mode`：

| 值 | 行为 |
| --- | --- |
| `auto` | 复用有效 Cookie；无 Turnstile 时先尝试 HTTP；需要验证时启动独立无头登录 |
| `http` | 仅复用 Cookie / HTTP 登录，需要浏览器时暂停 |
| `browser` | Cookie 失效时直接启动独立浏览器登录 |
| `disabled` | 不主动提交账号；有效 Cookie 仍可使用 |

浏览器登录采用 invisible-playwright Firefox，`auth.timeout` 默认 120 秒，`auth.headless` 默认 true。首次启动会关闭站点升级公告，再完成登录表单与验证；浏览器内和 HTTP 接管后都必须确认 `authenticated=true`。登录失败或限流停止本轮，不给整批资源累加永久失败记录。

Cookie 仅保存在进程内共享会话中，重启后按需重新认证；不保存密码、浏览器账号 profile 或下载票据到项目文件。Byparr 返回的游客账号/CSRF Cookie 不覆盖已登录账号，更新 CF 后重新请求原业务 URL。

一个账号使用一条固定线路。配置账号或账号 Cookie 时拒绝多代理线路；浏览器、HTTP 客户端与 Byparr 应使用同一出口。不要将服务器得到的 CF Cookie 搬到本机另一出口验证。HTTP 指纹沿用示例的 Firefox 系列，UA 由取得会话的浏览器同步；若部署的 Byparr 更换内核，需要重新验证指纹配合。

## 可选浏览器运行时

只采集公开正文磁链或只导入有效 Cookie 时，可保持基础依赖。启用自动无头登录时，在同一 Python 环境运行：

```bash
python -m pip install -r requirements-auth.txt
python -m invisible_playwright fetch
```

运行时版本已固定到本机验证的 invisible-playwright 0.45.0。浏览器二进制下载是安装步骤，首次在线认证不会代替这一步。Linux 还需要 Firefox 运行库；Dockerfile 的可选构建分支已声明相关包。

```bash
docker build --build-arg WITH_X1080X_BROWSER=true -t sehuatang:x1080x-browser .
```

默认构建参数为 false，基础镜像不包含浏览器。启动时传入上面的凭据和 Byparr 环境变量，并挂载私有配置。现有 256 MiB 内存上限不适合浏览器，建议先给至少 1 GiB 再按实测调整；这不是已经完成的生产容量验收。本机没有 Docker，镜像构建及服务器运行仍需部署环境验证。

Byparr 使用 Firefox，DrissionPage 使用 Chromium。本次 DrissionPage 4.1.1.4 的两次无头原型未通过本机 CF，因此未引入生产依赖。当前工作实现已通过 Firefox 登录和 HTTP Cookie 接管，后续若切换驱动，应重新验收完整链路。

## 验证与边界

`dry-run` 可以获取页面和附件预览，但不发领取 POST、不下载 torrent、不写数据库、不通知。正常采集每个免费附件先预览，再携带用户、版本、零价格与固定幂等键领取；重试复用同一领取 body，不静默改价或重新生成领取操作。收到 `receipt_id` 后由同会话下载并按原始 bencoded `info` 字节生成 v1 magnet，不在磁盘保留票据或 torrent。

本机已验证无头登录、HTTP 登录态、一个免费 torrent 的完整路径；公开正文磁链亦已通过用户提供的 Byparr 读取。目标服务器的浏览器依赖、同出口关系、容器内存与长期续期尚未现场验收。正式采集、数据库入库、通知与服务器部署不属于本次现场测试。

离线验收：隔离配置运行全部 372 项 Python 测试，5 项跳过，其余通过；字幕搜索 API 类型检查及 catalog/resources 两组测试共 70 项通过。测试包含真实 libcurl Cookie 接收、免费与收费预览、同一幂等键重试、混合正文/附件和 dry-run 不领取。

字幕搜索 API 同步支持新版字符串资源 ID 的详情地址重建；需要单独部署该仓库的相关提交后生效。
