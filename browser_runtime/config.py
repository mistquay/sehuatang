"""HTTP/CDP 与远程 Firefox 的显式配置。"""
import math
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class BrowserSettings:
    mode: str = "http"
    engine: str = "firefox"
    endpoint: str = ""
    proxy_url: str = ""
    endpoints: tuple[tuple[str, str], ...] = ()
    token: str = field(default="", repr=False)
    timeout: float = 120
    wait_selector: str = ""
    locale: str = "auto"
    timezone: str = ""
    headless: bool = True

    def validate(self):
        if self.mode not in {"http", "browser"}:
            raise ValueError("browser.mode 只支持 http/browser")
        if self.engine not in {"firefox", "obscura", "remote_firefox"}:
            raise ValueError("browser.engine 只支持 firefox/obscura/remote_firefox")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 600:
            raise ValueError("browser.timeout 必须在 0 到 600 秒之间")
        if self.engine == "firefox" and (self.endpoint or self.endpoints):
            raise ValueError("本地 firefox 不使用 endpoint；外置 Firefox 请使用 remote_firefox")
        for endpoint in [self.endpoint, *(url for _, url in self.endpoints)]:
            if not endpoint:
                continue
            parsed = urlsplit(endpoint)
            schemes = {"http", "https"} if self.engine == "remote_firefox" else {"ws", "wss", "http", "https"}
            if parsed.scheme not in schemes or not parsed.hostname or parsed.username:
                raise ValueError("browser.endpoint 必须是有效服务地址；认证通过 token 提供")
            parsed.port
        if self.engine != "firefox" and not (self.endpoint or self.endpoints):
            raise ValueError("远程浏览器必须配置 browser.endpoint 或 endpoints")
        if self.engine == "remote_firefox" and not self.token:
            raise ValueError("remote_firefox 必须配置 browser.token 或对应环境变量")

    def endpoint_for(self, proxy_url):
        if self.endpoints:
            mapping = dict(self.endpoints)
            if proxy_url not in mapping:
                raise ValueError("当前代理线路没有对应 browser.endpoints 配置")
            return mapping[proxy_url]
        if self.engine == "obscura" and proxy_url != self.proxy_url:
            raise ValueError("Obscura 服务固定代理必须与 browser.proxy_url 及当前线路一致")
        return self.endpoint


def load_browser_settings(raw=None, *, environ=None, prefix=""):
    raw = dict(raw or {})
    for name in ("mode", "engine", "endpoint", "proxy_url", "token", "timeout", "wait_selector", "locale", "timezone"):
        key = f"{prefix}BROWSER_{name.upper()}"
        if environ is not None and key in environ:
            raw[name] = environ[key]
    endpoints = raw.get("endpoints") or {}
    if not isinstance(endpoints, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              or not v for k, v in endpoints.items()):
        raise ValueError("browser.endpoints 必须为代理地址到浏览器服务地址的映射；直连键为空字符串")
    settings = BrowserSettings(
        mode=str(raw.get("mode", "http")), engine=str(raw.get("engine", "firefox")),
        endpoint=str(raw.get("endpoint") or ""), proxy_url=str(raw.get("proxy_url") or ""),
        endpoints=tuple(sorted(endpoints.items())), token=str(raw.get("token") or ""),
        timeout=float(raw.get("timeout", 120)),
        wait_selector=str(raw.get("wait_selector") or ""),
        locale=str(raw.get("locale") or "auto"), timezone=str(raw.get("timezone") or ""),
        headless=bool(raw.get("headless", True)),
    )
    settings.validate()
    return settings


def proxy_options(url):
    if not url:
        return None
    parsed = urlsplit(url)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    value = {"server": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")}
    if parsed.username:
        value.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return value
