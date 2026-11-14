"""浏览器和 HTTP 共用的页面识别，不依赖业务运行环境。"""
import re


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
_RATE_LIMIT_MARKERS = ("請求過於頻繁", "请求过于频繁", "訪問受限", "访问受限")
_LEGACY_CHARSET = re.compile(r'charset=["\']?(gbk|gb2312|big5)["\']?', re.I)


def rendered_html_bytes(html):
    # page.content()/验证服务返回 Unicode；保留旧 meta 会让解析器二次按 GBK 解码。
    return _LEGACY_CHARSET.sub('charset="utf-8"', html).encode("utf-8")


def is_cf_blocked(body):
    head = (body or b"")[:16000].lower()
    return b"cloudflare" in head and any(marker in head for marker in (
        b"sorry, you have been blocked", b"cf-error-details", b"error code: 1020",
    ))


def is_cf_challenge(body, status=None):
    if not body or is_cf_blocked(body):
        return False
    head = body[:5000]
    if any(marker in head for marker in (b"cf-challenge", b"__cf_chl", b"challenges.cloudflare.com")):
        return True
    match = _TITLE_RE.search(head)
    title = match.group(1).decode("utf-8", errors="ignore").strip().lower() if match else ""
    return any(word in title for word in ("just a moment", "attention required"))


def is_rate_limited(body):
    if not body or is_cf_blocked(body):
        return False
    text = body[:5000].decode("utf-8", errors="ignore")
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def browser_error_hint(exc):
    message = str(exc).lower()
    hint = "timeout" if isinstance(exc, TimeoutError) else "browser_error"
    for marker, label in (("executable", "executable_unavailable"), ("xvfb", "display_unavailable"),
                          ("shared librar", "library_unavailable"), ("closed", "browser_closed"),
                          ("timeout", "timeout"), ("proxy", "proxy_error")):
        if marker in message:
            hint = label
            break
    return f"error_type={type(exc).__name__} hint={hint}"


async def click_verification(page):
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
