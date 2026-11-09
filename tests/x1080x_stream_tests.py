"""逐页保存、账号校验诊断和持久浏览器的离线回归。"""
import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scrapers.core.config import HttpSettings
from scrapers.core.contracts import DetailValidationError, SourcePaused, NullFailureStore
from scrapers.core.models import FetchResult
from scrapers.sources.x1080x.auth import Authenticator, AuthSettings, browser_error_hint
from scrapers.sources.x1080x.http_client import X1080XHttpClient, SessionResult
from scrapers.sources.x1080x.list_browser import ListBrowser
from scrapers.sources.x1080x.source import X1080XSource
from scrapers.x1080x_scraper import X1080XScraper
from tests.x1080x_v2_tests import CAT, RID, OTHER, URL, detail, listing


class AuthStateTests(unittest.TestCase):
    def client(self, status, body):
        client = X1080XHttpClient(HttpSettings(), config={"cookie": "__Host-x1080x_session=private"})
        self.addCleanup(client.close)
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        client._api_fetch = Mock(return_value=FetchResult(URL, raw, status, 1, 0))
        return client

    def test_only_explicit_unauthenticated_responses_are_cookie_failures(self):
        for status, body in [(200, {"authenticated": False}), (401, b""),
                             (403, {"error": "session_expired"})]:
            with self.subTest(status=status, body=body):
                client = self.client(status, body)
                with self.assertRaises(SourcePaused) as caught:
                    Authenticator(AuthSettings()).ensure(client, RID)
                self.assertEqual("cookie_expired", caught.exception.reason)
                self.assertIn(f"http={status}", str(caught.exception))

    def test_unknown_403_and_invalid_json_are_not_reported_as_expired_cookies(self):
        for status, body, reason in [(403, {"message": "private value"}, "session_forbidden"),
                                     (200, b"<html>not json</html>", "session_unavailable"),
                                     (403, {"error": "browser_pass_required"}, "browser_pass_required"),
                                     (403, b"<title>Just a moment</title>", "cf_challenge")]:
            with self.subTest(reason=reason):
                client = self.client(status, body)
                with self.assertRaises(SourcePaused) as caught:
                    client.is_authenticated(RID)
                self.assertEqual(reason, caught.exception.reason)
                self.assertNotIn("private value", str(caught.exception))

    def test_authenticated_true_requires_a_successful_api_response(self):
        self.assertTrue(self.client(200, {"authenticated": True}).is_authenticated(RID))
        with self.assertRaises(SourcePaused):
            self.client(403, {"authenticated": True}).is_authenticated(RID)

    def test_auto_mode_does_not_submit_credentials_after_unknown_403(self):
        client = self.client(403, {})
        browser = Mock()
        with self.assertRaises(SourcePaused) as caught:
            Authenticator(AuthSettings("name", "secret", "auto"), browser).ensure(client, RID)
        self.assertEqual("session_forbidden", caught.exception.reason)
        browser.assert_not_called()

    def test_unconfirmed_account_cookies_stay_protected_during_cf_refresh(self):
        client = self.client(200, {"authenticated": False})
        client._prepare_cookies(URL)
        client.authenticated = False
        client._refresh_cf = Mock()
        client._transport = Mock()
        client._transport.fetch.return_value = FetchResult(URL, detail(), 200, 1, 0)
        client._bypass = Mock()
        client._resolve_challenge(URL, 0)
        client._refresh_cf.assert_called_once_with(URL)
        client._bypass.assert_not_called()
        self.assertEqual("private", client._cookie_copy()["__Host-x1080x_session"])
        self.assertIsNone(client.render_public(URL))

    def test_browser_error_diagnostic_never_echoes_raw_exception(self):
        hint = browser_error_hint(RuntimeError("proxy password=private https://site/?token=secret"))
        self.assertIn("proxy_error", hint)
        self.assertNotIn("private", hint)
        self.assertNotIn("secret", hint)


class StreamCrawlTests(unittest.TestCase):
    def scraper(self, events, pages, *, existing=(), restricted=()):
        scraper = X1080XScraper.__new__(X1080XScraper)
        scraper.config = {"categories": {CAT: "中文字幕"}, "page_limit": 2, "notify_telegram": False}
        scraper.failure_store = NullFailureStore()

        def fetch(url, stage="detail"):
            events.append(("fetch", url))
            body = pages[url]
            if isinstance(body, Exception):
                raise body
            return FetchResult(url, body, 200, 1, 0)

        scraper.http = SimpleNamespace(fetch=fetch, fetch_many=lambda urls, stage="detail": [fetch(url, stage) for url in urls])
        def save(items):
            events.append(("save", [x["source_key"] for x in items]))
            return {"upserted": len(items)}

        for target, value in [("find_existing_x1080x_keys", lambda keys: set(keys) & set(existing)),
                              ("save_x1080x_items", save)]:
            patcher = patch("scrapers.x1080x_scraper." + target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        scraper.failure_store.non_retryable_keys = lambda source, keys: set(keys) & set(restricted)
        return scraper

    def test_page_is_saved_before_next_list_and_later_pause_preserves_counts(self):
        source = X1080XSource({"categories": {CAT: "中文字幕"}})
        page1, page2 = source.list_url(CAT, 1), source.list_url(CAT, 2)
        events = []
        scraper = self.scraper(events, {page1: listing(), source.detail_url(RID): detail(),
                                        page2: SourcePaused("cf_challenge", "later page failed")})
        result = scraper.crawl()
        self.assertEqual(1, result["saved"])
        self.assertEqual(1, result["discovered"])
        self.assertEqual("cf_challenge", result["source_paused"])
        self.assertEqual("partial_success", result["status"])
        self.assertLess(events.index(("save", [RID])), events.index(("fetch", page2)))

    def test_auth_failure_on_first_page_prevents_remaining_list_scan(self):
        source = X1080XSource({"categories": {CAT: "中文字幕"}})
        events = []
        scraper = self.scraper(events, {source.list_url(CAT, 1): listing(), source.detail_url(RID): detail()})
        with patch.object(X1080XSource, "collect_detail", side_effect=SourcePaused("cookie_expired", "unconfirmed")):
            result = scraper.crawl()
        self.assertEqual("cookie_expired", result["source_paused"])
        self.assertEqual(1, result["pages"])
        self.assertEqual(0, result["saved"])
        self.assertNotIn(("fetch", source.list_url(CAT, 2)), events)

    def test_later_list_pause_adds_a_failure_after_an_earlier_detail_failure(self):
        source = X1080XSource({"categories": {CAT: "中文字幕"}})
        body = listing().replace(b'</div>', (f'</div><article><a class="resource-title" '
                                            f'href="/resources/{OTHER}">other</a></article>').encode(), 1)
        scraper = self.scraper([], {source.list_url(CAT, 1): body, source.detail_url(RID): detail(),
                                    source.detail_url(OTHER): detail(),
                                    source.list_url(CAT, 2): SourcePaused("cf_challenge", "later failure")})
        collect = X1080XSource.collect_detail
        def parse(adapter, target, *args):
            if target.key == OTHER:
                raise DetailValidationError("membership_required")
            return collect(adapter, target, *args)
        with patch.object(X1080XSource, "collect_detail", new=parse):
            result = scraper.crawl()
        self.assertEqual((1, 2), (result["saved"], result["failed"]))

    def test_existing_restricted_and_discovery_counts_accumulate_across_pages(self):
        source = X1080XSource({"categories": {CAT: "中文字幕"}})
        second = listing(2, 2).replace(RID.encode(), OTHER.encode())
        events = []
        scraper = self.scraper(events, {source.list_url(CAT, 1): listing(), source.list_url(CAT, 2): second},
                               existing=[RID], restricted=[OTHER])
        result = scraper.crawl()
        self.assertEqual((2, 2, 1, 1, 0), (result["pages"], result["discovered"], result["existing"],
                                          result["skipped_restricted"], result["requested"]))

    def test_free_boards_are_first_and_dry_run_does_not_persist(self):
        board = "3zfPdYrZvRsB5MYMHwE70K"
        source = X1080XSource({"categories": {CAT: "中文字幕"}, "boards": {board: "免费"}})
        self.assertEqual([board, CAT], list(source.type_map))
        events = []
        scraper = self.scraper(events, {source.list_url(CAT, 1): listing(1, 1), source.detail_url(RID): detail()})
        result = scraper.crawl(dry_run=True)
        self.assertEqual(0, result["saved"])
        self.assertFalse(any(kind == "save" for kind, _ in events))


class PersistentListBrowserTests(unittest.TestCase):
    def test_established_context_handles_next_lists_without_an_http_cf_roundtrip(self):
        client = X1080XHttpClient(HttpSettings())
        client._list_browser = Mock()
        client._list_browser.render.return_value = listing()
        client._transport = Mock()
        client._transport.fetch.return_value = FetchResult(URL, detail(), 200, 1, 0)
        try:
            result = client.fetch(f"https://1080xin.com/boards/{CAT}?page=2", stage="list")
            self.assertEqual(listing(), result.body)
            self.assertIs(client, result.client)
            client._transport.fetch.assert_not_called()
            self.assertEqual(detail(), client.fetch(URL).body)
            client._transport.fetch.assert_called_once()
        finally:
            client.close()

    def test_initial_cf_429_is_not_reused_after_challenge_navigation(self):
        renderer = ListBrowser(HttpSettings(), "https://example.test")
        renderer._page = SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=429)), url=URL,
            content=AsyncMock(side_effect=["<title>Just a moment</title>", listing().decode()]),
            locator=Mock(return_value=SimpleNamespace(count=AsyncMock(side_effect=[0, 1]))))
        with patch("scrapers.sources.x1080x.list_browser._click_verification", new_callable=AsyncMock), \
                patch("scrapers.sources.x1080x.list_browser.asyncio.sleep", new_callable=AsyncMock):
            try:
                self.assertEqual(listing(), renderer.render(f"https://example.test/boards/{CAT}"))
            finally:
                renderer.close()

    def test_timeout_reports_verification_failure_and_resets_the_context(self):
        renderer = ListBrowser(HttpSettings(), "https://example.test", timeout=0.01)
        async def render(url):
            renderer._stage = "verification"
            await asyncio.sleep(10)
        renderer._render = render
        renderer._reset = AsyncMock()
        try:
            with self.assertRaises(SourcePaused) as caught:
                renderer.render(f"https://example.test/boards/{CAT}")
            self.assertEqual("browser_pass_required", caught.exception.reason)
            self.assertIn("hint=timeout", str(caught.exception))
            renderer._reset.assert_awaited_once()
        finally:
            renderer.close()

    def test_context_and_event_loop_are_reused_and_closed_on_the_owner_thread(self):
        renderer = ListBrowser(HttpSettings(), "https://example.test")
        threads, loops = [], []
        async def render(url):
            threads.append(threading.get_ident())
            loops.append(asyncio.get_running_loop())
            return url.encode()
        renderer._render = render
        reset = AsyncMock()
        renderer._reset = reset
        try:
            for page in [1, 2]:
                url = f"https://example.test/boards/{CAT}?page={page}"
                self.assertEqual(url.encode(), renderer.render(url))
            self.assertEqual(threads[0], threads[1])
            self.assertIs(loops[0], loops[1])
            self.assertNotEqual(threads[0], threading.get_ident())
        finally:
            renderer.close()
        reset.assert_awaited_once()
        self.assertTrue(loops[0].is_closed())

    def test_runtime_context_is_created_once_without_account_cookies(self):
        renderer = ListBrowser(HttpSettings(), "https://example.test")
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                               content=AsyncMock(return_value=listing().decode()), url=URL,
                               locator=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=1))))
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(), add_cookies=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
        # 特殊方法由类型解析，使用 AsyncMock 的异步 context manager 协议。
        manager = AsyncMock()
        manager.__aenter__.return_value = browser
        module = SimpleNamespace(InvisiblePlaywright=Mock(return_value=manager))
        with patch.dict("sys.modules", {"invisible_playwright.async_api": module}):
            try:
                for number in (1, 2):
                    renderer.render(f"https://example.test/boards/{CAT}?page={number}")
            finally:
                renderer.close()
        browser.new_context.assert_awaited_once()
        context.add_cookies.assert_not_awaited()
        context.close.assert_awaited_once()
        self.assertEqual(2, page.goto.await_count)

    def test_client_reuses_renderer_and_keeps_account_session_unchanged(self):
        with patch("scrapers.sources.x1080x.http_client.ListBrowser") as factory:
            factory.available.return_value = True
            factory.return_value.render.return_value = listing()
            client = X1080XHttpClient(HttpSettings(), config={"cookie": "__Host-x1080x_session=private"})
            client._prepare_cookies(URL)
            before = client.browser_cookies()
            try:
                client.render_list(URL)
                client.render_list(URL)
                factory.assert_called_once()
                self.assertEqual(2, factory.return_value.render.call_count)
                self.assertEqual(before, client.browser_cookies())
            finally:
                client.close()
            factory.return_value.close.assert_called_once()

    def test_only_missing_runtime_falls_back_to_byparr(self):
        for reason in ("browser_unavailable", "rate_limited", "browser_pass_required"):
            with self.subTest(reason=reason), patch("scrapers.sources.x1080x.http_client.ListBrowser") as factory:
                factory.available.return_value = True
                factory.return_value.render.side_effect = SourcePaused(reason, "browser failed")
                client = X1080XHttpClient(HttpSettings())
                client._flaresolverr = Mock()
                client._flaresolverr.solve.return_value = (listing(), [], "Guest UA")
                try:
                    if reason == "browser_unavailable":
                        self.assertEqual(listing(), client.render_list(URL))
                        client._flaresolverr.solve.assert_called_once_with(URL)
                    else:
                        with self.assertRaises(SourcePaused) as caught:
                            client.render_list(URL)
                        self.assertEqual(reason, caught.exception.reason)
                        client._flaresolverr.solve.assert_not_called()
                finally:
                    client.close()


class RestartCleanupTests(unittest.TestCase):
    def test_admin_restart_closes_persistent_clients_before_replacing_process(self):
        from web.app import _restart_process
        events = []
        with patch("scrapers.core.pool.stop_shared_clients", side_effect=lambda: events.append("clients_closed")), \
                patch("scrapers.core.storage.drain_writers", side_effect=lambda: events.append("writes_drained")), \
                patch("notifications.memory_queue.shutdown_notifications", side_effect=lambda: events.append("notifications_closed")), \
                patch("web.app.os.execv", side_effect=lambda *args: events.append("exec")):
            _restart_process()
        self.assertEqual(["clients_closed", "writes_drained", "notifications_closed", "exec"], events)
