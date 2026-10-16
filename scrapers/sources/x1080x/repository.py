"""x1080x 入库仓储：按 source_key 幂等 upsert，默认只抓新帖。"""
from typing import Callable, List, Sequence

from scrapers.core.contracts import CrawlRecord, CrawlTarget, SaveResult
from util.log_util import log


class X1080XRepository:
    def __init__(
        self,
        existing_lookup: Callable,
        save_func: Callable,
        refresh_all: bool = False,
        on_saved: Callable = None,
        non_retryable_lookup: Callable = None,
    ):
        self._existing_lookup = existing_lookup
        self._save_func = save_func
        self.refresh_all = bool(refresh_all)
        self.existing_count = 0
        self._on_saved = on_saved
        self._non_retryable_lookup = non_retryable_lookup
        self.skipped_restricted = 0

    def select_targets(self, targets: Sequence[CrawlTarget]) -> List[CrawlTarget]:
        keys = [target.key for target in targets]
        existing = set(self._existing_lookup(keys))
        self.existing_count = len(existing)
        self.skipped_restricted = 0
        if self.refresh_all:
            return list(targets)
        pending = [target for target in targets if target.key not in existing]
        restricted = (set(self._non_retryable_lookup([target.key for target in pending]))
                      if pending and self._non_retryable_lookup else set())
        self.skipped_restricted = sum(target.key in restricted for target in pending)
        return [target for target in pending if target.key not in restricted]

    def save_many(self, records: Sequence[CrawlRecord]) -> SaveResult:
        payloads = [dict(record.payload) for record in records]
        result = self._save_func(payloads)
        if self._on_saved is not None:
            try:
                self._on_saved(payloads)
            except Exception as exc:
                log.bind(module="x1080x").error(f"通知入队失败，不重试已完成的资源写入: {type(exc).__name__}")
        return SaveResult(
            processed=int(result.get("processed", len(records))),
            saved=int(result.get("upserted", 0)),
            updated=int(result.get("modified", 0)),
        )
