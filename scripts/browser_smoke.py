"""通过真实执行器检查本地 fixture；不访问业务站点、不读取项目私有配置。"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_runtime import BrowserSettings, create_browser


BINARY = bytes(range(256)) * 32


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="text/html", headers=None):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/resources/") or self.path == "/api/state":
            cookies = self.headers.get("Cookie", "")
            self.reply(200, json.dumps({"authenticated": "authenticated=yes" in cookies,
                                       "identity": "two" if "identity=two" in cookies else "one"}), "application/json")
        elif self.path == "/api/file":
            self.reply(200, BINARY, "application/x-bittorrent")
        elif self.path == "/login":
            self.reply(200, """<html><body><form class="member-form">
                <input id="auth-username"><input id="auth-password" type="password">
                <button type="submit">Login</button></form><script>
                document.querySelector('form').addEventListener('submit', async e => {
                    e.preventDefault(); await fetch('/finish-login', {method:'POST'});
                    location.href='/profile';
                });</script></body></html>""")
        else:
            self.reply(200, """<html><body><h1 id="value">loading</h1><script>
                fetch('/api/state').then(r=>r.json()).then(state=>{
                    document.querySelector('#value').textContent = state.identity;
                    document.body.setAttribute('data-ready','true');
                });</script></body></html>""")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/finish-login":
            self.reply(200, "{}", "application/json", {"Set-Cookie": "authenticated=yes; Path=/; HttpOnly"})
        else:
            self.reply(200, raw or b"{}", "application/json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["firefox", "obscura", "remote_firefox"], default="firefox")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--fixture-origin", default="", help="远程容器访问本 fixture 的地址；本机测试留空")
    parser.add_argument("--fixture-port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0" if args.fixture_origin else "127.0.0.1", args.fixture_port), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = args.fixture_origin or f"http://127.0.0.1:{server.server_port}"
    settings = BrowserSettings(engine=args.engine, endpoint=args.endpoint,
        token=os.getenv("BROWSER_WORKER_TOKEN", "") if args.engine == "remote_firefox" else os.getenv("OBSCURA_CDP_TOKEN", ""),
        timeout=60, wait_selector='body[data-ready="true"]', locale="en-US", timezone="UTC")
    sessions = []
    started = time.monotonic()
    try:
        for identity in ("one", "two"):
            browser = create_browser(settings, origin, purpose="account", cookies=[
                {"name": "identity", "value": identity, "url": origin}])
            sessions.append(browser)
            html = browser.render(origin + "/").body.decode()
            assert f'>{identity}</h1>' in html, "dynamic HTML or identity isolation failed"
            state = json.loads(browser.request("GET", origin + "/api/state").body)
            assert state["identity"] == identity
            assert browser.request("GET", origin + "/api/file").body == BINARY, "binary response changed"
            claim = {"idempotency_key": "fixture-only", "text": "中文"}
            echoed = browser.request("POST", origin + "/api/claim", data=claim, csrf="fixture-only")
            assert json.loads(echoed.body) == claim, "POST payload changed"
        for page in ("/page2", "/page3"):
            assert '>one</h1>' in sessions[0].render(origin + page).body.decode()
        if args.engine != "obscura":
            sessions[0].login("fixture-user", "fixture-password", "fixture-resource")
            assert json.loads(sessions[0].request("GET", origin + "/api/resources/fixture-resource/state").body)["authenticated"]
            assert not json.loads(sessions[1].request("GET", origin + "/api/resources/fixture-resource/state").body)["authenticated"]
        print(json.dumps({"engine": args.engine, "result": "passed", "identities": 2,
            "checks": ["dynamic_html", "cookies", "pagination", "json", "post", "binary", "isolation"] +
                      (["persistent_login"] if args.engine != "obscura" else []),
            "elapsed_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False))
    finally:
        for browser in sessions:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
