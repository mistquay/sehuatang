"""Collection provenance and atomic effective-change clocks for resource readers."""
import hashlib
import json
from datetime import datetime, timezone


RESOURCE_FIELDS = (
    "title", "number", "code", "code_normalized", "date", "post_time", "url",
    "magnet", "torrent", "img", "size",
)

# x1080x 分类及多磁链变化也算有效资源变更，爬虫与离线补录共用。
X1080X_RESOURCE_FIELDS = RESOURCE_FIELDS + ("magnets", "typeid", "section", "board_id", "category_id")


def fingerprint(document, fields=RESOURCE_FIELDS):
    values = {key: document.get(key) for key in fields}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                    default=str).encode("utf-8")).hexdigest()


def collected_document(document, now=None, fields=RESOURCE_FIELDS):
    now = now or datetime.now(timezone.utc)
    return {**document, "collected_at": now, "resource_updated_at": now,
            "resource_fingerprint": fingerprint(document, fields)}


def resource_update_pipeline(document, fields=RESOURCE_FIELDS):
    """Run after the insert-only provenance upsert, so existing clocks survive."""
    digest = fingerprint(document, fields)
    return [{"$set": {
        **{key: {"$literal": value} for key, value in document.items()
           if key not in {"_id", "created_at", "collected_at", "resource_updated_at", "resource_fingerprint", "resource_collection_pending"}},
        "updated_at": "$$NOW",
        "collected_at": {"$cond": [{"$eq": ["$resource_collection_pending", True]},
                                    "$$NOW", {"$ifNull": ["$collected_at", "$$REMOVE"]}]},
        "resource_updated_at": {"$cond": [
            {"$ne": [{"$ifNull": ["$resource_fingerprint", None]}, digest]},
            "$$NOW", "$resource_updated_at",
        ]},
        "resource_fingerprint": digest,
    }}, {"$unset": "resource_collection_pending"}]
