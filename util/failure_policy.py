"""失败台账策略；HTTP 内部重试与台账累计失败次数分别计数。"""
from datetime import datetime, timedelta, timezone

from util.read_config import get_config


# 权限不会随短时退避恢复；保留台账，允许用户取得权限后手动重入队。
NON_RETRYABLE_ERRORS = ("membership_required", "cf_blocked")


def retry_stopped(row, maximum):
    return retry_count(row) >= maximum or (
        row.get("error_type") in NON_RETRYABLE_ERRORS and row.get("next_retry_at") is None
    )


def max_failures():
    value = int(get_config("crawler.retry_failed.max_failures", 5))
    if value < 1:
        raise ValueError("crawler.retry_failed.max_failures 必须大于等于 1")
    return value


def retry_count(row):
    return max(0, int(row.get("failure_count", 0)) - int(row.get("retry_reset_count", 0)))


def retry_minutes(count):
    return min(1440, 5 * 2 ** min(9, max(0, count - 1)))


def next_retry_at(count, now, maximum):
    return None if count >= maximum else now + timedelta(minutes=retry_minutes(count))


def describe_failure(row, maximum, now=None):
    row = dict(row)
    now = now or datetime.now(timezone.utc)
    count = retry_count(row)
    due = row.get("next_retry_at")
    if isinstance(due, str):
        due = datetime.fromisoformat(due)
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    state = "exhausted" if retry_stopped(row, maximum) else (
        "due" if due is not None and due <= now else "waiting"
    )
    row.update(retry_count=count, max_failures=maximum, state=state)
    if state == "exhausted":
        row["next_retry_at"] = None
    return row


def mongo_retry_count():
    return {"$max": [0, {"$subtract": [
        {"$ifNull": ["$failure_count", 0]},
        {"$ifNull": ["$retry_reset_count", 0]},
    ]}]}


def mongo_retry_stopped(maximum):
    return {"$or": [
        {"$gte": [mongo_retry_count(), maximum]},
        {"$and": [
            {"$in": ["$error_type", list(NON_RETRYABLE_ERRORS)]},
            {"$eq": [{"$ifNull": ["$next_retry_at", None]}, None]},
        ]},
    ]}


def mongo_due_query(source=None, now=None):
    query = {
        "resolved_at": None,
        "next_retry_at": {"$ne": None, "$lte": now or datetime.now(timezone.utc)},
        "$expr": {"$lt": [mongo_retry_count(), max_failures()]},
    }
    if source:
        query["source"] = source
    return query
