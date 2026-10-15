"""可选集成验证：仅连接显式指定的本机临时 MongoDB。"""
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from unittest import mock

import pymongo

from util.mongo import (find_due_crawl_failures, list_crawl_failures,
                        record_crawl_failures, requeue_crawl_failure,
                        requeue_exhausted_crawl_failures)


@unittest.skipUnless(os.getenv("CRAWLER_TEST_MONGO_URI"), "未指定本机临时 MongoDB")
class MongoRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        uri = os.environ["CRAWLER_TEST_MONGO_URI"]
        if urlsplit(uri).hostname != "127.0.0.1":
            raise ValueError("集成测试只允许本机临时 MongoDB")
        cls.client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=3000)
        cls.client.admin.command("ping")
        cls.database = cls.client["codex_recovery_test_" + uuid.uuid4().hex]

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(cls.database.name)
        cls.client.close()

    def setUp(self):
        self.collection = self.database[self._testMethodName]
        patch = mock.patch("util.failure_policy.get_config", return_value=5)
        patch.start()
        self.addCleanup(patch.stop)
        self.failure = {"source": "sehuatang", "source_key": "42", "stage": "fetch",
                        "url": "https://example.test/42", "attempts": 3,
                        "error_type": "timeout", "error_message": "$literal-value"}

    def test_atomic_count_delay_exhaustion_and_requeue(self):
        for count in range(1, 6):
            record_crawl_failures([self.failure], collection=self.collection)
            row = self.collection.find_one({})
            self.assertEqual(count, row["failure_count"])
            self.assertEqual("$literal-value", row["error_message"])
            if count < 5:
                delay = (row["next_retry_at"] - row["last_failed_at"]).total_seconds()/60
                self.assertEqual(5 * 2 ** (count - 1), delay)
            else:
                self.assertIsNone(row["next_retry_at"])
        self.assertEqual([], find_due_crawl_failures("sehuatang", collection=self.collection))
        data = list_crawl_failures(collection=self.collection)
        self.assertEqual({"due": 0, "waiting": 0, "exhausted": 1}, data["counts"])
        self.assertTrue(requeue_crawl_failure("sehuatang", "42", "fetch", self.collection))
        self.assertEqual(1, len(find_due_crawl_failures("sehuatang", collection=self.collection)))
        record_crawl_failures([self.failure], self.collection)
        row = list_crawl_failures(collection=self.collection)["failures"][0]
        self.assertEqual((6, 1, "waiting"), (row["failure_count"], row["retry_count"], row["state"]))

    def test_old_over_limit_data_is_not_due_without_migration(self):
        old = {**self.failure, "failure_count": 12,
               "next_retry_at": datetime.now(timezone.utc)-timedelta(days=1)}
        self.collection.insert_one(old)
        self.assertEqual([], find_due_crawl_failures("sehuatang", collection=self.collection))
        data = list_crawl_failures(state="due", collection=self.collection)
        self.assertEqual([], data["failures"])
        self.assertEqual({}, data["due_counts"])
        self.assertEqual(1, data["counts"]["exhausted"])

    def test_membership_and_block_stop_immediately_but_can_be_requeued(self):
        for reason in ("membership_required", "cf_blocked"):
            failure = {**self.failure, "source": "x1080x", "source_key": reason, "error_type": reason}
            record_crawl_failures([failure], self.collection)
            row = self.collection.find_one({"source_key": reason})
            self.assertEqual(1, row["failure_count"])
            self.assertIsNone(row["next_retry_at"])
        self.assertEqual([], find_due_crawl_failures("x1080x", collection=self.collection))
        self.assertEqual(2, list_crawl_failures(collection=self.collection)["counts"]["exhausted"])
        self.assertEqual(2, requeue_exhausted_crawl_failures("x1080x", self.collection))
        self.assertEqual(2, len(find_due_crawl_failures("x1080x", collection=self.collection)))
        self.assertEqual(0, requeue_exhausted_crawl_failures("x1080x", self.collection))

    def test_stage_transition_keeps_original_retry_budget(self):
        record_crawl_failures([self.failure], self.collection)
        changed = {**self.failure, "stage": "parse", "error_type": "invalid_document",
                   "metadata": {"retry_stage": "fetch"}}
        for _ in range(4):
            record_crawl_failures([changed], self.collection)
        self.assertEqual(1, self.collection.count_documents({}))
        row = self.collection.find_one({})
        self.assertEqual(("fetch", "parse", 5), (row["stage"], row["last_stage"], row["failure_count"]))
        self.assertEqual([], find_due_crawl_failures("sehuatang", collection=self.collection))

    def test_bulk_requeue_is_limited_to_source_and_exhausted_state(self):
        for _ in range(5):
            record_crawl_failures([self.failure, {**self.failure, "source": "javbee"}], self.collection)
        record_crawl_failures([{**self.failure, "source_key": "waiting"}], self.collection)
        self.assertEqual(1, requeue_exhausted_crawl_failures("sehuatang", self.collection))
        self.assertEqual(0, requeue_exhausted_crawl_failures("sehuatang", self.collection))
        data = list_crawl_failures(collection=self.collection)
        self.assertEqual({"due": 1, "waiting": 1, "exhausted": 1}, data["counts"])


if __name__ == "__main__":
    unittest.main()
