# x1080x 新版站点调查与改造方案

调查日期：2026-11-04（北京时间）。状态：调查完成，架构与字段调整方案待审核；尚未修改爬虫实现。

## 1. 实测结论

- 官网升级公告说明，站点于 2026-10-29 完成从 PHP Discuz 到新架构的整体重构。
- 浏览器和响应 HTML 可确认使用 Next.js；这不等于已确认其后端语言或数据库。
- 新版板块、分类、资源使用 22 位字母数字字符串 ID，不能继续 `int(tid)`。
- 正常完成验证并登录后，分类列表及详情正文可通过共享 Cookie 的普通 HTTP 请求取得，存在服务端渲染内容。暂不需要把整个采集引擎改成浏览器抓取。
- 普通未验证请求返回过 Cloudflare 403；登录表单还使用 Turnstile。实测均由浏览器自动完成验证，没有人工解题。
- 登录成功，会员中心显示提供的账号为“注册会员”。该账号可以免费下载实测免费附件，但无法读取实测超过六小时的 BT 综合区下载内容。
- 资源至少有“正文直接出现磁链”和“附件 torrent”两种形式，应并列支持。
- 本次仅登录、浏览和领取一份明确为免费的附件；未付费、未领取收费附件，未运行正式采集、数据库写入或通知。

## 2. 新版路由和分类

BT 综合区：

`/boards/7RHPO2w4jFUYiDhDsxz0wE`

分类分页示例：

`/boards/7RHPO2w4jFUYiDhDsxz0wE?category_id=6gGAPsMAGtQzqxIYyAQBbS&page=1`

详情示例：

`/resources/3VDY80SBcybkGiHoNji7Qd?page=1&order=asc`

实测点击列表第二页后 URL 使用 `page=2`。每页 20 个普通主题，另有置顶公告。分页组件仍有 `.pg`，但页码是按钮，末页可读取跳页输入框的 `max`。BT 全部列表调查时为 6016 页，此数字不是配置常量。

| 旧 typeid | 旧业务分类名称 | 新 category_id | 新页面名称 |
| --- | --- | --- | --- |
| 5212 | 国内成人 | 5CfYpFiXzzHoUtfJyFd3FV | 國產品牌 |
| 5206 | 亚洲有码 | 7aUtFOJbmFXIMDEx8Smqkm | 有碼HD |
| 5207 | 亚洲无码 | 2lrSOyEwyacrCPS2aN7ZM6 | 無碼HD |
| 5208 | FC2 | 1v7HJi1gXdgaOohKjlK9L0 | FC2HD |
| 5216 | MGS | 1RzNrulROCeqHTIW8wSADv | MGSHD |
| 5479 | 中文字幕 | 6gGAPsMAGtQzqxIYyAQBbS | 中文字幕 |
| 5213 | 探花精选 | 0hbFa1xIVkbRIvm6oOYbff | 探花精選 |
| 5217 | 主播精选 | 7EHxTALDTN1Yn12YTGMjdY | 主播精選 |

新页面还有“其他”分类 `2MwX9VbnIQu2FmMzWbI0Qe`。以上对应关系来自新旧配置及页面分类的语义对应，不代表站点提供了历史 ID 转换表。

免费板块另有：

- 无码 BT：`/boards/3zfPdYrZvRsB5MYMHwE70K`
- 有码 BT：`/boards/5PDZcHM9127Cbn21wUhl1q`

建议配置显式区分 `board_id` 和 `category_id`，允许选择免费板块。首轮仍以原 BT 综合区的八类为默认范围，免费板块按配置启用，不默认扩展到全站 VIP 板块。

旧的两个实测 `forum.php` 地址均返回 307 到 `/membership`，不是新资源的对应详情。因此不能继续依靠旧 URL 重建进行失败重试，也不能假定数字 tid 可直接转换为新 ID。

## 3. 两条下载路径与权限

### A. 正文直接提供磁链

用户提供的样本：

<https://1080xin.com/resources/3VDY80SBcybkGiHoNji7Qd?page=1&order=asc>

登录后实测：首帖 `.post-text .discuz-body` 的普通文本中包含完整 `magnet:?xt=urn:btih:...&dn=...`，附件数为 0。HTML 中 `&` 被编码为 `&amp;`，BeautifulSoup 文本和现有 `unescape` 处理可以复用。不能只扫描 `a[href]` 或附件列表。

标题来自 `.thread-header h1`；某些页面 h1 内还有 `.topic-tags`，提取标题时应排除分类标签。

日期优先使用 `#first-post time[datetime]`，按站点显示的北京时间转换后保存；不能直接把 UTC ISO 字符串截成日期。例如此样本为 `2026-10-30T17:11:28.535Z`，站点日期是 2026-10-31。

正文和图片只在 `#first-post .post-text` 内提取，避免混入头像、签名、回复和公告。图片继续过滤 GIF。

隔离游客 HTTP 探测取得过只有标题和统计、没有 `#first-post` 的初始 HTML；未完成该样本游客浏览器动态加载的对照实验，不能据此断言所有直出磁链页面都必须登录。

### B. 免费 torrent 附件

实测样本：

<https://1080xin.com/resources/4W0AwdtH70hoJa7qQUDTtg?page=1&order=asc>

- 游客状态：`.attachment.is-denied.is-guest` 显示“需登入”。
- 登录后：同一附件显示“免費下載”。附件标识来自 `data-slot-id`。
- 正常点击后的实际请求链路：

```text
GET  /api/slots/{slot_id}/preview
POST /api/downloads/claim
GET  /api/downloads/file/{临时领取标识}
```

领取请求使用 `X-CSRFToken`，JSON 字段包括：

```text
expected_user_id
slot_id
expected_version
expected_price
expected_payable
idempotency_key
```

预览响应提供 `viewer_id`、`version_id`、`status`、`reason`、`file_ready`、`price_points`、`payable_points` 和配额相关字段。

此次实测 `status=claimable`、`reason=free`、`price_points=0.00`、`payable_points=0.00`、`quota_units=0`。下载得到 148186 字节的有效 v1 torrent，可以从原始 `info` 字典字节计算 40 位 SHA-1 info hash，生成现有系统使用的磁链。

实现必须以最新预览为准，确认账号、附件版本与零价格，再领取；一次领取的网络重试复用同一个幂等键。收费、配额消耗、价格变化等情况不能自动当作免费资源处理。临时下载标识和链接不作为长期资源 URL 保存。

生成 info hash 时必须对原始 `info` 字典字节计算哈希；不能未经确认地解码再重编码后计算。种子可只在内存中处理，不需要长期保存文件。

### C. 登录后仍受 VIP 限制

实测 BT 样本：

<https://1080xin.com/resources/7dxJB6xmNXpaVuGLR46nxR>

普通会员登录后 `.body-locked` 明确显示“本帖的6小時限時免費查看已結束”，没有磁链和附件。登录不等于获得所有历史资源权限。

`/api/resources/{resource_id}/state` 的 `read.allowed=true` 不能直接解释成下载内容已解锁；此样本允许阅读帖子，但下载区仍然被锁定。

## 4. 登录、验证和 HTTP 会话

实测登录使用 `POST /api/auth/login`。登录请求字段包括 `username`、`login_type`、`password`；完整验证码/请求头协议需在实现阶段继续按真实请求核对，不能只凭这三个字段拼装登录。

页面和请求还观察到：

- `GET /api/auth/turnstile/settings`
- `GET /api/auth/csrf`（包含浏览器相关参数）
- `POST /api/auth/browser-pass`
- 初始页面公开脚本中的 ECDSA P-256 BrowserPass 逻辑。

登录后的 Cookie 包括 Cloudflare 通行、账号会话、只读会话、浏览器会话和 CSRF Cookie。不能只复制一项账号 Cookie，也不能把 CF 通过等同于账号登录完成。

推荐先采用“浏览器完成登录和验证 + 同线路 HTTP 复用会话”的方式：

1. 凭据由环境变量或本机私有配置提供；禁止写入提交、示例配置、调试 HTML 或日志。
2. 提供明确的登录初始化/续期入口，完成站点正常验证并取得当前会话。
3. 列表、详情、预览与领取尽量走 HTTP；本次共享 Cookie 的 APIRequestContext 已验证可以读取分类 SSR 列表。
4. 实现阶段再验证现有 `curl_cffi` 对 Cookie/UA 的复用效果；APIRequestContext 成功不等于已经验证 `curl_cffi` 和生产代理池。
5. 一个账号先使用一条固定线路和低并发。会话、CSRF 和 CF 通行状态按线路维护；不直接把一组 Cookie 分发到不同出口。
6. 失效后停止本轮、提示重新登录或续期；不让每个详情 worker 同时发起登录，也不反复提交账号密码。

如果后续确认可稳定通过站点正常登录 API 完成全部认证，再简化掉浏览器初始化依赖。当前不承诺仅凭用户名密码的纯 HTTP 无人值守登录已经可用。

## 5. 建议代码调整

| 位置 | 调整 |
| --- | --- |
| `scrapers/sources/x1080x/source.py` | 新板块/分类 URL，字符串 ID，新的发现及重试规则 |
| `scrapers/sources/x1080x/parser.py` | 新列表与首帖 DOM、按钮分页、日期时区、正文磁链及权限提示 |
| `scrapers/sources/x1080x/http_client.py` | 会话接入、登录态检查、CSRF 和来源专用请求能力 |
| 新增来源专用附件采集模块 | 价格预览、免费领取、下载及 torrent 转磁链；纯 HTML parser 不发网络请求 |
| `scrapers/x1080x_scraper.py` | 连接认证与采集步骤、分页补抓、dry-run 行为 |
| `util/mongo.py` / `util/resource_clock.py` | 取消新版记录对数字 tid 的依赖，保存新版来源定位与有效资源字段 |
| 失败台账与管理页 | 区分登录失效、VIP 限制、收费附件、真实缺磁链，补抓筛选改为新版分类 |
| `config/config.example.yaml` / 文档 / 测试 | 新配置、脱敏样本及双路径回归 |

新增来源专用详情采集步骤放在纯解析和入库之间，与公共引擎的连接方式在实现时保持最小；不让数据库仓储负责下载，也不把附件处理塞进公共 CF 处理器。

推荐新版文档使用：

```text
source_key = resource_id  # 保持站点原始字符串大小写
resource_id
board_id
category_id
section                  # 保留稳定业务标签，供通知与搜索分类使用
title / date / post_time / url / code / magnet / magnets / img
```

数字旧 `source_key` 和新版字符串 ID 自然区分。旧 `tid/fid/typeid` 仅保留在已有旧记录中；不把新 ID 伪装成数字 tid，也不猜测旧新帖对应关系。修改 saver 对新版字段的校验即可，无需批量迁移旧数据。

跨版本内容可能重复，当前没有可靠 ID 对照表；不能仅凭番号自动合并，因为同番号可能存在不同版本和发布记录。

旧失败台账保留，但新版重试入口不得把数字旧 key 拼到 `/resources/`。补抓检查点使用新板块/分类范围，不复用旧 Discuz 页码。默认不做全历史重扫。

两条路径的处理规则：先提取正文磁链；没有磁链时再处理可领取的免费 torrent 附件。有多个有效磁链时保留 `magnets` 并去重。只有受限附件或锁定正文时产生明确权限/价格结果，不误报普通解析损坏。

`dry-run` 可以读取列表、详情和价格预览，但不得发出领取 POST、消费配额或写数据库；附件待领取应显示为明确状态，不能伪装成已经得到磁链。会话失效属于来源级问题，不应把整个列表的资源永久标记为 VIP 限制。

## 6. 字幕搜索 API 的联动问题

另一个仓库 `D:/project/sehuatang-subtitle-search-api/src/catalog.ts` 的 `sourceDetailUrl()` 在配置 x1080x 当前域名时，强制调用 `forumDetailUrl()`，生成旧 Discuz 地址。新版采集成功后也会受影响。

需要对新资源重建 `/resources/{resource_id}`，保留安全的站点域名替换语义；未知旧数字 ID 不得伪造新版资源链接。

该仓库通过 `source_key` 查询 x1080x，已经使用字符串，主要问题是链接重建。其 `src/classification.ts` 当前按旧业务 `section` 名称分类，首轮建议保留稳定业务标签；启用免费板块时再增加对应分类标签。

## 7. 实施与验收 TODO

- [x] 读取现有来源、公共会话、入库、失败台账及相关约束。
- [x] 检查新版首页、BT 分类、第二页和详情 DOM。
- [x] 使用提供的账号登录并确认会员级别。
- [x] 验证正文直出磁链样本。
- [x] 验证免费附件预览、领取、下载和离线 torrent 解析。
- [x] 验证普通会员登录后仍存在六小时 VIP 限制。
- [x] 验证共享会话的 HTTP 列表获取，检查下游链接重建。
- [ ] 审核本方案涉及的登录机制、字段变化、采集板块及 API 联动范围。
- [ ] 实现来源适配，确认 `curl_cffi` 会话复用与认证失效行为。
- [ ] 用脱敏离线样本覆盖两类资源、混合内容、分页、时区与登录/价格状态。
- [ ] 验证非零价格、付费配额、版本变化、重试幂等键以及 dry-run 不领取。
- [ ] 运行受影响模块的离线回归及必要的 API 检查。
- [ ] 小范围登录态现场验收，不连接生产库、不发送 Telegram；通过后再由用户安排部署。

现有未提交的 README、脚本及相关测试改动与本调查无关，保持原样。调查浏览器截图/快照不作为代码或测试夹具提交；正文不记录密码、Cookie 值、CSRF 值或临时下载标识。
