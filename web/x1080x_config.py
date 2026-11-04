"""x1080x 专用配置表单；持久化仍使用项目现有 YAML 文件。"""
import os
from copy import deepcopy

from scrapers.core.config import load_source_settings
from scrapers.registry import _source_config

MODES = {"cookie", "auto", "http", "browser", "disabled"}
FIELDS = {"mode", "username", "password", "cookie", "user_agent", "impersonate"}


def _mapping(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是映射")
    return value


def _child(parent, key, label):
    value = _mapping(parent.get(key), label)
    parent[key] = value
    return value


def form_state(config):
    raw = _source_config(config, "x1080x")
    auth = _mapping(raw.get("auth"), "x1080x.auth")
    http = load_source_settings({"x1080x": raw, "crawler": {
        "defaults": (config.get("crawler") or {}).get("defaults") or {},
        "sources": {"x1080x": raw}}}, "x1080x")
    return {
        "mode": auth.get("mode", "cookie"),
        "username": auth.get("username", ""),
        "password_set": bool(auth.get("password")),
        "cookie_set": bool(raw.get("cookie")),
        "user_agent": http.user_agent,
        "impersonate": http.impersonate,
        "environment_overrides": [name for name in ("username", "password", "cookie")
                                  if f"CRAWLER_X1080X_{name.upper()}" in os.environ],
    }


def update_form(config, payload):
    if set(payload) - FIELDS:
        raise ValueError("包含不支持的 x1080x 配置字段")
    if any(not isinstance(value, str) for value in payload.values()):
        raise ValueError("x1080x 表单字段必须是字符串")
    if "mode" in payload and payload["mode"] not in MODES:
        raise ValueError("不支持的登录模式")
    for name in ("cookie", "user_agent", "impersonate", "username"):
        if any(char in payload.get(name, "") for char in "\r\n\x00"):
            raise ValueError(f"{name} 不能包含换行或空字符")
    if "impersonate" in payload:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        from typing import get_args
        if payload["impersonate"] not in get_args(BrowserTypeLiteral):
            raise ValueError("不支持的 HTTP 浏览器类型")
    if "user_agent" in payload and not payload["user_agent"].strip():
        raise ValueError("User-Agent 不能为空")
    if payload.get("cookie"):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie = SimpleCookie()
            cookie.load(payload["cookie"])
            if not cookie:
                raise ValueError("请填写请求头中完整的 Cookie 内容")
        except CookieError:
            raise ValueError("Cookie 格式无效") from None

    result = deepcopy(config)
    raw = _source_config(result, "x1080x")
    auth = dict(_mapping(raw.get("auth"), "x1080x.auth"))
    root = _child(result, "x1080x", "x1080x")
    for name in ("mode", "username", "password"):
        if name in payload:
            auth[name] = payload[name].strip() if name == "username" else payload[name]
    auth.setdefault("mode", "cookie")
    root["auth"] = auth
    root["cookie"] = (payload.get("cookie", raw.get("cookie")) or "").strip()
    # 统一保存认证到 x1080x，消除来源覆盖层里同名旧值，以便清空确实生效。
    crawler = _child(result, "crawler", "crawler")
    sources = _child(crawler, "sources", "crawler.sources")
    source = _child(sources, "x1080x", "crawler.sources.x1080x")
    source.pop("auth", None)
    source.pop("cookie", None)
    http = _child(source, "http", "crawler.sources.x1080x.http")
    for name in ("user_agent", "impersonate"):
        if name in payload:
            http[name] = payload[name].strip()
    return result
