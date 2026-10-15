# 多来源爬虫架构

## 模块边界

```text
run.py / scheduler
        |
scrapers.registry
        |
  +-----+------------------+
  |                        |
sources/sehuatang      sources/javbee
  |                        |
  +----- core + infrastructure -----+
```

`scrapers/core` 负责来源无关能力：

- `config.py`：默认值、来源覆盖、环境变量覆盖和配置校验
- `http.py`：单次传输、超时、错误分类和重试
- `pool.py` / `session.py`：固定端口常驻会话、CF 处理、有界并发和在途去重
- `rate_limit.py`：会话限速、冷却及补抓原目标恢复
- `storage.py`：有界写入队列、独立定时批量落库、故障通知和退出冲刷
- `contracts.py`：source、repository、failure store 的数据合同
- `engine.py`：发现、筛选、详情抓取、解析、保存和运行汇总
- `models.py`：请求结果及 `success/partial_success/failed` 状态

`scrapers/sources/<name>` 负责站点差异：

- 列表入口和分页发现
- HTML 解析及数据校验
- 唯一键、刷新策略和 collection 映射
- 站点专属挑战及特殊任务

`scrapers/infrastructure` 负责 MongoDB/JSON 失败台账等外部适配。

## HTTP 与重试

每个来源独立解析 `concurrency`、`per_proxy_concurrency`、`http.timeout`、`http.proxy` 和
`http.retry`。默认只重试连接类异常、空响应以及
`408/425/429/500/502/503/504`，普通 `4xx` 不重试。退避时间使用指数增长和
随机抖动，并尊重秒数或 HTTP 日期形式的 `Retry-After`。

三个来源使用公共 `SessionPool`，只有 Sehuatang 的 R18 safeid 是来源专属插件。
普通 429/限流页由公共冷却控制，CF 挑战由公共验证层处理。完整配置见
[通用多代理会话](../docs/MULTI_PROXY.md)。

环境变量使用明确的来源前缀：

```text
CRAWLER_JAVBEE_CONCURRENCY
CRAWLER_JAVBEE_TIMEOUT
CRAWLER_JAVBEE_PROXY_ENABLED
CRAWLER_JAVBEE_PROXY_URL
CRAWLER_JAVBEE_RETRY_ATTEMPTS
CRAWLER_SEHUATANG_FLARESOLVERR_URL
```

代理 URL 与 `PROXY_ENABLED=true` 应同时设置；把 `JAVBEE` 替换为
`SEHUATANG` 即可覆盖另一个来源。代理凭据不要写入仓库，
公共日志会隐藏 URL 用户信息和常见敏感查询参数。

## 运行入口

### x1080x 限流与历史补抓

当前使用普通页面并按发帖时间倒序。游客磁链仅免费期内可见；遇到明确封锁或会员限制，
失败台账停止自动重试，补抓停在当前页。旧 archiver 检查点不复用。
配置、兼容与验证边界见 [普通页抓取说明](../docs/X1080X_NORMAL_CRAWLING.md)。

`crawler.sources.x1080x.rate_limit` 控制该来源的请求频率：

```yaml
crawler:
  sources:
    x1080x:
      concurrency: 1
      rate_limit:
        min_interval_seconds: 2
        cooldown_seconds: 60
        max_cooldown_seconds: 900
```

已有配置没有 `rate_limit` 时也采用以上节流和冷却默认值；已有显式并发配置保持生效，
每端口并发由 `per_proxy_concurrency` 控制（默认 1），所有 worker 共享该端口的间隔与冷却。
来源总并发仍由 `concurrency` 限制，不因增加 worker 而改变请求速率额度，
覆盖列表、详情、HTTP 重试及发起过盾请求。不同进程/容器不共享限流状态，
同一出口不要同时启动多个补抓实例。上述值是保守起点，不代表站点公布的配额。

列表与详情均识别站点限流提示，以及没有 CF 挑战特征的 HTTP 429。
过盾服务返回限流页时同样进入冷却，不会作为正常详情送入解析器。
直连响应的数字 `Retry-After` 大于当前冷却时间时优先遵守服务端要求。

- 定时增量及 `retry-failed`：停止本轮，不再发送后续请求；限流不消耗单帖失败台账次数。
- `backfill-pages`：自动等待后重试**原列表页或受限详情**，保留同批已获取的成功结果。
  连续受限的冷却按 60、120、240、480、900 秒增加，之后每 900 秒重试；取得成功响应后重置。
  限流恢复没有次数上限，持续受限时任务保持运行并输出等待日志。成功后继续剩余页和分类。
- 当前页处理完成后才推进检查点。停止服务或 CLI 的 Ctrl+C 会唤醒冷却等待；
  已保存批次保留，当前未完成页不推进，重启后使用 `--resume` 继续。
  同批还在内存中的详情会随未完成页重新获取，已入库记录由去重逻辑跳过。
- 普通网络终态失败、真正缺标题/日期/正文/磁链的详情仍按原有失败台账恢复机制处理，
  不属于无限限流重试。此前已推进页面中的失败详情仍由台账恢复，`--resume` 不会回扫旧页。

```powershell
python run.py backfill-pages --source x1080x --end-page 1000 --resume
python run.py retry-failed --source x1080x
```

详情校验日志及台账区分 `page_unavailable`、`missing_title`、`missing_date`、
`missing_content`、`missing_magnet`。诊断 HTML 保存到 `data/debug/x1080x_detail_<tid>.html`，
最多保留最近 20 个文件，每个最多 512 KiB；日志只输出页面标题、大小、原因与文件路径。
`--dry-run` 不写诊断快照、失败台账、数据库或检查点。

```powershell
# 运行一个或全部来源
python run.py crawl --source javbee
python run.py crawl --source sehuatang
python run.py crawl --source all

# 只抓取和解析，不写库、不通知、不推进 checkpoint
python run.py crawl --source javbee --dry-run

# 重试失败台账中已到期的目标，不重新扫描列表页
python run.py retry-failed --source javbee
python run.py retry-failed --source sehuatang
```

`--mode once|javbee|backfill|health` 继续兼容。新配置使用
`crawler.sources.<source>.schedule.cron` 为每个来源创建独立任务；未配置来源级
cron 时，调度器回退到旧的统一 `schedule.schedule_cron`。

## 失败台账

终态详情失败保存以下信息：

```text
source / source_key / url / stage / attempts / error_type
error_message / metadata / failure_count / next_retry_at
```

MongoDB 启用时写入 `crawl_failures` collection，否则写入
`data/crawl_failures.json`。成功保存后会清除同来源、同 key 的失败记录。

## 新增来源

1. 在 `scrapers/sources/<name>` 实现来源发现和解析逻辑。
2. 使用 `shared_pool(source, settings, base_url)`；补抓可包装 `BackfillHttpClient`，不要复制代理和重试循环。
3. 实现 `RecordRepository`，保留该来源自己的 schema 和唯一键。
4. 在 `scrapers/registry.py` 显式注册来源及 runner。
5. 为解析 fixture、刷新策略、部分成功和失败恢复补充测试。

不使用目录扫描或动态插件加载；显式 registry 更容易审查，也足以支持当前规模。
