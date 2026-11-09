"""同一线路复用公开列表的浏览器 context；不导入或回传账号 Cookie。"""
import asyncio
import importlib.util
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

from scrapers.core.cf_challenge import is_cf_blocked, is_cf_challenge, is_rate_limited
from scrapers.core.contracts import SourcePaused
from .auth import _click_verification, browser_error_hint, browser_proxy


class ListBrowser:
    @staticmethod
    def available():
        return importlib.util.find_spec("invisible_playwright") is not None

    def __init__(self, settings, base_url, timeout=120):
        self.settings = settings
        self.origin = urlsplit(base_url).netloc
        self.timeout = timeout
        # Playwright 对象的创建、调用和关闭必须始终在同一线程/事件循环。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x1080x-list-browser")
        self._loop = None
        self._stack = None
        self._page = None
        self._stage = "browser_start"

    def render(self, url):
        parsed = urlsplit(url)
        if parsed.netloc != self.origin or not parsed.path.startswith("/boards/"):
            raise ValueError("公开列表浏览器只允许当前站点的板块地址")
        return self._executor.submit(self._run, url).result()

    def _run(self, url):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        try:
            return self._loop.run_until_complete(asyncio.wait_for(self._render(url), self.timeout))
        except Exception as exc:
            stage = self._stage
            self._loop.run_until_complete(self._reset())
            if isinstance(exc, SourcePaused):
                raise
            reason = "browser_unavailable" if stage == "browser_start" else "browser_pass_required"
            raise SourcePaused(reason, f"x1080x 列表浏览器未完成：stage={stage} "
                               f"{browser_error_hint(exc)}") from None

    async def _render(self, url):
        if self._page is None:
            self._stage = "browser_start"
            from invisible_playwright.async_api import InvisiblePlaywright
            self._stack = AsyncExitStack()
            browser = await self._stack.enter_async_context(InvisiblePlaywright(
                headless=True, proxy=browser_proxy(self.settings), humanize=True))
            context = await browser.new_context()
            self._stack.push_async_callback(context.close)
            self._page = await context.new_page()
        page = self._page
        self._stage = "navigation"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        self._stage = "verification"
        next_click = 0.0
        while True:
            body = (await page.content()).encode("utf-8")
            if is_cf_blocked(body):
                raise SourcePaused("cf_blocked", "x1080x 列表浏览器被 Cloudflare 明确封锁")
            if is_rate_limited(body) or (response and response.status == 429
                                          and not is_cf_challenge(body, 429)):
                raise SourcePaused("rate_limited", "x1080x 列表浏览器请求命中限流")
            # goto 的初始状态可能属于 CF 挑战，不能套用到验证后新加载的文档。
            response = None
            if urlsplit(page.url).netloc != self.origin:
                raise SourcePaused("browser_pass_required", "x1080x 列表浏览器跳转到其他站点，本轮暂停")
            if await page.locator(".thread-list").count():
                return body
            # 初始 BrowserPass 壳页会异步加载列表；普通异常页交回解析器校验。
            if not is_cf_challenge(body, 200) and b'"browserPassRequired"' not in body.replace(b'\\"', b'"'):
                return body
            if time.monotonic() >= next_click:
                await _click_verification(page)
                next_click = time.monotonic() + 4
            await asyncio.sleep(0.25)

    async def _reset(self):
        try:
            if self._stack is not None:
                await self._stack.aclose()
        finally:
            self._stack = self._page = None

    def _close(self):
        if self._loop is not None:
            try:
                self._loop.run_until_complete(self._reset())
            finally:
                self._loop.close()
                self._loop = None

    def close(self):
        try:
            self._executor.submit(self._close).result()
        finally:
            self._executor.shutdown(wait=True)
