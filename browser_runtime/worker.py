"""仅依赖 Byparr 已有运行时的持久 Firefox 服务，独立于爬虫 Web 应用。"""
import asyncio
import hmac
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import BrowserSettings
from .session import BrowserError, BrowserSession


class SessionExpired(KeyError):
    pass


class SessionStore:
    def __init__(self, *, capacity=16, idle_seconds=1800, factory=BrowserSession):
        self.capacity, self.idle_seconds, self.factory = capacity, idle_seconds, factory
        self._lock = threading.Lock()
        self._sessions = {}

    def create(self, payload):
        if set(payload) - {"base_url", "proxy", "cookies", "purpose", "timeout", "locale", "timezone"}:
            raise ValueError("unknown session field")
        purpose = payload.get("purpose", "public")
        if purpose not in {"public", "account"}:
            raise ValueError("invalid purpose")
        settings = BrowserSettings(timeout=float(payload.get("timeout", 120)),
                                   locale=payload.get("locale") or "auto", timezone=payload.get("timezone") or "")
        with self._lock:
            if len(self._sessions) >= self.capacity:
                raise BrowserError("browser_capacity", "queue")
            browser = self.factory(settings, payload["base_url"], proxy=payload.get("proxy"),
                                   cookies=payload.get("cookies") or (), purpose=purpose)
            key = uuid.uuid4().hex
            self._sessions[key] = dict(browser=browser, active=False, used=time.monotonic())
        return key

    def execute(self, key, payload):
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                raise SessionExpired(key)
            if entry["active"]:
                raise BrowserError("browser_capacity", "queue")
            entry["active"] = True
        try:
            data = dict(payload)
            action = data.pop("action")
            return entry["browser"].execute(action, **data)
        finally:
            with self._lock:
                entry.update(active=False, used=time.monotonic())

    def close(self, key):
        with self._lock:
            entry = self._sessions.pop(key, None)
        if entry:
            entry["browser"].close()

    def reap(self):
        with self._lock:
            keys = [key for key, entry in self._sessions.items()
                    if not entry["active"] and time.monotonic() - entry["used"] > self.idle_seconds]
            expired = [self._sessions.pop(key) for key in keys]
        for entry in expired:
            entry["browser"].close()

    def shutdown(self):
        with self._lock:
            keys = list(self._sessions)
        for key in keys:
            self.close(key)


def create_app(token=None, store=None):
    token = token if token is not None else os.getenv("BROWSER_WORKER_TOKEN", "")
    store = store or SessionStore()

    @asynccontextmanager
    async def lifespan(app):
        if len(token.encode()) < 32:
            raise RuntimeError("BROWSER_WORKER_TOKEN must contain at least 32 bytes")
        async def reap():
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(store.reap)
        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await asyncio.to_thread(store.shutdown)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "")
            if not token or not hmac.compare_digest(supplied, "Bearer " + token):
                return JSONResponse({"reason": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(BrowserError)
    async def browser_error(request, exc):
        return JSONResponse({"reason": exc.reason, "stage": exc.stage,
                             "retry_after": exc.retry_after}, status_code=503)

    async def payload(request):
        if int(request.headers.get("content-length", 0)) > 1024 * 1024:
            raise HTTPException(413, "request too large")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "request too large")
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise HTTPException(400, "invalid request") from None

    @app.get("/health")
    def health():
        return {"status": "ready", "engine": "firefox"}

    @app.post("/v1/sessions")
    async def create(request: Request):
        try:
            key = await asyncio.to_thread(store.create, await payload(request))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "invalid session configuration") from None
        return {"session_id": key}

    @app.post("/v1/sessions/{key}")
    async def execute(key: str, request: Request):
        try:
            result = await asyncio.to_thread(store.execute, key, await payload(request))
            return result.wire()
        except SessionExpired:
            raise HTTPException(410, "browser session expired") from None
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "invalid browser operation") from None

    @app.delete("/v1/sessions/{key}")
    async def close(key: str):
        await asyncio.to_thread(store.close, key)
        return {"status": "closed"}

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host=os.getenv("BROWSER_WORKER_HOST", "127.0.0.1"),
                port=int(os.getenv("BROWSER_WORKER_PORT", "8192")), access_log=False,
                timeout_graceful_shutdown=15)
