"""每端口共享验证状态和限速，HTTP Session 由各常驻 worker 独立持有。"""
import copy
import socket
import threading
import time
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Iterable, List, Optional

from curl_cffi import requests

from scrapers.core.cf_challenge import (
    FlareSolverrClient,
    SiteBlocked,
    is_cf_blocked,
    is_cf_challenge,
    is_rate_limited,
    merge_solution_cookies,
)
from scrapers.core.config import HttpSettings
from scrapers.core.http import CrawlerHttpClient, redact_url
from scrapers.core.models import FetchResult
from util.log_util import log
from .rate_limit import RateLimitSettings, RequestGate, SiteRateLimited
from .network import NetworkMonitor, PROBE_TIMEOUT, CONTROL_URL, NETWORK_ERRORS, exception_info


class SessionHttpClient:
    """实现引擎所需的 fetch/fetch_many 合同，内部处理 CF 挑战。"""

    def __init__(
        self,
        settings: HttpSettings,
        flaresolverr_url: str = "",
        transport: Optional[CrawlerHttpClient] = None,
        rate_settings: Optional[RateLimitSettings] = None,
        source: str = "x1080x",
        before_request=None,
    ):
        if len(settings.proxy.addresses) > 1:
            raise ValueError("多代理请求必须使用公共 SessionPool")
        self.settings = settings
        self.source = source
        self.log = log.bind(module=source)
        self.network = NetworkMonitor(source, settings.proxy.url if settings.proxy.enabled else "", self.log)
        self.before_request = before_request
        self.gate = RequestGate(rate_settings or RateLimitSettings(
            settings.min_interval_seconds, settings.cooldown_seconds, settings.max_cooldown_seconds), logger=self.log)
        self._cookie: dict = {}
        self._jar = requests.Cookies()
        self._cookie_lock = threading.Lock()
        # 验证中的重试可重入；普通请求仅在取快照/限速时短暂持有，网络调用不持锁。
        self._solve_lock = threading.RLock()
        self._cookie_version = 0
        self._failed_validation_version = None
        self._local = threading.local()
        self._user_agent = settings.user_agent
        self._flaresolverr = (
            FlareSolverrClient(
                flaresolverr_url,
                source=source,
                provider=settings.solver_provider,
                proxy_url=settings.proxy.url if settings.proxy.enabled else None,
                raise_on_rate_limit=True,
                request_guard=self.gate.check,
            )
            if flaresolverr_url
            else None
        )
        # 429 进入冷却；有 CF 特征的响应由验证层处理，普通 503 仍可重试。
        retry = replace(
            settings.retry,
            statuses=tuple(
                status
                for status in settings.retry.statuses
                if status != 429
            ),
        )
        self._transport = transport or CrawlerHttpClient(
            source,
            replace(settings, retry=retry),
            request_func=self._request_with_cookies,
            sleeper=self._sleep,
            attempt_observer=self.network.attempt,
            request_timing=self._consume_request_timing,
            retry_context=lambda: (getattr(self._local, "attempt_budget", settings.retry.attempts),
                                   getattr(self._local, "retrying", False)),
        )

    # ---------- 引擎合同 ----------

    def fetch(self, url: str, stage: str = "detail", *, attempt_budget=None, retrying=False) -> FetchResult:
        self._local.attempt_budget = attempt_budget or self.settings.retry.attempts
        self._local.retrying = retrying
        try:
            self.gate.check()
            result = self._fetch_once(url, stage)
        except SiteRateLimited:
            return FetchResult(url, None, 429, 0, 0,
                               error_type="rate_limited", error_message="站点正在冷却")
        if result.error_type == "cf_blocked":
            return result
        if (is_rate_limited(result.body) or result.error_type == "siteratelimited"
                or (result.status_code == 429 and not is_cf_challenge(result.body, 200))):
            self.gate.limit()
            return replace(result, status_code=429, error_type="rate_limited",
                           error_message="站点请求过于频繁，等待冷却后恢复")
        if result.ok:
            self.gate.success()
        return result

    def wait_for_retry(self):
        self.gate.wait_for_retry()

    def _fetch_once(self, url: str, stage: str) -> FetchResult:
        started = time.monotonic()
        version = self.validation_version()
        self._local.request_version = None
        result = self._transport.fetch(url, stage)
        if self._local.request_version is not None:
            version = self._local.request_version
        if result.error_type == "cf_blocked" or is_cf_blocked(result.body):
            return replace(result, error_type="cf_blocked", error_message="Cloudflare 明确拒绝访问")
        if (is_rate_limited(result.body) or result.error_type == "siteratelimited"
                or (result.status_code == 429 and not is_cf_challenge(result.body, 200))):
            return result
        if not is_cf_challenge(result.body, result.status_code):
            return result

        self.log.info(f"触发 CF 挑战: stage={stage} url={redact_url(url)}")
        try:
            body, extra_attempts = self._resolve_challenge(url, version)
        except SiteBlocked as exc:
            return FetchResult(url, None, 403, result.attempts + getattr(exc, "attempts", 1),
                               int((time.monotonic() - started) * 1000),
                               error_type="cf_blocked", error_message=str(exc))
        except SiteRateLimited as exc:
            return FetchResult(url, None, 429,
                               result.attempts + getattr(exc, "attempts", 0),
                               int((time.monotonic() - started) * 1000),
                               error_type="rate_limited", error_message="站点请求过于频繁")
        if body is not None:
            return FetchResult(
                url=url,
                body=body,
                status_code=200,
                attempts=result.attempts + extra_attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return FetchResult(
            url=url,
            body=None,
            status_code=result.status_code,
            attempts=result.attempts + extra_attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_type="cf_challenge",
            error_message="Cloudflare 挑战未通过",
        )

    def fetch_many(
        self,
        urls: Iterable[str],
        stage: str = "detail",
    ) -> List[FetchResult]:
        ordered_urls = list(urls)
        if not ordered_urls:
            return []
        results: List[Optional[FetchResult]] = [None] * len(ordered_urls)
        worker_count = min(self.settings.concurrency, len(ordered_urls))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(self.fetch, url, stage): index
                for index, url in enumerate(ordered_urls)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = FetchResult(
                        url=ordered_urls[index],
                        body=None,
                        status_code=None,
                        attempts=1,
                        elapsed_ms=0,
                        error_type=type(exc).__name__.lower(),
                        error_message=str(exc),
                    )
        return [result for result in results if result is not None]

    # ---------- 内部 ----------

    def _prepare_cookies(self, url):
        """来源插件可在首次请求或检测前应用配置 Cookie。"""

    def _request_with_cookies(self, url: str, **kwargs):
        self._prepare_cookies(url)
        self._local.network_timing = None
        # 每次真实请求（包括 transport 内部重试）先检查会话和来源的间隔。
        headers = dict(kwargs.pop("headers", {}) or {})
        with self._solve_lock:
            self.gate.acquire()
            if self.before_request:
                self.before_request()
            self.gate.check()
            with self._cookie_lock:
                headers["User-Agent"] = self._user_agent
                version = self._cookie_version
                cookies = requests.Cookies()
                for cookie in self._jar.jar:
                    cookies.jar.set_cookie(copy.deepcopy(cookie))
            self._local.request_version = version
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        session = self._local.session
        session.cookies.clear()
        started = time.monotonic()
        try:
            response = session.get(url, cookies=cookies, headers=headers, **kwargs)
        finally:
            self._local.network_timing = (started, max(0, int((time.monotonic() - started) * 1000)))
        if not is_cf_blocked(response.content) and (is_rate_limited(response.content)
                or (response.status_code == 429 and not is_cf_challenge(response.content, 200))):
            self.gate.limit(CrawlerHttpClient._retry_after(response))
        with self._cookie_lock:
            if version == self._cookie_version:
                if hasattr(response.cookies, "jar"):
                    self._jar.update(response.cookies)
                self._cookie.update(response.cookies.get_dict())
        return response

    def _consume_request_timing(self):
        timing = getattr(self._local, "network_timing", None)
        self._local.network_timing = None
        return timing

    def probe_network(self, url, revision, *, check_control=False):
        """空闲 worker 原线路单次 GET；复用 Cookie/限速，不触发过盾或资源写入。"""
        response = error = evidence = control = None
        elapsed_ms = None
        try:
            self._prepare_cookies(url)
            if check_control:
                control = self._check_control()
            response = self._request_with_cookies(
                url, proxies=self._transport.proxies,
                timeout=min(PROBE_TIMEOUT, self.settings.timeout),
                allow_redirects=True, impersonate=self.settings.impersonate,
            )
            if getattr(self, "_is_r18_block", lambda body: False)(response.content):
                error = dict(error_type="r18_challenge", curl_code=None, phase="response", summary="目标站返回 R18 验证页面")
        except SiteRateLimited:
            pass
        except Exception as exc:
            error = exception_info(exc)
            if error["error_type"] in NETWORK_ERRORS:
                timing = self._consume_request_timing()
                if timing:
                    elapsed_ms = timing[1]
                control = control or self._check_control()
                evidence = ("target_path" if control["outcome"] == "ok" else
                            "proxy_unreachable" if control.get("evidence") == "proxy_unreachable" else "undetermined")
        finally:
            timing = self._consume_request_timing()
            if timing:
                elapsed_ms = timing[1]
            self.network.finish_probe(url, revision, response, error, evidence, control, elapsed_ms)

    def _check_control(self):
        """经原代理访问测试地址，返回独立于目标站的诊断结果。"""
        started = time.monotonic()
        result = dict(outcome="response_error", status=None, error=None)
        try:
            if self.settings.proxy.enabled:
                self.gate.acquire()
                proxy = urlsplit(self.settings.proxy.url)
                port = proxy.port or {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}[proxy.scheme]
                try:
                    with socket.create_connection((proxy.hostname, port), timeout=3):
                        pass
                except OSError:
                    result.update(outcome="network_error", evidence="proxy_unreachable")
                    return result
            response = self._request_with_cookies(
                CONTROL_URL, proxies=self._transport.proxies, timeout=PROBE_TIMEOUT,
                allow_redirects=False, impersonate=self.settings.impersonate,
            )
            from .network import response_outcome
            result.update(status=response.status_code, outcome="ok" if response.status_code == 204 else
                          response_outcome(response.status_code, response.content))
        except SiteRateLimited:
            result.update(outcome="deferred", evidence="deferred")
        except Exception as exc:
            error = exception_info(exc)
            result.update(outcome="network_error" if error["error_type"] in NETWORK_ERRORS else "response_error", error=error)
        finally:
            result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            self._consume_request_timing()
        return result

    def validation_version(self):
        with self._cookie_lock:
            return self._cookie_version

    def _cookie_copy(self) -> dict:
        with self._cookie_lock:
            return {k: v for k, v in self._cookie.items() if v}

    def _resolve_challenge(self, url: str, version: int):
        """仅当等待期间验证状态更新，才尝试复用；省掉必然失败的重复直连。"""
        attempts = 0
        with self._solve_lock:
            self.gate.check()
            if version != self._cookie_version:
                # 同一批旧请求复用失败结果，不让每个等待线程再次启动过盾。
                if self._failed_validation_version == self._cookie_version:
                    return None, attempts
                retry = self._transport.fetch(url, stage="cf_retry")
                attempts += retry.attempts
                if retry.error_type == "cf_blocked" or is_cf_blocked(retry.body):
                    exc = SiteBlocked("复用会话后 Cloudflare 明确拒绝访问")
                    exc.attempts = attempts
                    raise exc
                if (is_rate_limited(retry.body) or retry.error_type == "siteratelimited"
                        or (retry.status_code == 429 and not is_cf_challenge(retry.body, 200))):
                    self.gate.limit()
                    raise SiteRateLimited()
                if retry.ok and not is_cf_challenge(retry.body, retry.status_code):
                    return retry.body, attempts
            try:
                body = self._bypass(url)
            except SiteBlocked as exc:
                exc.attempts = attempts + 1
                raise
            attempts += 1
            if body is not None:
                return body, attempts
            if self._flaresolverr and self._cookie_copy():
                with self._cookie_lock:
                    self._cookie.clear()
                    self._jar.clear()
                    self._cookie_version += 1
                self.log.warning("CF 过盾失败，已清空缓存 Cookie 重试")
                try:
                    body = self._bypass(url)
                except SiteBlocked as exc:
                    exc.attempts = attempts + 1
                    raise
                attempts += 1
                if body is not None:
                    return body, attempts
            with self._cookie_lock:
                self._cookie_version += 1
                self._failed_validation_version = self._cookie_version
            return None, attempts

    def _bypass(self, url: str) -> Optional[bytes]:
        if self._flaresolverr is None:
            self.log.warning("未配置 flaresolverr_url，无法自动过 CF")
            return None
        started = time.monotonic()
        self.gate.acquire()
        if self.before_request:
            self.before_request()
        try:
            solution = self._flaresolverr.solve(url, cookies=self._cookie_copy())
        except SiteRateLimited as exc:
            self.gate.limit()
            exc.attempts = 1
            raise
        if solution is None:
            return None
        body, cookies, user_agent = solution
        if is_rate_limited(body):
            self.gate.limit()
        with self._cookie_lock:
            merge_solution_cookies(self._cookie, cookies)
            if user_agent:
                self._user_agent = user_agent
            self._cookie_version += 1
            self._failed_validation_version = None
            self._merge_cookies(cookies, url)
        self.log.info(
            "CF 挑战处理完成: "
            f"url={redact_url(url)} cookies={len(cookies)} "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}"
        )
        return body


    def _merge_cookies(self, cookies, url):
        from http.cookiejar import Cookie
        host = urlsplit(url).hostname or ""
        for item in cookies:
            name = item.get("name")
            if not name:
                continue
            domain = item.get("domain") or host
            expires = item.get("expires")
            expires = int(expires) if expires and expires > 0 else None
            self._jar.jar.set_cookie(Cookie(0, name, str(item.get("value", "")),
                None, False, domain, bool(item.get("domain")), domain.startswith("."),
                item.get("path") or "/", True, bool(item.get("secure")), expires,
                expires is None, None, None, {}, False))

    def _sleep(self, seconds):
        from .rate_limit import CrawlStopped
        if self.gate.stop_event.wait(seconds):
            raise CrawlStopped()

    def close(self):
        self.gate.stop_event.set()
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            del self._local.session

    def get_html(self, url):
        result = self.fetch(url)
        return result.body if result.ok else None
