"""Sehuatang R18 验证插件；会话、CF、代理及限流复用公共层。"""
import re
from dataclasses import replace
from http.cookies import SimpleCookie

from scrapers.core.config import load_source_settings
from scrapers.core.pool import shared_pool
from scrapers.core.session import SessionHttpClient
from util.read_config import get_config

_SAFEID_RE = re.compile(r"safeid\s*=\s*['\"]([^'\"]+)['\"]")


class HttpClient(SessionHttpClient):
    def __init__(self, settings=None, transport=None, **kwargs):
        settings = settings or load_source_settings(get_config(), "sehuatang")
        kwargs.setdefault("source", "sehuatang")
        kwargs.setdefault("flaresolverr_url", settings.solver_url)
        super().__init__(settings, transport=transport, **kwargs)
        configured = SimpleCookie()
        configured.load(str(get_config("sehuatang.cookie", "") or ""))
        self._initial_cookies = {name: item.value for name, item in configured.items()}

    def _prepare_cookies(self, url):
        with self._cookie_lock:
            if self._initial_cookies:
                self._merge_cookies([{"name": key, "value": value}
                                     for key, value in self._initial_cookies.items()], url)
                self._cookie.update(self._initial_cookies)
                self._initial_cookies.clear()

    def _fetch_once(self, url, stage):
        version = self.validation_version()
        result = super()._fetch_once(url, stage)
        attempts, elapsed = result.attempts, result.elapsed_ms
        for _ in range(3):
            if not result.ok or not self._is_r18_block(result.body):
                return replace(result, attempts=attempts, elapsed_ms=elapsed)
            match = _SAFEID_RE.search(result.body.decode("utf-8", errors="ignore"))
            with self._solve_lock:
                if version == self.validation_version():
                    with self._cookie_lock:
                        self._cookie["_safe"] = match.group(1)
                        self._merge_cookies([{"name": "_safe", "value": match.group(1)}], url)
                        self._cookie_version += 1
                        self._failed_validation_version = None
                    if self._browser is not None:
                        self._browser.set_cookies([{"name": "_safe", "value": match.group(1),
                                                   "url": self._origin(url)}])
                # 等待期间其他线程已完成 R18/CF 时，先使用最新状态重试。
                version = self.validation_version()
                result = super()._fetch_once(url, "r18_retry")
            attempts += result.attempts
            elapsed += result.elapsed_ms
        if self._is_r18_block(result.body):
            return replace(result, body=None, attempts=attempts, elapsed_ms=elapsed,
                           error_type="r18_challenge", error_message="R18 验证未通过")
        return replace(result, attempts=attempts, elapsed_ms=elapsed)

    @staticmethod
    def _is_r18_block(body):
        return bool(body and len(body) <= 10000 and
                    _SAFEID_RE.search(body.decode("utf-8", errors="ignore")))


def shared_http_client():
    settings = load_source_settings(get_config(), "sehuatang")
    domain = str(get_config("sehuatang.domain_name", ""))
    return shared_pool("sehuatang", settings, "https://" + domain,
                       session_factory=HttpClient,
                       identity=str(get_config("sehuatang.cookie", "") or ""))
