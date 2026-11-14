"""默认只使用手动 Cookie；显式启用账号模式时才执行独立登录。"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from scrapers.core.contracts import SourcePaused
from browser_runtime import BrowserError
from browser_runtime.config import BrowserSettings, load_browser_settings, proxy_options
from browser_runtime.pages import browser_error_hint, click_verification as _click_verification
from browser_runtime.x1080x import login_page, verified_session


@dataclass(frozen=True)
class AuthSettings:
    username: str = ""
    password: str = field(default="", repr=False)
    mode: str = "cookie"
    timeout: float = 120
    headless: bool = True
    transport: str = "auto"
    browser: BrowserSettings = field(default_factory=BrowserSettings)

    @classmethod
    def from_config(cls, config):
        raw = config.get("auth") or {}
        mode = str(raw.get("mode", "cookie"))
        if mode not in {"cookie", "auto", "http", "browser", "disabled"}:
            raise ValueError("x1080x.auth.mode 只支持 cookie/auto/http/browser/disabled")
        timeout = float(raw.get("timeout", 120))
        if not 0 < timeout <= 600:
            raise ValueError("x1080x.auth.timeout 必须在 0 到 600 秒之间")
        transport = str(os.getenv("CRAWLER_X1080X_AUTH_TRANSPORT", raw.get("transport", "auto")))
        if transport not in {"auto", "http", "browser"}:
            raise ValueError("x1080x.auth.transport 只支持 auto/http/browser")
        browser = load_browser_settings(raw.get("browser"), environ=os.environ, prefix="CRAWLER_X1080X_AUTH_")
        if browser.engine == "obscura":
            raise ValueError("账号验证需要 Firefox；Obscura 可用于公开列表和普通动态页面")
        return cls(os.getenv("CRAWLER_X1080X_USERNAME", raw.get("username", "")),
                   os.getenv("CRAWLER_X1080X_PASSWORD", raw.get("password", "")),
                   mode, timeout, bool(raw.get("headless", True)), transport, browser)


class Authenticator:
    def __init__(self, settings, browser_login=None):
        self.settings = settings
        self._persistent_login = browser_login is None
        self.browser_login = browser_login or login_in_browser

    def ensure(self, client, resource_id):
        try:
            authenticated = client.is_authenticated(resource_id)
        except SourcePaused as exc:
            if (exc.reason not in {"cf_challenge", "browser_pass_required"}
                    or self.settings.mode in {"cookie", "disabled"}):
                raise
            authenticated = False
        if authenticated:
            client.authenticated = True
            return
        client.authenticated = False
        cfg = self.settings
        if cfg.mode == "cookie":
            diagnostic = getattr(client, "auth_diagnostic", "")
            detail = f"（{diagnostic}）" if isinstance(diagnostic, str) and diagnostic else ""
            raise SourcePaused("cookie_expired", f"x1080x 服务端未确认账号登录态{detail}；"
                               "请核对 Cookie、同出口和浏览器会话后更新，本轮暂停")
        if cfg.mode == "disabled" or not cfg.username or not cfg.password:
            raise SourcePaused("login_required", "x1080x 资源需要登录，请在管理页配置账号或手动 Cookie；本轮暂停")
        if cfg.mode in {"auto", "http"}:
            try:
                if self._http_login(client, resource_id):
                    client.authenticated = True
                    return
            except SourcePaused as exc:
                if cfg.mode == "http" or exc.reason in {"authentication_failed", "rate_limited"}:
                    raise
            if cfg.mode == "http":
                raise SourcePaused("browser_required", "x1080x 登录需要浏览器验证，请使用 auto 或 browser 认证模式")
        if self._persistent_login and getattr(client, "supports_persistent_browser", False) is True:
            client.login_with_browser(cfg, resource_id)
        else:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="x1080x-login") as executor:
                cookies, user_agent = executor.submit(self.browser_login, client, cfg, resource_id).result()
            client.install_browser_session(cookies, user_agent)
        if not client.is_authenticated(resource_id):
            raise SourcePaused("authentication_failed", "x1080x 浏览器登录后 HTTP 会话验证未通过，请核对同一出口和浏览器指纹")
        client.authenticated = True

    def _http_login(self, client, resource_id):
        settings = client.api_json("GET", "/api/auth/turnstile/settings")
        if settings.get("enabled") is not False:
            return False
        csrf = client.api_json("GET", "/api/auth/csrf")
        result = client.api_json("POST", "/api/auth/login", data={
            "username": self.settings.username, "login_type": "username", "password": self.settings.password,
        }, csrf=csrf.get("csrf_token"), allow_auth_error=True)
        if result.get("error") in {"invalid_credentials", "invalid_password", "invalid_login"}:
            raise SourcePaused("authentication_failed", "x1080x 账号认证失败，请检查凭据；本轮暂停")
        return client.is_authenticated(resource_id)


def login_in_browser(client, settings, resource_id):
    progress = {"stage": "browser_start"}
    try:
        return asyncio.run(_login_in_browser(client, settings, resource_id, progress))
    except SourcePaused:
        raise
    except ImportError:
        raise SourcePaused("browser_unavailable", "x1080x 自动登录缺少浏览器运行时：请在管理页填写手动 Cookie，"
                           "或使用 WITH_X1080X_BROWSER=true 构建镜像并安装浏览器") from None
    except Exception as exc:
        # 浏览器异常可能包含输入内容、请求参数或 Cookie；只报告异常类型。
        raise SourcePaused("authentication_failed", f"x1080x 无头登录未完成：stage={progress['stage']} "
                           f"{browser_error_hint(exc)}；本轮暂停") from None


def browser_proxy(settings):
    return proxy_options(settings.proxy.url if settings.proxy.enabled else "")


async def _verified_session(page, context, client, resource_id, deadline):
    try:
        return await verified_session(page, context, client.base_url, resource_id, deadline)
    except BrowserError as exc:
        raise SourcePaused(exc.reason, str(exc)) from None


async def _login_in_browser(client, settings, resource_id, progress):
    from invisible_playwright.async_api import InvisiblePlaywright
    async with InvisiblePlaywright(headless=settings.headless, proxy=browser_proxy(client.settings), humanize=True) as browser:
        context = await browser.new_context()
        try:
            await context.add_cookies(client.browser_cookies())
            page = await context.new_page()
            return await login_page(page, context, client.base_url, settings.username, settings.password,
                resource_id, timeout=settings.timeout, progress=lambda stage: progress.update(stage=stage))
        except BrowserError as exc:
            raise SourcePaused(exc.reason, str(exc)) from None
        finally:
            await context.close()
