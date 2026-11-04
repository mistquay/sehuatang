"""按页区间补抓的共享基础设施。

设计（学习自可用实现）：
- 补抓 = 指定列表页区间 + 跳过已存在 + 详情失败进失败台账；
- 检查点按「完整处理完的页」推进，详情失败不阻塞检查点
  （失败由台账负责退避重试，页覆盖进度与失败恢复解耦）；
- 检查点按任务范围、source 和 partition 隔离，x1080x 的 partition 是 typeid，
  sehuatang 的 partition 是 fid。
- Sehuatang 分页补抓在读取正常列表后核对明确末页、当前页与整页重复；
  异常列表不推进检查点。已有越界检查点不会自动改写。
"""
import json
from pathlib import Path
from typing import Optional, Sequence

from scrapers.core.contracts import CrawlTarget, DiscoveryResult
from util.log_util import log
from scrapers.infrastructure.file_lock import locked_json


class PageCheckpointStore:
    """记录最后完成页；有 scope 时隔离任务范围，空 scope 兼容旧键。"""

    def __init__(self, path: Optional[Path] = None, scope: str = ""):
        self.scope = scope
        self.path = Path(path) if path else (
            Path(__file__).resolve().parent.parent
            / "data"
            / "page_backfill_progress.json"
        )

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning(f"读取分页补抓检查点失败，视为无进度: {exc}")
            return {}

    def _key(self, source: str, partition) -> str:
        return f"{self.scope}:{source}:{partition}" if self.scope else f"{source}:{partition}"

    @locked_json
    def load(self, source: str, partition) -> int:
        try:
            return int(self._read().get(self._key(source, partition), 0) or 0)
        except (TypeError, ValueError):
            return 0

    @locked_json
    def save(self, source: str, partition, page: int) -> None:
        data = self._read()
        data[self._key(source, partition)] = int(page)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)

    @locked_json
    def clear(self, source: str, partition) -> None:
        data = self._read()
        if data.pop(self._key(source, partition), None) is not None:
            temp_path = self.path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)

    @locked_json
    def snapshot(self):
        return {"path": str(self.path), "progress": self._read()}


class FixedTargetSource:
    """把一批已知目标包装成 SourceAdapter，供 CrawlEngine 复用
    fetch/parse/save/失败台账 流程（跳过 discover 阶段的列表抓取）。"""

    def __init__(self, source, targets: Sequence[CrawlTarget]):
        self.name = source.name
        self._source = source
        self._targets = list(targets)

    def discover(self, context, http) -> DiscoveryResult:
        return DiscoveryResult(targets=self._targets)

    def parse_detail(self, target, result):
        return self._source.parse_detail(target, result)

    def collect_detail(self, target, result, http, context):
        collect = getattr(self._source, "collect_detail", None)
        return collect(target, result, http, context) if collect else self.parse_detail(target, result)


class MongoPageCheckpointStore:
    def __init__(self, collection, scope):
        self.collection = collection
        self.scope = scope

    def _key(self, source, partition):
        return f"{self.scope}:{source}:{partition}"

    def load(self, source, partition):
        row = self.collection.find_one({"_id": self._key(source, partition)})
        return int(row.get("page", 0)) if row else 0

    def save(self, source, partition, page):
        from datetime import datetime, timezone
        self.collection.update_one({"_id": self._key(source, partition)},
            {"$set": {"page": int(page), "source": source, "partition": str(partition),
                      "updated_at": datetime.now(timezone.utc)}}, upsert=True)


def build_checkpoint_store(source, start_page, end_page, base_url, order):
    import hashlib
    from util.read_config import get_config
    scope = hashlib.sha256(json.dumps([source, start_page, end_page, base_url, order],
                                     ensure_ascii=False).encode()).hexdigest()[:24]
    if get_config("mongodb.enable", False):
        from util.mongo import db
        return MongoPageCheckpointStore(db["crawl_checkpoints"], scope)
    return PageCheckpointStore(scope=scope)


def checkpoint_snapshot():
    from util.read_config import get_config
    if get_config("mongodb.enable", False):
        from util.mongo import db
        rows = db["crawl_checkpoints"].find({}, {"_id": 1, "page": 1})
        return {"path": "MongoDB:crawl_checkpoints",
                "progress": {row["_id"]: row["page"] for row in rows}}
    return PageCheckpointStore().snapshot()
