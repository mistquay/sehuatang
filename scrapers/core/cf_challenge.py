"""Cloudflare 挑战检测与 FlareSolverr 过盾客户端。

过盾成功后调用方应缓存 solution 里的 Cookie 和 User-Agent，
后续请求直连复用，仅在 Cookie 失效重新触发挑战时再次过盾。
"""
import threading
from typing import Iterable, Mapping, Optional

from curl_cffi import requests

from util.log_util import log
from browser_runtime.pages import is_cf_blocked, is_cf_challenge, is_rate_limited, rendered_html_bytes

# 同一个过盾服务实例串行处理，不同端点互不阻塞。
_SOLVE_LOCKS = {}
_SOLVE_LOCKS_LOCK = threading.Lock()


def solver_lock(endpoint):
    with _SOLVE_LOCKS_LOCK:
        return _SOLVE_LOCKS.setdefault(endpoint, threading.Lock())

CF_STATUS = (403, 429, 503)


class SiteRateLimited(Exception):
    pass


class SiteBlocked(Exception):
    """服务端明确拒绝访问，继续解题或冷却不能视为可恢复挑战。"""


class FlareSolverrClient:
    """封装 FlareSolverr request.get，返回 (html_bytes, cookies, user_agent)。"""

    def __init__(
        self,
        endpoint: str,
        *,
        proxy_url: Optional[str] = None,
        max_timeout_ms: int = 120000,
        request_timeout: Optional[float] = None,
        raise_on_rate_limit: bool = False,
        source: str = "system",
        provider: str = "flaresolverr",
        request_guard=None,
    ):
        self.endpoint = endpoint.strip().rstrip("/")
        self.log = log.bind(module=source)
        self.proxy_url = proxy_url or None
        if provider not in {"byparr", "flaresolverr"}:
            raise ValueError("未知过盾 provider")
        self.provider = provider
        self.request_guard = request_guard
        self.raise_on_rate_limit = raise_on_rate_limit
        self.max_timeout_ms = int(max_timeout_ms)
        # 未显式指定时跟随解题预算，另留 30s 网络往返余量
        self.request_timeout = (
            float(request_timeout)
            if request_timeout is not None
            else self.max_timeout_ms / 1000 + 30
        )

    def solve(
        self,
        url: str,
        cookies: Optional[Mapping[str, str]] = None,
    ) -> Optional[tuple[bytes, list, Optional[str]]]:
        with solver_lock(self.endpoint):
            if self.request_guard is not None:
                self.request_guard()
            return self._solve_locked(url, cookies)

    def _solve_locked(
        self,
        url: str,
        cookies: Optional[Mapping[str, str]] = None,
    ) -> Optional[tuple[bytes, list, Optional[str]]]:
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self.max_timeout_ms,
            "cookies": [
                {"name": name, "value": value}
                for name, value in (cookies or {}).items()
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.proxy_url and self.provider == "byparr":
            headers["X-Proxy-Server"] = self.proxy_url
        elif self.proxy_url:
            payload["proxy"] = {"url": self.proxy_url}
        try:
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.request_timeout,
            )
            solution = (response.json() or {}).get("solution") or {}
        except Exception as exc:
            self.log.error(f"FlareSolverr 请求失败: endpoint={self.endpoint} error={exc}")
            return None

        response_body = (solution.get("response") or "").encode("utf-8")
        if is_cf_blocked(response_body):
            raise SiteBlocked("验证服务返回 Cloudflare 明确封锁页")
        if self.raise_on_rate_limit and (is_rate_limited(response_body)
                or (solution.get("status") == 429 and not is_cf_challenge(response_body, 200))):
            raise SiteRateLimited("FlareSolverr 返回站点限流响应")

        if solution.get("status") != 200:
            self.log.warning(
                "FlareSolverr 返回异常: "
                f"status={solution.get('status')} url={url}"
            )
            return None

        html = solution.get("response") or ""
        # lxml/bs4 会信任 meta charset；FlareSolverr 返回的已是 unicode 文本，
        # 统一改写为 utf-8 避免二次解码错乱。
        body = rendered_html_bytes(html)
        if is_cf_challenge(body, 200):
            self.log.warning(f"FlareSolverr 过盾后仍是挑战页: url={url}")
            return None
        return body, list(solution.get("cookies") or []), solution.get("userAgent")


def merge_solution_cookies(
    cookie_store: dict,
    solution_cookies: Iterable[Mapping],
) -> None:
    """把 FlareSolverr solution 的 Cookie 合并进调用方的缓存字典。"""
    for cookie in solution_cookies or []:
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if name and value is not None:
            cookie_store[name] = str(value)
