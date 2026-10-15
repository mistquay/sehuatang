"""x1080x 抓取入口，使用公共爬虫引擎。"""
import os
from typing import Dict, Optional

from scrapers.core.config import load_source_settings, load_storage_settings
from scrapers.core.contracts import CrawlTarget
from scrapers.core.engine import CrawlEngine
from scrapers.core.http import CrawlerHttpClient
from scrapers.infrastructure import build_failure_store
from scrapers.page_backfill import FixedTargetSource, PageCheckpointStore, build_checkpoint_store
from scrapers.sources.x1080x import X1080XRepository, X1080XSource
from scrapers.sources.x1080x.http_client import shared_http_client
from scrapers.sources.x1080x.source import DEFAULT_BASE_URL
from scrapers.sources.x1080x.rate_limit import BackfillHttpClient, RateLimitSettings
from util.log_util import log
from util.mongo import find_existing_x1080x_keys, save_x1080x_items
from util.read_config import get_config


def _resolve_flaresolverr_url(config: dict) -> str:
    challenge = dict(config.get("challenge") or {})
    return str(
        os.getenv("CRAWLER_X1080X_FLARESOLVERR_URL")
        or challenge.get("flaresolverr_url")
        or ""
    ).strip()


class X1080XScraper:
    def __init__(self, config=None, http=None, failure_store=None):
        self.config = dict(config or get_config("x1080x", {}) or {})
        base_url = os.getenv("CRAWLER_X1080X_BASE_URL", "").strip()
        if base_url:
            self.config["base_url"] = base_url
        settings_config = {
            "x1080x": self.config,
            "crawler": {"defaults": get_config("crawler.defaults", {}) or {},
                        "sources": {"x1080x": self.config}},
        }
        self.settings = load_source_settings(settings_config, "x1080x")
        self.storage_settings = load_storage_settings(settings_config, "x1080x")
        self.http = http or shared_http_client(
            self.settings,
            _resolve_flaresolverr_url(self.config),
            self.config.get("base_url") or DEFAULT_BASE_URL,
        )
        self.failure_store = failure_store or build_failure_store(
            mongodb_enabled=bool(get_config("mongodb.enable", False))
        )

    def crawl(
        self,
        *,
        dry_run: bool = False,
        retry_failed: bool = False,
    ) -> Dict[str, object]:
        source = X1080XSource(self.config, diagnostics=not dry_run)
        notify = (not dry_run and not retry_failed and not self.config.get("refresh_all", False)
                  and bool(self.config.get("notify_telegram", True)))
        repository = X1080XRepository(
            existing_lookup=find_existing_x1080x_keys,
            save_func=save_x1080x_items,
            refresh_all=bool(self.config.get("refresh_all", False)),
            on_saved=self._enqueue_new_items if notify else None,
        )
        summary = CrawlEngine(self.http, self.failure_store, getattr(self, "storage_settings", None)).run(
            source,
            repository,
            dry_run=dry_run,
            retry_failed=retry_failed,
        )
        summary.details["existing"] = repository.existing_count
        result = summary.as_dict()
        log.info(
            "x1080x 抓取汇总: "
            f"status={result['status']} pages={result.get('pages', 0)} "
            f"discovered={result['discovered']} existing={result['existing']} "
            f"requested={result['requested']} failed={result['failed']} "
            f"saved={result['saved']} updated={result['updated']}"
        )
        return result

    def _enqueue_new_items(self, payloads):
        """每批成功保存后立即入队，后续抓取不等待图片下载与 Telegram。"""
        try:
            from scrapers.notification_manager import NotificationManager
            NotificationManager().enqueue_x1080x_notifications(payloads)
        except Exception as exc:
            log.error(f"x1080x 通知入队失败（不影响抓取结果）: {type(exc).__name__}")

    def backfill_pages(
        self,
        start_page: int,
        end_page: int,
        *,
        typeids=None,
        resume: bool = False,
        dry_run: bool = False,
        checkpoint_store: Optional[PageCheckpointStore] = None,
    ) -> Dict[str, object]:
        """按页区间补抓历史数据。

        检查点按完整处理完的页推进；详情失败进失败台账、
        由 retry-failed 恢复，不阻塞页进度。
        """
        source = X1080XSource(self.config, diagnostics=not dry_run)
        repository = X1080XRepository(
            existing_lookup=find_existing_x1080x_keys,
            save_func=save_x1080x_items,
        )
        http = BackfillHttpClient(self.http)
        engine = CrawlEngine(http, self.failure_store, getattr(self, "storage_settings", None))
        checkpoints = checkpoint_store or build_checkpoint_store(
            "x1080x", start_page, end_page, source.base_url,
            f"fid={source.fid};normal;orderby=dateline;v1")

        selected = {
            str(typeid): source.type_map[str(typeid)]
            for typeid in (typeids or source.type_map)
            if str(typeid) in source.type_map
        }
        if not selected:
            raise ValueError(f"未匹配到任何 typeid: {typeids}")

        summary = {
            "source": "x1080x",
            "start_page": start_page,
            "end_page": end_page,
            "pages_scanned": 0,
            "discovered": 0,
            "existing": 0,
            "requested": 0,
            "saved": 0,
            "failed": 0,
            "partitions": {},
        }
        seen_tids = set()

        for typeid, section in selected.items():
            seen_pages = set()
            first_page = start_page
            if resume:
                completed = checkpoints.load("x1080x", typeid)
                if completed:
                    first_page = max(start_page, completed + 1)
                    log.info(
                        f"x1080x 分类 {typeid} 从检查点第 {first_page} 页继续"
                    )
            partition_summary = {
                "section": section,
                "pages": 0,
                "saved": 0,
                "failed": 0,
                "stopped": "",
            }
            summary["partitions"][typeid] = partition_summary

            for page in range(first_page, end_page + 1):
                result, parsed = source.fetch_list_page(http, typeid, page)
                if not result.ok:
                    partition_summary["stopped"] = f"list_failed@{page}"
                    summary["failed"] += 1
                    log.warning(
                        "x1080x 补抓列表页失败，该分类暂停（检查点未推进，可 --resume 继续）: "
                        f"typeid={typeid} page={page} error_type={result.error_type}"
                    )
                    if result.error_type == "cf_blocked":
                        summary["stopped"] = "cf_blocked"
                    break

                tids = parsed.tids
                if not tids:
                    partition_summary["stopped"] = f"exhausted@{page}"
                    log.info(f"x1080x 分类 {typeid} 第 {page} 页无内容，视为到底")
                    break
                signature = frozenset(tids)
                if signature in seen_pages:
                    partition_summary["stopped"] = f"repeated_page@{page}"
                    summary["failed"] += 1
                    break
                seen_pages.add(signature)

                targets = []
                for tid in tids:
                    if tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    targets.append(
                        CrawlTarget(
                            key=str(tid),
                            url=source.detail_url(tid),
                            partition=typeid,
                            metadata={
                                "tid": tid,
                                "typeid": typeid,
                                "section": section,
                            },
                        )
                    )

                page_run = engine.run(
                    FixedTargetSource(source, targets),
                    repository,
                    dry_run=dry_run,
                    page_context=f"source=x1080x partition={typeid} page={page}",
                )
                summary["pages_scanned"] += 1
                summary["discovered"] += len(targets)
                summary["existing"] += repository.existing_count
                summary["requested"] += page_run.requested
                summary["saved"] += page_run.saved
                summary["failed"] += page_run.failed
                partition_summary["pages"] += 1
                partition_summary["saved"] += page_run.saved
                partition_summary["failed"] += page_run.failed

                blocked = ("cf_blocked" if page_run.details.get("cf_blocked") else
                           "membership_required" if page_run.details.get("membership_required") else "")
                if blocked:
                    partition_summary["stopped"] = f"{blocked}@{page}"
                    summary["stopped"] = blocked
                    log.warning(f"x1080x 补抓暂停: {blocked}，当前页检查点未推进")
                    break
                if not dry_run:
                    checkpoints.save("x1080x", typeid, page)
                if parsed.last_page is not None and page >= parsed.last_page:
                    partition_summary["stopped"] = f"last_page@{page}"
                    break

            if summary.get("stopped"):
                break

        log.info(
            "x1080x 分页补抓结束: "
            f"pages={summary['pages_scanned']} discovered={summary['discovered']} "
            f"existing={summary['existing']} requested={summary['requested']} "
            f"saved={summary['saved']} failed={summary['failed']}"
        )
        return summary
