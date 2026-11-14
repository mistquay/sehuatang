"""x1080x 登录操作；由本地或外置浏览器在同一账号 context 中执行。"""
import asyncio
import time
from urllib.parse import quote, urlsplit

from .pages import click_verification, is_cf_blocked, is_rate_limited
from .session import BrowserError


async def verified_session(page, context, base_url, resource_id, deadline):
    while time.monotonic() < deadline:
        state = await page.evaluate("""async path => {
            const r = await fetch(path, {credentials: 'same-origin', redirect: 'error'});
            const data = await r.json().catch(() => ({}));
            return {authenticated: data.authenticated === true, status: r.status};
        }""", f"/api/resources/{quote(str(resource_id), safe='')}/state")
        if state.get("authenticated") and state.get("status") == 200:
            return await context.cookies(base_url), await page.evaluate("navigator.userAgent")
        await asyncio.sleep(1)
    raise BrowserError("authentication_failed", "session_verification")


async def login_page(page, context, base_url, username, password, resource_id, *, timeout, progress):
    if not username or not password:
        raise BrowserError("login_required", "login_form")
    origin = urlsplit(base_url)
    deadline = time.monotonic() + timeout

    async def check_page():
        parsed = urlsplit(page.url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise BrowserError("authentication_failed", "redirect")
        body = (await page.content()).encode("utf-8")
        if is_cf_blocked(body):
            raise BrowserError("cf_blocked", "login_verification")
        if is_rate_limited(body):
            raise BrowserError("rate_limited", "login_verification")

    progress("login_page")
    await page.goto(base_url + "/login", wait_until="commit", timeout=int(timeout * 1000))
    next_click = 0.0
    progress("login_page_verification")
    while time.monotonic() < deadline:
        await check_page()
        if urlsplit(page.url).path == "/profile":
            progress("session_verification")
            await page.wait_for_load_state("domcontentloaded")
            return await verified_session(page, context, base_url, resource_id, deadline)
        username_input = page.locator("#auth-username")
        if await username_input.count() and await username_input.is_visible():
            break
        if time.monotonic() >= next_click:
            await click_verification(page)
            next_click = time.monotonic() + 4
        await asyncio.sleep(0.5)
    else:
        raise BrowserError("authentication_failed", "login_page_verification", "hint=timeout")

    progress("login_form")
    announcement = page.get_by_role("dialog").filter(has_text="網站升級完工公告")
    if await announcement.count() and await announcement.is_visible():
        await announcement.get_by_role("button", name="關閉對話框").click()
    await page.locator("#auth-username").fill(username)
    await page.locator("#auth-password").fill(password)
    submit = page.locator('.member-form button[type="submit"]')
    progress("login_form_verification")
    while time.monotonic() < deadline:
        await check_page()
        if await submit.is_enabled():
            break
        if time.monotonic() >= next_click:
            await click_verification(page)
            next_click = time.monotonic() + 4
        await asyncio.sleep(0.5)
    else:
        raise BrowserError("authentication_failed", "login_form_verification", "hint=timeout")
    progress("login_submit")
    await submit.click()
    await page.wait_for_url(lambda url: urlsplit(str(url)).path != "/login",
                            timeout=max(1, int((deadline - time.monotonic()) * 1000)))
    await check_page()
    progress("session_verification")
    return await verified_session(page, context, base_url, resource_id, deadline)
