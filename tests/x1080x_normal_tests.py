"""普通页抓取回归：权限、分页、旧台账和明确封锁。全部使用离线响应。"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scrapers.core.cf_challenge import FlareSolverrClient, is_cf_blocked, is_cf_challenge, is_rate_limited
from scrapers.core.config import HttpSettings, RetrySettings
from scrapers.core.contracts import CrawlContext, CrawlFailure, CrawlTarget, DetailValidationError
from scrapers.core.engine import CrawlEngine
from scrapers.core.http import CrawlerHttpClient
from scrapers.core.models import FetchResult
from scrapers.core.network import response_outcome
from scrapers.core.session import SessionHttpClient
from scrapers.infrastructure.json_failures import JsonFailureStore
from scrapers.sources.x1080x.parser import ListValidationError, X1080XParser
from scrapers.sources.x1080x.source import X1080XSource
from tests import crawler_core_tests as core_tests
from tests import page_backfill_tests as backfill_tests
from tests.x1080x_tests import DETAIL_HTML, DETAIL_HTML_NO_MAGNET, EMPTY_LIST_HTML, LIST_HTML
from util.failure_policy import describe_failure


BLOCKED = b'<title>Attention Required! | Cloudflare</title><h1>Sorry, you have been blocked</h1>'
LOCKED = DETAIL_HTML_NO_MAGNET.replace(
    '只有文字'.encode(),
    '<div class="locked"><a href="/vip/index.html">本片已过6小时免费下载时限，加入VIP查看下载链接</a></div>'.encode(),
)


def list_page(page, last, tids=(1001, 1002)):
    links = ''.join(f'<a href="forum.php?mod=viewthread&tid={tid}">title</a>' for tid in tids)
    next_link = f'<a class="nxt" href="forum.php?page={page + 1}">下一頁</a>' if page < last else ''
    return (f'<div id="threadlist">{links}</div><div class="pg"><strong>{page}</strong>'
            f'<a class="last" href="forum.php?page={last}">... {last}</a>{next_link}</div>').encode()


class NormalParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = X1080XParser()

    def test_locked_download_is_membership_failure_not_missing_magnet(self):
        with self.assertRaises(DetailValidationError) as caught:
            self.parser.parse_detail(LOCKED, 'https://example.test/1', tid=1, fid=244, strict=True)
        self.assertEqual('membership_required', caught.exception.reason)
        self.assertIsNone(self.parser.parse_detail(LOCKED, 'u', tid=1, fid=244))

    def test_ordinary_magnetless_content_is_not_membership_failure(self):
        source = X1080XSource({}, diagnostics=False)
        with self.assertRaises(DetailValidationError) as caught:
            source.parse_detail(CrawlTarget('1', 'u'), FetchResult('u', DETAIL_HTML_NO_MAGNET, 200, 1, 0))
        self.assertEqual('missing_magnet', caught.exception.reason)

    def test_free_post_and_relative_date_title_are_parsed(self):
        body = DETAIL_HTML.replace('发表于 2026/9/6'.encode(),
            '發表於 <span title="2026-10-15 15:06">5小時前</span>'.encode())
        item = self.parser.parse_detail(body, 'u', tid=1, fid=244, strict=True)
        self.assertEqual('2026-10-15', item['date'])
        self.assertTrue(item['magnet'])

    def test_only_explicit_empty_is_exhaustion(self):
        self.assertEqual([], self.parser.parse_list_page(EMPTY_LIST_HTML, 1).tids)
        for body in (b'<div id="threadlist"></div>', b'<title>Login</title>', BLOCKED,
                     b'<title>Just a moment...</title>', b'<title>Database Error</title>'):
            with self.subTest(body=body), self.assertRaises(ListValidationError):
                self.parser.parse_list_page(body, 1)

    def test_page_redirect_and_inconsistent_pagers_are_rejected(self):
        with self.assertRaises(ListValidationError):
            self.parser.parse_list_page(list_page(1, 4), 2)
        with self.assertRaises(ListValidationError):
            self.parser.parse_list_page(list_page(1, 4) + b'<div class="pg"><strong>2</strong></div>', 1)

    def test_exact_last_page_is_used_instead_of_neighbor_link(self):
        self.assertEqual(311, self.parser.parse_list_page(list_page(1, 311), 1).last_page)
        self.assertEqual(311, self.parser.parse_list_page(list_page(1, 311).replace(b'class="last"', b''), 1).last_page)
        page = b'<div id="threadlist"><a href="thread-1001-1-1.html">x</a></div>'
        page += b'<div class="pg"><strong>1</strong><a href="?page=2">2</a><a class="nxt" href="?page=2">next</a></div>'
        self.assertIsNone(self.parser.parse_list_page(page, 1).last_page)


class NormalSourceTests(unittest.TestCase):
    def test_retry_rebuilds_old_domain_and_archiver_url_without_mutating_ledger(self):
        source = X1080XSource({'base_url': 'https://current.example'}, diagnostics=False)
        old = CrawlTarget('1001', 'https://old.example/forum.php?mod=viewthread&tid=1001&archiver=1',
                          metadata={'typeid': '5479', 'retry_stage': 'fetch'})
        failures = core_tests.CrawlEngineTests.FakeFailureStore()
        failures.due_targets = lambda _: [old]
        url = source.detail_url(1001)
        http = backfill_tests.FakeHttp({url: DETAIL_HTML})
        repository = core_tests.CrawlEngineTests.FakeRepository()
        result = CrawlEngine(http, failures).run(source, repository, retry_failed=True)
        self.assertEqual(1, result.saved)
        self.assertEqual([url], http.fetched)
        self.assertIn('archiver=1', old.url)
        self.assertEqual(old.metadata, repository.saved_records[0].target.metadata)
        self.assertEqual(url, repository.saved_records[0].payload['url'])

    def test_incremental_last_page_avoids_an_extra_request(self):
        source = X1080XSource({'typeids': {'5479': '中文字幕'}, 'page_limit': 3}, diagnostics=False)
        url = source.list_url('5479', 1)
        http = backfill_tests.FakeHttp({url: list_page(1, 1)})
        result = source.discover(CrawlContext('x1080x', 'test'), http)
        self.assertEqual(2, len(result.targets))
        self.assertEqual([url], http.fetched)
        self.assertNotIn('archiver', url)
        self.assertIn('orderby=dateline', url)

    def test_repeated_incremental_page_counts_as_failure(self):
        source = X1080XSource({'typeids': {'5479': '中文字幕'}}, diagnostics=False)
        http = backfill_tests.FakeHttp({source.list_url('5479', p): LIST_HTML for p in (1, 2)})
        result = source.discover(CrawlContext('x1080x', 'test'), http)
        self.assertEqual(1, result.failed)
        self.assertEqual(2, len(result.targets))

    def test_invalid_list_retry_failure_is_not_exhaustion(self):
        source = X1080XSource({}, diagnostics=False)
        http = Mock()
        http.fetch.side_effect = [FetchResult('u', b'<title>loading</title>', 200, 1, 0),
                                 FetchResult('u', None, 500, 2, 0, error_type='http_status')]
        result, parsed = source.fetch_list_page(http, '5479', 1)
        self.assertEqual('http_status', result.error_type)
        self.assertEqual(3, result.attempts)
        self.assertIsNone(parsed)


class NormalBackfillTests(backfill_tests.X1080XBackfillTests):
    def test_membership_stops_all_partitions_and_preserves_page_checkpoint(self):
        urls = self._urls()
        scraper = self._scraper({urls['list1']: LIST_HTML,
            urls['detail1001']: DETAIL_HTML, urls['detail1002']: LOCKED})
        scraper.config['typeids']['5206'] = '亚洲有码'
        result = scraper.backfill_pages(1, 10, checkpoint_store=self.checkpoints)
        self.assertEqual('membership_required', result['stopped'])
        self.assertEqual(1, result['saved'])
        self.assertEqual(0, self.checkpoints.load('x1080x', '5479'))
        self.assertNotIn('5206', result['partitions'])

    def test_invalid_and_repeated_lists_do_not_advance_checkpoint(self):
        urls = self._urls()
        for body in (b'<html>login</html>', list_page(1, 3), LIST_HTML):
            with self.subTest(body=body):
                scraper = self._scraper({urls['list1']: LIST_HTML, urls['list2']: body,
                    urls['detail1001']: DETAIL_HTML, urls['detail1002']: DETAIL_HTML})
                with patch('scrapers.sources.x1080x.source.X1080XSource.dump_empty_list_page'):
                    result = scraper.backfill_pages(1, 10, checkpoint_store=self.checkpoints)
                self.assertEqual(1, self.checkpoints.load('x1080x', '5479'))
                self.assertEqual(1, result['failed'])
                self.assertNotIn('exhausted', result['partitions']['5479']['stopped'])

    def test_default_checkpoint_scope_is_distinct_from_archiver(self):
        scraper = self._scraper({})
        with patch('scrapers.x1080x_scraper.build_checkpoint_store', return_value=self.checkpoints) as build:
            scraper.backfill_pages(1, 1)
        self.assertEqual('fid=244;normal;orderby=dateline;v1', build.call_args.args[-1])


class BlockedResponseTests(unittest.TestCase):
    def test_block_is_not_a_challenge_even_with_http_200(self):
        self.assertTrue(is_cf_blocked(BLOCKED))
        self.assertFalse(is_cf_challenge(BLOCKED, 403))
        self.assertEqual('restricted', response_outcome(200, BLOCKED))
        self.assertFalse(is_rate_limited(BLOCKED + '访问受限'.encode()))

    def test_block_never_retries_or_calls_solver(self):
        for status in (200, 403, 429, 503):
            with self.subTest(status=status):
                request = Mock(return_value=SimpleNamespace(status_code=status, content=BLOCKED))
                transport = CrawlerHttpClient('x1080x', HttpSettings(
                    retry=RetrySettings(attempts=3)), request_func=request)
                client = SessionHttpClient(HttpSettings(), transport=transport)
                client._flaresolverr = Mock()
                result = client.fetch('https://example.test/1')
                self.assertEqual('cf_blocked', result.error_type)
                request.assert_called_once()
                client._flaresolverr.solve.assert_not_called()

    def test_solver_block_does_not_clear_cookies_and_retry(self):
        transport = Mock()
        transport.fetch.return_value = FetchResult('u', b'<title>Just a moment...</title>', 403, 1, 0)
        client = SessionHttpClient(HttpSettings(), transport=transport)
        client._cookie['existing'] = 'keep'
        client._flaresolverr = FlareSolverrClient('https://solver.example/v1')
        response = SimpleNamespace(json=lambda: {'solution': {'status': 403, 'response': BLOCKED.decode()}})
        with patch('scrapers.core.cf_challenge.requests.post', return_value=response) as post:
            result = client.fetch('u')
            self.assertEqual('cf_blocked', result.error_type)
            self.assertEqual(2, result.attempts)
        post.assert_called_once()
        self.assertEqual('keep', client._cookie['existing'])


class NonRetryableLedgerTests(unittest.TestCase):
    def test_json_preserves_reason_and_allows_explicit_requeue(self):
        with tempfile.TemporaryDirectory() as root:
            store = JsonFailureStore(Path(root) / 'failures.json')
            for reason in ('membership_required', 'cf_blocked'):
                failure = CrawlFailure('x1080x', reason, 'u', 'parse', 1, reason)
                store.record([failure])
                row = next(row for row in store.snapshot()['failures'] if row['source_key'] == reason)
                self.assertEqual(('exhausted', 1, None), (row['state'], row['failure_count'], row['next_retry_at']))
                self.assertEqual([], store.due_targets('x1080x'))
                self.assertTrue(store.requeue('x1080x', reason, 'parse'))
                self.assertEqual(reason, store.due_targets('x1080x')[0].key)
                store.record([failure])
                self.assertEqual([], store.due_targets('x1080x'))
            self.assertEqual(2, store.requeue_exhausted('x1080x'))
            self.assertEqual(0, store.requeue_exhausted('x1080x'))

    def test_manually_requeued_permission_failure_is_due(self):
        now = datetime.now(timezone.utc)
        self.assertEqual('due', describe_failure({'error_type': 'membership_required',
            'failure_count': 1, 'next_retry_at': now}, 5, now)['state'])
