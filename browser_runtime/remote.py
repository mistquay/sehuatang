"""外置 Firefox 客户端；仅传输规定操作，不提供任意脚本执行接口。"""
import threading
import time

from .pages import browser_error_hint
from .session import BrowserError, BrowserResponse


class RemoteBrowserSession:
    def __init__(self, settings, base_url, *, proxy=None, cookies=(), purpose="public", stop_event=None):
        from curl_cffi import requests
        settings.validate()
        self.settings, self.base_url, self.purpose = settings, base_url, purpose
        self.stop_event = threading.Event()
        self._parent_stop = stop_event
        self._lock = threading.RLock()
        self._http = requests.Session(trust_env=False)
        self._session_id = None
        self._closed = False
        self._initial = {"base_url": base_url, "proxy": proxy, "cookies": list(cookies),
                         "purpose": purpose, "timeout": settings.timeout,
                         "locale": settings.locale, "timezone": settings.timezone}
        self.last_used = time.monotonic()

    def _send(self, method, path, payload=None):
        try:
            response = self._http.request(method, self.settings.endpoint.rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.settings.token}, json=payload,
                timeout=self.settings.timeout + 15, allow_redirects=False)
            value = response.json()
        except Exception as exc:
            raise BrowserError("browser_unavailable", "remote_transport", browser_error_hint(exc)) from None
        if response.status_code == 410:
            # 服务重启/过期后不自动重放刚才的登录、POST 或领取操作。
            self._session_id = None
            raise BrowserError("browser_session_expired", "remote_session")
        if response.status_code in {401, 403}:
            raise BrowserError("browser_auth_failed", "remote_transport")
        if response.status_code != 200:
            known = {"browser_unavailable", "browser_timeout", "browser_error", "browser_stopped",
                     "cf_challenge", "cf_blocked", "rate_limited", "authentication_failed",
                     "login_required", "response_too_large", "browser_capacity"}
            reason = value.get("reason")
            stages = {"browser_start", "navigation", "verification", "login_page", "login_form", "login_submit",
                      "login_page_verification", "login_form_verification", "session_verification",
                      "authenticated_api", "csrf", "response", "queue", "redirect", "login_verification"}
            stage = value.get("stage")
            raise BrowserError(reason if reason in known else "browser_unavailable",
                               stage if stage in stages else "remote_operation",
                               retry_after=float(value.get("retry_after") or 0))
        return value

    def execute(self, action, **payload):
        if action not in {"render", "request", "login_x1080x", "set_cookies"}:
            raise ValueError("未知浏览器操作")
        with self._lock:
            if self._closed or self.stop_event.is_set() or (self._parent_stop is not None and self._parent_stop.is_set()):
                raise BrowserError("browser_stopped", "queue")
            if self._session_id is None:
                self._session_id = self._send("POST", "/v1/sessions", self._initial)["session_id"]
                # 重建时只使用首次提供的状态，不复制其他身份的 Cookie。
            self.last_used = time.monotonic()
            value = self._send("POST", f"/v1/sessions/{self._session_id}", {"action": action, **payload})
            try:
                result = BrowserResponse.from_wire(value)
            except (KeyError, ValueError, TypeError):
                raise BrowserError("browser_unavailable", "remote_response") from None
            self._initial["cookies"] = result.cookies
            return result

    def render(self, url, *, ready=""):
        return self.execute("render", url=url, ready=ready or self.settings.wait_selector)

    def request(self, method, url, *, data=None, csrf=None):
        return self.execute("request", method=method, url=url, data=data, csrf=csrf)

    def login(self, username, password, resource_id):
        return self.execute("login_x1080x", username=username, password=password, resource_id=resource_id)

    def set_cookies(self, cookies):
        return self.execute("set_cookies", cookies=cookies)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
            try:
                if self._session_id:
                    self._send("DELETE", f"/v1/sessions/{self._session_id}")
            except BrowserError:
                pass
            finally:
                self._http.close()
