from typing import Iterable

from scrapers.core.contracts import CrawlFailure, CrawlTarget
from util.mongo import (
    clear_crawl_failures,
    find_due_crawl_failures,
    find_non_retryable_crawl_keys,
    record_crawl_failures,
    list_crawl_failures,
    requeue_crawl_failure,
    requeue_exhausted_crawl_failures,
)


class MongoFailureStore:
    def non_retryable_keys(self, source: str, keys: Iterable[str]) -> set[str]:
        return find_non_retryable_crawl_keys(source, keys)

    def snapshot(self, source=None, state=None, limit=100):
        return list_crawl_failures(source=source, state=state, limit=limit)

    def requeue(self, source, key, stage):
        return requeue_crawl_failure(source, key, stage)

    def requeue_exhausted(self, source):
        return requeue_exhausted_crawl_failures(source)

    def record(self, failures: Iterable[CrawlFailure]) -> None:
        record_crawl_failures(
            [
                {
                    "source": failure.source,
                    "source_key": failure.key,
                    "url": failure.url,
                    "stage": failure.stage,
                    "attempts": failure.attempts,
                    "error_type": failure.error_type,
                    "error_message": failure.error_message,
                    "metadata": dict(failure.metadata),
                }
                for failure in failures
            ]
        )

    def clear(self, source: str, keys: Iterable[str]) -> None:
        clear_crawl_failures(source, list(keys))

    def due_targets(self, source: str):
        # 同一帖子可遗留多个阶段的失败行，每轮只请求一次。
        seen = set()
        rows = []
        for row in find_due_crawl_failures(source):
            if row.get("source_key") and row.get("url") and row["source_key"] not in seen:
                seen.add(row["source_key"])
                rows.append(row)
        return [
            CrawlTarget(
                key=row["source_key"],
                url=row["url"],
                partition=str((row.get("metadata") or {}).get("fid") or "") or None,
                metadata={
                    **dict(row.get("metadata") or {}),
                    "retry_stage": row.get("stage"),
                },
            )
            for row in rows
        ]
