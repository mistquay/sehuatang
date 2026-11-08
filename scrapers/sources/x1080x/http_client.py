"""同线路共享 CF、账号和 CSRF；业务请求不交给 Byparr 执行。"""
import hashlib
import json
import time
import threading
from dataclasses import dataclass, field, replace
from functools import lru_cache, partial
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

from scrapers.core.cf_challenge import is_cf_challenge, is_rate_limited, merge_solution_cookies
from scrapers.core.contracts import SourcePaused
from scrapers.core.http import CrawlerHttpClient
from scrapers.core.models import FetchResult
from scrapers.core.config import ProxySettings
from scrapers.core.network import proxy_label
from scrapers.core.session import SessionHttpClient
from scrapers.core.pool import shared_pool, stop_shared_clients
from .auth import Authenticator, AuthSettings
from util.log_util import log


@dataclass(frozen=True)
class SessionResult(FetchResult):
    client: object = field(default=None, repr=False, compare=False)


class X1080XHttpClient(SessionHttpClient):
    def __init__(self, settings, *, config=None, base_url="https://1080xin.com", **kwargs):
        super().__init__(settings, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.config = config or {}
        self.authenticator = Authenticator(AuthSettings.from_config(self.config))
        self._auth_lock = threading.Lock()
        self.authenticated = False
        cookie = SimpleCookie()
        cookie.load(str(self.config.get("cookie") or ""))
        self._initial_cookies = [{"name": key, "value": item.value, "secure": True}
                                 for key, item in cookie.items()]
        if self._initial_cookies:
            self.authenticated = True  # 先保护配置的账号 Cookie；使用前仍会验证真实状态。

    def _prepare_cookies(self, url):
        with self._cookie_lock:
            if self._initial_cookies:
                self._merge_cookies(self._initial_cookies, self.base_url)
                merge_solution_cookies(self._cookie, self._initial_cookies)
                self._initial_cookies.clear()

    def fetch(self, url, stage="detail", **kwargs):
        try:
            result = super().fetch(url, stage, **kwargs)
        except SourcePaused as exc:
            result = FetchResult(url, None, None, 0, 0, "session_paused", exc.reason)
        return SessionResult(**vars(result), client=self)

    def browser_cookies(self):
        self._prepare_cookies(self.base_url)
        with self._cookie_lock:
            return [{"name": c.name, "value": c.value, "domain": c.domain,
                     "path": c.path, "secure": c.secure,
                     "httpOnly": c.has_nonstandard_attr("HttpOnly") or c.name.startswith("__Host-x1080x_session"),
                     **({"expires": c.expires} if c.expires and c.expires > time.time() else {})}
                    for c in self._jar.jar if not c.is_expired()]

    def install_browser_session(self, cookies, user_agent):
        with self._solve_lock, self._cookie_lock:
            self._jar.clear()
            self._cookie.clear()
            self._merge_cookies(cookies, self.base_url)
            merge_solution_cookies(self._cookie, cookies)
            if user_agent:
                self._user_agent = user_agent
            self._cookie_version += 1
            self._failed_validation_version = None

    def ensure_authenticated(self, resource_id):
        with self._auth_lock:
            self.authenticator.ensure(self, resource_id)

    def is_authenticated(self, resource_id):
        result = self._api_fetch("GET", f"/api/resources/{resource_id}/state")
        if result.status_code in {401, 403}:
            return False
        if not result.ok:
            raise SourcePaused(result.error_type or "session_unavailable", "x1080x 登录状态检查失败，本轮暂停")
        try:
            return json.loads(result.body).get("authenticated") is True
        except (ValueError, AttributeError):
            raise SourcePaused("session_unavailable", "x1080x 登录状态响应无效，本轮暂停") from None

    def _refresh_cf(self, url):
        if not self._flaresolverr:
            raise SourcePaused("cf_challenge", "x1080x 需要配置 Byparr 处理 CF")
        with self._solve_lock:
            self.gate.acquire()
            if self.before_request:
                self.before_request()
            solution = self._flaresolverr.solve(self.base_url + "/")
            if solution is None:
                raise SourcePaused("cf_challenge", "x1080x Byparr 验证未通过，本轮暂停")
            _, cookies, ua = solution
            # Byparr 创建游客 context，禁止将游客 session/CSRF 覆盖已登录账号。
            cookies = [c for c in cookies if str(c.get("name", "")).startswith(("cf_", "__cf"))]
            with self._cookie_lock:
                self._merge_cookies(cookies, url)
                merge_solution_cookies(self._cookie, cookies)
                if ua:
                    self._user_agent = ua
                self._cookie_version += 1
                self._failed_validation_version = None

    def _resolve_challenge(self, url, version):
        if not self.authenticated:
            return super()._resolve_challenge(url, version)
        self._refresh_cf(url)
        retry = self._transport.fetch(url, "cf_authenticated_retry")
        if retry.ok and not is_cf_challenge(retry.body, retry.status_code):
            return retry.body, 1 + retry.attempts
        raise SourcePaused("cf_challenge", "x1080x 已保留账号会话，但 CF 后重试未通过；本轮暂停")

    def render_public(self, url):
        if self.authenticated:
            return None
        return self._bypass(url)

    def render_list(self, url):
        """仅渲染公开列表；Byparr 的游客会话不覆盖爬虫账号。"""
        from scrapers.core.cf_challenge import SiteBlocked, SiteRateLimited

        if not self._flaresolverr:
            raise SourcePaused("browser_pass_required", "x1080x 列表需要 BrowserPass，请配置 Byparr 后重试")
        with self._solve_lock:
            self.gate.acquire()
            if self.before_request:
                self.before_request()
            try:
                solution = self._flaresolverr.solve(url)
            except SiteRateLimited:
                self.gate.limit()
                raise SourcePaused("rate_limited", "x1080x 列表浏览器请求命中限流，本轮暂停") from None
            except SiteBlocked:
                raise SourcePaused("cf_blocked", "x1080x 列表浏览器请求被 Cloudflare 封锁") from None
        if solution is None:
            raise SourcePaused("browser_pass_required", "x1080x 列表浏览器验证未完成，请检查 Byparr")
        body, _, _ = solution
        return body

    def _api_fetch(self, method, path, *, data=None, csrf=None):
        url = urljoin(self.base_url + "/", path)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc or not url.startswith(self.base_url + "/api/"):
            raise ValueError("x1080x API 请求必须在当前站点内")
        headers = {"Referer": self.base_url + "/", "Origin": self.base_url}
        if method == "POST":
            self._prepare_cookies(url)
            csrf = csrf or self._cookie_copy().get("__Host-x1080x_csrf")
            if not csrf:
                raise SourcePaused("login_required", "x1080x 缺少 CSRF 会话，请重新登录")
            headers["X-CSRFToken"] = csrf

        def request(target, **kwargs):
            kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
            return self._request_with_cookies(target, _method=method,
                                               **({"json": data} if method == "POST" else {}), **kwargs)

        api_settings = replace(self.settings, retry=replace(self.settings.retry,
            statuses=tuple(s for s in self.settings.retry.statuses if s != 429)))
        transport = CrawlerHttpClient("x1080x", api_settings, request_func=request,
                                      sleeper=self._sleep, attempt_observer=self.network.attempt,
                                      request_timing=self._consume_request_timing)
        try:
            result = transport.fetch(url, "authenticated_api")
            if is_cf_challenge(result.body, result.status_code):
                self._refresh_cf(url)
                retry = transport.fetch(url, "authenticated_api_cf_retry")
                result = replace(retry, attempts=result.attempts + 1 + retry.attempts)
        except SourcePaused:
            raise
        except Exception as exc:
            from scrapers.core.cf_challenge import SiteRateLimited
            if isinstance(exc, SiteRateLimited):
                self.gate.limit()
                raise SourcePaused("rate_limited", "x1080x 附件请求命中限流，本轮暂停") from None
            raise SourcePaused("session_unavailable", f"x1080x API 请求未完成（{type(exc).__name__}）") from None
        if result.status_code == 429 or is_rate_limited(result.body):
            raise SourcePaused("rate_limited", "x1080x 附件请求命中限流，本轮暂停")
        return result

    def api_json(self, method, path, *, data=None, csrf=None, allow_auth_error=False):
        result = self._api_fetch(method, path, data=data, csrf=csrf)
        try:
            value = json.loads(result.body or b"null")
        except ValueError:
            value = None
        if result.status_code in {401, 403} and not allow_auth_error:
            raise SourcePaused("login_required", "x1080x API 会话失效，请重新登录")
        if not isinstance(value, dict) or (not result.ok and not allow_auth_error):
            raise SourcePaused("session_unavailable", "x1080x API 返回异常，本轮暂停")
        return value

    def fetch_torrent(self, path):
        if not urlsplit(path).path.startswith("/api/downloads/file/"):
            raise ValueError("x1080x 下载地址不是站内领取地址")
        result = self._api_fetch("GET", path)
        if not result.ok:
            raise SourcePaused("download_failed", "x1080x 免费附件下载失败，本轮暂停")
        return result.body


@lru_cache(maxsize=16)
def _factory(config_json, base_url):
    return partial(X1080XHttpClient, config=json.loads(config_json), base_url=base_url)


@lru_cache(maxsize=16)
def _fixed_account_lane(settings):
    """账号会话固定到首条线路，避免继承全局代理池时阻断来源与监测页。"""
    proxy = settings.proxy.addresses[0]
    log.bind(module="x1080x").warning(
        f"x1080x 账号会话固定使用首条代理: {proxy_label(proxy)}；"
        "其他来源继续使用原代理池；可在 crawler.sources.x1080x.http.proxy 单独指定线路")
    return replace(settings, proxy=ProxySettings(enabled=True, url=proxy))


def shared_http_client(settings, endpoint, base_url, rate_settings=None, config=None):
    settings = replace(settings, solver_url=endpoint or settings.solver_url)
    if rate_settings:
        settings = replace(settings, min_interval_seconds=rate_settings.min_interval_seconds,
                           cooldown_seconds=rate_settings.cooldown_seconds,
                           max_cooldown_seconds=rate_settings.max_cooldown_seconds)
    config_json = json.dumps(config or {}, sort_keys=True, ensure_ascii=False)
    auth = AuthSettings.from_config(config or {})
    uses_account = auth.mode not in {"cookie", "disabled"} and auth.username
    if (uses_account or (config or {}).get("cookie")) and len(settings.proxy.addresses) > 1:
        settings = _fixed_account_lane(settings)
    return shared_pool("x1080x", settings, base_url,
                       session_factory=_factory(config_json, base_url),
                       identity=hashlib.sha256(config_json.encode()).hexdigest())
