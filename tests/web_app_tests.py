import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

from scrapers.registry import source_registry
from web.app import create_app


def setUpModule():
    global _config_patch
    _config_patch = mock.patch("util.read_config._config_manager._config_cache",
                               {"mongodb": {"enable": False}})
    _config_patch.start()


def tearDownModule():
    _config_patch.stop()


def make_client(tmp_dir, token="test-token", **kwargs):
    config_path = Path(tmp_dir) / "config.yaml"
    config_path.write_text("mongodb:\n  enable: false\n", encoding="utf-8")
    app = create_app(token=token, config_path=str(config_path), **kwargs)
    return TestClient(app), config_path


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_rejects_missing_or_wrong_token(self):
        client, _ = make_client(self.tmp.name)
        self.assertEqual(401, client.get("/api/status").status_code)
        self.assertEqual(
            401,
            client.get("/api/status", headers={"X-Token": "wrong"}).status_code,
        )

    def test_accepts_valid_token_via_header_and_query(self):
        client, _ = make_client(self.tmp.name)
        self.assertEqual(
            200,
            client.get("/api/status", headers={"X-Token": "test-token"}).status_code,
        )
        self.assertEqual(
            200,
            client.get("/api/status?token=test-token").status_code,
        )

    def test_without_token_rejects_non_loopback_client(self):
        # TestClient 的 client host 是 "testclient"，非回环地址
        client, _ = make_client(self.tmp.name, token="")
        response = client.get("/api/status")
        self.assertEqual(401, response.status_code)
        self.assertIn("token", response.json()["detail"])

    def test_index_page_is_public(self):
        client, _ = make_client(self.tmp.name)
        response = client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn("爬虫管理", response.text)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client, _ = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}

    def test_status_lists_registered_sources(self):
        data = self.client.get("/api/status", headers=self.headers).json()
        names = {source["name"] for source in data["sources"]}
        self.assertEqual(set(source_registry.names()), names)
        for source in data["sources"]:
            self.assertIn("enabled", source)
            self.assertIn("running", source)

    def test_runs_and_failures_empty_without_mongodb(self):
        runs = self.client.get("/api/runs", headers=self.headers).json()
        failures = self.client.get("/api/failures", headers=self.headers).json()
        self.assertEqual([], runs["runs"])
        self.assertEqual([], failures["failures"])


class ConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client, self.config_path = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}

    def test_read_returns_file_content(self):
        data = self.client.get("/api/config", headers=self.headers).json()
        self.assertEqual(str(self.config_path), data["path"])
        self.assertIn("mongodb", data["content"])

    def test_write_validates_yaml(self):
        response = self.client.put(
            "/api/config",
            headers=self.headers,
            json={"content": "mongodb: [broken"},
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("YAML", response.json()["detail"])
        # 原文件未被破坏
        self.assertIn("enable: false", self.config_path.read_text(encoding="utf-8"))

    def test_write_rejects_non_mapping(self):
        response = self.client.put(
            "/api/config",
            headers=self.headers,
            json={"content": "- just\n- a list\n"},
        )
        self.assertEqual(400, response.status_code)

    def test_write_saves_and_backs_up(self):
        new_content = "mongodb:\n  enable: true\n"
        response = self.client.put(
            "/api/config",
            headers=self.headers,
            json={"content": new_content},
        )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["restart_required"])
        self.assertEqual(
            new_content,
            self.config_path.read_text(encoding="utf-8"),
        )
        backup = self.config_path.with_suffix(".yaml.bak")
        self.assertIn("enable: false", backup.read_text(encoding="utf-8"))

    def test_x1080x_form_requires_authentication(self):
        self.assertEqual(401, self.client.get("/api/config/x1080x").status_code)
        self.assertEqual(401, self.client.put("/api/config/x1080x", json={"mode": "cookie"}).status_code)

    def seed_x1080x(self):
        config = {"mongodb": {"enable": False}, "custom": {"keep": [1, 2]}, "x1080x": {
            "categories": {"a": "中文字幕"}, "cookie": "session=private-cookie", "http": {"user_agent": "Original-login-browser"},
            "auth": {"mode": "auto", "username": "member", "password": "private-password", "timeout": 90}},
            "crawler": {"sources": {"x1080x": {"concurrency": 1, "http": {"impersonate": "firefox147"}}}}}
        self.config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        return config

    def test_x1080x_read_does_not_return_saved_password_or_cookie(self):
        self.seed_x1080x()
        response = self.client.get("/api/config/x1080x", headers=self.headers)
        data = response.json()
        self.assertEqual("member", data["username"])
        self.assertTrue(data["password_set"])
        self.assertTrue(data["cookie_set"])
        self.assertEqual("Original-login-browser", data["user_agent"])
        self.assertNotIn("private-password", response.text)
        self.assertNotIn("private-cookie", response.text)
        self.assertNotIn("password", data)
        self.assertNotIn("cookie", data)

    def test_x1080x_form_saves_and_preserves_unsubmitted_secrets_and_other_settings(self):
        original = self.seed_x1080x()
        response = self.client.put("/api/config/x1080x", headers=self.headers, json={
            "mode": "cookie", "user_agent": "Firefox test", "impersonate": "firefox147"})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["restart_required"])
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(original["custom"], saved["custom"])
        self.assertEqual(original["x1080x"]["cookie"], saved["x1080x"]["cookie"])
        self.assertEqual(original["x1080x"]["auth"]["password"], saved["x1080x"]["auth"]["password"])
        self.assertEqual(90, saved["x1080x"]["auth"]["timeout"])
        self.assertEqual("cookie", saved["x1080x"]["auth"]["mode"])
        self.assertEqual("Firefox test", saved["crawler"]["sources"]["x1080x"]["http"]["user_agent"])
        self.assertEqual(original, yaml.safe_load(self.config_path.with_suffix(".yaml.bak").read_text(encoding="utf-8")))

    def test_x1080x_clear_removes_override_values_without_changing_proxy_or_categories(self):
        config = self.seed_x1080x()
        config["crawler"]["sources"]["x1080x"].update(auth={"password": "override", "headless": False}, cookie="session=override")
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        response = self.client.put("/api/config/x1080x", headers=self.headers,
                                   json={"cookie": "", "password": "", "mode": "cookie"})
        self.assertEqual(200, response.status_code)
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("", saved["x1080x"]["cookie"])
        self.assertEqual("", saved["x1080x"]["auth"]["password"])
        self.assertFalse(saved["x1080x"]["auth"]["headless"])
        self.assertNotIn("auth", saved["crawler"]["sources"]["x1080x"])
        self.assertNotIn("cookie", saved["crawler"]["sources"]["x1080x"])
        self.assertEqual(config["x1080x"]["categories"], saved["x1080x"]["categories"])

    def test_x1080x_invalid_form_never_overwrites_file(self):
        self.seed_x1080x()
        original = self.config_path.read_bytes()
        for payload in ({"mode": "invalid"}, {"password": None}, {"impersonate": "invalid"},
                        {"cookie": "not-a-cookie"}, {"cookie": "a=b\r\nX-Other: secret"},
                        {"user_agent": ""}, {"unexpected": "value"}):
            with self.subTest(payload=payload):
                response = self.client.put("/api/config/x1080x", headers=self.headers, json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual(original, self.config_path.read_bytes())

    def test_x1080x_reports_environment_override_names_without_values(self):
        with mock.patch.dict("os.environ", {"CRAWLER_X1080X_PASSWORD": "env-private", "CRAWLER_X1080X_COOKIE": "env-private-cookie"}):
            response = self.client.get("/api/config/x1080x", headers=self.headers)
        self.assertEqual("cookie", response.json()["mode"])
        self.assertIn("cookie", response.json()["environment_overrides"])
        self.assertIn("password", response.json()["environment_overrides"])
        self.assertNotIn("env-private", response.text)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client, _ = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}

    def test_crawl_rejects_unknown_source(self):
        response = self.client.post(
            "/api/actions/crawl",
            headers=self.headers,
            json={"source": "nope"},
        )
        self.assertEqual(400, response.status_code)

    def test_backfill_validates_page_range(self):
        response = self.client.post(
            "/api/actions/backfill-pages",
            headers=self.headers,
            json={"source": "x1080x", "start_page": 5, "end_page": 2},
        )
        self.assertEqual(400, response.status_code)

    def test_backfill_rejects_unsupported_source(self):
        response = self.client.post(
            "/api/actions/backfill-pages",
            headers=self.headers,
            json={"source": "javbee", "end_page": 3},
        )
        self.assertEqual(400, response.status_code)

    def test_restart_uses_injected_func(self):
        called = threading.Event()
        client, _ = make_client(self.tmp.name, restart_func=called.set)
        response = client.post("/api/actions/restart", headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.assertTrue(called.wait(timeout=3))


class LogsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client, _ = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}
        self.logs_dir = Path(self.tmp.name) / "logs"
        self.logs_dir.mkdir()
        patcher = mock.patch("util.log_util.logs_dir", self.logs_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_unknown_file(self):
        response = self.client.get("/api/logs?file=other", headers=self.headers)
        self.assertEqual(400, response.status_code)

    def test_missing_file_returns_empty(self):
        data = self.client.get("/api/logs", headers=self.headers).json()
        self.assertEqual([], data["lines"])

    def test_returns_last_lines_of_large_file(self):
        (self.logs_dir / "crawler.log").write_text(
            "\n".join(f"line-{i}" for i in range(1, 2001)) + "\n",
            encoding="utf-8",
        )
        data = self.client.get(
            "/api/logs?lines=10", headers=self.headers
        ).json()
        self.assertEqual(10, len(data["lines"]))
        self.assertEqual("line-2000", data["lines"][-1])

    def test_error_log_selectable(self):
        (self.logs_dir / "error.log").write_text("boom\n", encoding="utf-8")
        data = self.client.get(
            "/api/logs?file=error", headers=self.headers
        ).json()
        self.assertEqual(["boom"], data["lines"])

    def test_module_and_level_filters_are_combined(self):
        from tests.log_filter_tests import entry
        (self.logs_dir / "crawler.log").write_text(
            entry(1, module="x1080x", level="ERROR") + entry(2, module="telegram", level="ERROR")
            + entry(3, module="x1080x", level="INFO"), encoding="utf-8")
        response = self.client.get("/api/logs?module=x1080x&level=ERROR", headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.assertEqual([entry(1, level="ERROR").strip()], response.json()["lines"])
        self.assertEqual(1, response.json()["matched_count"])

    def test_invalid_module_and_level_are_rejected(self):
        for query in ("module=unknown", "level=unknown"):
            self.assertEqual(400, self.client.get(f"/api/logs?{query}", headers=self.headers).status_code)


class FailureLifecycleApiTests(unittest.TestCase):
    def setUp(self):
        from scrapers.core.contracts import CrawlFailure
        from scrapers.infrastructure.json_failures import JsonFailureStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonFailureStore(Path(self.tmp.name) / "failures.json")
        for _ in range(5):
            self.store.record([CrawlFailure("sehuatang", "42", "https://example.test/42",
                                            "fetch", 1, "timeout")])
        patcher = mock.patch("scrapers.infrastructure.build_failure_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client, _ = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}
        self.payload = {"source": "sehuatang", "key": "42", "stage": "fetch"}

    def test_filter_and_requeue_preserve_total_and_require_auth(self):
        data = self.client.get("/api/failures?state=exhausted", headers=self.headers).json()
        self.assertEqual(1, data["counts"]["exhausted"])
        self.assertEqual(5, data["failures"][0]["retry_count"])
        self.assertEqual(401, self.client.post("/api/failures/requeue", json=self.payload).status_code)
        self.assertEqual(200, self.client.post("/api/failures/requeue", headers=self.headers,
                                               json=self.payload).status_code)
        row = self.client.get("/api/failures?state=due", headers=self.headers).json()["failures"][0]
        self.assertEqual((5, 0), (row["failure_count"], row["retry_count"]))

    def test_requeue_rejects_busy_source_and_invalid_state(self):
        with source_registry.activity("sehuatang", "crawl", "test"):
            response = self.client.post("/api/failures/requeue", headers=self.headers, json=self.payload)
            self.assertEqual(409, response.status_code)
            status = self.client.get("/api/status", headers=self.headers).json()
            self.assertEqual("crawl", status["active_tasks"][0]["kind"])
        self.assertEqual(400, self.client.get("/api/failures?state=unknown", headers=self.headers).status_code)

    def test_bulk_requeue_requires_explicit_source(self):
        url = "/api/failures/requeue-exhausted"
        self.assertEqual(400, self.client.post(url, json={}, headers=self.headers).status_code)
        result = self.client.post(url, json={"source": "sehuatang"}, headers=self.headers)
        self.assertEqual({"ok": True, "requeued": 1}, result.json())


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        from notifications.memory_queue import MemoryNotificationQueue, NotificationJob
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = MemoryNotificationQueue(sender=mock.Mock(side_effect=ValueError("invalid target")))
        self.addCleanup(lambda: self.queue.close(1))
        self.queue.enqueue(NotificationJob("sehuatang", "42", "test", {}))
        # 等待消费者记录终态；不关闭队列，以便验证手动重试。
        until = time.monotonic() + 2
        while not self.queue.snapshot()["history"] and time.monotonic() < until:
            time.sleep(0.005)
        patcher = mock.patch("notifications.memory_queue.get_notification_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client, _ = make_client(self.tmp.name)
        self.headers = {"X-Token": "test-token"}

    def test_read_is_authenticated_and_disabled_does_not_allow_retry(self):
        self.assertEqual(401, self.client.get("/api/notifications").status_code)
        state = self.client.get("/api/notifications", headers=self.headers).json()
        self.assertEqual(1, state["failed"])
        response = self.client.post("/api/notifications/retry", headers=self.headers,
                                    json={"id": state["history"][0]["id"]})
        self.assertEqual(409, response.status_code)

    def test_retry_uses_only_in_memory_job(self):
        with mock.patch("util.read_config._config_manager._config_cache",
                        {"sendMessage": {"send_telegram_enable": True}}):
            state = self.client.get("/api/notifications", headers=self.headers).json()
            response = self.client.post("/api/notifications/retry", headers=self.headers,
                                        json={"id": state["history"][0]["id"]})
            self.assertEqual(200, response.status_code)
            self.assertEqual(409, self.client.post("/api/notifications/retry", headers=self.headers,
                                                   json={"id": "missing"}).status_code)


class RegistryLockTests(unittest.TestCase):
    def test_run_returns_already_running_when_locked(self):
        import asyncio

        name = source_registry.names()[0]
        lock = source_registry._run_locks[name]
        self.assertTrue(lock.acquire(blocking=False))
        try:
            result = asyncio.run(
                source_registry.run(name, {}, force=True, dry_run=True)
            )
            self.assertEqual("already_running", result["status"])
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
