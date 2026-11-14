"""浏览器执行、账号隔离、远程恢复和二进制合同；默认不启动真实浏览器。"""
import asyncio
import base64
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from browser_runtime import BrowserError, BrowserResponse, BrowserSession, BrowserSettings, load_browser_settings
from browser_runtime.remote import RemoteBrowserSession
from browser_runtime.worker import SessionStore, create_app
from scrapers.core.config import HttpSettings, ProxySettings, RetrySettings, load_source_settings
from scrapers.core.contracts import SourcePaused
from scrapers.core.session import SessionHttpClient
from scrapers.sources.x1080x.auth import Authenticator, AuthSettings
from scrapers.sources.x1080x.http_client import X1080XHttpClient
from tests.x1080x_v2_tests import RID


ORIGIN = "https://site.test"
TOKEN = "test-browser-worker-token-32-characters"


def response(body=b"ok", status=200, cookies=()):
    return BrowserResponse(ORIGIN + "/", body, status, {}, list(cookies), "Browser-UA")


def fake_page(body="<h1>ok</h1>"):
    return SimpleNamespace(url=ORIGIN + "/", goto=AsyncMock(return_value=SimpleNamespace(
        status=200, all_headers=AsyncMock(return_value={}))), content=AsyncMock(return_value=body),
        evaluate=AsyncMock(return_value="Browser-UA"),
        locator=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=0))))


class BrowserConfigTests(unittest.TestCase):
    def test_source_overrides_and_environment_select_an_explicit_engine(self):
        config = {"crawler": {"defaults": {"browser": {"mode": "http"}}, "sources": {
            "javbee": {"browser": {"engine": "obscura", "endpoint": "ws://browser:9222"}}}}}
        settings = load_source_settings(config, "javbee", {"CRAWLER_JAVBEE_BROWSER_MODE": "browser"})
        self.assertEqual(("obscura", "browser"), (settings.browser.engine, settings.browser.mode))
        self.assertEqual("ws://browser:9222", settings.browser.endpoint_for(""))

    def test_obscura_cannot_silently_use_a_different_proxy(self):
        browser = BrowserSettings(engine="obscura", endpoint="ws://browser:9222")
        with self.assertRaisesRegex(ValueError, "固定代理"):
            HttpSettings(proxy=ProxySettings(True, "http://proxy:17891"), browser=browser).validate()
        configured = replace(browser, endpoints=(("http://proxy:17891", "ws://browser-1:9222"),))
        self.assertEqual("ws://browser-1:9222", configured.endpoint_for("http://proxy:17891"))
        with self.assertRaises(ValueError):
            configured.endpoint_for("http://proxy:17892")

    def test_remote_requires_token_and_secrets_do_not_appear_in_repr(self):
        with self.assertRaises(ValueError):
            load_browser_settings({"engine": "remote_firefox", "endpoint": "http://worker:8192"})
        settings = load_browser_settings({"engine": "remote_firefox", "endpoint": "http://worker:8192", "token": TOKEN})
        self.assertNotIn(TOKEN, repr(settings))

    def test_invalid_engine_timeout_and_endpoint_fail_at_configuration(self):
        for raw in ({"engine": "unknown"}, {"timeout": float("nan")}, {"timeout": 601},
                    {"engine": "obscura", "endpoint": "ws://user:secret@browser:9222"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_browser_settings(raw)


class BrowserSessionTests(unittest.TestCase):
    def session(self, engine="firefox", body="<h1>ok</h1>"):
        settings = BrowserSettings(engine=engine, endpoint="ws://browser:9222" if engine == "obscura" else "")
        session = BrowserSession(settings, ORIGIN)
        session._page = fake_page(body)
        session._context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
        self.addCleanup(session.close)
        return session

    def test_context_and_event_loop_stay_on_one_owner_thread(self):
        session = self.session()
        threads, loops = [], []
        async def execute(action, payload):
            threads.append(threading.get_ident())
            loops.append(asyncio.get_running_loop())
            return response()
        session._execute = execute
        for _ in range(2):
            session.render(ORIGIN + "/")
        self.assertEqual(threads[0], threads[1])
        self.assertNotEqual(threading.get_ident(), threads[0])
        self.assertIs(loops[0], loops[1])
        session.close()
        session.close()
        self.assertTrue(loops[0].is_closed())

    def test_close_does_not_stop_the_parent_http_lane(self):
        parent = threading.Event()
        session = BrowserSession(BrowserSettings(), ORIGIN, stop_event=parent)
        session.close()
        self.assertFalse(parent.is_set())

    def test_navigation_and_requests_reject_other_origins_before_io(self):
        session = self.session()
        for url in ("https://evil.test/api/claim", "http://site.test/api/claim", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                session.request("POST", url, data={})
        session._page.goto.assert_not_awaited()

    def test_obscura_challenge_is_not_a_success_or_an_unbounded_wait(self):
        session = self.session("obscura", "<title>Just a moment</title>")
        with self.assertRaises(BrowserError) as caught:
            session.render(ORIGIN + "/")
        self.assertEqual("cf_challenge", caught.exception.reason)
        self.assertIn("unsupported_interactive_challenge", str(caught.exception))
        self.assertIsNotNone(session._page)

    def test_explicit_block_and_retry_after_are_preserved(self):
        with self.assertRaises(BrowserError) as caught:
            BrowserSession._check_body(b"Cloudflare: Sorry, you have been blocked", 403)
        self.assertEqual("cf_blocked", caught.exception.reason)
        with self.assertRaises(BrowserError) as caught:
            BrowserSession._check_body(b"slow down", 429, {"retry-after": "1200"})
        self.assertEqual(("rate_limited", 1200), (caught.exception.reason, caught.exception.retry_after))

    def test_rate_limit_preserves_the_verified_context_for_the_next_navigation(self):
        session = self.session(body="请求过于频繁")
        page = session._page
        with self.assertRaises(BrowserError) as caught:
            session.render(ORIGIN + "/")
        self.assertEqual("rate_limited", caught.exception.reason)
        self.assertIs(page, session._page)
        page.content.return_value = "<h1>resumed</h1>"
        self.assertIn(b"resumed", session.render(ORIGIN + "/").body)

    def test_timeout_resets_context_and_redacts_exception_details(self):
        session = BrowserSession(BrowserSettings(timeout=0.02), ORIGIN)
        self.addCleanup(session.close)
        async def execute(action, payload):
            session._stage = "navigation"
            await asyncio.sleep(10)
        session._execute = execute
        with self.assertRaises(BrowserError) as caught:
            session.render(ORIGIN + "/")
        self.assertEqual("browser_timeout", caught.exception.reason)
        self.assertIn("hint=timeout", str(caught.exception))

    def test_parent_stop_cancels_a_navigation_without_waiting_for_timeout(self):
        parent, entered = threading.Event(), threading.Event()
        session = BrowserSession(BrowserSettings(timeout=20), ORIGIN, stop_event=parent)
        self.addCleanup(session.close)
        async def execute(action, payload):
            entered.set()
            await asyncio.sleep(20)
        session._execute = execute
        errors = []
        def run():
            try:
                session.render(ORIGIN + "/")
            except BrowserError as exc:
                errors.append(exc.reason)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        parent.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["browser_stopped"], errors)

    def test_binary_response_is_not_decoded_as_html_and_csrf_comes_from_context(self):
        session = self.session()
        raw = bytes(range(256))
        calls = []
        async def evaluate(script, payload=None):
            if payload is None:
                return "Browser-UA"
            calls.append(payload)
            return {"url": payload["url"], "status": 200, "headers": {},
                    "body": base64.b64encode(raw).decode()}
        session._page.evaluate = evaluate
        session._context.cookies.return_value = [{"name": "__Host-x1080x_csrf", "value": "latest"}]
        claim = {"idempotency_key": "fixed"}
        result = session.request("POST", ORIGIN + "/api/downloads/claim", data=claim)
        self.assertEqual(raw, result.body)
        self.assertEqual("latest", calls[0]["csrf"])
        self.assertIs(claim, calls[0]["data"])
        self.assertEqual(raw, BrowserResponse.from_wire(result.wire()).body)

    def test_guest_cannot_execute_login(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.login("name", "private", RID)

    def test_ready_selector_returns_html_serialized_after_the_selector_check(self):
        session = self.session()
        session._page.content.side_effect = ["<h1>loading</h1>", '<h1 data-ready="true">loaded</h1>']
        session._page.locator.return_value.count.return_value = 1
        result = session.render(ORIGIN + "/", ready="[data-ready]")
        self.assertIn(b"loaded", result.body)
        self.assertNotIn(b"loading", result.body)

    def test_rendered_legacy_charset_does_not_corrupt_chinese_parser_output(self):
        from bs4 import BeautifulSoup
        session = self.session(body='<meta charset="gbk"><h1>中文标题</h1>')
        result = session.render(ORIGIN + "/")
        self.assertEqual("中文标题", BeautifulSoup(result.body, "html.parser").h1.get_text())


class RemoteBrowserTests(unittest.TestCase):
    def test_worker_restart_never_replays_the_pending_post(self):
        settings = BrowserSettings(engine="remote_firefox", endpoint="http://worker:8192", token=TOKEN)
        http = Mock()
        http.request.side_effect = [SimpleNamespace(status_code=200, json=lambda: {"session_id": "one"}),
                                    SimpleNamespace(status_code=410, json=lambda: {"detail": "expired"})]
        with patch("curl_cffi.requests.Session", return_value=http):
            session = RemoteBrowserSession(settings, ORIGIN, purpose="account")
            try:
                with self.assertRaises(BrowserError) as caught:
                    session.request("POST", ORIGIN + "/api/downloads/claim", data={"idempotency_key": "fixed"})
                self.assertEqual("browser_session_expired", caught.exception.reason)
                self.assertEqual(2, http.request.call_count)
                self.assertIsNone(session._session_id)
            finally:
                session.close()

    def test_remote_transport_errors_do_not_echo_tokens_or_passwords(self):
        settings = BrowserSettings(engine="remote_firefox", endpoint="http://worker:8192", token=TOKEN)
        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.request.side_effect = RuntimeError("password=private token=" + TOKEN)
            session = RemoteBrowserSession(settings, ORIGIN)
            try:
                with self.assertRaises(BrowserError) as caught:
                    session.render(ORIGIN + "/")
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
            finally:
                session.close()

    def test_worker_auth_validation_identity_isolation_and_expiry(self):
        from fastapi.testclient import TestClient
        created = []
        def factory(*args, **kwargs):
            browser = Mock()
            browser.execute.return_value = response(cookies=[{"name": "session", "value": str(len(created))}])
            created.append(browser)
            return browser
        store = SessionStore(capacity=2, idle_seconds=-1, factory=factory)
        app = create_app(TOKEN, store)
        headers = {"Authorization": "Bearer " + TOKEN}
        with TestClient(app) as client:
            self.assertEqual(401, client.post("/v1/sessions", json={}).status_code)
            self.assertEqual(200, client.get("/health").status_code)
            keys = [client.post("/v1/sessions", headers=headers, json={"base_url": ORIGIN}).json()["session_id"] for _ in range(2)]
            self.assertNotEqual(keys[0], keys[1])
            self.assertEqual(503, client.post("/v1/sessions", headers=headers, json={"base_url": ORIGIN}).status_code)
            value = client.post("/v1/sessions/" + keys[0], headers=headers,
                                json={"action": "render", "url": ORIGIN + "/"})
            self.assertEqual("0", value.json()["cookies"][0]["value"])
            created[1].execute.assert_not_called()
            store.reap()
            self.assertEqual(410, client.post("/v1/sessions/" + keys[0], headers=headers,
                             json={"action": "request", "url": ORIGIN + "/api/claim", "method": "POST"}).status_code)
            for browser in created:
                browser.close.assert_called_once()


class CrawlerBrowserTests(unittest.TestCase):
    def test_shared_browser_mode_uses_lane_proxy_and_closes_once(self):
        settings = HttpSettings(browser=BrowserSettings(mode="browser", engine="obscura", endpoint="ws://browser:9222"))
        browser = Mock()
        browser.render.return_value = response()
        with patch("scrapers.core.session.create_browser", return_value=browser) as factory:
            client = SessionHttpClient(settings, source="javbee")
            try:
                client._transport = Mock()
                self.assertTrue(client.fetch(ORIGIN + "/page1").ok)
                self.assertTrue(client.fetch(ORIGIN + "/page2").ok)
                factory.assert_called_once()
                client._transport.fetch.assert_not_called()
            finally:
                client.close()
                client.close()
            browser.close.assert_called_once()

    def test_persistent_login_retains_browser_and_checks_state_in_that_browser(self):
        client = X1080XHttpClient(HttpSettings(), base_url=ORIGIN)
        self.addCleanup(client.close)
        browser = Mock()
        browser.login.return_value = response(cookies=[{"name": "__Host-x1080x_session", "value": "account",
                                                        "domain": "site.test", "path": "/", "secure": True}])
        browser.request.return_value = response(b'{"authenticated":true}')
        client._account_browser = browser
        client.is_authenticated = Mock(side_effect=[False, True])
        client.api_json = Mock(return_value={"enabled": True})
        cfg = AuthSettings("name", "private", mode="auto")
        Authenticator(cfg).ensure(client, RID)
        browser.login.assert_called_once_with("name", "private", RID)
        self.assertTrue(client._account_browser_active)
        browser.close.assert_not_called()
        client._transport = Mock()
        actual = X1080XHttpClient.api_json(client, "GET", f"/api/resources/{RID}/state")
        self.assertEqual({"authenticated": True}, actual)
        client._transport.fetch.assert_not_called()

    def test_browser_post_retry_keeps_claim_body_and_idempotency_key(self):
        settings = HttpSettings(retry=RetrySettings(attempts=2, base_delay=0, max_delay=0))
        client = X1080XHttpClient(settings, base_url=ORIGIN, config={"auth": {"transport": "browser"}})
        self.addCleanup(client.close)
        browser = Mock()
        browser.request.side_effect = [response(b"retry", 503), response(b'{"receipt_id":"ok"}')]
        client._account_browser = browser
        claim = {"idempotency_key": "fixed"}
        self.assertEqual("ok", client.api_json("POST", "/api/downloads/claim", data=claim)["receipt_id"])
        self.assertEqual(2, browser.request.call_count)
        for call in browser.request.call_args_list:
            self.assertIs(claim, call.kwargs["data"])

    def test_manual_cookie_mode_never_submits_credentials_in_browser_transport(self):
        client = X1080XHttpClient(HttpSettings(), base_url=ORIGIN, config={"auth": {
            "mode": "cookie", "transport": "browser", "username": "name", "password": "private"}})
        self.addCleanup(client.close)
        browser = Mock()
        browser.request.return_value = response(b'{"authenticated":false}')
        client._account_browser = browser
        with self.assertRaises(SourcePaused) as caught:
            client.ensure_authenticated(RID)
        self.assertEqual("cookie_expired", caught.exception.reason)
        browser.login.assert_not_called()

    def test_account_browser_cooldown_returns_a_recoverable_result_without_io(self):
        client = X1080XHttpClient(HttpSettings(), base_url=ORIGIN, config={"auth": {"transport": "browser"}})
        self.addCleanup(client.close)
        browser = Mock()
        client._account_browser = browser
        client.gate.limit(1200)
        result = client.fetch(ORIGIN + "/resources/" + RID)
        self.assertEqual(("rate_limited", 429), (result.error_type, result.status_code))
        with self.assertRaises(SourcePaused) as caught:
            client.api_json("GET", f"/api/resources/{RID}/state")
        self.assertEqual("rate_limited", caught.exception.reason)
        browser.render.assert_not_called()
        browser.request.assert_not_called()

    def test_explicit_http_handoff_closes_the_temporary_account_browser(self):
        client = X1080XHttpClient(HttpSettings(), base_url=ORIGIN)
        self.addCleanup(client.close)
        browser = Mock()
        browser.login.return_value = response()
        client._account_browser = browser
        client.login_with_browser(AuthSettings("name", "private", transport="http"), RID)
        browser.close.assert_called_once()
        self.assertIsNone(client._account_browser)
        self.assertFalse(client._account_browser_active)


if __name__ == "__main__":
    unittest.main()
