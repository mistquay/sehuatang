"""代理故障转移与独立检测；所有 HTTP/TCP 均为模拟，不访问外部服务。"""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from scrapers.core.config import ProxySettings, RetrySettings
from scrapers.core.pool import SessionPool, stop_shared_clients
from scrapers.core.rate_limit import CrawlStopped
from scrapers.core.session import SessionHttpClient
from tests import multi_proxy_tests as helpers
from util.read_config import _config_manager
from web.app import create_app


class PoolTestCase(unittest.TestCase):
    fake_sessions = helpers.PoolTests.fake_sessions
    pool = helpers.PoolTests.pool

    def wait_idle(self, pool):
        deadline = time.monotonic() + 3
        with pool._condition:
            while pool._busy and time.monotonic() < deadline:
                pool._condition.wait(.02)
        self.assertFalse(pool._busy)

    def retry(self, attempts=3):
        return RetrySettings(attempts=attempts, base_delay=0, max_delay=0)


class FailoverTests(PoolTestCase):
    def test_all_sources_switch_retry_and_skip_failed_proxy_on_next_request(self):
        from scrapers.http_client import HttpClient
        for source in ("sehuatang", "javbee", "x1080x"):
            with self.subTest(source=source):
                calls = []
                def get(session, url, **kw):
                    proxy = kw["proxies"]["http"]
                    calls.append(proxy)
                    if proxy.endswith("17891"):
                        raise TimeoutError()
                    return helpers.response()
                self.fake_sessions(get)
                factory = HttpClient if source == "sehuatang" else SessionHttpClient
                with patch("scrapers.http_client.get_config", return_value=""):
                    pool = SessionPool(source, helpers.settings(retry=self.retry()), session_factory=factory)
                self.addCleanup(pool.close)
                first = pool.fetch("https://site.test/first")
                second = pool.fetch("https://site.test/second")
                self.assertTrue(first.ok and second.ok)
                self.assertEqual((2, 1), (first.attempts, second.attempts))
                self.assertEqual(["17891", "17892", "17892"], [p.rsplit(":", 1)[-1] for p in calls])
                rows = [lane.network.snapshot()[0] for lane in pool.lanes]
                self.assertEqual(1, rows[1]["retries"])
                self.assertEqual(0, sum(row["failed"] for row in rows))
                self.assertEqual(2, sum(row["completed"] for row in rows))
                pool.close()

    def test_total_budget_is_shared_across_four_proxies(self):
        calls = []
        def get(session, url, **kw):
            calls.append(kw["proxies"]["http"])
            raise ConnectionError()
        self.fake_sessions(get)
        pool = self.pool(retry=self.retry(), proxy=ProxySettings(True, urls=tuple(
            f"http://proxy.test:{port}" for port in range(1001, 1005))))
        result = pool.fetch("https://site.test/item")
        self.assertEqual(3, result.attempts)
        self.assertEqual(3, len(set(calls)))
        self.assertEqual(3, len(calls))

    def test_same_proxy_http_retries_and_cross_proxy_retries_share_budget(self):
        calls = []
        def get(session, url, **kw):
            calls.append(kw["proxies"]["http"])
            if len(calls) == 2:
                raise TimeoutError()
            return helpers.response(b"unavailable", 503)
        self.fake_sessions(get)
        result = self.pool(retry=self.retry()).fetch("https://site.test/item")
        self.assertEqual(3, result.attempts)
        self.assertEqual(["17891", "17891", "17892"], [p.rsplit(":", 1)[-1] for p in calls])

    def test_parallel_failed_workers_release_without_retry_deadlock(self):
        barrier = threading.Barrier(2)
        def get(*args, **kw):
            barrier.wait(2)
            raise TimeoutError()
        self.fake_sessions(get)
        pool = self.pool(retry=self.retry())
        with ThreadPoolExecutor(max_workers=1) as executor:
            results = executor.submit(pool.fetch_many, ["https://site.test/a", "https://site.test/b"]).result(3)
        self.assertTrue(all(r.error_type == "proxy_unavailable" for r in results))
        self.assertEqual([1, 1], [r.attempts for r in results])
        later = pool.fetch("https://site.test/later")
        self.assertEqual(("proxy_unavailable", None, 0), (later.error_type, later.status_code, later.attempts))

    def test_network_failure_is_scoped_to_target_origin(self):
        def get(session, url, **kw):
            if url.startswith("https://bad.test"):
                raise TimeoutError()
            return helpers.response()
        self.fake_sessions(get)
        pool = self.pool(retry=self.retry(1))
        pool.fetch("https://bad.test/item")
        self.assertFalse(pool.lanes[0].network.available("https://bad.test/other"))
        self.assertTrue(pool.lanes[0].network.available("https://good.test/item"))
        self.assertTrue(pool.fetch("https://good.test/item").ok)

    def test_other_lane_rate_limit_cannot_clear_network_failure_quarantine(self):
        def get(*args, **kw):
            raise TimeoutError()
        self.fake_sessions(get)
        pool = self.pool(retry=self.retry(), cooldown_seconds=60, max_cooldown_seconds=900)
        pool.lanes[1].gate.limit()
        result = pool.fetch("https://site.test/item")
        self.assertEqual(("rate_limited", 1), (result.error_type, result.attempts))
        row = pool.lanes[0].network.snapshot()[0]
        self.assertTrue(row["quarantined"])
        self.assertEqual("unstable", row["state"])
        self.assertEqual((1, 1), (row["completed"], row["failed"]))

    def test_cooldown_probe_restores_line_without_increasing_crawl_counters(self):
        restored = False
        def get(session, url, **kw):
            if not restored:
                raise TimeoutError()
            return helpers.response()
        self.fake_sessions(get)
        pool = self.pool(retry=self.retry(1))
        lane = pool.lanes[0]
        pool.fetch("https://site.test/item")
        self.wait_idle(pool)
        clock = time.monotonic()
        lane.network._now = lambda: clock
        pool._dispatch_diagnostics()
        self.assertFalse(lane.network.available("https://site.test/item"))
        clock += 61
        restored = True
        pool._dispatch_diagnostics()
        self.wait_idle(pool)
        self.assertTrue(lane.network.available("https://site.test/item"))
        row = lane.network.snapshot()[0]
        self.assertEqual((1, 1, 1), (row["attempts"], row["completed"], row["failed"]))
        self.assertEqual("ok", row["diagnostic"]["outcome"])

    def test_target_limits_and_challenges_do_not_quarantine_proxy(self):
        for status, body in [(403, b"denied"), (429, b"Too Many Requests"),
                             (403, b"<title>Just a moment</title>")]:
            with self.subTest(status=status, body=body):
                self.fake_sessions(lambda *a, **kw: helpers.response(body, status))
                pool = self.pool(retry=self.retry())
                pool.fetch("https://site.test/item")
                self.assertTrue(pool.lanes[0].network.available("https://site.test/item"))
                pool.close()

    def test_stop_interrupts_cross_proxy_retry_backoff(self):
        failed = threading.Event()
        def get(*args, **kw):
            failed.set()
            raise TimeoutError()
        self.fake_sessions(get)
        pool = self.pool(retry=RetrySettings(attempts=3, base_delay=15, max_delay=15, jitter=0))
        cancel = threading.Event()
        future = pool._submit("https://site.test/item", "detail", False, cancel)
        self.assertTrue(failed.wait(1))
        cancel.set()
        with self.assertRaises(CrawlStopped):
            future.result(1)


class IndependentCheckTests(PoolTestCase):
    # 仅复用辅助方法，下面 API 用例不执行抓取。
    def setUp(self):
        self.addCleanup(stop_shared_clients)
        self.config = {"mongodb": {"enable": False}, "sehuatang": {"domain_name": "site.test", "cookie": "visitor=configured"},
                       "crawler": {"defaults": {"rate_limit": {"min_interval_seconds": 0}, "http": {"proxy": {"enabled": True, "urls": [
                           "http://user:password@proxy.test:17891", "http://user:password@proxy.test:17892"]}}}}}
        patcher = patch.object(_config_manager, "_config_cache", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(token="test", config_path=self.temp.name + "/config.yaml"))
        self.headers = {"X-Token": "test"}

    def test_configured_lines_display_before_crawl_and_check_reuses_crawler_pool(self):
        from scrapers.http_client import shared_http_client
        calls = []
        def get(session, url, **kw):
            calls.append((url, kw["proxies"]["http"]))
            self.assertEqual("configured", kw["cookies"].get("visitor"))
            return helpers.response(b"" if "gstatic" in url else b"page", 204 if "gstatic" in url else 200)
        self.fake_sessions(get)
        with patch("scrapers.core.session.socket.create_connection"):
            snapshot = self.client.get("/api/network", headers=self.headers).json()
            self.assertEqual(2, len(snapshot["lines"]))
            self.assertEqual([], calls)
            self.assertTrue(all(r["state"] == "stale" for r in snapshot["lines"]))
            result = self.client.post("/api/network/check", headers=self.headers, json={"source": "sehuatang"})
            self.assertEqual({"queued": 2}, result.json())
            pool = shared_http_client()
            self.wait_idle(pool)
        self.assertEqual(4, len(calls))
        snapshot = self.client.get("/api/network", headers=self.headers).json()
        self.assertNotIn("password", json.dumps(snapshot))
        for row in snapshot["lines"]:
            self.assertEqual((0, 0, 0), (row["attempts"], row["completed"], row["failed"]))
            self.assertEqual("healthy", row["state"])
            self.assertEqual("ok", row["diagnostic"]["control"]["outcome"])
            self.assertIsNotNone(row["diagnostic"]["elapsed_ms"])

    def test_independent_checks_share_the_actual_pool_for_all_three_sources(self):
        from scrapers.core.network_management import configured_pools
        from scrapers.javbee_scraper import JavbeeScraper
        from scrapers.x1080x_scraper import X1080XScraper
        self.config.update(javbee={"enabled": False}, x1080x={"enabled": False})
        self.fake_sessions(lambda session, url, **kw: helpers.response(b"", 204) if "gstatic" in url else helpers.response())
        pools = configured_pools(self.config)
        self.assertIs(pools["javbee"][0], JavbeeScraper(failure_store=Mock()).http)
        self.assertIs(pools["x1080x"][0], X1080XScraper(failure_store=Mock()).http)
        with patch("scrapers.core.session.socket.create_connection"):
            self.assertEqual(6, self.client.post("/api/network/check", headers=self.headers, json={}).json()["queued"])
            for pool, _ in pools.values():
                self.wait_idle(pool)
        snapshot = self.client.get("/api/network", headers=self.headers).json()
        self.assertEqual(6, len(snapshot["lines"]))
        self.assertTrue(all(row["state"] == "healthy" for row in snapshot["lines"]))

    def test_x1080x_authenticated_pool_pins_one_inherited_lane_without_breaking_monitoring(self):
        from scrapers.core.network_management import configured_pools
        from scrapers.x1080x_scraper import X1080XScraper
        self.config.update(javbee={"enabled": False}, x1080x={"auth": {
            "mode": "auto", "username": "member", "password": "secret"}})
        self.fake_sessions(lambda *a, **kw: helpers.response())
        response = self.client.get("/api/network", headers=self.headers)
        self.assertEqual(200, response.status_code)
        pools = configured_pools(self.config)
        self.assertEqual(2, len(pools["sehuatang"][0].lanes))
        self.assertEqual(2, len(pools["javbee"][0].lanes))
        pool = pools["x1080x"][0]
        self.assertEqual(1, len(pool.lanes))
        self.assertEqual("http://user:password@proxy.test:17891", pool.lanes[0].settings.proxy.url)
        self.assertIs(pool, X1080XScraper(failure_store=Mock()).http)
        self.assertEqual(5, len(response.json()["lines"]))

    def test_check_single_line_is_authenticated_and_validates_selector(self):
        self.fake_sessions(lambda *a, **kw: helpers.response())
        self.assertEqual(401, self.client.post("/api/network/check", json={}).status_code)
        for payload in ({"source": "missing"}, {"proxy_slot": 0}, {"source": "sehuatang", "proxy_slot": 99},
                        {"source": "sehuatang", "proxy_slot": True}, {"source": ["sehuatang"]}):
            response = self.client.post("/api/network/check", headers=self.headers, json=payload)
            self.assertEqual(400, response.status_code)

    def test_manual_check_deduplicates_running_probe_and_detects_target_restriction(self):
        from scrapers.http_client import shared_http_client
        started, release = threading.Event(), threading.Event()
        def get(session, url, **kw):
            if "gstatic" in url:
                return helpers.response(b"", 204)
            started.set()
            self.assertTrue(release.wait(2))
            return helpers.response(b"denied", 403)
        self.fake_sessions(get)
        with patch("scrapers.core.session.socket.create_connection"):
            try:
                payload = {"source": "sehuatang", "proxy_slot": 0}
                self.assertEqual(1, self.client.post("/api/network/check", headers=self.headers, json=payload).json()["queued"])
                self.assertTrue(started.wait(1))
                self.assertEqual(0, self.client.post("/api/network/check", headers=self.headers, json=payload).json()["queued"])
            finally:
                release.set()
            pool = shared_http_client()
            self.wait_idle(pool)
        row = pool.lanes[0].network.snapshot()[0]
        self.assertEqual("restricted", row["state"])
        self.assertFalse(row["quarantined"])

    def test_probe_reports_r18_challenge_without_claiming_target_is_healthy(self):
        from scrapers.http_client import shared_http_client
        def get(session, url, **kw):
            return helpers.response(b"", 204) if "gstatic" in url else helpers.response(b"<script>var safeid='test'</script>")
        self.fake_sessions(get)
        with patch("scrapers.core.session.socket.create_connection"):
            self.client.post("/api/network/check", headers=self.headers, json={"source": "sehuatang", "proxy_slot": 0})
            pool = shared_http_client()
            self.wait_idle(pool)
        self.assertEqual("challenge", pool.lanes[0].network.snapshot()[0]["state"])


if __name__ == "__main__":
    unittest.main()
