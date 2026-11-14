import os
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from browser_runtime.config import BrowserSettings, load_browser_settings


_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


@dataclass(frozen=True)
class RetrySettings:
    attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 15.0
    jitter: float = 0.3
    statuses: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)


@dataclass(frozen=True)
class ProxySettings:
    enabled: bool = False
    url: str = ""
    urls: tuple[str, ...] = ()

    @property
    def addresses(self):
        return (self.urls or (self.url,)) if self.enabled else ("",)

    def validate(self) -> None:
        if not self.enabled:
            return
        for url in self.addresses:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in _ALLOWED_PROXY_SCHEMES or not parsed.hostname:
                raise ValueError("启用代理时必须提供有效的 http/https/socks5/socks5h URL")
            parsed.port  # 校验显式端口；HTTP(S) 可使用协议默认端口。
        if len(set(self.addresses)) != len(self.addresses):
            raise ValueError("代理地址不能重复")


@dataclass(frozen=True)
class HttpSettings:
    concurrency: int = 4
    timeout: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
    impersonate: str = "chrome110"
    proxy: ProxySettings = field(default_factory=ProxySettings)
    retry: RetrySettings = field(default_factory=RetrySettings)
    min_interval_seconds: float = 0.0
    cooldown_seconds: float = 60.0
    max_cooldown_seconds: float = 900.0
    site_interval_seconds: float = 0.0
    solver_url: str = ""
    solver_provider: str = "byparr"
    per_proxy_concurrency: int = 1
    browser: BrowserSettings = field(default_factory=BrowserSettings)

    def validate(self) -> None:
        if not all(math.isfinite(value) for value in (self.timeout, self.min_interval_seconds,
                self.site_interval_seconds, self.cooldown_seconds, self.max_cooldown_seconds)):
            raise ValueError("HTTP 时间设置必须为有限数值")
        if min(self.min_interval_seconds, self.site_interval_seconds) < 0:
            raise ValueError("请求间隔不能为负数")
        if not 0 < self.cooldown_seconds <= self.max_cooldown_seconds:
            raise ValueError("冷却时间必须为正且不超过最大冷却时间")
        if self.solver_provider not in {"byparr", "flaresolverr"}:
            raise ValueError("challenge.provider 只支持 byparr 或 flaresolverr")
        if self.concurrency < 1:
            raise ValueError("concurrency 必须大于等于 1")
        if type(self.per_proxy_concurrency) is not int or self.per_proxy_concurrency < 1:
            raise ValueError("per_proxy_concurrency 必须为大于等于 1 的整数")
        if self.timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if self.retry.attempts < 1:
            raise ValueError("retry.attempts 必须大于等于 1")
        if self.retry.base_delay < 0 or self.retry.max_delay < 0:
            raise ValueError("retry delay 不能为负数")
        if not 0 <= self.retry.jitter <= 1:
            raise ValueError("retry.jitter 必须位于 0 到 1 之间")
        self.proxy.validate()
        self.browser.validate()
        if self.browser.engine != "firefox":
            for proxy_url in self.proxy.addresses:
                self.browser.endpoint_for(proxy_url)


def _merge_dict(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class StorageSettings:
    batch_size: int = 10
    flush_interval_seconds: float = 1.0
    queue_capacity: int = 100
    retry_attempts: int = 3
    retry_delay_seconds: float = 1.0

    def validate(self):
        for name in ("batch_size", "queue_capacity", "retry_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"storage.{name} 必须为正整数")
        if not math.isfinite(self.flush_interval_seconds) or self.flush_interval_seconds <= 0:
            raise ValueError("storage.flush_interval_seconds 必须为有限正数")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("storage.retry_delay_seconds 必须为有限非负数")


def load_storage_settings(config, source):
    crawler = config.get("crawler") or {}
    raw = dict((crawler.get("defaults") or {}).get("storage") or {})
    raw.update((config.get(source) or {}).get("storage") or {})
    raw.update(((crawler.get("sources") or {}).get(source) or {}).get("storage") or {})
    settings = StorageSettings(**raw)
    settings.validate()
    return settings


def _legacy_source_settings(config: Mapping[str, Any], source: str) -> Dict[str, Any]:
    source_config = dict(config.get(source) or {})
    if source == "javbee":
        return {
            "concurrency": source_config.get("concurrent_workers", 6),
            "http": {
                "timeout": source_config.get("request_timeout", 20),
                "user_agent": source_config.get("user_agent"),
                "proxy": {
                    "enabled": source_config.get("proxy_enable", False),
                    "url": source_config.get("proxy_url", ""),
                },
                "retry": {
                    "attempts": source_config.get("retry_attempts", 3),
                },
            },
        }

    http_config = dict(config.get("http_client") or {})
    proxy_config = dict(config.get("proxy") or {})
    browser_config = dict(config.get("browser") or {})
    return {
        "concurrency": http_config.get("concurrent_workers", 6),
        "http": {
            "timeout": http_config.get("request_timeout", 15),
            "user_agent": browser_config.get("user_agent"),
            "proxy": {
                "enabled": proxy_config.get("proxy_enable", False),
                "url": proxy_config.get("proxy_url") or proxy_config.get("proxy_host", ""),
            },
            "retry": {
                "attempts": http_config.get("retry_attempts", 3),
            },
        },
    }


def _env_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无效布尔环境变量值: {value}")


def load_source_settings(
    config: Mapping[str, Any],
    source: str,
    environ: Mapping[str, str] = os.environ,
) -> HttpSettings:
    crawler = dict(config.get("crawler") or {})
    defaults = dict(crawler.get("defaults") or {})
    configured_sources = dict(crawler.get("sources") or {})
    explicit = dict(configured_sources.get(source) or {})
    raw = _merge_dict(_legacy_source_settings(config, source), defaults)
    raw = _merge_dict(raw, explicit)
    prefix = f"CRAWLER_{source.upper()}_"
    env_override: Dict[str, Any] = {}
    if f"{prefix}CONCURRENCY" in environ:
        env_override["concurrency"] = int(environ[f"{prefix}CONCURRENCY"])
    if f"{prefix}PER_PROXY_CONCURRENCY" in environ:
        env_override["per_proxy_concurrency"] = int(environ[f"{prefix}PER_PROXY_CONCURRENCY"])
    http_override: Dict[str, Any] = {}
    if f"{prefix}TIMEOUT" in environ:
        http_override["timeout"] = float(environ[f"{prefix}TIMEOUT"])
    proxy_override: Dict[str, Any] = {}
    if f"{prefix}PROXY_ENABLED" in environ:
        proxy_override["enabled"] = _env_bool(environ[f"{prefix}PROXY_ENABLED"])
    if f"{prefix}PROXY_URL" in environ:
        proxy_override["url"] = environ[f"{prefix}PROXY_URL"]
        proxy_override["urls"] = []
    if f"{prefix}PROXY_URLS" in environ:
        proxy_override["urls"] = json.loads(environ[f"{prefix}PROXY_URLS"])
    if proxy_override:
        http_override["proxy"] = proxy_override
    retry_override: Dict[str, Any] = {}
    if f"{prefix}RETRY_ATTEMPTS" in environ:
        retry_override["attempts"] = int(environ[f"{prefix}RETRY_ATTEMPTS"])
    if retry_override:
        http_override["retry"] = retry_override
    if http_override:
        env_override["http"] = http_override
    raw = _merge_dict(raw, env_override)
    http = dict(raw.get("http") or {})
    proxy = dict(http.get("proxy") or raw.get("proxy") or {})
    retry = dict(http.get("retry") or {})
    rate = dict(raw.get("rate_limit") or {})
    challenge = dict(raw.get("challenge") or {})
    urls = proxy.get("urls") or []
    if not isinstance(urls, (list, tuple)) or any(not isinstance(url, str) for url in urls):
        raise ValueError("http.proxy.urls 必须为代理地址列表")

    settings = HttpSettings(
        concurrency=int(raw.get("concurrency", 4)),
        per_proxy_concurrency=raw.get("per_proxy_concurrency", 1),
        timeout=float(http.get("timeout", 30)),
        user_agent=str(http.get("user_agent") or HttpSettings.user_agent),
        impersonate=str(http.get("impersonate") or "chrome110"),
        proxy=ProxySettings(
            enabled=bool(proxy.get("enabled", False)),
            url=str(proxy.get("url") or ""),
            urls=tuple(url.strip() for url in urls),
        ),
        min_interval_seconds=float(rate.get("min_interval_seconds", 2 if source == "x1080x" else 0)),
        cooldown_seconds=float(rate.get("cooldown_seconds", 60)),
        max_cooldown_seconds=float(rate.get("max_cooldown_seconds", 900)),
        site_interval_seconds=float(rate.get("site_interval_seconds", 0)),
        solver_url=str(environ.get(f"{prefix}FLARESOLVERR_URL") or challenge.get("flaresolverr_url")
                       or (config.get("http_client", {}).get("flaresolverr_url") if source == "sehuatang" else "") or ""),
        solver_provider=str(challenge.get("provider", "byparr")),
        browser=load_browser_settings(raw.get("browser"), environ=environ, prefix=prefix),
        retry=RetrySettings(
            attempts=int(retry.get("attempts", 3)),
            base_delay=float(retry.get("base_delay", 2)),
            max_delay=float(retry.get("max_delay", 15)),
            jitter=float(retry.get("jitter", 0.3)),
            statuses=tuple(
                int(status)
                for status in retry.get(
                    "statuses",
                    RetrySettings.statuses,
                )
            ),
        ),
    )
    settings.validate()
    return settings
