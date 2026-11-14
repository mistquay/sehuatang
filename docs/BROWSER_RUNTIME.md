# 浏览器执行层与轻量部署

本次增加公共浏览器执行层、Obscura/CDP 接入、独立的持久 Firefox 服务，以及轻量镜像构建和验收工作流。当前默认镜像保留内置 Firefox；轻量部署通过单独的 Compose 覆盖文件启用，不改写已有私有配置。

## 1. 已实现的行为

- 三个来源可以在 `crawler.sources.<source>.browser` 中显式选择浏览器执行；普通 HTTP 为默认方式，Byparr 仍按原配置处理 HTTP 链路的 CF。
- `firefox` 使用本地已安装的 invisible-playwright；`obscura` 使用标准 Playwright/CDP；`remote_firefox` 使用外置服务的受控操作接口。
- 一个浏览器会话由一个专用线程和事件循环拥有，串行执行、复用 context，并在客户端关闭时清理。异常重建保留本身份最新 Cookie；本地 Firefox 在该会话内复用同一随机 seed。
- x1080x 公开列表不接收账号 Cookie。确认列表元素出现后重新序列化 HTML，避免返回列表插入前的壳页。
- x1080x 默认 `auth.transport=auto` 在浏览器登录后保留账号 context，后续详情与 API 在同一浏览器执行；明确选择 `http` 才走原来的 HTTP 接管验收。
- API 请求通过页面内同源 `fetch` 执行，使用浏览器网络、当前 Cookie 与 CSRF，不使用独立的 Playwright APIRequestContext。
- POST 的请求体和领取幂等键在 HTTP 状态重试中保持不变；远程服务重启返回 `browser_session_expired`，不会自动重放正在执行的 POST。
- 原始二进制经 Base64 跨执行器传输，最多 8 MiB；不会将 torrent 当 HTML 解码。原有零价格/零配额校验、失败台账、逐页保存和检查点语义沿用。

## 2. 轻量部署

镜像分工：

| 镜像标签（以 v2 分支为例） | 内容 |
| --- | --- |
| `cxsz16888/sehuatang:v2` | 当前兼容镜像，含内置 Firefox |
| `cxsz16888/sehuatang:v2-lite` | Python 应用及 Playwright 驱动，不含完整 Firefox |
| `cxsz16888/sehuatang:v2-browser-runtime` | 基于与 Byparr 相同的固定基础镜像，增加持久 Firefox 操作服务 |
| `cxsz16888/sehuatang:v2-obscura` | Obscura v0.2.7 的官方 render+stealth 发行件，构建时校验 SHA256 |

这些新标签需要新工作流成功发布后才可拉取；本地提交本身不会发布镜像。也可以从当前代码自行构建。

为私有 `.env` 设置至少 32 字节的随机令牌：

```dotenv
BROWSER_WORKER_TOKEN=在本机生成至少32字节的随机值
```

可在服务器使用 `openssl rand -hex 32` 生成。`.env` 不提交、不放入镜像，也不用于浏览器站点账号。

使用覆盖文件构建及启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.browser.yml config
docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d --build
```

覆盖文件为 x1080x 设置：公开列表直接使用外置 Firefox；账号请求选择浏览器传输。认证方式仍以现有 `auth.mode` 为准，手动 Cookie 模式不会提交账号密码。代理按原线路传给浏览器服务；容器内必须能访问该代理地址。

Firefox 服务与 Byparr 共用已核实为 v3.1.1 的基础镜像 digest：

```text
sha256:c66fa161187b8fa527bfad7eefa2647ea1c553f77df68cbffd6fe83366d1787b
```

仓库并不存在可直接使用的 `ghcr.io/thephaseless/byparr:v3.1.1` 标签，因此这里固定摘要。两个容器仍有各自的运行内存；Docker 可以复用它们相同的基础镜像层，避免主应用再打包一份 Firefox。

浏览器服务不发布宿主机端口，只在 Compose 网络内使用。它只提供创建/关闭会话、渲染、同源 GET/POST、Cookie 更新和 x1080x 登录，不提供任意 JavaScript RPC。账号与游客按独立 session ID 隔离，闲置会话 30 分钟回收，上限 16 个。

## 3. 启用 Obscura

先为私有 `.env` 增加 `OBSCURA_CDP_TOKEN`（至少 32 字节），然后启动可选服务：

```bash
docker compose -f docker-compose.yml -f docker-compose.browser.yml --profile obscura up -d --build
```

以直连的 Javbee 为例，在现有配置中合并：

```yaml
crawler:
  sources:
    javbee:
      browser:
        mode: browser
        engine: obscura
        endpoint: ws://obscura:9222
        proxy_url: ""
        timeout: 60
        wait_selector: "" # 如页面需异步加载，填真实内容的 CSS selector
```

同时为爬虫容器传入 `CRAWLER_JAVBEE_BROWSER_TOKEN`，值与 Obscura 服务的令牌一致；配套 Compose 覆盖文件已把 `.env` 中的令牌传给 Sehuatang 和 Javbee。Sehuatang 使用相同字段和 `CRAWLER_SEHUATANG_` 前缀。

Obscura 服务启动时使用固定代理。若该来源启用了代理，必须给 Obscura 的启动命令增加对应 `--proxy`，并把来源 `browser.proxy_url` 设置为完全相同的代理 URL。多线路需多个服务，并配置：

```yaml
browser:
  mode: browser
  engine: obscura
  endpoints:
    "http://proxy-host:17891": "ws://obscura-1:9222"
    "http://proxy-host:17892": "ws://obscura-2:9222"
```

缺少线路映射时拒绝启动，不会悄悄走另一出口。默认不挂载持久 profile，context 使用独立身份。

Obscura 官方不承诺通过 CF 交互式挑战。执行器遇到这种挑战会明确失败，不返回成功，也不会自动导入 Byparr 的 Firefox 身份并反复尝试。需要完整浏览器的站点使用 Firefox；不要仅为了使用 Obscura 而强制迁移验证流程。

## 4. x1080x 认证配置

原来的 `auth.mode=cookie/auto/http/browser/disabled` 继续决定是否可以提交账号。新增的传输方式是独立选择：

| `auth.transport` | 行为 |
| --- | --- |
| `auto`（默认） | 先复用原 HTTP 路径；一旦通过浏览器登录，后续保留账号浏览器执行 |
| `http` | 浏览器登录只导出状态，随后关闭浏览器并验证 HTTP 接管 |
| `browser` | 从账号状态检查开始使用浏览器；仍遵守手动 Cookie/禁用登录等模式 |

本地部署可继续使用内置 Firefox。外置服务可通过 YAML 指定：

```yaml
x1080x:
  auth:
    mode: cookie
    transport: browser
    browser:
      engine: remote_firefox
      endpoint: http://firefox:8192
```

令牌通过 `CRAWLER_X1080X_AUTH_BROWSER_TOKEN` 提供。Compose 覆盖文件已经设置相关环境变量，不需要重复修改 YAML；环境变量优先。管理页的认证模式与 Cookie 保存规则保持不变。

浏览器的 locale/timezone 默认沿用运行时自动判定；可在 browser 配置中显式填写，但应与固定出口一致。不会因为用户界面时区为上海就自动改变浏览器身份。

## 5. 验证与发布

离线回归使用示例配置隔离数据库、通知和真实代理：

```bash
python -m tests.run_offline
```

真实浏览器冒烟只访问本机模拟站点，不读项目私有配置、不使用真实账号：

```bash
python scripts/browser_smoke.py --engine firefox
python scripts/browser_smoke.py --engine obscura --endpoint ws://127.0.0.1:9222
python scripts/browser_smoke.py --engine remote_firefox --endpoint http://127.0.0.1:8192
```

Obscura 测试本机 fixture 时需要在服务启动参数中显式启用 `--allow-private-network`；生产服务默认不启用。远程 Firefox 客户端通过 `BROWSER_WORKER_TOKEN` 读取测试令牌，Obscura 客户端读取 `OBSCURA_CDP_TOKEN`。

`browser-images.yml` 构建轻量爬虫、Firefox 服务和固定版 Obscura，随后在独立内部网络中运行相同的真实链路检查，全部成功才发布镜像，并记录镜像解压后的逻辑大小。当前默认的两套完整镜像工作流保持原有发布行为。

本地真实冒烟已覆盖动态页面、Cookie、连续导航、双身份隔离、JSON、POST 与原始二进制；Firefox 还覆盖登录后的持续会话。Windows 本机结果不能代替 Linux 容器验收或真实站点 CF 长期稳定性验收。

最终本地验证：完整离线回归 431 项，426 项通过、5 项 MongoDB 集成测试按原条件跳过；Actionlint、Python AST、UTF-8 无 BOM 检查通过。官方独立 Compose v5.7.1 CLI 已验证两个 profile 的配置合并；该检查不启动容器、不构建镜像。

## 6. 尚需现场确认

- 本机没有 Docker CLI 或可用的 Docker 构建环境，Linux 镜像构建、实际尺寸与容器检查由新 CI 执行；发布前必须查看实际结果。
- 真实站点、代理出口、账号 BrowserPass、免费附件领取和至少 24 小时会话续期仍需按原方案灰度验收。
- 本地浏览器操作支持停止事件取消；远程调用受 RPC 请求超时约束。服务端明确过期时暂停，不自动重放原操作。
- Byparr 的约 304 MiB 权限调整层尚未重构。本次复用其固定基础层，没有把未重建测量的收益算入优化结果。
- 原独立 Scrapy 工程 `D:\project\python\javbee` 未在本次改动中迁移；本次公共入口覆盖 sehuatang 项目内的三个来源。
