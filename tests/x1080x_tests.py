import unittest
from types import SimpleNamespace

from scrapers.core.cf_challenge import is_cf_challenge
from scrapers.core.contracts import CrawlContext, CrawlTarget
from scrapers.core.models import FetchResult
from scrapers.sources.x1080x import X1080XParser, X1080XRepository, X1080XSource
from util.javbee_code import resolve_x1080x_code
from util.mongo import X1080X_RESOURCE_FIELDS, save_x1080x_items
from util.resource_clock import fingerprint

TYPE_MAP = {"5479": "中文字幕", "5206": "亚洲有码"}

LIST_HTML = """
<html><body><div id="threadlist">
  <a href="forum.php?mod=viewthread&amp;tid=1001&amp;archiver=1">ABC-001 标题一</a>
  <a href="thread-1002-1-1.html">ABC-002 标题二</a>
  <a href="forum.php?mod=viewthread&amp;tid=1001&amp;archiver=1">重复链接</a>
  <a href="forum.php?mod=misc">无关链接</a>
</div></body></html>
""".encode("utf-8")

EMPTY_LIST_HTML = '<html><body><div id="threadlist">本版块或指定的范围内尚无主题</div></body></html>'.encode('utf-8')

UNAVAILABLE_HTML = "<html><head><title>提示</title></head><body>archiver 功能没有开</body></html>".encode("utf-8")

DETAIL_HTML = """
<html><body>
<div id="pt">论坛 › 会员资源 › 中文字幕</div>
<span id="thread_subject">[FHD] MIDV-086 标题文本</span>
<em id="authorposton123">发表于 2026/9/6</em>
<a href="forum.php?mod=forumdisplay&amp;fid=244&amp;filter=typeid&amp;typeid=5479">中文字幕</a>
<div id="postmessage_123">
  正文说明
  <img src="https://img.example/a.jpg">
  <img zoomfile="https://img.example/b.png" src="static/placeholder.png">
  <img src="https://img.example/c.gif">
  [code]magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567[/code]
  magnet:?xt=urn:btih:aaaabbbbccccddddeeeeffff0000111122223333，
</div>
</body></html>
""".encode("utf-8")

DETAIL_HTML_NAV_ONLY = """
<html><body>
<div id="nav">论坛 › 中文字幕 › MIDV-086 备用标题</div>
<p class="author">x 发表于 2026-09-06</p>
<div id="content">
  magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567
</div>
</body></html>
""".encode("utf-8")

DETAIL_HTML_NO_MAGNET = """
<html><body>
<span id="thread_subject">ABC-003 无磁链帖</span>
<em id="authorposton1">发表于 2026-09-05</em>
<div id="postmessage_1">只有文字</div>
</body></html>
""".encode("utf-8")


class X1080XParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = X1080XParser(TYPE_MAP)

    def test_parses_list_tids_from_both_link_formats(self):
        self.assertEqual([1001, 1002], self.parser.parse_list(LIST_HTML))

    def test_unavailable_page_yields_no_tids(self):
        self.assertEqual([], self.parser.parse_list(UNAVAILABLE_HTML))

    def test_parses_detail_contract(self):
        item = self.parser.parse_detail(
            DETAIL_HTML,
            "https://agaghhh.cc/forum.php?mod=viewthread&tid=1001&archiver=1",
            tid=1001,
            fid=244,
        )

        self.assertEqual("1001", item["source_key"])
        self.assertEqual(1001, item["tid"])
        self.assertEqual(244, item["fid"])
        self.assertEqual("5479", item["typeid"])
        self.assertEqual("中文字幕", item["section"])
        self.assertEqual("[FHD] MIDV-086 标题文本", item["title"])
        self.assertEqual("MIDV-086", item["code"])
        self.assertEqual("MIDV086", item["code_normalized"])
        self.assertEqual("2026-09-06", item["date"])
        self.assertEqual(2, len(item["magnets"]))
        self.assertEqual(item["magnets"][0], item["magnet"])
        self.assertTrue(item["magnet"].startswith("magnet:?xt=urn:btih:0123"))
        # 第二条磁链应剥离行尾中文标点
        self.assertTrue(item["magnets"][1].endswith("22223333"))
        # gif 预览图被过滤；相对路径补全为绝对 URL
        self.assertEqual(
            [
                "https://img.example/a.jpg",
                "https://img.example/b.png",
                "https://agaghhh.cc/static/placeholder.png",
            ],
            item["img"],
        )

    def test_detail_falls_back_to_nav_title_and_metadata_typeid(self):
        item = self.parser.parse_detail(
            DETAIL_HTML_NAV_ONLY,
            "https://agaghhh.cc/forum.php?mod=viewthread&tid=1002&archiver=1",
            tid=1002,
            fid=244,
            typeid="5206",
            section="亚洲有码",
        )

        self.assertEqual("MIDV-086 备用标题", item["title"])
        # 面包屑里的已知分类名优先于列表页传入的 typeid
        self.assertEqual("5479", item["typeid"])
        self.assertEqual("中文字幕", item["section"])
        self.assertEqual("2026-09-06", item["date"])

    def test_detail_without_date_is_invalid(self):
        html = DETAIL_HTML.replace("发表于 2026/9/6".encode("utf-8"), b"")
        self.assertIsNone(
            self.parser.parse_detail(html, "https://x/1", tid=1, fid=244)
        )


class X1080XCodeResolutionTests(unittest.TestCase):
    def test_extracts_code_from_title_bracket(self):
        r = resolve_x1080x_code(
            "(杏吧傳媒)(xb-5441)(20260828)公園情趣絲襪長凳椅大戰騷爆"
        )
        self.assertEqual("XB-5441", r.code)
        self.assertEqual(("title", "bracket", "high"), (r.source, r.rule, r.confidence))

    def test_fullwidth_brackets_and_date_bracket_not_confused(self):
        r = resolve_x1080x_code("（杏吧傳媒）（xb-5507）（20260830）極品大學生")
        self.assertEqual("XB-5507", r.code)
        self.assertIsNone(resolve_x1080x_code("(20260830)只有日期括号").code)

    def test_falls_back_to_magnet_dn(self):
        r = resolve_x1080x_code(
            "纯描述标题没有括号",
            ["magnet:?xt=urn:btih:abc&dn=xb-5507"],
        )
        self.assertEqual("XB-5507", r.code)
        self.assertEqual("magnet_dn", r.rule)

    def test_falls_back_to_javbee_title_rules(self):
        r = resolve_x1080x_code("[FHD] MIDV-086 标题文本")
        self.assertEqual("MIDV-086", r.code)


class X1080XSourceTests(unittest.TestCase):
    def _http(self, pages):
        class FakeHttp:
            def fetch(self, url, stage="detail"):
                body = pages.get(url)
                return FetchResult(
                    url=url,
                    body=body,
                    status_code=200 if body else 500,
                    attempts=1,
                    elapsed_ms=0,
                    error_type=None if body else "http_status",
                )

        return FakeHttp()

    def _source(self, page_limit=2):
        return X1080XSource(
            {
                "base_url": "https://agaghhh.cc",
                "fid": 244,
                "typeids": TYPE_MAP,
                "page_limit": page_limit,
            }
        )

    def test_discover_stops_on_empty_page_and_dedups_across_typeids(self):
        source = self._source()
        pages = {
            source.list_url("5479", 1): LIST_HTML,
            source.list_url("5479", 2): EMPTY_LIST_HTML,
            # 5206 分类第一页返回与 5479 相同的 tid
            source.list_url("5206", 1): LIST_HTML,
            source.list_url("5206", 2): EMPTY_LIST_HTML,
        }
        discovery = source.discover(
            CrawlContext(source="x1080x", run_id="test"),
            self._http(pages),
        )

        self.assertEqual(2, len(discovery.targets))
        target = discovery.targets[0]
        self.assertEqual("1001", target.key)
        self.assertEqual(
            "https://agaghhh.cc/forum.php?mod=viewthread&tid=1001",
            target.url,
        )
        self.assertEqual(
            {"tid": 1001, "typeid": "5479", "section": "中文字幕"},
            dict(target.metadata),
        )

    def test_discover_retries_once_when_first_page_parses_empty(self):
        # 首页第一次拿到无列表内容的中间态页面（过盾偶发），重试后恢复
        source = self._source(page_limit=1)
        interim = b"<html><title>loading</title><body></body></html>"
        calls = []

        class FlakyHttp:
            def fetch(self, url, stage="detail"):
                calls.append(url)
                body = interim if calls.count(url) == 1 else LIST_HTML
                return FetchResult(
                    url=url,
                    body=body,
                    status_code=200,
                    attempts=1,
                    elapsed_ms=0,
                )

        discovery = source.discover(
            CrawlContext(source="x1080x", run_id="test"),
            FlakyHttp(),
        )

        self.assertEqual(2, len(discovery.targets))
        # 每个分类首页各请求了两次（原始 + 重试）
        for typeid in TYPE_MAP:
            self.assertEqual(2, calls.count(source.list_url(typeid, 1)))

    def test_discover_aborts_on_rate_limit_page(self):
        # 过盾成功但站点返回"请求过于频繁"限流页，应立即中止整轮
        source = self._source()
        rl_body = "<html><title>訪問受限 - 請求過於頻繁</title></html>".encode("utf-8")
        pages = {source.list_url(typeid, 1): rl_body for typeid in TYPE_MAP}
        with self.assertRaises(RuntimeError) as ctx:
            source.discover(
                CrawlContext(source="x1080x", run_id="test"),
                self._http(pages),
            )
        self.assertIn("限流", str(ctx.exception))

    def test_discover_raises_when_all_lists_fail(self):
        source = self._source()
        with self.assertRaises(RuntimeError):
            source.discover(
                CrawlContext(source="x1080x", run_id="test"),
                self._http({}),
            )

    def test_parse_detail_drops_magnetless_posts(self):
        source = self._source()
        target = CrawlTarget(
            key="1003",
            url="https://agaghhh.cc/forum.php?mod=viewthread&tid=1003&archiver=1",
            metadata={"tid": 1003, "typeid": "5479", "section": "中文字幕"},
        )
        result = FetchResult(
            url=target.url,
            body=DETAIL_HTML_NO_MAGNET,
            status_code=200,
            attempts=1,
            elapsed_ms=0,
        )
        from scrapers.core.contracts import DetailValidationError
        source.diagnostics = False
        with self.assertRaises(DetailValidationError) as caught:
            source.parse_detail(target, result)
        self.assertEqual("missing_magnet", caught.exception.reason)


class X1080XRepositoryTests(unittest.TestCase):
    def _targets(self):
        return [
            CrawlTarget(key="1", url="u1"),
            CrawlTarget(key="2", url="u2"),
        ]

    def test_new_only_filters_existing_keys(self):
        repository = X1080XRepository(
            existing_lookup=lambda keys: {"1"},
            save_func=lambda items: {},
        )
        selected = repository.select_targets(self._targets())
        self.assertEqual(["2"], [target.key for target in selected])
        self.assertEqual(1, repository.existing_count)

    def test_refresh_all_keeps_existing_keys(self):
        repository = X1080XRepository(
            existing_lookup=lambda keys: {"1"},
            save_func=lambda items: {},
            refresh_all=True,
        )
        selected = repository.select_targets(self._targets())
        self.assertEqual(["1", "2"], [target.key for target in selected])


class X1080XMongoTests(unittest.TestCase):
    def test_save_uses_provenance_upsert_and_partition_indexes(self):
        class FakeCollection:
            def __init__(self):
                self.indexes = []
                self.operations = []

            def create_index(self, keys, **options):
                self.indexes.append((keys, options))

            def bulk_write(self, operations, ordered):
                self.operations = operations
                self.ordered = ordered
                return SimpleNamespace(
                    matched_count=0,
                    modified_count=0,
                    upserted_count=1,
                )

        collection = FakeCollection()
        summary = save_x1080x_items(
            [
                {
                    "source_key": "1001",
                    "tid": 1001,
                    "typeid": "5479",
                    "section": "中文字幕",
                    "title": "MIDV-086 标题",
                    "date": "2026-09-06",
                    "url": "https://agaghhh.cc/forum.php?mod=viewthread&tid=1001",
                    "magnet": "magnet:?xt=urn:btih:0123",
                    "magnets": ["magnet:?xt=urn:btih:0123"],
                }
            ],
            collection=collection,
        )

        index_names = [options["name"] for _, options in collection.indexes]
        self.assertIn("uniq_source_key", index_names)
        self.assertIn("idx_typeid_date", index_names)
        self.assertIn("idx_date_tid", index_names)
        self.assertIn("idx_code_normalized_date", index_names)
        self.assertIn("idx_resource_collected", index_names)

        self.assertEqual(2, len(collection.operations))
        insert_op = collection.operations[0]._doc["$setOnInsert"]
        self.assertTrue(insert_op["resource_collection_pending"])
        self.assertIn("created_at", insert_op)
        self.assertEqual(1, summary["upserted"])

    def test_missing_required_field_raises(self):
        with self.assertRaises(ValueError):
            save_x1080x_items(
                [{"source_key": "1", "tid": 1, "title": "t"}],
                collection=object(),
            )

    def test_section_change_alters_resource_fingerprint(self):
        base = {
            "title": "t",
            "magnet": "magnet:?xt=a",
            "typeid": "5479",
            "section": "中文字幕",
        }
        moved = {**base, "typeid": "5206", "section": "亚洲有码"}
        self.assertNotEqual(
            fingerprint(base, X1080X_RESOURCE_FIELDS),
            fingerprint(moved, X1080X_RESOURCE_FIELDS),
        )
        # 运维字段不影响指纹
        self.assertEqual(
            fingerprint(base, X1080X_RESOURCE_FIELDS),
            fingerprint({**base, "updated_at": "later"}, X1080X_RESOURCE_FIELDS),
        )


class CfChallengeDetectionTests(unittest.TestCase):
    def test_detects_by_status_title_and_marker(self):
        self.assertFalse(is_cf_challenge(b"", 403))
        self.assertTrue(
            is_cf_challenge(b"<title>Just a moment...</title>", 200)
        )
        self.assertTrue(is_cf_challenge(b'src="https://challenges.cloudflare.com/x"', 200))
        self.assertFalse(is_cf_challenge(b"<title>ok</title>", 200))
        self.assertFalse(is_cf_challenge(None, 200))


class X1080XNotificationTests(unittest.TestCase):
    def _scraper(self, notified):
        from scrapers.core.contracts import NullFailureStore
        from scrapers.x1080x_scraper import X1080XScraper

        class FakeHttp:
            def __init__(self, pages):
                self.pages = pages

            def fetch(self, url, stage="detail"):
                body = self.pages.get(url)
                return FetchResult(
                    url=url,
                    body=body,
                    status_code=200 if body else 500,
                    attempts=1,
                    elapsed_ms=0,
                    error_type=None if body else "http_status",
                )

            def fetch_many(self, urls, stage="detail"):
                return [self.fetch(url, stage) for url in urls]

        scraper = X1080XScraper.__new__(X1080XScraper)
        scraper.config = {
            "base_url": "https://agaghhh.cc",
            "fid": 244,
            "typeids": {"5479": "中文字幕"},
            "page_limit": 1,
        }
        base = "https://agaghhh.cc/forum.php"
        scraper.http = FakeHttp({
            f"{base}?mod=forumdisplay&fid=244&page=1&filter=typeid&typeid=5479&orderby=dateline": LIST_HTML,
            f"{base}?mod=viewthread&tid=1001": DETAIL_HTML,
            f"{base}?mod=viewthread&tid=1002": DETAIL_HTML,
        })
        scraper.failure_store = NullFailureStore()

        import scrapers.x1080x_scraper as module
        self._orig_lookup = module.find_existing_x1080x_keys
        self._orig_save = module.save_x1080x_items
        module.find_existing_x1080x_keys = lambda keys: set()
        module.save_x1080x_items = lambda items: {
            "processed": len(items), "upserted": len(items), "modified": 0,
        }
        self.addCleanup(self._restore, module)

        class FakeNotifier:
            def enqueue_x1080x_notifications(self, data_list):
                notified.extend(data_list)
                return True

        import scrapers.notification_manager as notification_module
        self._orig_manager = notification_module.NotificationManager
        notification_module.NotificationManager = FakeNotifier
        self.addCleanup(
            setattr, notification_module, "NotificationManager", self._orig_manager
        )
        return scraper

    def _restore(self, module):
        module.find_existing_x1080x_keys = self._orig_lookup
        module.save_x1080x_items = self._orig_save

    def test_incremental_crawl_notifies_saved_items(self):
        notified = []
        scraper = self._scraper(notified)

        scraper.crawl()

        self.assertEqual(2, len(notified))
        self.assertEqual("1001", notified[0]["source_key"])
        self.assertTrue(notified[0]["magnet"].startswith("magnet:?"))

    def test_dry_run_and_retry_failed_do_not_notify(self):
        notified = []
        scraper = self._scraper(notified)

        scraper.crawl(dry_run=True)
        self.assertEqual([], notified)

        scraper.crawl(retry_failed=True)
        self.assertEqual([], notified)

    def test_notify_can_be_disabled_by_config(self):
        notified = []
        scraper = self._scraper(notified)
        scraper.config["notify_telegram"] = False

        scraper.crawl()

        self.assertEqual([], notified)


class X1080XHttpClientTests(unittest.TestCase):
    def test_cf_challenge_triggers_bypass_and_reuses_cookies(self):
        from scrapers.core.config import HttpSettings
        from scrapers.sources.x1080x.http_client import X1080XHttpClient

        cf_body = b"<title>Just a moment...</title>"
        ok_body = b"<title>forum</title><div id='content'>ok</div>"

        class FakeTransport:
            def __init__(self):
                self.calls = 0

            def fetch(self, url, stage="detail"):
                self.calls += 1
                return FetchResult(
                    url=url,
                    body=cf_body,
                    status_code=403,
                    attempts=1,
                    elapsed_ms=0,
                )

        class FakeSolver:
            def solve(self, url, cookies=None):
                return ok_body, [{"name": "cf_clearance", "value": "abc"}], "UA/2.0"

        client = X1080XHttpClient(
            HttpSettings(),
            transport=FakeTransport(),
        )
        client._flaresolverr = FakeSolver()

        result = client.fetch("https://agaghhh.cc/forum.php")

        self.assertTrue(result.ok)
        self.assertEqual(ok_body, result.body)
        self.assertEqual({"cf_clearance": "abc"}, client._cookie_copy())
        self.assertEqual("UA/2.0", client._user_agent)

    def test_bypass_failure_returns_cf_error(self):
        from scrapers.core.config import HttpSettings
        from scrapers.sources.x1080x.http_client import X1080XHttpClient

        class FakeTransport:
            def fetch(self, url, stage="detail"):
                return FetchResult(
                    url=url,
                    body=b"<title>Just a moment...</title>",
                    status_code=403,
                    attempts=1,
                    elapsed_ms=0,
                )

        client = X1080XHttpClient(HttpSettings(), transport=FakeTransport())
        result = client.fetch("https://agaghhh.cc/forum.php")

        self.assertFalse(result.ok)
        self.assertEqual("cf_challenge", result.error_type)


if __name__ == "__main__":
    unittest.main()
