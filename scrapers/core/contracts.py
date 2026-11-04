from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from .http import CrawlerHttpClient
from .models import FetchResult


class DetailValidationError(ValueError):
    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


class SourcePaused(RuntimeError):
    """来源会话不可用；停止本轮，不累计每个资源的失败次数。"""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class CrawlContext:
    source: str
    run_id: str
    dry_run: bool = False
    retry_failed: bool = False


@dataclass(frozen=True)
class CrawlTarget:
    key: str
    url: str
    partition: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryResult:
    targets: Sequence[CrawlTarget]
    failed: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrawlRecord:
    target: CrawlTarget
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SaveResult:
    processed: int = 0
    saved: int = 0
    updated: int = 0


@dataclass(frozen=True)
class CrawlFailure:
    source: str
    key: str
    url: str
    stage: str
    attempts: int
    error_type: str
    error_message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    name: str

    def discover(
        self,
        context: CrawlContext,
        http: CrawlerHttpClient,
    ) -> DiscoveryResult: ...

    def parse_detail(
        self,
        target: CrawlTarget,
        result: FetchResult,
    ) -> Optional[CrawlRecord]: ...


class RecordRepository(Protocol):
    def select_targets(self, targets: Sequence[CrawlTarget]) -> List[CrawlTarget]: ...

    def save_many(self, records: Sequence[CrawlRecord]) -> SaveResult: ...


class FailureStore(Protocol):
    def record(self, failures: Iterable[CrawlFailure]) -> None: ...

    def clear(self, source: str, keys: Iterable[str]) -> None: ...

    def due_targets(self, source: str) -> List[CrawlTarget]: ...

    def non_retryable_keys(self, source: str, keys: Iterable[str]) -> set[str]: ...


class NullFailureStore:
    def record(self, failures: Iterable[CrawlFailure]) -> None:
        return None

    def clear(self, source: str, keys: Iterable[str]) -> None:
        return None

    def due_targets(self, source: str) -> List[CrawlTarget]:
        return []

    def non_retryable_keys(self, source: str, keys: Iterable[str]) -> set[str]:
        return set()
