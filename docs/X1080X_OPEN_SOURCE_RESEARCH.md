# x1080x 开源项目调查

调查日期：2026-10-15（北京时间）。使用 GitHub 仓库、README、源码和 issue API 做只读检索；
未安装、执行第三方脚本，未调用第三方采集服务或下载资源。

结论：已核查项目中没有找到已验证可用、可直接替代 archiver 的游客历史磁链采集方案。
有源码的浏览器脚本采用普通页面和现有登录状态；另一个项目已报告相同 archiver 故障。
这不是对所有开源项目的穷尽结论，也不等于测试过这些项目的生产运行效果。

## Kesuy/x1080x-ex：普通页面与浏览器会话

- 仓库：[Kesuy/x1080x-ex](https://github.com/Kesuy/x1080x-ex)。本次检查提交：
  `37b744ada35d963509609fea4505652a1b8558f5`；GitHub 显示最后推送于 2026-10-14。
- [core.js](https://github.com/Kesuy/x1080x-ex/blob/37b744ada35d963509609fea4505652a1b8558f5/src/core.js#L342)
  的 `contentMagnets()` 对正文 `textContent` 正则提取磁链；`extractThreadResources()` 在首帖
  `postmessage_*` / `.t_f` 中提取已有磁链、图片和附件。
- [userscript.js](https://github.com/Kesuy/x1080x-ex/blob/37b744ada35d963509609fea4505652a1b8558f5/src/userscript.js#L783)
  的同源附件下载使用 `window.fetch(..., credentials: 'include')`，复用浏览器会话。
  `validateResponse()` 明确拒绝登录页、权限错误页和浏览器验证页，不把 HTML 当附件。
- [torrent.js](https://github.com/Kesuy/x1080x-ex/blob/37b744ada35d963509609fea4505652a1b8558f5/src/torrent.js#L606)
  的缓存与 qBittorrent 回退先执行 `extractBtih(magnet)`，用于已知磁链转 `.torrent`；
  不能在服务端未返回磁链时凭空补出哈希。
- README 的批量开帖默认随机间隔为 1.8–3.5 秒，支持已访问帖子去重和分页进度定位。
  这是项目默认值，不代表站点配额。
- 可借鉴：普通 DOM 解析、会话复用、严格响应校验、已有目标去重。
  不能据此推断可以绕过 VIP 或 6 小时限制。HDblog/JavFree 的回退主要是预览图和跨站检索，
  没有证据证明可替代本项目需要的 x1080x 历史磁链覆盖。

## envyafish/Immortal：同类故障尚无公开解决记录

- [Issue #24：x1080x无法爬取获取资源了](https://github.com/envyafish/Immortal/issues/24)
  创建于 2026-10-09。报告原文：“10月4号开始，x1080x把archiver锁了，爬取不到资源了！”
- 检索时状态 `open`，评论数 0，没有公开修复说明。
- 仓库当前树 `dfca2af801f23e3c2609fe76df9b93c9c3e1ec79` 主要是说明与版本元数据，
  未提供可供核查的 x1080x 采集器源码，不能把它当作已经解决问题的开源实现。
- 报告日期和“从 10 月 4 日开始”属于 issue 作者的观察，不是本次独立验证的站点变更时间。

## li-peifeng/AVdb-Only：宣称支持采集，但公开仓库不足以验证实现

- [仓库 README](https://github.com/li-peifeng/AVdb-Only) 宣称内置色花堂 / X1080X，
  支持游标增量、全量回填、版块过滤、并发控制和异常重试。
- 本次检查当前树 `95421ec809d87d5b7c725e0b9f44d1d00c3b6058`，看到部署文件、插件、
  二进制及前端注入辅助文件，没有可核查的 x1080x 采集器源码。
- 因此这里只记录其功能声明，不声称它在 archiver 封锁后仍能抓取，也不把发行仓库等同于采集器源码开放。
- `LIUBANGJIAN/AvdbAsst` 的相关命中是对上游 AVdb 数据/API 的管理，未提供替代原站采集的证据。

## 排除的相关命中

- [jasmineamber/tampermonkey](https://github.com/jasmineamber/tampermonkey) 最后推送于 2023-02-16，
  当前树主要是解压库，README 只有标题；其 [Issue #1](https://github.com/jasmineamber/tampermonkey/issues/1)
  在 2024 年询问还能否下载 x1080x 附件，无回复。没有找到当前可复用的采集逻辑。
- [MetaTube PR #306](https://github.com/metatube-community/metatube-sdk-go/pull/306)
  虽名为优化 `x1080x` 格式解析，实际修改 `common/number/number.go` 中的文件名/番号清理，
  并非站点抓取或权限处理。
- `media-bridge-backend` 的命中是文件名中 `_X1080X` 标记清理，同样不能解决站点采集。

## 对当前实现的建议

1. 普通页面、低并发、短周期增量是有实测和源码参考支持的方向。
2. Cookie/UA 与请求线路一致仍需部署后验证；本次没有证明生产 Byparr 会话复用已稳定。
3. 历史资源优先验证具备相应权限的普通页会话；在此之前保留历史数据和检查点，不重跑大范围补抓。
4. 不因其他项目宣称“支持 X1080X”就引入不透明二进制、未知服务或大范围参数试探。

检索覆盖 `x1080x` 仓库名/README、`agaghhh` README、`x1080x archiver` issue 及相关项目源码。
grep.app 查询返回 HTTP 429，未将其作为可用搜索结果；GitHub 结果和上述源码均取得成功响应。
