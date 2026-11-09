# x1080x 新版采集运行说明

## 逐页处理与浏览器会话复用（2026-11-09）

正常增量采集现在优先扫描独立免费板块，每完成一页的目标筛选、详情处理和批量保存后，才读取下一页。后续列表验证失败时，已保存页保留，汇总仍包含此前的发现、已入库、权限台账跳过和保存数量。遇到第一条需要登录的资源且账号会话未通过校验时立即停止后续页；公开正文磁链继续按原规则处理，不强制登录。分页补抓的检查点和手动失败重试继续使用原机制。

列表需要 BrowserPass 时，默认优先使用镜像中已有的 invisible-playwright Firefox，在固定代理出口上复用独立的游客 browser/context/page。浏览器对象在专用线程及同一事件循环中创建、调用和关闭，不导入账号 Cookie，也不把游客 Cookie/UA 写回账号 HTTP 会话。会话在进程内保留至来源客户端关闭，不增加浏览器 profile 文件；管理页重启也会先关闭客户端并排空已入队写入，再替换进程。账号会话即使校验未通过，也不会被游客过盾结果覆盖。

`x1080x.challenge.list_renderer` 可设置为 `auto`（默认）、`browser` 或 `byparr`。`auto` 在缺少可选浏览器依赖或浏览器启动失败时回退已有 Byparr；明确选择 `browser` 时，缺少运行时会暂停。浏览器已启动后的限流、封锁或验证超时不会通过切换服务继续重试。当前 Byparr 上游不提供输入 Cookie/持久会话接口，选择 `byparr` 仍可能每页重新验证；仅缓存它返回的 Cookie 不能保证跨页复用。

一旦列表浏览器会话建立，后续列表页直接在该会话中导航，不再先发送 HTTP 列表请求并重复触发 CF。详情和附件仍使用账号 HTTP 会话，列表的游客会话不参与认证或领取。

认证诊断只记录 HTTP 状态、布尔登录态、允许列表内的机器错误码，以及浏览器失败阶段/固定错误标签，不记录响应正文、账号、Cookie 或票据。HTTP 401 或服务端明确未登录时，手动模式以 `cookie_expired` 暂停；未知 403 为 `session_forbidden`，BrowserPass 和 CF 验证分别报告，不再把所有 403 归为 Cookie 过期。自动登录模式同样不会因未知 403 或网络异常盲目提交凭据。

线上 2026-11-09 16:40 / 17:00 两轮均成功读取 30 页、发现 600 条，50 条已入库、480 条因权限台账跳过，剩余目标因账号状态未通过校验而暂停。修复不会自行刷新用户的手动 Cookie；部署后应在同出口下更新有效会话，确认首个免费板块的单页详情与入库结果，再验收整轮采集。代码与离线检查通过不代表线上账号认证已恢复。

## 采集范围

新版站点使用 `/boards/{board_id}` 和 `/resources/{resource_id}`。更新 `config/config.example.yaml` 中的 `x1080x` 与 `crawler.sources.x1080x` 配置；不要只替换域名而继续请求 Discuz URL。

先提取首帖正文的磁链；没有磁链时，只领取价格、实付积分和消耗配额都明确为零的 `.torrent`。VIP 下载区、非零价格、配额消耗和异常预览不会领取。正文直出磁链可能无需登录，附件需要有效账号。

默认保留八个 BT 分类的业务名称，另配置两个独立免费板块。`categories: {}` 可关闭 BT 分类，只保留 `boards`。旧 `typeids` 数字仅作为分类配置别名；旧数字帖子失败台账保留并跳过，不猜测新版 ID。页码检查点使用新版范围，旧检查点不会沿用。

## 升级时核对域名和分类配置

更新程序不会自动替换现有私有 YAML 配置。当前正式站点使用 `https://1080xin.com`；部署时同时检查 `x1080x.base_url`、`crawler.sources.x1080x.base_url` 和 `CRAWLER_X1080X_BASE_URL`，环境变量仍优先。2026-11-04 实测旧域名 `https://agaghhh.cc` 对新版 `/boards/...` 路径返回 HTTP 200、标题“歡迎訪問 · x1080x”，页面只有入口链接，没有资源列表。HTTP 连通或线路状态 healthy 不等于业务页面正确。

板块 `board_id`、分类 `category_id`、资源 `resource_id/source_key` 是三类不同 ID，新站均使用保留大小写的 22 位字母数字字符串。例如旧分类配置别名 `5212` 会映射到 `5CfYpFiXzzHoUtfJyFd3FV`；它不是新版资源 ID，也不会作为数字参数发送到站点。推荐将旧 `typeids` 改为示例中的 `categories`，直接使用新版 ID。旧别名仅为已有配置和 CLI 兼容保留。

日志现在记录实际 `board_id`、`category_id` 和失败请求 URL，诊断文件名也使用实际 ID。欢迎入口页会以 `site_entry_required` 暂停本轮，不再重复扫描八个分类或尝试在入口页登录；这种问题也不写入每个资源的失败台账。Web 补抓支持直接输入新版分类或板块 ID，按字符串提交；`--typeid` 参数名称为兼容保留，值可以是新版字符串。

本次服务器已将旧域名与八个数字分类配置更新为正式域名及新版分类 ID，保留账号和单代理设置。通过服务器中文字幕第一页 dry-run 验证：发现并请求 20 个新版字符串资源 ID，不再出现 `invalid_list`；这 20 条详情均返回会员权限限制，成功资源数为 0，没有领取、资源写入或推进检查点。这次验收确认域名和列表解析恢复，不代表这批受限帖子可由普通账号免费下载。

本次代码完整离线回归 388 项，5 项跳过，其余通过；另在本地浏览器拦截补抓请求，验证新版 ID 和旧别名保留为字符串，sehuatang 的 fid 仍按数字提交。

## 有板块标题却报 invalid_list（BrowserPass）

2026-11-08 公开页面初始 Next.js 数据包含 `BoardAdmissionRecovery` 和 `browserPassRequired=true`，资源列表在浏览器完成 BrowserPass 后再异步加载。HTTP 200 和“無碼BT / 有碼BT”标题不代表响应已经包含列表；反复请求同一初始页面仍可能没有 `.thread-list`。

爬虫现在识别这种初始页面，使用当前线路已配置的 Byparr 渲染公开列表，再按原规则验证当前页、末页和资源 ID。已包含有效列表的响应仍直接解析。Byparr 读取公开列表时不提交账号，也不将游客 Cookie 或 User-Agent 覆盖到爬虫的登录会话；附件预览、领取和下载继续使用原账号会话。

缺少验证服务或渲染后仍停在初始页面时，本轮以 `browser_pass_required` 暂停；检查现有 `challenge.flaresolverr_url` 和 Byparr 的同出口配置后重试。限流与明确封锁分别保留为 `rate_limited`、`cf_blocked`。浏览器返回错误页码或缺失分页结构时仍视为无效列表，不能作为末页推进检查点。此路径用于公开列表，不授予游客访问账号专属板块或深分页的权限。

本次完整离线回归 398 项，393 项通过、5 项按既有条件跳过。只读现场检查确认無碼BT 首页可解析 20 条；另以初始壳页触发修复后的真实 Byparr 加载路径，有碼BT 首页、無碼BT 第 2 页各解析 20 条，页码校验通过。没有领取附件、写数据库、通知或部署；这不代表服务器已运行修复版本，也不代表登录后的详情采集已验收。

## discovered 很多但 requested 为零

`existing` 统计已入库、按 source_key 命中的资源；`skipped_restricted` 统计未入库但被权限/价格/封锁失败台账挡住的资源。这两个计数互相独立。明确的 `membership_required` 等结果记入停止自动重试的台账后，下次列表仍能发现同一 ID，但不会再次请求详情。只有访问权限确已变化时，才需要在失败台账里重新入队，不要为了增加请求量反复请求已锁定的 VIP 帖子。

2026-11-04 21:20 的服务器日志 `discovered=480 existing=0 skipped_restricted=480 requested=0`，对应前一轮截至 21:00 已请求过的 480 条详情，全部返回 `membership_required`。远端失败台账最近 500 行也均为该原因。公开抽查一条帖子发布时间为 2023 年，正文明确显示“加入 VIP 查看內容”。BT 综合区中文字幕第一页的条目日期为 2026-05-07，不能将这些条目当作最近半个月发布的新资源。

当时服务器只有八个 BT 分类，`boards` 为空，没有启用示例里的两个独立免费板块。已补上无碼BT、有碼BT，保留原分类、代理和账号；无碼BT 第一页能看到 2026-11-04 的帖子。单页 dry-run 发现 20 个新目标并进入详情处理，随后因 `browser_unavailable` 暂停：当前配置为自动登录，但已部署镜像不含可选浏览器运行时，没有领取或入库。

继续采集可使用 Web 页面里的手动 Cookie 模式，填写完整 Cookie 与同次登录的 User-Agent，保存后重启；或构建启用了 `WITH_X1080X_BROWSER=true` 的镜像。当时的官方构建流程没有打开该参数，因此部署的基础镜像不具备自动无头登录能力。后续已按用户要求更新两套 Actions，当前构建行为见下文；未清空旧权限台账，受限记录也没有被错误地计为已入库。

汇总日志增加 `partitions`、`free_boards`，全部因台账跳过时额外解释这两个计数。本次相关 57 项离线测试通过；这轮现场验证没有完成登录后的免费附件采集，仍需要有效 Cookie 或浏览器运行时。

## Web 配置与手动 Cookie（推荐）

在管理页打开“配置 → x1080x 登录与 Cookie”，无需设置环境变量或手写 YAML：

1. 选择“手动 Cookie（推荐，不自动登录）”。新配置默认使用此模式；已有显式 `auto` 配置继续有效。
2. 手动登录网站并完成 CF。在浏览器开发者工具的“网络”中，复制一个已登录请求的完整 `Cookie` 请求头内容，粘贴到页面。仅需头部值，不含 `Cookie:` 字样。
3. 同时复制该请求的 `User-Agent`，按实际登录浏览器选择 Firefox 或 Chrome / Edge。Cookie 和 User-Agent 应来自同一次登录，并与采集保持相同网络出口。
4. 点击“保存 x1080x 配置”，按项目现有规则重启服务生效。页面保存到现有 YAML 配置文件，没有新增凭据存储。

不要只复制 `document.cookie`：它看不到 HttpOnly Cookie，可能缺少 `__Host-x1080x_session` 等账号会话。完整 Cookie 还应包含浏览器会话、CSRF 与 CF 状态。页面只显示密码、Cookie 是否已保存，不回填原值；输入框留空会保留，输入新值会替换，勾选“清空”才会删除。

手动 Cookie 模式不提交账号密码，也不启动登录浏览器，即使旧配置中仍保存了账号密码。有效 Cookie 用于详情、附件预览、零价格领取和文件下载；失效后以 `cookie_expired` 暂停本轮，回到页面更新 Cookie 即可。网络错误或限流仍按实际原因暂停，不误报 Cookie 失效。程序仍执行免费领取和 torrent 转磁链，不处理账号登录。

高级 YAML 编辑器继续保留。专用表单和 YAML 都有未保存修改时，会要求先保存或重新加载其中一处，防止相互覆盖。配置保存按原机制生成备份；配置文件、备份和临时文件已排除出 Git 与镜像构建上下文。

## 可选自动登录与旧环境变量兼容

若希望失效后自动重新登录，可在同一表单选择“账号自动登录”，填写账号密码。只有这类模式需要下文的可选浏览器依赖。

以下环境变量仍兼容已有部署，并优先于页面保存的对应字段；页面检测到覆盖时会提示字段名。要完全通过页面管理，请在部署配置中移除对应覆盖变量，再重启：

```text
CRAWLER_X1080X_USERNAME
CRAWLER_X1080X_PASSWORD
CRAWLER_X1080X_FLARESOLVERR_URL
```

第三项设置为现有 Byparr 的 `/v1` 地址，也可继续在高级 YAML 的 challenge 配置中填写。可选 `CRAWLER_X1080X_COOKIE` 覆盖完整 Cookie Header；使用前仍检查服务端登录态。凭据不写示例配置、日志或提交。

`x1080x.auth.mode`：

| 值 | 行为 |
| --- | --- |
| `cookie` | 默认、推荐；仅使用手动 Cookie，失效后暂停等待更新，绝不自动登录 |
| `auto` | 复用有效 Cookie；无 Turnstile 时先尝试 HTTP；需要验证时启动独立无头登录 |
| `http` | 仅复用 Cookie / HTTP 登录，需要浏览器时暂停 |
| `browser` | Cookie 失效时直接启动独立浏览器登录 |
| `disabled` | 不主动提交账号；有效 Cookie 仍可使用 |

可选浏览器登录采用 invisible-playwright Firefox，`auth.timeout` 默认 120 秒，`auth.headless` 默认 true。首次启动会关闭站点升级公告，再完成登录表单与验证；浏览器内和 HTTP 接管后都必须确认 `authenticated=true`。登录失败或限流停止本轮，不给整批资源累加永久失败记录。

页面填写的密码和 Cookie 保存到项目私有 YAML 配置，按现有机制备份；运行时更新的 Cookie 只保存在进程内，重启后使用配置中手动提供的 Cookie，或在自动模式下按需认证。不额外保存浏览器账号 profile 或下载票据。Byparr 返回的游客账号/CSRF Cookie 不覆盖已登录账号，更新 CF 后重新请求原业务 URL。

一个账号使用一条固定线路。配置账号或账号 Cookie 时，如果继承了全局多代理列表，程序会固定使用列表第一条并输出一次提示；游客采集保留多线路，其他来源的代理池不受影响。浏览器、HTTP 客户端与 Byparr 应使用同一出口。不要将服务器得到的 CF Cookie 搬到本机另一出口验证。HTTP 指纹沿用示例的 Firefox 系列，UA 由取得会话的浏览器同步；若部署的 Byparr 更换内核，需要重新验证指纹配合。

需要指定其他固定线路，或旧版本报“x1080x 账号认证请使用一条固定代理线路”时，在高级 YAML 中仅覆盖 x1080x：

```yaml
crawler:
  sources:
    x1080x:
      http:
        proxy:
          enabled: true
          url: "http://proxy-host:17891" # 换成现有代理池中选定的实际地址
          urls: [] # 必须清空继承列表，只写 url 不会覆盖全局 urls
```

保存后重启加载。不要为了 x1080x 改掉全局四线配置。如果设置了 `CRAWLER_X1080X_PROXY_URLS`，它仍优先于 YAML；可以移除它或改用单个 `CRAWLER_X1080X_PROXY_URL`。

## GitHub Actions 镜像内置浏览器运行时

`.github/workflows/docker-build-push.yml` 和 `docker-build-simple.yml` 现在均显式传入 `WITH_X1080X_BROWSER=true`。继续使用原镜像及其原有分支/版本标签（例如 `cxsz16888/sehuatang:v2`），浏览器运行时和爬虫包含在同一镜像中。手动 Cookie 与自动登录模式都可以使用它，不需要在已启动容器中再执行 pip 或下载浏览器。

镜像包含固定版本的 invisible-playwright、对应 Firefox 二进制、Xvfb、字体和 GTK/OpenGL 等运行库。基础系统固定为 Python 3.11 / Debian Trixie，使用实际存在的 t64 系统库包名。缓存统一为 `/opt/invisible-playwright`，构建下载和运行登录使用同一路径。

Docker 构建会断网执行 `python scripts/check_x1080x_browser.py`：要求浏览器已存在，启动隐藏显示与 Firefox，打开本地 data 页面并执行 JavaScript。缺少二进制、Xvfb 或动态库时构建失败。发布后 Actions 还会按本次构建的镜像 digest 运行同一检查和隔离健康检查；主工作流会启动真实 Web 进程并验证管理接口。检查不访问站点、不使用账号、不采集资源。

Actions 成功后，在服务器更新 Compose 配置和镜像再重建采集容器：

```bash
docker compose pull sehuatang-crawler
docker compose up -d --no-deps sehuatang-crawler
```

Compose 示例已增加 `init: true`、256 MiB 共享内存，并把采集容器内存上限由 256 MiB 调整为 1 GiB、CPU 上限调整为 1 核。已有部署也需要同步检查资源限制，单纯更新镜像不会替换服务器的 Compose 文件。1 GiB 是登录试运行的起点，仍需按实际负载观察。

## 本地安装与自行构建

不使用镜像、直接运行 Python 时，启用自动无头登录需要在同一环境运行：

```bash
python -m pip install -r requirements-auth.txt
python -m invisible_playwright fetch
```

运行时版本已固定到本机验证的 invisible-playwright 0.45.0。浏览器二进制下载是安装步骤，首次在线认证不会代替这一步。Linux 还需要 Xvfb 和 Firefox 运行库，Dockerfile 中已声明相关包。

```bash
docker build --build-arg WITH_X1080X_BROWSER=true -t sehuatang:local .
```

Dockerfile 的可选参数默认仍为 false，自行构建时需像上面一样显式传 true；两套 Actions 已固定传 true。挂载私有配置后，通过 Web 表单选择自动登录并保存账号密码即可。本机没有 Docker，本地已完成工作流静态校验和 Windows 浏览器启动检查；Linux 镜像的实际构建和断网冒烟检查须由更新后的 Actions 完成，不能将本地检查视为服务器登录验收。

Byparr 使用 Firefox，DrissionPage 使用 Chromium。本次 DrissionPage 4.1.1.4 的两次无头原型未通过本机 CF，因此未引入生产依赖。当前工作实现已通过 Firefox 登录和 HTTP Cookie 接管，后续若切换驱动，应重新验收完整链路。

## 验证与边界

`dry-run` 可以获取页面和附件预览，但不发领取 POST、不下载 torrent、不写数据库、不通知。正常采集每个免费附件先预览，再携带用户、版本、零价格与固定幂等键领取；重试复用同一领取 body，不静默改价或重新生成领取操作。收到 `receipt_id` 后由同会话下载并按原始 bencoded `info` 字节生成 v1 magnet，不在磁盘保留票据或 torrent。

本机已验证无头登录、HTTP 登录态、一个免费 torrent 的完整路径；公开正文磁链亦已通过用户提供的 Byparr 读取。目标服务器的浏览器依赖、同出口关系、容器内存与长期续期尚未现场验收。正式采集、数据库入库、通知与服务器部署不属于本次现场测试。

离线验收：Web/Cookie 配置改造后，隔离配置运行全部 380 项 Python 测试，5 项跳过，其余通过。新增回归覆盖手动模式不登录、秘密字段不返回、留空保留/明确清空、来源覆盖层与非法输入；浏览器使用独立虚拟配置验证模式切换、Cookie 替换/清空、YAML 编辑保护和手机横竖屏布局。本轮没有使用实际账号登录或更新真实配置。

此前字幕搜索 API 类型检查及 catalog/resources 两组测试共 70 项通过。既有回归包含真实 libcurl Cookie 接收、免费与收费预览、同一幂等键重试、混合正文/附件和 dry-run 不领取。

2026-11-04 服务器代理初始化修复：发现三个来源都继承全局四线配置，x1080x 自动登录触发旧版硬限制，导致采集初始化和整个 `/api/network` 失败。仅为 x1080x 显式固定原代理池第一条（17891 端口），确认空闲后通过管理接口重启；监测恢复为 HTTP 200，三个来源分别显示 4/4/1 条线路，x1080x 单线路检测完成后状态为 `healthy`。没有触发正式采集或修改账号、Cookie。新增代码回归覆盖账号继承代理池、手动 Cookie 和游客模式，以及监测与采集复用同一会话池；对应完整离线测试 382 项，5 项跳过，其余通过。

字幕搜索 API 同步支持新版字符串资源 ID 的详情地址重建；需要单独部署该仓库的相关提交后生效。
