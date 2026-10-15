import json
import threading
from .file_lock import FileLock
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from scrapers.core.contracts import CrawlFailure, CrawlTarget
from util.failure_policy import (NON_RETRYABLE_ERRORS, describe_failure, max_failures,
                                 next_retry_at, retry_count, retry_stopped)

_FILE_LOCK = threading.RLock()


class JsonFailureStore:
    """MongoDB 未启用时使用的轻量持久化失败台账。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (
            Path(__file__).resolve().parents[2] / "data" / "crawl_failures.json"
        )
        self._lock = _FILE_LOCK

    def record(self, failures: Iterable[CrawlFailure]) -> None:
        failures = list(failures)
        if not failures:
            return
        with self._lock, FileLock(str(self.path) + ".lock"):
            rows = self._load()
            by_key = {
                (row["source"], row["source_key"], row["stage"]): row
                for row in rows
            }
            now = datetime.now(timezone.utc)
            for failure in failures:
                stage = failure.metadata.get("retry_stage") or failure.stage
                key = (failure.source, failure.key, stage)
                previous = by_key.get(key) or {}
                attempts = max(1, failure.attempts)
                count = retry_count(previous) + 1
                due = (None if failure.error_type in NON_RETRYABLE_ERRORS
                       else next_retry_at(count, now, max_failures()))
                by_key[key] = {
                    "source": failure.source,
                    "source_key": failure.key,
                    "url": failure.url,
                    "stage": stage,
                    "last_stage": failure.stage,
                    "attempts": attempts,
                    "failure_count": int(previous.get("failure_count", 0)) + 1,
                    "retry_reset_count": int(previous.get("retry_reset_count", 0)),
                    "requeued_at": previous.get("requeued_at"),
                    "error_type": failure.error_type,
                    "error_message": failure.error_message[:1000],
                    "metadata": dict(failure.metadata),
                    "created_at": previous.get("created_at") or now.isoformat(),
                    "last_failed_at": now.isoformat(),
                    "next_retry_at": due.isoformat() if due else None,
                }
            self._save(list(by_key.values()))

    def clear(self, source: str, keys: Iterable[str]) -> None:
        key_set = set(keys)
        if not key_set:
            return
        with self._lock, FileLock(str(self.path) + ".lock"):
            rows = [
                row
                for row in self._load()
                if not (
                    row.get("source") == source
                    and row.get("source_key") in key_set
                )
            ]
            self._save(rows)

    def due_targets(self, source: str):
        now = datetime.now(timezone.utc)
        with self._lock, FileLock(str(self.path) + ".lock"):
            rows = self._load()
        targets = []
        seen = set()
        rows.sort(key=lambda row: row.get("next_retry_at") or "")
        for row in rows:
            if row.get("source") != source or row.get("source_key") in seen:
                continue
            if describe_failure(row, max_failures(), now)["state"] != "due":
                continue
            seen.add(row["source_key"])
            metadata = dict(row.get("metadata") or {})
            metadata["retry_stage"] = row.get("stage")
            targets.append(
                CrawlTarget(
                    key=row["source_key"],
                    url=row["url"],
                    partition=str(metadata.get("fid") or "") or None,
                    metadata=metadata,
                )
            )
        return targets[:500]

    def snapshot(self, source=None, state=None, limit=100):
        now = datetime.now(timezone.utc)
        maximum = max_failures()
        with self._lock, FileLock(str(self.path) + ".lock"):
            rows = [describe_failure(row, maximum, now) for row in self._load()
                    if not source or row.get("source") == source]
        counts = {name: 0 for name in ("due", "waiting", "exhausted")}
        due_counts = {}
        for row in rows:
            counts[row["state"]] += 1
            if row["state"] == "due":
                due_counts[row["source"]] = due_counts.get(row["source"], 0) + 1
        rows.sort(key=lambda row: row.get("last_failed_at", ""), reverse=True)
        rows = [row for row in rows if not state or row["state"] == state]
        return {"failures": rows[:max(1, min(500, limit))], "due_counts": due_counts,
                "counts": counts, "max_failures": maximum}

    def requeue(self, source, key, stage):
        return bool(self._requeue(source, key, stage))

    def requeue_exhausted(self, source):
        return self._requeue(source)

    def _requeue(self, source, key=None, stage=None):
        with self._lock, FileLock(str(self.path) + ".lock"):
            rows = self._load()
            count = 0
            now = datetime.now(timezone.utc).isoformat()
            for row in rows:
                if row["source"] != source:
                    continue
                if key is not None:
                    if (row["source_key"], row["stage"]) != (key, stage):
                        continue
                elif not retry_stopped(row, max_failures()):
                    continue
                row["retry_reset_count"] = int(row.get("failure_count", 0))
                row["requeued_at"] = now
                row["next_retry_at"] = now
                count += 1
            if count:
                self._save(rows)
            return count

    def _load(self):
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _save(self, rows) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
