"""用示例配置隔离本机凭据与外部服务，再执行单元测试。"""
import sys
import unittest
from pathlib import Path

import yaml

from util.read_config import _config_manager


def main():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] /
                             "config/config.example.yaml").read_text(encoding="utf-8"))
    config["mongodb"].update(enable=False, use_conn_str=False,
                             db_host="127.0.0.1", db_port=27017)
    config["proxy"]["proxy_enable"] = False
    config["sendMessage"].update(send_telegram_enable=False, tg_bot_token="123:test")
    config["logging"]["console_output"] = False
    for source in config["crawler"]["sources"].values():
        source.setdefault("http", {})["proxy"] = {"enabled": False}
        source["challenge"] = {"flaresolverr_url": ""}
    _config_manager._config_cache = config
    names = sys.argv[1:] or [
        "tests.recovery_tests", "tests.crawler_core_tests", "tests.sehuatang_source_tests",
        "tests.x1080x_tests", "tests.web_app_tests", "tests.page_backfill_tests",
        "tests.x1080x_rate_limit_tests",
        "tests.x1080x_normal_tests",
        "tests.x1080x_code_tests",
        "tests.log_filter_tests",
        "tests.javbee_tests", "tests.backfill_tests", "tests.mongo_recovery_tests",
        "tests.notification_tests",
        "tests.sehuatang_run_tests",
        "tests.network_monitor_tests",
        "tests.proxy_failover_tests",
    ]
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return not result.wasSuccessful()
    finally:
        if "util.mongo" in sys.modules:
            sys.modules["util.mongo"].client.close()


if __name__ == "__main__":
    raise SystemExit(main())
