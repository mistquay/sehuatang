import asyncio
import tempfile
import unittest
from pathlib import Path

from scrapers.core.contracts import NullFailureStore
from scrapers.core.models import FetchResult
from scrapers.data_manager import DataManager
from scrapers.data_processor import DataProcessor
from scrapers.page_backfill import PageCheckpointStore
from scrapers.page_parser import PageParser
from scrapers.sources.x1080x import X1080XRepository
from scrapers.web_scraper import WebScraper
from scrapers.x1080x_scraper import X1080XScraper
from tests.x1080x_tests import DETAIL_HTML, EMPTY_LIST_HTML, LIST_HTML, TYPE_MAP


class PageCheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PageCheckpointStore(Path(self.tmp.name) / "progress.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_partition_isolation(self):
        self.assertEqual(0, self.store.load("x1080x", "5479"))
        self.store.save("x1080x", "5479", 12)
        self.store.save("sehuatang", 103, 7)
        self.assertEqual(12, self.store.load("x1080x", "5479"))
        self.assertEqual(7, self.store.load("sehuatang", 103))

        self.store.clear("x1080x", "5479")
        self.assertEqual(0, self.store.load("x1080x", "5479"))
        self.assertEqual(7, self.store.load("sehuatang", 103))

    def test_corrupted_file_treated_as_empty(self):
        path = Path(self.tmp.name) / "progress.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(0, self.store.load("x1080x", "5479"))


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch(self, url, stage="detail"):
        self.fetched.append(url)
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


class X1080XBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkpoints = PageCheckpointStore(Path(self.tmp.name) / "p.json")
        self.saved_items = []

    def tearDown(self):
        self.tmp.cleanup()

    def _scraper(self, pages, existing=frozenset()):
        scraper = X1080XScraper.__new__(X1080XScraper)
        scraper.config = {
            "base_url": "https://agaghhh.cc",
            "fid": 244,
            "typeids": {"5479": "中文字幕"},
            "page_limit": 3,
        }
        scraper.http = FakeHttp(pages)
        scraper.failure_store = NullFailureStore()

        def fake_save(items):
            self.saved_items.extend(items)
            return {"processed": len(items), "upserted": len(items), "modified": 0}

        import scrapers.x1080x_scraper as module
        self._orig_lookup = module.find_existing_x1080x_keys
        self._orig_save = module.save_x1080x_items
        module.find_existing_x1080x_keys = lambda keys: set(existing) & set(keys)
        module.save_x1080x_items = fake_save
        self.addCleanup(self._restore, module)
        return scraper

    def _restore(self, module):
        module.find_existing_x1080x_keys = self._orig_lookup
        module.save_x1080x_items = self._orig_save

    def _urls(self):
        base = "https://agaghhh.cc/forum.php"
        return {
            "list1": f"{base}?mod=forumdisplay&fid=244&page=1&filter=typeid&typeid=5479&orderby=dateline",
            "list2": f"{base}?mod=forumdisplay&fid=244&page=2&filter=typeid&typeid=5479&orderby=dateline",
            "detail1001": f"{base}?mod=viewthread&tid=1001",
            "detail1002": f"{base}?mod=viewthread&tid=1002",
        }

    def test_backfills_page_range_and_advances_checkpoint(self):
        urls = self._urls()
        scraper = self._scraper({
            urls["list1"]: LIST_HTML,
            urls["list2"]: EMPTY_LIST_HTML,
            urls["detail1001"]: DETAIL_HTML,
            urls["detail1002"]: DETAIL_HTML,
        })

        summary = scraper.backfill_pages(
            1, 5, checkpoint_store=self.checkpoints
        )

        self.assertEqual(1, summary["pages_scanned"])
        self.assertEqual(2, summary["discovered"])
        self.assertEqual(2, summary["saved"])
        self.assertEqual("exhausted@2", summary["partitions"]["5479"]["stopped"])
        self.assertEqual(1, self.checkpoints.load("x1080x", "5479"))
        self.assertEqual(2, len(self.saved_items))

    def test_skips_existing_and_resumes_from_checkpoint(self):
        urls = self._urls()
        self.checkpoints.save("x1080x", "5479", 1)
        scraper = self._scraper(
            {
                urls["list2"]: EMPTY_LIST_HTML,
            },
            existing={"1001"},
        )

        summary = scraper.backfill_pages(
            1, 5, resume=True, checkpoint_store=self.checkpoints
        )

        # 检查点为 1，续跑从第 2 页开始且不再请求第 1 页
        self.assertNotIn(urls["list1"], scraper.http.fetched)
        self.assertEqual("exhausted@2", summary["partitions"]["5479"]["stopped"])

    def test_list_failure_stops_partition_without_checkpoint(self):
        urls = self._urls()
        scraper = self._scraper({
            urls["list1"]: LIST_HTML,
            urls["detail1001"]: DETAIL_HTML,
            urls["detail1002"]: DETAIL_HTML,
            # list2 缺失 -> 请求失败
        })

        summary = scraper.backfill_pages(
            1, 5, checkpoint_store=self.checkpoints
        )

        self.assertEqual("list_failed@2", summary["partitions"]["5479"]["stopped"])
        # 第 1 页完成检查点推进到 1，失败页未推进
        self.assertEqual(1, self.checkpoints.load("x1080x", "5479"))

    def test_unknown_typeid_raises(self):
        scraper = self._scraper({})
        with self.assertRaises(ValueError):
            scraper.backfill_pages(
                1, 2, typeids=["9999"], checkpoint_store=self.checkpoints
            )

    def test_rate_limited_list_and_detail_recover_before_checkpoint(self):
        from tests.x1080x_rate_limit_tests import RATE_HTML, scripted_client
        urls = self._urls()
        scraper = self._scraper({})
        scraper.http, clock, calls = scripted_client({
            urls["list1"]: [RATE_HTML, RATE_HTML, LIST_HTML],
            urls["detail1001"]: [DETAIL_HTML],
            urls["detail1002"]: [RATE_HTML, DETAIL_HTML],
            urls["list2"]: [EMPTY_LIST_HTML],
        })
        waited = []

        def wait(seconds):
            self.assertEqual(0, self.checkpoints.load("x1080x", "5479"))
            waited.append(seconds)
            clock.wait(seconds)

        scraper.http.gate._wait = wait
        from unittest.mock import Mock
        scraper.failure_store = Mock()
        summary = scraper.backfill_pages(1, 2, checkpoint_store=self.checkpoints)
        self.assertEqual(2, summary["saved"])
        self.assertEqual(0, summary["failed"])
        self.assertEqual(1, self.checkpoints.load("x1080x", "5479"))
        self.assertEqual(240, sum(waited))
        self.assertEqual(1, sum(url == urls["detail1001"] for url, _, _ in calls))
        scraper.failure_store.record.assert_not_called()

    def test_stop_during_detail_cooldown_keeps_previous_checkpoint(self):
        from tests.x1080x_rate_limit_tests import RATE_HTML, scripted_client
        from scrapers.sources.x1080x.rate_limit import CrawlStopped
        urls = self._urls()
        scraper = self._scraper({})
        scraper.http, _, _ = scripted_client({
            urls["list1"]: [LIST_HTML],
            urls["detail1001"]: [DETAIL_HTML],
            urls["detail1002"]: [RATE_HTML],
        })
        scraper.http.gate._wait = lambda _: scraper.http.gate.stop_event.set()
        with self.assertRaises(CrawlStopped):
            scraper.backfill_pages(1, 2, checkpoint_store=self.checkpoints)
        self.assertEqual(0, self.checkpoints.load("x1080x", "5479"))

    def test_dry_run_rate_limit_recovery_does_not_write(self):
        from tests.x1080x_rate_limit_tests import RATE_HTML, scripted_client
        urls = self._urls()
        scraper = self._scraper({})
        scraper.http, _, _ = scripted_client({
            urls["list1"]: [RATE_HTML, LIST_HTML],
            urls["detail1001"]: [DETAIL_HTML],
            urls["detail1002"]: [RATE_HTML, DETAIL_HTML],
        })
        summary = scraper.backfill_pages(1, 1, dry_run=True, checkpoint_store=self.checkpoints)
        self.assertEqual(0, summary["failed"])
        self.assertEqual([], self.saved_items)
        self.assertEqual(0, self.checkpoints.load("x1080x", "5479"))


def _plate_html(tids):
    rows = "".join(
        f"""
        <tbody id="normalthread_{tid}">
          <tr>
            <th><a class="s xst">ABP-{tid} title</a></th>
            <td class="by"><em><span title="2020-05-0{i + 1}">2020-05-0{i + 1}</span></em></td>
            <td><a class="showcontent y" id="content_{tid}"></a></td>
          </tr>
        </tbody>
        """
        for i, tid in enumerate(tids)
    )
    return f'<html><body><div id="threadlist"><table id="threadlisttableid">{rows}</table></div></body></html>'


def _pager(current, last=None, next_page=None):
    links = f'<strong>{current}</strong>'
    if last is not None:
        links += f'<a class="last" href="forum.php?mod=forumdisplay&amp;fid=103&amp;page={last}">... {last}</a>'
    if next_page is not None:
        links += f'<a class="nxt" href="forum-103-{next_page}.html">下一页</a>'
    if current > 1:
        links += f'<a href="forum-103-{current - 1}.html">{current - 1}</a>'
    return f'<div class="pg">{links}</div>'


class SehuatangPaginationParserTests(unittest.TestCase):
    def test_explicit_last_links_and_total_hints(self):
        parser = PageParser()
        for pager in (_pager(1, 50, 2),
                      '<div class="pg"><strong>1</strong><a class="last" href="forum-103-50.html">...50</a></div>',
                      '<div class="pg"><strong>1</strong><label><span title="共 50 页"> / 50 页</span></label></div>'):
            result = parser.parse_plate_pagination(pager)
            self.assertEqual((1, 50), (result.current_page, result.last_page))

    def test_visible_neighbour_pages_are_not_a_total(self):
        pager = '<div class="pg"><strong>1</strong><a href="forum-103-10.html">10</a><a class="nxt" href="forum-103-2.html">下一页</a></div>'
        self.assertIsNone(PageParser().parse_plate_pagination(pager).last_page)
        self.assertIsNone(PageParser().parse_plate_pagination(_plate_html(["1"])).last_page)

    def test_current_page_without_forward_links_can_confirm_last(self):
        self.assertEqual(50, PageParser().parse_plate_pagination(_pager(50)).last_page)
        self.assertEqual(1, PageParser().parse_plate_pagination(_pager(1)).last_page)

    def test_conflicting_pagination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不一致"):
            PageParser().parse_plate_pagination(_pager(1, 50, 2) + _pager(1, 60, 2))

    def test_strict_empty_list_requires_valid_structure(self):
        parser = PageParser()
        self.assertEqual([], parser.parse_backfill_page(_plate_html([]))[0])
        for body in ('<html><body></body></html>',
                     '<div id="threadlist"><div id="messagetext">抱歉，没有权限</div></div>',
                     '<form id="loginform"></form><div id="threadlist"></div>',
                     '<title>Just a moment</title><div id="threadlist"></div>',
                     '<title>访问受限</title><div id="threadlist"></div>'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parser.parse_backfill_page(body)

    def test_partial_thread_parse_failure_is_not_silently_dropped(self):
        body = _plate_html(["100"]) + '<tbody id="normalthread_101"><tr><td>broken</td></tr></tbody>'
        with self.assertRaisesRegex(ValueError, "字段解析不完整"):
            PageParser().parse_backfill_page(body)


class SehuatangBackfillPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkpoints = PageCheckpointStore(Path(self.tmp.name) / "p.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _scraper(self, page_bodies, detail_results):
        scraper = WebScraper.__new__(WebScraper)
        scraper.log = __import__("util.log_util", fromlist=["log"]).log
        scraper.dry_run = False
        scraper.page_parser = PageParser()
        scraper.data_processor = DataProcessor()
        scraper.data_manager = DataManager.__new__(DataManager)
        scraper.data_manager.log = scraper.log
        scraper.data_manager.mongodb_enable = False
        scraper.failure_store = NullFailureStore()

        class FakeInnerHttp:
            def __init__(self):
                self.fetched = []

            def get_html(self, url):
                self.fetched.append(url)
                return page_bodies.get(url)

        scraper.http = FakeInnerHttp()

        saved = []
        scraper._saved = saved

        def fake_details(info_list):
            data = [
                detail_results[str(info["tid"])]
                for info in info_list
                if str(info["tid"]) in detail_results
            ]
            failures = len(info_list) - len(data)
            return data, failures

        scraper._get_thread_details_batch_result = fake_details

        def fake_save(data_list, fid, strict=False, dry_run=False):
            saved.extend(data_list)
            return data_list

        scraper.data_manager.filter_and_save_data = fake_save
        scraper.data_manager.compare_existing_data = (
            lambda tids, fid, infos: (tids, infos)
        )
        scraper._clear_detail_failures = lambda data_list: None
        return scraper

    def _url(self, fid, page):
        return WebScraper._build_plate_url(fid, page, ordered_by_dateline=True)

    def _detail(self, tid):
        return {"tid": tid, "post_time": "2020-05-01", "magnet": "magnet:?a"}

    def test_stops_at_known_last_page_without_requesting_beyond_it(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100"]) + _pager(1, 2, 2),
            self._url(103, 2): _plate_html(["101"]) + _pager(2),
        }, {tid: self._detail(tid) for tid in ("100", "101")})
        result = asyncio.run(scraper.backfill_pages(103, 1, 2000, checkpoint_store=self.checkpoints))
        self.assertEqual("last_page@2", result["stopped"])
        self.assertEqual(2, result["saved"])
        self.assertEqual(2, self.checkpoints.load("sehuatang", 103))
        self.assertEqual([self._url(103, 1), self._url(103, 2)], scraper.http.fetched)

    def test_resume_beyond_last_only_reads_first_page_and_preserves_old_checkpoint(self):
        self.checkpoints.save("sehuatang", 103, 1000)
        scraper = self._scraper({self._url(103, 1): _plate_html(["100"]) + _pager(1, 50, 2)}, {})
        result = asyncio.run(scraper.backfill_pages(103, 1, 2000, resume=True, checkpoint_store=self.checkpoints))
        self.assertEqual("last_page@50", result["stopped"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(0, result["pages_scanned"])
        self.assertEqual(1000, self.checkpoints.load("sehuatang", 103))
        self.assertEqual([self._url(103, 1)], scraper.http.fetched)

    def test_repeated_page_is_failure_without_advancing_checkpoint(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100", "101"]),
            self._url(103, 2): _plate_html(["101", "100"]),
        }, {tid: self._detail(tid) for tid in ("100", "101")})
        result = asyncio.run(scraper.backfill_pages(103, 1, 2000, checkpoint_store=self.checkpoints))
        self.assertEqual("list_failed@2", result["stopped"])
        self.assertIn("重复", result["error"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", 103))
        self.assertEqual(2, len(scraper.http.fetched))

    def test_wrong_current_page_is_failure_even_when_threads_differ(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100"]) + _pager(1, 5, 2),
            self._url(103, 2): _plate_html(["101"]) + _pager(1, 5, 2),
        }, {"100": self._detail("100")})
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("list_failed@2", result["stopped"])
        self.assertIn("页码错位", result["error"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", 103))

    def test_actual_last_page_returned_for_out_of_range_request_is_not_processed_twice(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100"]),
            self._url(103, 2): _plate_html(["100"]) + _pager(1),
        }, {"100": self._detail("100")})
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("last_page@1", result["stopped"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(1, result["saved"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", 103))

    def test_existing_resources_and_partial_overlap_do_not_stop_pagination(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100", "101"]),
            self._url(103, 2): _plate_html(["101", "102"]),
            self._url(103, 3): _plate_html([]),
        }, {})
        scraper.data_manager.compare_existing_data = lambda *args: ([], [])
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("exhausted@3", result["stopped"])
        self.assertEqual(2, result["pages_scanned"])
        self.assertEqual(0, result["requested"])
        self.assertEqual(2, self.checkpoints.load("sehuatang", 103))

    def test_invalid_page_pauses_and_other_partition_can_continue(self):
        scraper = self._scraper({
            self._url(103, 1): '<html><div id="messagetext">没有权限</div></html>',
            self._url(104, 1): _plate_html(["100"]) + _pager(1),
        }, {"100": self._detail("100")})
        first = asyncio.run(scraper.backfill_pages(103, 1, 1000, checkpoint_store=self.checkpoints))
        second = asyncio.run(scraper.backfill_pages(104, 1, 1000, checkpoint_store=self.checkpoints))
        self.assertEqual("list_failed@1", first["stopped"])
        self.assertEqual("last_page@1", second["stopped"])
        self.assertEqual(0, self.checkpoints.load("sehuatang", 103))
        self.assertEqual(1, self.checkpoints.load("sehuatang", 104))

    def test_empty_page_before_known_last_is_not_exhaustion(self):
        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100"]) + _pager(1, 5, 2),
            self._url(103, 2): _plate_html([]),
        }, {"100": self._detail("100")})
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("list_failed@2", result["stopped"])
        self.assertIn("空列表", result["error"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", 103))

    def test_dry_run_does_not_update_checkpoint(self):
        scraper = self._scraper({self._url(103, 1): _plate_html(["100"]) + _pager(1)}, {})
        scraper.dry_run = True
        scraper.data_manager.compare_existing_data = lambda *args: ([], [])
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("last_page@1", result["stopped"])
        self.assertEqual(0, self.checkpoints.load("sehuatang", 103))

    def test_neighbour_page_links_do_not_truncate_scan(self):
        pages = {}
        for page in range(1, 4):
            pages[self._url(103, page)] = _plate_html([str(100 + page)]) + _pager(page, next_page=page + 1)
        pages[self._url(103, 4)] = _plate_html([])
        scraper = self._scraper(pages, {})
        scraper.data_manager.compare_existing_data = lambda *args: ([], [])
        result = asyncio.run(scraper.backfill_pages(103, 1, 10, checkpoint_store=self.checkpoints))
        self.assertEqual("exhausted@4", result["stopped"])
        self.assertEqual(3, result["pages_scanned"])

    def test_finished_requested_range_does_not_issue_preflight(self):
        self.checkpoints.save("sehuatang", 103, 1000)
        scraper = self._scraper({}, {})
        result = asyncio.run(scraper.backfill_pages(103, 1, 1000, resume=True, checkpoint_store=self.checkpoints))
        self.assertEqual([], scraper.http.fetched)
        self.assertEqual(0, result["failed"])

    def test_main_continues_next_partition_after_reaching_short_partition_end(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        import main

        scraper = self._scraper({
            self._url(103, 1): _plate_html(["100"]) + _pager(1),
            self._url(104, 1): _plate_html(["200"]) + _pager(1, 2, 2),
            self._url(104, 2): _plate_html(["201"]) + _pager(2),
        }, {})
        scraper.data_manager.compare_existing_data = lambda *args: ([], [])
        with patch.object(main, "WebScraper", return_value=nullcontext(scraper)), \
                patch("scrapers.page_backfill.build_checkpoint_store", return_value=self.checkpoints):
            result = asyncio.run(main._backfill_pages("sehuatang", 1, 2000, fids=[103, 104]))
        self.assertTrue(result)
        self.assertEqual([self._url(103, 1), self._url(104, 1), self._url(104, 2)], scraper.http.fetched)
        self.assertEqual(1, self.checkpoints.load("sehuatang", 103))
        self.assertEqual(2, self.checkpoints.load("sehuatang", 104))

    def test_backfills_range_without_date_filter_and_checkpoints(self):
        detail = {"tid": "100", "post_time": "2020-05-01 10:00", "magnet": "magnet:?a"}
        scraper = self._scraper(
            {
                self._url(103, 1): _plate_html(["100"]),
                self._url(103, 2): _plate_html([]),
            },
            {"100": detail},
        )

        summary = asyncio.run(
            scraper.backfill_pages(
                103, 1, 5, checkpoint_store=self.checkpoints
            )
        )

        self.assertFalse(scraper.data_processor.date_filter)
        self.assertEqual(1, summary["pages_scanned"])
        self.assertEqual(1, summary["saved"])
        self.assertEqual("exhausted@2", summary["stopped"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", "103"))
        self.assertEqual([detail], scraper._saved)

    def test_detail_failures_do_not_block_checkpoint(self):
        scraper = self._scraper(
            {
                self._url(103, 1): _plate_html(["100", "101"]),
                self._url(103, 2): _plate_html([]),
            },
            # 101 详情失败
            {"100": {"tid": "100", "post_time": "2020-05-01", "magnet": "magnet:?a"}},
        )

        summary = asyncio.run(
            scraper.backfill_pages(
                103, 1, 5, checkpoint_store=self.checkpoints
            )
        )

        self.assertEqual(1, summary["failed"])
        self.assertEqual(1, summary["saved"])
        # 详情失败由失败台账负责，页检查点照常推进
        self.assertEqual(1, self.checkpoints.load("sehuatang", "103"))

    def test_list_failure_stops_and_preserves_checkpoint(self):
        scraper = self._scraper(
            {
                self._url(103, 1): _plate_html(["100"]),
                # 第 2 页请求失败
            },
            {"100": {"tid": "100", "post_time": "2020-05-01", "magnet": "magnet:?a"}},
        )

        summary = asyncio.run(
            scraper.backfill_pages(
                103, 1, 5, checkpoint_store=self.checkpoints
            )
        )

        self.assertEqual("list_failed@2", summary["stopped"])
        self.assertEqual(1, self.checkpoints.load("sehuatang", "103"))

        # 续跑先检查第一页的分页信息，再从未完成的第 2 页继续。
        scraper2 = self._scraper(
            {
                self._url(103, 1): _plate_html(["100"]),
                self._url(103, 2): _plate_html(["102"]),
                self._url(103, 3): _plate_html([]),
            },
            {"102": {"tid": "102", "post_time": "2020-05-02", "magnet": "magnet:?b"}},
        )
        summary2 = asyncio.run(
            scraper2.backfill_pages(
                103, 1, 5, resume=True, checkpoint_store=self.checkpoints
            )
        )
        self.assertEqual(1, summary2["saved"])


class DateFilterToggleTests(unittest.TestCase):
    def test_merge_keeps_all_dates_when_filter_disabled(self):
        processor = DataProcessor(target_date="2026-09-07")
        results = [
            ({"post_time": "2019-01-01 08:00", "magnet": "magnet:?x"}, {
                "number": "ABP-1", "title": "t", "date": "2019-01-01", "tid": "1",
            }),
        ]

        self.assertEqual(0, len(processor.merge_thread_data(results, [])))

        processor.date_filter = False
        merged = processor.merge_thread_data(results, [])
        self.assertEqual(1, len(merged))
        self.assertEqual("1", merged[0]["tid"])


if __name__ == "__main__":
    unittest.main()
