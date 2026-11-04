"""新版 x1080x 来源：正文磁链和免费 torrent 附件。"""
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlencode
from bs4 import BeautifulSoup

from scrapers.core.contracts import (
    CrawlContext,
    CrawlRecord,
    CrawlTarget,
    DiscoveryResult,
    DetailValidationError,
    SourcePaused,
)
from scrapers.core.cf_challenge import is_rate_limited
from scrapers.core.http import CrawlerHttpClient
from scrapers.core.models import FetchResult
from util.log_util import log

from .parser import X1080XParser, ListValidationError
from .attachments import collect_free_torrents
from util.javbee_code import normalize_code_key, resolve_x1080x_code

DEFAULT_BASE_URL = "https://1080xin.com"
DEFAULT_BOARD_ID = "7RHPO2w4jFUYiDhDsxz0wE"
DEFAULT_TYPE_MAP = {
    "5CfYpFiXzzHoUtfJyFd3FV": "国内成人",
    "7aUtFOJbmFXIMDEx8Smqkm": "亚洲有码",
    "2lrSOyEwyacrCPS2aN7ZM6": "亚洲无码",
    "1v7HJi1gXdgaOohKjlK9L0": "FC2",
    "1RzNrulROCeqHTIW8wSADv": "MGS",
    "6gGAPsMAGtQzqxIYyAQBbS": "中文字幕",
    "0hbFa1xIVkbRIvm6oOYbff": "探花精选",
    "7EHxTALDTN1Yn12YTGMjdY": "主播精选",
}
LEGACY_CATEGORIES = dict(zip(("5212", "5206", "5207", "5208", "5216", "5479", "5213", "5217"), DEFAULT_TYPE_MAP))


class X1080XSource:
    name = "x1080x"

    def __init__(self, config: Mapping[str, Any], parser: Optional[X1080XParser] = None,
                 *, diagnostics: bool = True):
        self.config = dict(config)
        self.diagnostics = diagnostics
        self.base_url = str(
            self.config.get("base_url", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.board_id = str(self.config.get("board_id", DEFAULT_BOARD_ID))
        configured_typeids = self.config.get("categories", self.config.get("typeids"))
        self.type_map = {
            str(key): str(value)
            for key, value in (DEFAULT_TYPE_MAP if configured_typeids is None else configured_typeids).items()
        }
        self.boards = {str(k): str(v) for k, v in (self.config.get("boards") or {}).items()}
        self.type_map.update(self.boards)
        self.page_limit = max(1, int(self.config.get("page_limit", 3)))
        self.parser = parser or X1080XParser(self.type_map)

    def list_url(self, typeid: str, page: int) -> str:
        board, category = self.partition_ids(typeid)
        query = {"page": page}
        if category:
            query["category_id"] = category
        return f"{self.base_url}/boards/{board}?{urlencode(query)}"

    def partition_ids(self, partition):
        if partition in self.boards:
            return partition, ""
        return self.board_id, LEGACY_CATEGORIES.get(str(partition), str(partition))

    def detail_url(self, resource_id) -> str:
        return f"{self.base_url}/resources/{resource_id}?page=1&order=asc"

    def make_target(self, resource_id, partition, section):
        board, category = self.partition_ids(partition)
        return CrawlTarget(str(resource_id), self.detail_url(resource_id), partition,
                           {"resource_id": str(resource_id), "board_id": board,
                            "category_id": category, "section": section})

    def normalize_target(self, target: CrawlTarget) -> CrawlTarget:
        """新版目标使用当前域名；不猜测旧数字 tid 与新版资源的对应关系。"""
        if str(target.key).isdigit() and not (target.metadata or {}).get("resource_id"):
            log.warning("x1080x 跳过旧 Discuz 失败目标，保留原台账")
            return None
        return replace(target, url=self.detail_url(target.key))

    def fetch_list_page(self, http, typeid, page):
        result = http.fetch(self.list_url(typeid, page), stage="list")
        self._check_rate_limit(result)
        if not result.ok:
            return result, None
        try:
            parsed = self.parser.parse_list_page(result.body, page)
        except ListValidationError:
            # 只重试一次非目标页面，不能把重试失败或中间态当作列表到底。
            retry = http.fetch(self.list_url(typeid, page), stage="list")
            self._check_rate_limit(retry)
            result = replace(retry, attempts=result.attempts + retry.attempts)
            if not result.ok:
                return result, None
            try:
                parsed = self.parser.parse_list_page(result.body, page)
            except ListValidationError as exc:
                self.dump_empty_list_page(typeid, page, result.body)
                return replace(result, error_type="invalid_list", error_message=str(exc)), None
        return result, parsed

    def dump_empty_list_page(self, typeid: str, page: int, body) -> None:
        """首个列表页就解析出 0 条时留现场：告警并把响应体落盘。

        正常「到底」只会发生在深分页；首页为空大概率是页面结构变化、
        镜像域名跳转页或过盾返回了非目标内容，靠 dump 文件排查。
        """
        raw = body or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "ignore")
        match = re.search(rb"<title[^>]*>(.*?)</title>", raw[:5000], re.I | re.S)
        title = match.group(1).decode("utf-8", "ignore").strip() if match else ""
        dump_note = ""
        try:
            if not self.diagnostics:
                return
            debug_dir = Path(__file__).resolve().parents[3] / "data" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            dump_path = debug_dir / f"x1080x_list_{typeid}_p{page}.html"
            dump_path.write_bytes(self._safe_debug_html(raw))
            dump_note = f" dump={dump_path}"
        except OSError as exc:
            dump_note = f" dump_failed={exc}"
        log.warning(
            "x1080x 列表页解析为 0 条（疑似结构变化或非目标页面）: "
            f"typeid={typeid} page={page} bytes={len(raw)} "
            f"title={title!r}{dump_note}"
        )

    def discover(
        self,
        context: CrawlContext,
        http: CrawlerHttpClient,
    ) -> DiscoveryResult:
        targets = []
        seen_tids = set()
        list_failures = 0
        list_retries = 0
        pages_fetched = 0

        for typeid, section in self.type_map.items():
            seen_pages = set()
            for page in range(1, self.page_limit + 1):
                result, parsed = self.fetch_list_page(http, typeid, page)
                list_retries += max(0, result.attempts - 1)
                self._check_rate_limit(result)
                if not result.ok:
                    if result.error_type == "session_paused":
                        raise SourcePaused(result.error_message or "session_unavailable", "x1080x 列表会话不可用，本轮暂停")
                    if result.error_type == "cf_blocked":
                        raise RuntimeError("x1080x 列表被 Cloudflare 明确封锁，停止本轮抓取")
                    list_failures += 1
                    log.warning(
                        "x1080x 列表页获取失败: "
                        f"typeid={typeid} page={page} "
                        f"error_type={result.error_type}"
                    )
                    break

                pages_fetched += 1
                tids = parsed.tids
                if not tids:
                    break
                signature = frozenset(tids)
                if signature in seen_pages:
                    list_failures += 1
                    log.warning(f"x1080x 分类 {typeid} 第 {page} 页重复，停止该分类")
                    break
                seen_pages.add(signature)
                for tid in tids:
                    if tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    targets.append(self.make_target(tid, typeid, section))
                if parsed.last_page is not None and page >= parsed.last_page:
                    break

        if not pages_fetched:
            raise RuntimeError("x1080x 所有分类列表页均获取失败")

        return DiscoveryResult(
            targets=targets,
            failed=list_failures,
            details={
                "pages": pages_fetched,
                "list_pages_failed": list_failures,
                "list_retries": list_retries,
                "discovery_retries": list_retries,
            },
        )

    def parse_detail(
        self,
        target: CrawlTarget,
        result: FetchResult,
    ) -> Optional[CrawlRecord]:
        payload = self._parse_payload(target, result)
        if not payload.get("magnet"):
            raise DetailValidationError("missing_magnet")
        payload.pop("attachment_slots", None)
        return CrawlRecord(target=target, payload=payload)

    def _parse_payload(self, target, result):
        metadata = dict(target.metadata or {})
        try:
            return self.parser.parse_resource(result.body, target.url,
                resource_id=str(target.key), board_id=metadata.get("board_id", self.board_id),
                category_id=str(metadata.get("category_id") or metadata.get("typeid") or ""),
                section=str(metadata.get("section") or ""))
        except DetailValidationError as exc:
            if exc.reason not in {"membership_required", "login_required"}:
                self._diagnose_detail(target.key, result, exc.reason)
            raise

    def collect_detail(self, target, result, http, context):
        client = getattr(result, "client", None)
        try:
            payload = self._parse_payload(target, result)
        except DetailValidationError as exc:
            if exc.reason != "login_required" or client is None:
                raise
            rendered = client.render_public(target.url)
            if rendered:
                result = replace(result, body=rendered)
                try:
                    payload = self._parse_payload(target, result)
                except DetailValidationError as retry_exc:
                    if retry_exc.reason != "login_required":
                        raise
                    rendered = None
            if not rendered:
                client.ensure_authenticated(target.key)
                result = client.fetch(target.url)
                if not result.ok:
                    raise SourcePaused("session_unavailable", "x1080x 登录后详情请求未通过")
                payload = self._parse_payload(target, result)
        slots = payload.pop("attachment_slots", [])
        if not payload.get("magnet") and slots:
            if client is None:
                raise SourcePaused("login_required", "x1080x 附件需要同线路的共享登录会话")
            magnets = collect_free_torrents(client, target.key, slots, dry_run=context.dry_run)
            if context.dry_run:
                payload["download_pending"] = True
            else:
                payload.update(magnet=magnets[0] if magnets else None, magnets=magnets)
                resolution = resolve_x1080x_code(payload["title"], magnets)
                payload.update(code=resolution.code, code_normalized=normalize_code_key(resolution.code),
                               code_source=resolution.source, code_confidence=resolution.confidence)
        if not payload.get("magnet") and not payload.get("download_pending"):
            raise DetailValidationError("missing_magnet")
        return CrawlRecord(target, payload)

    @staticmethod
    def _check_rate_limit(result):
        if result.error_type == "rate_limited" or is_rate_limited(result.body):
            raise RuntimeError("x1080x 命中站点限流，中止本轮增量抓取，等待下次调度")

    @staticmethod
    def _safe_debug_html(raw):
        soup = BeautifulSoup(raw[:512 * 1024], "html.parser")
        for node in soup.select("script, input, textarea, .header-member"):
            node.decompose()
        return str(soup).encode("utf-8")[:512 * 1024]

    def _diagnose_detail(self, tid, result, reason):
        raw = result.body or b""
        match = re.search(rb"<title[^>]*>(.*?)</title>", raw[:5000], re.I | re.S)
        title = match.group(1).decode("utf-8", "replace")[:160] if match else ""
        dump_note = ""
        if self.diagnostics:
            try:
                debug_dir = Path(__file__).resolve().parents[3] / "data" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                dump_path = debug_dir / f"x1080x_detail_{tid}.html"
                dump_path.write_bytes(self._safe_debug_html(raw))
                files = sorted(debug_dir.glob("x1080x_detail_*.html"),
                               key=lambda path: path.stat().st_mtime, reverse=True)
                for old in files[20:]:
                    old.unlink()
                dump_note = f" dump={dump_path}"
            except OSError as exc:
                dump_note = f" dump_failed={exc}"
        log.warning(f"x1080x 详情校验失败: tid={tid} reason={reason} "
                    f"status={result.status_code} bytes={len(raw)} title={title!r}{dump_note}")
