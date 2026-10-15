import time
import uuid
from typing import Optional

from util.log_util import log

from .contracts import (
    CrawlContext,
    CrawlFailure,
    DiscoveryResult,
    DetailValidationError,
    FailureStore,
    NullFailureStore,
    RecordRepository,
    SourceAdapter,
)
from .http import CrawlerHttpClient, redact_url
from .models import RunSummary
from .config import StorageSettings
from .storage import BatchWriter, PendingWrite, completed_results, retry_resource_write


class CrawlEngine:
    """执行来源发现、目标筛选、详情抓取、解析和保存。"""

    def __init__(
        self,
        http: CrawlerHttpClient,
        failure_store: Optional[FailureStore] = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self.storage_settings = storage_settings or StorageSettings()
        self.http = http
        self.failure_store = failure_store or NullFailureStore()

    def run(
        self,
        source: SourceAdapter,
        repository: RecordRepository,
        *,
        dry_run: bool = False,
        retry_failed: bool = False,
        run_id: Optional[str] = None,
        batch_size: int = 20,
        page_context: str = "",
    ) -> RunSummary:
        logger = log.bind(module=source.name)
        context = CrawlContext(
            source=source.name,
            run_id=run_id or uuid.uuid4().hex,
            dry_run=dry_run,
            retry_failed=retry_failed,
        )
        started = time.monotonic()
        summary = RunSummary(source=source.name, run_id=context.run_id)

        if retry_failed:
            discovery = DiscoveryResult(
                targets=self.failure_store.due_targets(source.name),
                details={"retry_failed": True},
            )
        else:
            discovery = source.discover(context, self.http)
        summary.discovered = len(discovery.targets)
        summary.failed = discovery.failed
        summary.details.update(discovery.details)
        summary.retries = int(discovery.details.get("discovery_retries", 0))

        targets = (
            list(discovery.targets)
            if retry_failed
            else repository.select_targets(discovery.targets)
        )
        targets = list({target.key: target for target in targets}.values())
        normalize = getattr(source, "normalize_target", None)
        if normalize is not None:
            targets = [normalize(target) for target in targets]
        summary.requested = len(targets)

        def persist(items):
            records = [item.record for item in items if item.record is not None]
            failures = [item.failure for item in items if item.failure is not None]
            if records:
                saved = retry_resource_write(lambda: repository.save_many(records), self.storage_settings, logger)
                summary.saved += saved.saved
                summary.updated += saved.updated
            if failures:
                # 失败台账会增加次数，不自动重放可能部分成功的写入。
                self.failure_store.record(failures)
            if records:
                try:
                    self.failure_store.clear(source.name, [record.target.key for record in records])
                except Exception as exc:
                    logger.error(f"失败台账清理失败: run_id={context.run_id} source={source.name} error={exc}")

        batch_size = max(1, int(batch_size))
        with BatchWriter(source.name, persist, settings=self.storage_settings,
                         context=f"run_id={context.run_id} {page_context}") as writer:
            for start in range(0, len(targets), batch_size):
                writer.check()
                batch = targets[start:start + batch_size]
                for index, result in completed_results(self.http, [target.url for target in batch], writer.failed):
                    writer.check()
                    self._process_batch(context, source, [batch[index]], summary, [result], writer)
                if summary.details.get("rate_limited") and (
                    not hasattr(type(self.http), "all_cooling") or self.http.all_cooling
                ):
                    break
                if summary.details.get("cf_blocked"):
                    break
        summary.details.update(write_batches=writer.batches, persist_ms=writer.persist_ms,
                               write_queue_wait_ms=writer.queue_wait_ms)

        summary.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            ("本页处理完成: " if page_context else "来源抓取结束: ")
            + f"{page_context} "
            f"run_id={context.run_id} source={source.name} status={summary.status.value} "
            f"discovered={summary.discovered} requested={summary.requested} "
            f"succeeded={summary.succeeded} failed={summary.failed} "
            f"saved={summary.saved} updated={summary.updated} "
            f"retries={summary.retries} elapsed_ms={summary.elapsed_ms}"
        )
        return summary

    def _process_batch(
        self,
        context: CrawlContext,
        source: SourceAdapter,
        targets,
        summary: RunSummary,
        fetch_results,
        writer,
    ) -> None:
        """抓取、解析并保存一批目标，累加进汇总。"""
        logger = log.bind(module=source.name)
        records = []
        failures = []
        for target, result in zip(targets, fetch_results):
            summary.retries += max(0, result.attempts - 1)
            if result.error_type == "rate_limited":
                summary.failed += 1
                summary.details["rate_limited"] = True
                # 站点级暂时阻塞不计入单帖失败次数；补抓客户端会在返回前恢复。
                continue
            if not result.ok:
                if result.error_type == "cf_blocked":
                    summary.details["cf_blocked"] = True
                failures.append(
                    CrawlFailure(
                        source=source.name,
                        key=target.key,
                        url=target.url,
                        stage="fetch",
                        attempts=result.attempts,
                        error_type=result.error_type or "request_failed",
                        error_message=result.error_message or "",
                        metadata=target.metadata,
                    )
                )
                logger.warning(
                    "详情请求终态失败: "
                    f"run_id={context.run_id} source={source.name} stage=detail "
                    f"target={target.key} attempts={result.attempts} "
                    f"error_type={result.error_type} url={redact_url(target.url)}"
                )
                continue

            try:
                record = source.parse_detail(target, result)
            except Exception as exc:
                error_type = (exc.reason if isinstance(exc, DetailValidationError)
                              else type(exc).__name__.lower())
                if error_type == "membership_required":
                    summary.details["membership_required"] = summary.details.get("membership_required", 0) + 1
                logger.warning(
                    "详情解析异常: "
                    f"run_id={context.run_id} source={source.name} "
                    f"target={target.key} error_type={error_type}"
                )
                failures.append(
                    CrawlFailure(
                        source=source.name,
                        key=target.key,
                        url=target.url,
                        stage="parse",
                        attempts=result.attempts,
                        error_type=error_type,
                        error_message=str(exc),
                        metadata=target.metadata,
                    )
                )
                continue
            if record is None:
                logger.warning(
                    "详情文档校验失败: "
                    f"run_id={context.run_id} source={source.name} "
                    f"target={target.key}"
                )
                failures.append(
                    CrawlFailure(
                        source=source.name,
                        key=target.key,
                        url=target.url,
                        stage="parse",
                        attempts=result.attempts,
                        error_type="invalid_document",
                        metadata=target.metadata,
                    )
                )
                continue
            records.append(record)

        summary.succeeded += len(records)
        summary.failed += len(failures)

        if not context.dry_run:
            for record in records:
                writer.submit(PendingWrite(record.target.key, record=record))
            for failure in failures:
                writer.submit(PendingWrite(failure.key, failure=failure))
