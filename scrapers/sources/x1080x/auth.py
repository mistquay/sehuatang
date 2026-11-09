"""默认只使用手动 Cookie；显式启用账号模式时才执行独立登录。"""
import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from scrapers.core.contracts import SourcePaused


@dataclass(frozen=True)
class AuthSettings:
    username: str = ""
    password: str = field(default="", repr=False)
    mode: str = "cookie"
    timeout: float = 120
    headless: bool = True

    @classmethod
    def from_config(cls, config):
        raw = config.get("auth") or {}
        mode = str(raw.get("mode", "cookie"))
        if mode not in {"cookie", "auto", "http", "browser", "disabled"}:
            raise ValueError("x1080x.auth.mode 只支持 cookie/auto/http/browser/disabled")
        timeout = float(raw.get("timeout", 120))
        if not 0 < timeout <= 600:
            raise ValueError("x1080x.auth.timeout 必须在 0 到 600 秒之间")
        return cls(os.getenv("CRAWLER_X1080X_USERNAME", raw.get("username", "")),
                   os.getenv("CRAWLER_X1080X_PASSWORD", raw.get("password", "")),
                   mode, timeout, bool(raw.get("headless", True)))


class Authenticator:
    def __init__(self, settings, browser_login=None):
        self.settings = settings
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
        # 不在正在运行的 asyncio loop 中嵌套 asyncio.run；浏览器生命周期独立且仅用于认证。
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


def browser_error_hint(exc):
    """只输出固定诊断标签；浏览器原始异常可能携带 Cookie、密码和 URL 参数。"""
    message = str(exc).lower()
    hint = "timeout" if isinstance(exc, TimeoutError) else "browser_error"
    for marker, label in (("executable", "executable_unavailable"), ("xvfb", "display_unavailable"),
                          ("shared librar", "library_unavailable"), ("closed", "browser_closed"),
                          ("timeout", "timeout"), ("proxy", "proxy_error")):
        if marker in message:
            hint = label
            break
    return f"error_type={type(exc).__name__} hint={hint}"


def browser_proxy(settings):
    if not settings.proxy.enabled:
        return None
    from urllib.parse import unquote
    parsed = urlsplit(settings.proxy.url)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    proxy = {"server": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")}
    if parsed.username:
        proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return proxy


async def _click_verification(page):
    """与 Byparr 相同的可见控件点击方式；不改写页面 token 或跳过服务器校验。"""
    token = page.locator('input[name="cf-turnstile-response"]')
    if await token.count() and await token.first.input_value():
        return
    for depth in range(1, 5):
        widget = page.locator(f'input[name="cf-turnstile-response"] >> xpath=ancestor::div[{depth}]')
        if not await widget.count():
            continue
        box = await widget.first.bounding_box(timeout=1000)
        if box and box["width"] > 40 and 20 < box["height"] < 120:
            await page.mouse.click(box["x"] + 25, box["y"] + box["height"] / 2)
            return


async def _verified_session(page, context, client, resource_id, deadline):
    """页面跳转不是登录证据；必须在浏览器内确认服务端认证状态。"""
    while time.monotonic() < deadline:
        state = await page.evaluate("""async path => {
            const r = await fetch(path, {credentials: 'same-origin'});
            const data = await r.json().catch(() => ({}));
            return {authenticated: data.authenticated === true, status: r.status};
        }""", f"/api/resources/{resource_id}/state")
        if state.get("authenticated"):
            return await context.cookies(client.base_url), await page.evaluate("navigator.userAgent")
        await asyncio.sleep(1)
    raise SourcePaused("authentication_failed", "x1080x 浏览器内登录状态验证未通过，本轮暂停")


async def _login_in_browser(client, settings, resource_id, progress):
    # 与当前 Byparr 3.x 使用同族 Firefox 运行时，避免拿 Firefox CF Cookie 配 Chromium 指纹。
    from invisible_playwright.async_api import InvisiblePlaywright

    proxy = browser_proxy(client.settings)
    deadline = time.monotonic() + settings.timeout
    async with InvisiblePlaywright(headless=settings.headless, proxy=proxy, humanize=True) as browser:
        context = await browser.new_context()
        try:
            await context.add_cookies(client.browser_cookies())
            page = await context.new_page()
            progress["stage"] = "login_page"
            await page.goto(client.base_url + "/login", wait_until="commit",
                            timeout=int(settings.timeout * 1000))
            next_click = 0.0
            progress["stage"] = "login_page_verification"
            while time.monotonic() < deadline:
                if urlsplit(page.url).path == "/profile":
                    progress["stage"] = "session_verification"
                    await page.wait_for_load_state("domcontentloaded")
                    return await _verified_session(page, context, client, resource_id, deadline)
                username = page.locator("#auth-username")
                if await username.count() and await username.is_visible():
                    break
                if time.monotonic() >= next_click:
                    await _click_verification(page)
                    next_click = time.monotonic() + 4
                await asyncio.sleep(0.5)
            else:
                raise SourcePaused("authentication_failed", "x1080x 无头浏览器未通过登录页 CF 验证")
            announcement = page.get_by_role("dialog").filter(has_text="網站升級完工公告")
            progress["stage"] = "login_form"
            if await announcement.count() and await announcement.is_visible():
                await announcement.get_by_role("button", name="關閉對話框").click()
            await page.locator("#auth-username").fill(settings.username)
            await page.locator("#auth-password").fill(settings.password)
            submit = page.locator('.member-form button[type="submit"]')
            progress["stage"] = "login_form_verification"
            while time.monotonic() < deadline:
                if await submit.is_enabled():
                    break
                if time.monotonic() >= next_click:
                    await _click_verification(page)
                    next_click = time.monotonic() + 4
                await asyncio.sleep(0.5)
            else:
                raise SourcePaused("authentication_failed", "x1080x 登录表单 Turnstile 验证未完成")
            progress["stage"] = "login_submit"
            await submit.click()
            await page.wait_for_url(lambda url: urlsplit(str(url)).path != "/login",
                                    timeout=max(1, int((deadline - time.monotonic()) * 1000)))
            progress["stage"] = "session_verification"
            return await _verified_session(page, context, client, resource_id, deadline)
        finally:
            await context.close()
