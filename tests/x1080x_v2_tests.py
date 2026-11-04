"""新版页面、认证降级、零价格领取及共享会话的离线回归。"""
import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from curl_cffi import requests
from scrapers.core.config import HttpSettings, ProxySettings, RetrySettings
from scrapers.core.contracts import CrawlContext, CrawlTarget, DetailValidationError, SourcePaused
from scrapers.core.engine import CrawlEngine
from scrapers.core.models import FetchResult
from scrapers.sources.x1080x.auth import Authenticator, AuthSettings
from scrapers.sources.x1080x.attachments import collect_free_torrents, free_preview, torrent_magnet
from scrapers.sources.x1080x.http_client import SessionResult, X1080XHttpClient
from scrapers.sources.x1080x.parser import ListValidationError, SiteEntryPage, X1080XParser
from scrapers.sources.x1080x.source import DEFAULT_BOARD_ID, X1080XSource
from tests.crawler_core_tests import CrawlEngineTests

RID = "3VDY80SBcybkGiHoNji7Qd"
OTHER = "4W0AwdtH70hoJa7qQUDTtg"
CAT = "6gGAPsMAGtQzqxIYyAQBbS"
SLOT = "5yKpArJbxErf8dniOjBgv9"
URL = "https://example.test/resources/" + RID
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=ABC-123"


def detail(content=MAGNET):
    return (f'<header class="thread-header"><h1><span class="topic-tags">'
            f'<a class="topic-category" href="/boards/{DEFAULT_BOARD_ID}?category_id={CAT}">中文字幕</a>'
            '</span>ABC-123 示例</h1></header><article id="first-post">'
            '<time datetime="2026-10-30T17:11:28.535Z">2026-10-31</time>'
            f'<div class="post-text"><div class="discuz-body">{content}</div></div></article>'
            '<article><div class="post-text">magnet:?xt=urn:btih:reply</div></article>').encode()


def listing(page=1, maximum=3):
    return (f'<div class="thread-list"><div class="pg"><strong>{page}</strong>'
            f'<input type="number" max="{maximum}"></div><article class="thread-row is-sticky">'
            f'<a class="resource-title" href="/resources/{OTHER}">公告</a></article>'
            f'<article class="topic-card"><a class="resource-title" href="/resources/{RID}?return=abc">主题</a>'
            f'<a class="resource-title" href="/resources/{RID}">重复</a></article></div>').encode()


def preview(**kwargs):
    return {"viewer_id": "viewer", "version_id": "version", "slot_id": SLOT,
            "status": "claimable", "file_ready": True, "price_points": "0.00",
            "payable_points": "0.00", "quota_units": 0, **kwargs}


# info 字典故意使用非排序键，确保哈希取原始字节，而不是重新编码后的字节。
INFO = b'd4:name7:ABC-1236:lengthi100e12:piece lengthi16384e6:pieces20:' + b'a' * 20 + b'e'
TORRENT = b'd4:info' + INFO + b'e'


class ParserTests(unittest.TestCase):
    def test_legacy_category_is_an_input_alias_for_the_new_string_id(self):
        source = X1080XSource({"typeids": {"5479": "中文字幕"}})
        self.assertEqual(f"https://1080xin.com/boards/{DEFAULT_BOARD_ID}?page=1&category_id={CAT}", source.list_url("5479", 1))
        self.assertEqual(f"board_id={DEFAULT_BOARD_ID} category_id={CAT}", source.partition_label("5479"))
        target = source.make_target(RID, "5479", "中文字幕")
        self.assertEqual(CAT, target.metadata["category_id"])
        self.assertEqual(RID, target.key)

    def test_welcome_page_is_not_a_list_or_a_resource(self):
        body = '<title>歡迎訪問 · x1080x</title><a href="01.html">進入</a>'.encode()
        parser = X1080XParser()
        with self.assertRaises(SiteEntryPage):
            parser.parse_list_page(body, 1)
        with self.assertRaises(DetailValidationError) as caught:
            parser.parse_resource(body, URL, resource_id=RID)
        self.assertEqual("site_entry_required", caught.exception.reason)
        self.assertEqual([RID], parser.parse_list_page(b'<title>Welcome</title>' + listing(), 1).tids)

    def test_first_post_title_date_category_and_images(self):
        parser = X1080XParser({CAT: "中文字幕"})
        body = detail(MAGNET.replace("&", "&amp;") + '<img src="/cover.jpg"><img src="/a.gif">')
        item = parser.parse_resource(body, URL, resource_id=RID)
        self.assertEqual(RID, item["source_key"])
        self.assertEqual("ABC-123 示例", item["title"])
        self.assertEqual("2026-10-31", item["date"])
        self.assertEqual("2026-10-31 01:11:28", item["post_time"])
        self.assertEqual(CAT, item["category_id"])
        self.assertEqual([MAGNET], item["magnets"])
        self.assertEqual(["https://example.test/cover.jpg"], item["img"])
        self.assertNotIn("tid", item)

    def test_list_excludes_sticky_and_checks_page(self):
        parser = X1080XParser()
        self.assertEqual([RID], parser.parse_list_page(listing(), 1).tids)
        self.assertEqual(3, parser.parse_list_page(listing(), 1).last_page)
        with self.assertRaises(ListValidationError):
            parser.parse_list_page(listing(), 2)
        with self.assertRaises(ListValidationError):
            parser.parse_list_page(listing().replace(b'<article class="topic-card">', b'<article>')
                                   .replace(b'class="resource-title"', b'class="other"'), 1)

    def test_attachment_and_locked_states(self):
        parser = X1080XParser()
        body = detail(f'<article class="attachment" data-slot-id="{SLOT}"><span class="file-name">a.torrent</span></article>')
        item = parser.parse_resource(body, URL, resource_id=RID)
        self.assertEqual([SLOT], item["attachment_slots"])
        with self.assertRaises(DetailValidationError) as caught:
            parser.parse_resource(detail('<div class="body-locked">6小時結束，加入VIP</div>'), URL, resource_id=RID)
        self.assertEqual("membership_required", caught.exception.reason)
        with self.assertRaises(DetailValidationError) as caught:
            parser.parse_resource(detail('<div class="body-locked">請登入會員後查看</div>'), URL, resource_id=RID)
        self.assertEqual("login_required", caught.exception.reason)

    def test_new_target_retry_uses_current_domain_without_changing_key_case(self):
        source = X1080XSource({"base_url": "https://new.example"})
        target = source.normalize_target(CrawlTarget(RID, "https://old.example/x"))
        self.assertEqual(f"https://new.example/resources/{RID}?page=1&order=asc", target.url)
        self.assertIsNone(source.normalize_target(CrawlTarget("12345", "https://old.example/x")))


class TorrentTests(unittest.TestCase):
    def test_hashes_exact_original_info_bytes(self):
        magnet = torrent_magnet(TORRENT)
        self.assertEqual("magnet:?xt=urn:btih:" + hashlib.sha1(INFO).hexdigest() + "&dn=ABC-123", magnet)
        for body in (b'<html>login</html>', TORRENT + b'junk', b'd4:infodee', b'd4:infoi1ee'):
            with self.subTest(body=body), self.assertRaises(DetailValidationError):
                torrent_magnet(body)

    def test_prices_must_be_explicit_finite_zero_without_consuming_quota(self):
        self.assertIsNotNone(free_preview(preview()))
        for fields in ({"price_points": "1"}, {"payable_points": "0.01"}, {"quota_units": 1},
                       {"price_points": "NaN"}, {"payable_points": None}, {"viewer_id": None}):
            with self.subTest(fields=fields), self.assertRaises(DetailValidationError):
                free_preview(preview(**fields))

    def client(self):
        client = Mock()
        client.api_json.side_effect = [preview(), {"receipt_id": "ticket"}]
        client.fetch_torrent.return_value = TORRENT
        return client

    def test_claim_carries_quote_identity_and_idempotency(self):
        client = self.client()
        magnets = collect_free_torrents(client, RID, [SLOT])
        self.assertEqual(1, len(magnets))
        client.ensure_authenticated.assert_called_once_with(RID)
        body = client.api_json.call_args_list[1].kwargs["data"]
        self.assertEqual(("viewer", "version", "0.00", "0.00"),
                         tuple(body[k] for k in ("expected_user_id", "expected_version", "expected_price", "expected_payable")))
        self.assertTrue(body["idempotency_key"])
        client.fetch_torrent.assert_called_once_with("/api/downloads/file/ticket")

    def test_stale_claim_and_mismatched_slot_never_download(self):
        for claim in ({"requires_new_claim": True, "receipt_id": "expired"}, {}, {"receipt_id": None}):
            client = self.client()
            client.api_json.side_effect = [preview(), claim]
            with self.assertRaises(DetailValidationError):
                collect_free_torrents(client, RID, [SLOT])
            client.fetch_torrent.assert_not_called()
            self.assertEqual(2, client.api_json.call_count)
        client = self.client()
        client.api_json.side_effect = [preview(slot_id="other")]
        with self.assertRaises(DetailValidationError):
            collect_free_torrents(client, RID, [SLOT])
        self.assertEqual(1, client.api_json.call_count)

    def test_dry_run_and_paid_resources_never_claim_or_download(self):
        client = self.client()
        self.assertEqual([], collect_free_torrents(client, RID, [SLOT], dry_run=True))
        self.assertEqual(1, client.api_json.call_count)
        client.fetch_torrent.assert_not_called()
        client = Mock()
        client.api_json.return_value = preview(price_points="1.00")
        with self.assertRaises(DetailValidationError) as caught:
            collect_free_torrents(client, RID, [SLOT])
        self.assertEqual("paid_resource", caught.exception.reason)
        self.assertEqual(1, client.api_json.call_count)
        client.fetch_torrent.assert_not_called()


class CollectionTests(unittest.TestCase):
    def collect(self, content, client, dry_run=False):
        source = X1080XSource({"categories": {CAT: "中文字幕"}}, diagnostics=False)
        target = source.make_target(RID, CAT, "中文字幕")
        result = SessionResult(target.url, detail(content), 200, 1, 0, client=client)
        return source.collect_detail(target, result, Mock(), CrawlContext("x1080x", "offline", dry_run))

    def test_body_magnet_avoids_claim_even_when_attachment_is_present(self):
        client = Mock()
        content = MAGNET + f'<div class="attachment" data-slot-id="{SLOT}"><b class="file-name">a.torrent</b></div>'
        record = self.collect(content, client)
        self.assertEqual(MAGNET, record.payload["magnet"])
        client.ensure_authenticated.assert_not_called()
        client.api_json.assert_not_called()

    def test_attachment_dry_run_only_previews_and_marks_pending(self):
        client = Mock()
        client.api_json.return_value = preview()
        content = f'<div class="attachment" data-slot-id="{SLOT}"><b class="file-name">a.torrent</b></div>'
        record = self.collect(content, client, dry_run=True)
        self.assertTrue(record.payload["download_pending"])
        self.assertFalse(record.payload["magnet"])
        client.api_json.assert_called_once_with("GET", f"/api/slots/{SLOT}/preview")
        client.fetch_torrent.assert_not_called()


class AuthenticationTests(unittest.TestCase):
    def test_welcome_list_stops_immediately_without_retrying_other_categories(self):
        source = X1080XSource({"categories": {CAT: "中文字幕", "another": "其他"}}, diagnostics=False)
        http = Mock()
        body = '<title>歡迎訪問 · x1080x</title>'.encode()
        http.fetch.return_value = FetchResult(URL, body, 200, 1, 0)
        failures = CrawlEngineTests.FakeFailureStore()
        summary = CrawlEngine(http, failures).run(source, CrawlEngineTests.FakeRepository())
        self.assertEqual("site_entry_required", summary.details["source_paused"])
        self.assertEqual(1, http.fetch.call_count)
        self.assertEqual([], failures.failures)

    def test_welcome_detail_pauses_without_attempting_login(self):
        source = X1080XSource({}, diagnostics=False)
        client = Mock()
        target = source.make_target(RID, CAT, "中文字幕")
        body = '<title>欢迎访问 · x1080x</title>'.encode()
        result = SessionResult(target.url, body, 200, 1, 0, client=client)
        with self.assertRaises(SourcePaused) as caught:
            source.collect_detail(target, result, Mock(), CrawlContext("x1080x", "offline"))
        self.assertEqual("site_entry_required", caught.exception.reason)
        client.ensure_authenticated.assert_not_called()
        client.render_public.assert_not_called()

    def client(self, enabled=True):
        client = Mock()
        client.is_authenticated.side_effect = [False, True]
        client.api_json.side_effect = [{"enabled": enabled}, {"csrf_token": "csrf"}, {"ok": True}]
        return client

    def test_http_without_turnstile_avoids_browser(self):
        client = self.client(False)
        browser = Mock()
        Authenticator(AuthSettings("name", "secret", mode="auto"), browser).ensure(client, RID)
        browser.assert_not_called()
        self.assertTrue(client.authenticated)
        self.assertEqual("/api/auth/login", client.api_json.call_args.args[1])

    def test_turnstile_uses_independent_browser_and_validates_imported_session(self):
        client = self.client()
        browser = Mock(return_value=([{"name": "session", "value": "secret"}], "Firefox"))
        auth = Authenticator(AuthSettings("name", "secret", mode="auto"), browser)
        auth.ensure(client, RID)
        self.assertEqual(1, client.api_json.call_count)
        browser.assert_called_once()
        client.install_browser_session.assert_called_once()
        self.assertTrue(client.authenticated)

    def test_missing_credentials_pauses_without_browser_or_login_post(self):
        client = Mock()
        client.is_authenticated.return_value = False
        browser = Mock()
        with self.assertRaises(SourcePaused):
            Authenticator(AuthSettings(), browser).ensure(client, RID)
        client.api_json.assert_not_called()
        browser.assert_not_called()

    def test_browser_session_rejected_by_http_pauses_instead_of_reporting_success(self):
        client = self.client()
        client.is_authenticated.side_effect = [False, False]
        browser = Mock(return_value=([{"name": "session", "value": "secret"}], "Firefox"))
        with self.assertRaises(SourcePaused) as caught:
            Authenticator(AuthSettings("name", "secret", mode="auto"), browser).ensure(client, RID)
        self.assertEqual("authentication_failed", caught.exception.reason)
        self.assertFalse(client.authenticated)

    def test_cookie_mode_never_logs_in_even_when_credentials_are_present(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                client = Mock()
                client.is_authenticated.return_value = valid
                browser = Mock()
                auth = Authenticator(AuthSettings("name", "secret", mode="cookie"), browser)
                if valid:
                    auth.ensure(client, RID)
                    self.assertTrue(client.authenticated)
                else:
                    with self.assertRaises(SourcePaused) as caught:
                        auth.ensure(client, RID)
                    self.assertEqual("cookie_expired", caught.exception.reason)
                browser.assert_not_called()
                client.api_json.assert_not_called()
                client.install_browser_session.assert_not_called()
        client = Mock()
        client.is_authenticated.side_effect = SourcePaused("session_unavailable", "network unavailable")
        browser = Mock()
        with self.assertRaises(SourcePaused) as caught:
            Authenticator(AuthSettings(mode="cookie"), browser).ensure(client, RID)
        self.assertEqual("session_unavailable", caught.exception.reason)
        browser.assert_not_called()
        client.api_json.assert_not_called()

    def test_missing_mode_defaults_to_cookie_without_using_environment_credentials(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"CRAWLER_X1080X_USERNAME": "name", "CRAWLER_X1080X_PASSWORD": "secret"}):
            settings = AuthSettings.from_config({})
        self.assertEqual("cookie", settings.mode)

    def test_login_failure_does_not_create_resource_failure_ledger(self):
        source = X1080XSource({"categories": {CAT: "中文字幕"}}, diagnostics=False)
        source.discover = Mock(return_value=SimpleNamespace(targets=[CrawlTarget(RID, URL)], failed=0, details={}))
        source.collect_detail = Mock(side_effect=SourcePaused("authentication_failed", "login failed"))
        http = Mock()
        http.fetch_many.return_value = [FetchResult(URL, detail(), 200, 1, 0)]
        failures = CrawlEngineTests.FakeFailureStore()
        summary = CrawlEngine(http, failures).run(source, CrawlEngineTests.FakeRepository())
        self.assertEqual("authentication_failed", summary.details["source_paused"])
        self.assertEqual([], failures.failures)

    def test_list_session_pause_stops_before_other_categories(self):
        source = X1080XSource({"categories": {CAT: "中文字幕", "another": "其他"}}, diagnostics=False)
        http = Mock()
        http.fetch.return_value = FetchResult(URL, None, 403, 1, 0, "session_paused", "cf_challenge")
        failures = CrawlEngineTests.FakeFailureStore()
        summary = CrawlEngine(http, failures).run(source, CrawlEngineTests.FakeRepository())
        self.assertEqual("cf_challenge", summary.details["source_paused"])
        self.assertEqual(1, http.fetch.call_count)
        self.assertEqual([], failures.failures)


class SharedSessionTests(unittest.TestCase):
    def test_cookie_pool_pins_first_lane_but_guest_pool_keeps_all_lanes(self):
        from scrapers.sources.x1080x.http_client import shared_http_client
        from scrapers.core.pool import stop_shared_clients
        self.addCleanup(stop_shared_clients)
        settings = HttpSettings(proxy=ProxySettings(True, urls=("http://proxy.test:17891", "http://proxy.test:17892")))
        account = shared_http_client(settings, "", "https://example.test", config={
            "auth": {"mode": "cookie"}, "cookie": "session=private"})
        self.assertEqual(1, len(account.lanes))
        self.assertEqual(("http://proxy.test:17891",), account.settings.proxy.addresses)
        self.assertEqual(2, len(settings.proxy.addresses))
        guest = shared_http_client(settings, "", "https://example.test", config={"auth": {"mode": "cookie"}})
        self.assertEqual(2, len(guest.lanes))

    def test_browser_host_cookies_are_accepted_by_real_libcurl(self):
        from curl_cffi import Curl, CurlOpt, CurlInfo
        from curl_cffi.requests.cookies import CurlMorsel
        client = X1080XHttpClient(HttpSettings(), base_url="https://example.test")
        client.install_browser_session([
            {"name": "__Host-x1080x_session_browser", "value": "binding", "domain": "example.test",
             "path": "/", "secure": True, "httpOnly": True},
            {"name": "cf_clearance", "value": "cf", "domain": ".example.test", "path": "/", "secure": True},
        ], "Firefox")
        curl = Curl()
        try:
            curl.setopt(CurlOpt.COOKIEFILE, b"")
            for cookie in client._jar.jar:
                curl.setopt(CurlOpt.COOKIELIST, CurlMorsel.from_cookiejar_cookie(cookie).to_curl_format().encode())
            accepted = curl.getinfo(CurlInfo.COOKIELIST)
            self.assertEqual(2, len(accepted))
            self.assertTrue(any(b"__Host-x1080x_session_browser" in row for row in accepted))
        finally:
            curl.close()

    def test_api_post_retries_reuse_cookies_csrf_and_claim_body(self):
        settings = HttpSettings(retry=RetrySettings(attempts=2, base_delay=0, max_delay=0))
        client = X1080XHttpClient(settings, base_url="https://example.test", config={
            "cookie": "__Host-x1080x_session=account; __Host-x1080x_csrf=csrf"})
        seen = []
        def post(url, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(status_code=500 if len(seen) == 1 else 200,
                                   content=b'{"download_url":"/api/downloads/file/ticket"}',
                                   headers={}, cookies=requests.Cookies())
        client._local.session = SimpleNamespace(cookies=requests.Cookies(), post=post)
        body = {"idempotency_key": "fixed"}
        result = client.api_json("POST", "/api/downloads/claim", data=body)
        self.assertTrue(result["download_url"])
        self.assertEqual(2, len(seen))
        for item in seen:
            self.assertEqual("account", item["cookies"].get("__Host-x1080x_session"))
            self.assertEqual("csrf", item["headers"]["X-CSRFToken"])
            self.assertIs(body, item["json"])

    def test_cf_guest_cookies_do_not_replace_account_or_csrf(self):
        client = X1080XHttpClient(HttpSettings(), base_url="https://example.test", config={
            "cookie": "__Host-x1080x_session=account; __Host-x1080x_csrf=csrf"})
        client._prepare_cookies(URL)
        client._flaresolverr = Mock()
        client._flaresolverr.solve.return_value = (b'guest', [
            {"name": "cf_clearance", "value": "new"},
            {"name": "__Host-x1080x_session", "value": "guest"},
            {"name": "__Host-x1080x_csrf", "value": "guestcsrf"}], "Firefox")
        client._refresh_cf(URL)
        self.assertEqual("account", client._cookie_copy()["__Host-x1080x_session"])
        self.assertEqual("csrf", client._cookie_copy()["__Host-x1080x_csrf"])
        self.assertEqual("new", client._cookie_copy()["cf_clearance"])

    def test_external_download_cannot_receive_account_cookies(self):
        client = X1080XHttpClient(HttpSettings(), base_url="https://example.test")
        with self.assertRaises(ValueError):
            client.fetch_torrent("https://evil.example/api/downloads/file/ticket")


if __name__ == "__main__":
    unittest.main()
