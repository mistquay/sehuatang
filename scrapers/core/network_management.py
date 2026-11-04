"""从当前配置建立检测用公共会话池；不实例化抓取任务或访问数据库。"""
import os

from .config import load_source_settings
from .pool import shared_pool, network_snapshot


def configured_pools(config):
    from scrapers.registry import _source_config, source_registry
    from scrapers.http_client import shared_http_client
    from scrapers.sources.x1080x.source import DEFAULT_BASE_URL

    pools = {}
    for source in source_registry.names():
        # 仍展示显式配置的未启用来源，方便启动采集前检测。
        if source not in config and source not in ((config.get("crawler") or {}).get("sources") or {}):
            continue
        raw = _source_config(config, source)
        if source == "sehuatang":
            target = "https://" + str(config.get("sehuatang", {}).get("domain_name") or "sehuatang.org")
            pool = shared_http_client()
        else:
            target = str(raw.get("base_url") or ("https://javbee.co" if source == "javbee" else DEFAULT_BASE_URL)).rstrip("/")
            if source == "x1080x":
                target = os.getenv("CRAWLER_X1080X_BASE_URL", "").strip() or target
            settings = load_source_settings({source: raw, "crawler": {
                "defaults": (config.get("crawler") or {}).get("defaults") or {},
                "sources": {source: raw}}}, source)
            if source == "x1080x":
                from scrapers.sources.x1080x.http_client import shared_http_client as x1080x_http_client
                endpoint = (os.getenv("CRAWLER_X1080X_FLARESOLVERR_URL")
                            or (raw.get("challenge") or {}).get("flaresolverr_url") or "").strip()
                pool = x1080x_http_client(settings, endpoint, target,
                    config={**raw, "cookie": os.getenv("CRAWLER_X1080X_COOKIE", raw.get("cookie", ""))})
            else:
                pool = shared_pool(source, settings, target)
        for lane in pool.lanes:
            lane.network.register_target(target)
        pools[source] = (pool, target)
    return pools


def configured_snapshot(config):
    return network_snapshot([pool for pool, _ in configured_pools(config).values()])


def check_configured_network(config, source=None, proxy_slot=None):
    if source is not None and not isinstance(source, str):
        raise ValueError("来源必须为字符串")
    pools = configured_pools(config)
    if source is not None and source not in pools:
        raise ValueError("来源未配置或不存在")
    if proxy_slot is not None and (source is None or type(proxy_slot) is not int or proxy_slot < 0):
        raise ValueError("检测单条线路需要有效的来源和代理编号")
    selected = [pools[source]] if source is not None else pools.values()
    queued = sum(pool.check_network(target, proxy_slot) for pool, target in selected)
    return {"queued": queued}
