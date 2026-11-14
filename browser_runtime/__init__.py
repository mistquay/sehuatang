"""可独立部署的浏览器运行层，不加载爬虫配置、数据库或通知。"""

from .config import BrowserSettings, load_browser_settings
from .session import BrowserError, BrowserResponse, BrowserSession


def create_browser(settings, base_url, *, proxy=None, cookies=(), purpose="public", stop_event=None):
    if settings.engine == "remote_firefox":
        from .remote import RemoteBrowserSession
        return RemoteBrowserSession(settings, base_url, proxy=proxy, cookies=cookies,
                                    purpose=purpose, stop_event=stop_event)
    return BrowserSession(settings, base_url, proxy=proxy, cookies=cookies,
                          purpose=purpose, stop_event=stop_event)


__all__ = ["BrowserSettings", "load_browser_settings", "BrowserError", "BrowserResponse",
           "BrowserSession", "create_browser"]
