"""每个身份一个拥有线程/事件循环的持久浏览器会话。"""
import asyncio
import base64
import secrets
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .pages import browser_error_hint, click_verification, is_cf_blocked, is_cf_challenge, is_rate_limited, rendered_html_bytes


MAX_BODY_BYTES = 8 * 1024 * 1024


class BrowserError(RuntimeError):
    def __init__(self, reason, stage, hint="", retry_after=0):
        self.reason, self.stage, self.hint = reason, stage, hint
        self.retry_after = retry_after
        super().__init__(f"浏览器未完成: reason={reason} stage={stage} {hint}".strip())


@dataclass(frozen=True)
class BrowserResponse:
    url: str
    body: bytes
    status_code: int
    headers: dict = field(default_factory=dict)
    cookies: list = field(default_factory=list, repr=False)
    user_agent: str = ""

    def wire(self):
        return {"url": self.url, "body": base64.b64encode(self.body).decode("ascii"),
                "status_code": self.status_code, "headers": self.headers,
                "cookies": self.cookies, "user_agent": self.user_agent}

    @classmethod
    def from_wire(cls, value):
        body = value.get("body", "")
        if not isinstance(body, str) or len(body) > (MAX_BODY_BYTES + 2) // 3 * 4:
            raise BrowserError("response_too_large", "response")
        return cls(url=value["url"], body=base64.b64decode(body, validate=True),
                   status_code=int(value["status_code"]), headers=value.get("headers") or {},
                   cookies=value.get("cookies") or [], user_agent=value.get("user_agent") or "")


class BrowserSession:
    def __init__(self, settings, base_url, *, proxy=None, cookies=(), purpose="public", stop_event=None):
        settings.validate()
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("浏览器需要有效的 HTTP(S) 站点地址")
        self.origin = (parsed.scheme, parsed.netloc)
        self.proxy = proxy
        self.purpose = purpose
        self._initial_cookies = list(cookies)
        self._seed = secrets.randbits(31)
        self.stop_event = threading.Event()
        self._parent_stop = stop_event
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crawler-browser")
        self._lock = threading.Lock()
        self._loop = self._stack = self._page = self._context = None
        self._closed = False
        self._stage = "browser_start"
        self.last_used = time.monotonic()

    def _check_url(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self.origin or parsed.username:
            raise ValueError("浏览器只允许当前站点的 HTTP(S) 地址")

    def execute(self, action, **payload):
        if action not in {"render", "request", "login_x1080x", "set_cookies"}:
            raise ValueError("未知浏览器操作")
        if action == "login_x1080x" and self.purpose != "account":
            raise ValueError("只有独立账号会话可以登录")
        if action == "request" and payload.get("method") not in {"GET", "POST"}:
            raise ValueError("浏览器请求只支持 GET/POST")
        if "url" in payload:
            self._check_url(payload["url"])
        with self._lock:
            if self._closed or self._stopping():
                raise BrowserError("browser_stopped", "queue")
            future = self._executor.submit(self._run, action, payload)
        return future.result()

    def render(self, url, *, ready=""):
        return self.execute("render", url=url, ready=ready or self.settings.wait_selector)

    def request(self, method, url, *, data=None, csrf=None):
        if method not in {"GET", "POST"}:
            raise ValueError("浏览器请求只支持 GET/POST")
        return self.execute("request", method=method, url=url, data=data, csrf=csrf)

    def login(self, username, password, resource_id):
        return self.execute("login_x1080x", username=username, password=password, resource_id=resource_id)

    def set_cookies(self, cookies):
        return self.execute("set_cookies", cookies=cookies)

    def _stopping(self):
        return self.stop_event.is_set() or (self._parent_stop is not None and self._parent_stop.is_set())

    def _run(self, action, payload):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        self.last_used = time.monotonic()
        try:
            return self._loop.run_until_complete(self._bounded(action, payload))
        except Exception as exc:
            stage = self._stage
            if isinstance(exc, BrowserError) and exc.reason in {
                    "rate_limited", "cf_blocked", "cf_challenge", "login_required", "response_too_large"}:
                # 站点状态不代表浏览器损坏；保留验证上下文，冷却后继续同一身份。
                raise
            self._loop.run_until_complete(self._reset())
            if isinstance(exc, (BrowserError, ValueError)):
                raise
            reason = "browser_unavailable" if stage == "browser_start" else "browser_timeout" if isinstance(exc, TimeoutError) else "browser_error"
            raise BrowserError(reason, stage, browser_error_hint(exc)) from None
        finally:
            self.last_used = time.monotonic()

    async def _bounded(self, action, payload):
        task = asyncio.create_task(self._execute(action, payload))
        deadline = time.monotonic() + self.settings.timeout
        try:
            while True:
                if self._stopping():
                    raise BrowserError("browser_stopped", self._stage)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                done, _ = await asyncio.wait({task}, timeout=min(0.1, remaining))
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _open(self):
        if self._page is not None:
            return
        self._stage = "browser_start"
        self._stack = AsyncExitStack()
        if self.settings.engine == "obscura":
            from playwright.async_api import async_playwright
            driver = await self._stack.enter_async_context(async_playwright())
            headers = {"Authorization": "Bearer " + self.settings.token} if self.settings.token else None
            browser = await driver.chromium.connect_over_cdp(self.settings.endpoint, headers=headers,
                                                            timeout=int(self.settings.timeout * 1000))
            self._stack.push_async_callback(browser.close)
            # 显式新建非默认 context，不继承服务启动 profile 的账号状态。
            self._context = await browser.new_context()
        else:
            import platform
            import sys
            from invisible_core.download import cache_dir_for_seal
            from invisible_core.seal import active_seal
            from invisible_playwright.async_api import InvisiblePlaywright
            seal = active_seal()
            executable = cache_dir_for_seal(seal) / seal.asset_for(sys.platform, platform.machine()).entry_rel
            if not executable.is_file():
                raise RuntimeError("Firefox executable unavailable; install it before starting the service")
            browser = await self._stack.enter_async_context(InvisiblePlaywright(
                seed=self._seed, binary_path=str(executable), headless=self.settings.headless, proxy=self.proxy, humanize=True,
                locale=self.settings.locale, timezone=self.settings.timezone))
            self._context = await browser.new_context()
        self._stack.push_async_callback(self._context.close)
        if self._initial_cookies:
            await self._context.add_cookies(self._initial_cookies)
        self._page = await self._context.new_page()

    async def _execute(self, action, payload):
        await self._open()
        if action == "render":
            return await self._render(**payload)
        if action == "request":
            return await self._request(**payload)
        if action == "set_cookies":
            await self._context.add_cookies(payload["cookies"])
            return await self._response(self.base_url, b"", 200, {})
        from .x1080x import login_page
        await login_page(self._page, self._context, self.base_url,
                         timeout=self.settings.timeout, progress=self._progress, **payload)
        return await self._response(self._page.url, b"", 200, {})

    def _progress(self, stage):
        self._stage = stage

    async def _render(self, url, ready=""):
        page = self._page
        self._stage = "navigation"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.settings.timeout * 1000))
        status = response.status if response else 200
        headers = await response.all_headers() if response else {}
        self._stage = "verification"
        next_click = 0.0
        challenged = False
        while True:
            self._check_url(page.url)
            body = rendered_html_bytes(await page.content())
            self._check_body(body, status, headers)
            challenge = is_cf_challenge(body, status)
            browser_pass = ready == "x1080x_list" and b'"browserPassRequired"' in body.replace(b'\\"', b'"')
            if ready == "x1080x_list" and await page.locator(".thread-list").count():
                body = rendered_html_bytes(await page.content())
                self._check_url(page.url)
                self._check_body(body, 200 if challenged else status, headers)
                return await self._response(page.url, body, 200 if challenged else status, headers)
            if not challenge and not browser_pass:
                if not ready or ready == "x1080x_list" or status >= 400 or await page.locator(ready).count():
                    if ready and ready != "x1080x_list":
                        body = rendered_html_bytes(await page.content())
                        self._check_url(page.url)
                        self._check_body(body, 200 if challenged else status, headers)
                    return await self._response(page.url, body, 200 if challenged else status, headers)
                await asyncio.sleep(0.1)
                continue
            if self.settings.engine == "obscura" and challenge:
                raise BrowserError("cf_challenge", "verification", "unsupported_interactive_challenge")
            challenged = True
            # 初始 CF 状态不能套用到验证后新加载的文档。
            status = 200
            if self.settings.engine == "firefox" and time.monotonic() >= next_click:
                await click_verification(page)
                next_click = time.monotonic() + 4
            await asyncio.sleep(0.25)

    @staticmethod
    def _check_body(body, status, headers=None):
        if len(body) > MAX_BODY_BYTES:
            raise BrowserError("response_too_large", "response")
        if is_cf_blocked(body):
            raise BrowserError("cf_blocked", "response")
        if is_rate_limited(body) or (status == 429 and not is_cf_challenge(body, status)):
            value = next((v for k, v in (headers or {}).items() if k.lower() == "retry-after"), "0")
            try:
                delay = max(0, float(value))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                    delay = max(0, (parsed - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    delay = 0
            raise BrowserError("rate_limited", "response", retry_after=delay)

    async def _request(self, method, url, data=None, csrf=None):
        # 同源页面中的 fetch 使用浏览器自己的网络与 Cookie，不使用独立 APIRequestContext。
        if (urlsplit(self._page.url).scheme, urlsplit(self._page.url).netloc) != self.origin:
            await self._render(self.base_url + "/")
        self._stage = "authenticated_api"
        if method == "POST" and not csrf:
            cookies = await self._context.cookies(self.base_url)
            csrf = next((item["value"] for item in cookies if item["name"] == "__Host-x1080x_csrf"), None)
            if not csrf:
                raise BrowserError("login_required", "csrf")
        value = await self._page.evaluate("""async ({url, method, data, csrf, limit, timeout}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const headers = {};
                if (method === 'POST') {
                    headers['Content-Type'] = 'application/json';
                    headers['X-CSRFToken'] = csrf;
                }
                const r = await fetch(url, {method, credentials: 'same-origin', headers,
                    redirect: 'error', signal: controller.signal,
                    ...(method === 'POST' ? {body: JSON.stringify(data)} : {})});
                if (Number(r.headers.get('content-length') || 0) > limit) return {too_large: true};
                const bytes = new Uint8Array(await r.arrayBuffer());
                if (bytes.length > limit) return {too_large: true};
                let binary = '';
                for (let i = 0; i < bytes.length; i += 8192)
                    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
                return {url: r.url, status: r.status, headers: Object.fromEntries(r.headers.entries()),
                    body: btoa(binary)};
            } finally { clearTimeout(timer); }
        }""", {"url": url, "method": method, "data": data, "csrf": csrf,
               "limit": MAX_BODY_BYTES, "timeout": int(self.settings.timeout * 1000)})
        if value.get("too_large"):
            raise BrowserError("response_too_large", "response")
        self._check_url(value["url"])
        body = base64.b64decode(value["body"], validate=True)
        self._check_body(body, value["status"], value["headers"])
        if is_cf_challenge(body, value["status"]):
            raise BrowserError("cf_challenge", "authenticated_api")
        return await self._response(value["url"], body, value["status"], value["headers"])

    async def _response(self, url, body, status, headers):
        cookies = await self._context.cookies(self.base_url)
        self._initial_cookies = cookies
        return BrowserResponse(url, body, status, headers, cookies,
                               await self._page.evaluate("navigator.userAgent"))

    async def _reset(self):
        try:
            if self._stack is not None:
                await asyncio.wait_for(self._stack.aclose(), 10)
        except Exception:
            # 清理错误不能覆盖已经脱敏的业务/浏览器错误。
            pass
        finally:
            self._stack = self._page = self._context = None

    def _close(self):
        if self._loop is not None:
            try:
                self._loop.run_until_complete(self._reset())
            finally:
                self._loop.close()
                self._loop = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
            future = self._executor.submit(self._close)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True)
