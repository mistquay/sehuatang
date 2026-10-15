"""线路请求观测与按目标站隔离的网络故障状态。"""
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from .cf_challenge import is_cf_blocked, is_cf_challenge, is_rate_limited


WINDOW_SECONDS = 300
PROBE_INTERVAL = 60
PROBE_IDLE_SECONDS = 5
PROBE_TIMEOUT = 10
CONTROL_URL = "https://www.gstatic.com/generate_204"
FAILURE_THRESHOLD = 3
NETWORK_ERRORS = {"timeout", "proxy", "dns", "connection", "tls", "send", "receive"}


def origin(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")


def proxy_label(url):
    return origin(url) if url else "direct"


def exception_info(exc):
    """保留错误码与可确认的阶段，不输出可能含代理凭据的原始异常文本。"""
    code = getattr(exc, "code", None)
    code = int(code) if isinstance(code, int) else None
    mapping = {
        5: ("dns", "proxy_dns", "代理主机解析失败"),
        6: ("dns", "dns", "目标域名解析失败"),
        7: ("connection", "connect", "连接建立失败（代理或目标端）"),
        28: ("timeout", "unknown", "请求超时，无法仅凭错误码定位阶段"),
        35: ("tls", "tls", "TLS 握手失败"),
        60: ("tls", "tls", "TLS 证书校验失败"),
        97: ("proxy", "proxy_handshake", "代理握手失败"),
        55: ("send", "send", "发送数据失败"),
        56: ("receive", "receive", "接收数据失败"),
    }
    kind, phase, summary = mapping.get(code, (None, "unknown", "请求异常，阶段未知"))
    if kind is None:
        name = type(exc).__name__.lower()
        if "timeout" in name or "timed out" in str(exc).lower():
            kind, summary = "timeout", "请求超时，阶段未知"
        else:
            kind = next((key for key in ("proxy", "dns", "connection", "tls") if key in name), name)
    return {"error_type": kind, "curl_code": code, "phase": phase, "summary": summary}


def response_outcome(status, body, error_type=None):
    if error_type in NETWORK_ERRORS:
        return "network_error"
    if status == 407:
        return "proxy_auth"
    if error_type == "cf_blocked" or is_cf_blocked(body):
        return "restricted"
    if error_type in ("cf_challenge", "r18_challenge"):
        return "challenge"
    if is_cf_challenge(body, status):
        return "challenge"
    if status in (403, 429) or is_rate_limited(body):
        return "restricted"
    if status == 200 and body and not error_type:
        return "ok"
    return "response_error"


class NetworkMonitor:
    def __init__(self, source, proxy, logger, *, monotonic=time.monotonic, wall=time.time):
        self.source = source
        self.proxy = proxy_label(proxy)
        self.log = logger
        self._now, self._wall = monotonic, wall
        self._lock = threading.Lock()
        self._domains = OrderedDict()
        self._last_probe = -float("inf")

    def _entry(self, url):
        key = origin(url)
        if key not in self._domains:
            if len(self._domains) >= 16:
                self._domains.popitem(last=False)
            self._domains[key] = dict(
                target=key, buckets=deque(), consecutive=0, alert=False,
                last_attempt=None, last_success=None, last_real=-float("inf"),
                last_failure_start=-float("inf"), last_probe=-float("inf"),
                last_success_start=-float("inf"),
                probing=False, probe_url=None, diagnostic=None, outcome=None,
                recovered_at=None, last_error=None, revision=0,
                quarantined=False, blocked_until=0,
            )
        self._domains.move_to_end(key)
        return self._domains[key]

    def _bucket(self, row):
        now = int(self._now())
        buckets = row["buckets"]
        while buckets and buckets[0]["second"] <= now - WINDOW_SECONDS:
            buckets.popleft()
        if not buckets or buckets[-1]["second"] != now:
            buckets.append(dict(second=now, attempts=0, retries=0, timeouts=0,
                                elapsed_ms=0, completed=0, failed=0))
        return buckets[-1]

    def _outcome(self, row, outcome, started, error, *, real=True):
        # 较早发出的请求晚到，不能覆盖更新的网络失败证据。
        if outcome != "network_error" and started < row["last_failure_start"]:
            return
        if outcome == "network_error" and started < row["last_success_start"]:
            return
        row["outcome"] = outcome
        if outcome == "network_error":
            row["last_failure_start"] = max(started, row["last_failure_start"])
            row["consecutive"] += int(real)
            row["last_error"] = error
            row["quarantined"] = True
            row["blocked_until"] = self._now() + PROBE_INTERVAL
            if row["consecutive"] >= FAILURE_THRESHOLD and not row["alert"]:
                row["alert"] = True
                self.log.warning(f"线路疑似异常: source={self.source} proxy={self.proxy} "
                                 f"target={row['target']} consecutive={row['consecutive']}")
        else:
            row["last_success_start"] = max(started, row["last_success_start"])
            row["consecutive"] = 0
            row["last_error"] = None
            row["quarantined"] = False
            row["blocked_until"] = 0
            if row["alert"]:
                if outcome == "ok":
                    row["recovered_at"] = self._now()
                self.log.info(f"线路网络响应恢复: source={self.source} proxy={self.proxy} "
                              f"target={row['target']} outcome={outcome}")
            # 收到 HTTP 响应意味着传输恢复；受限 / 验证状态仍单独展示。
            row["alert"] = False
            if outcome == "ok":
                row["last_success"] = self._wall()
                row["last_error"] = None

    def attempt(self, url, *, started, elapsed_ms, status, body, retry=False, error=None):
        with self._lock:
            row = self._entry(url)
            bucket = self._bucket(row)
            bucket["attempts"] += 1
            bucket["retries"] += int(retry)
            bucket["elapsed_ms"] += elapsed_ms
            bucket["timeouts"] += int(bool(error and error["error_type"] == "timeout"))
            row["last_attempt"] = self._wall()
            row["last_real"] = self._now()
            row["probe_url"] = url
            row["revision"] += 1
            self._outcome(row, response_outcome(status, body, error["error_type"] if error else None), started, error)

    def completed(self, url, ok, started=None, error_type=None):
        with self._lock:
            row = self._entry(url)
            bucket = self._bucket(row)
            bucket["completed"] += 1
            bucket["failed"] += int(not ok)
            # CF / R18 验证完成后的逻辑结果；不增加实际请求次数。
            if ok and started is not None:
                self._outcome(row, "ok", started, None)
            elif started is not None and error_type in ("cf_challenge", "r18_challenge", "rate_limited"):
                self._outcome(row, "restricted" if error_type == "rate_limited" else "challenge", started, None)

    def available(self, url):
        with self._lock:
            row = self._domains.get(origin(url))
            return row is None or not row["quarantined"]

    def register_target(self, url):
        with self._lock:
            self._entry(url)

    def claim_probe(self, url=None):
        with self._lock:
            now = self._now()
            if any(r["probing"] for r in self._domains.values()):
                return None
            if url is not None:
                row = self._entry(url)
                row["probe_url"] = url
                row["probing"] = True
                row["last_probe"] = self._last_probe = now
                return url, row["revision"]
            if now - self._last_probe < PROBE_INTERVAL:
                return None
            for row in self._domains.values():
                recovery_due = row["quarantined"] and now >= row["blocked_until"]
                if (recovery_due and not row["probing"]
                        and now - row["last_real"] >= PROBE_IDLE_SECONDS
                        and now - row["last_probe"] >= PROBE_INTERVAL):
                    row["probing"] = True
                    row["last_probe"] = now
                    self._last_probe = now
                    return row["probe_url"], row["revision"]
        return None

    def finish_probe(self, url, revision, result=None, error=None, evidence=None, control=None, elapsed_ms=None):
        with self._lock:
            row = self._domains.get(origin(url))
            if row is None:
                return
            row["probing"] = False
            if revision != row["revision"]:
                return
            outcome = response_outcome(result.status_code, result.content, error["error_type"] if error else None) if result is not None else (
                response_outcome(None, None, error["error_type"]) if error else "deferred")
            row["diagnostic"] = dict(at=self._wall(), outcome=outcome, error=error,
                                     status=result.status_code if result is not None else None,
                                     evidence=evidence, control=control, elapsed_ms=elapsed_ms)
            if outcome != "deferred":
                self._outcome(row, outcome, row["last_probe"], error, real=False)

    def snapshot(self):
        with self._lock:
            now = self._now()
            rows = []
            for row in self._domains.values():
                self._bucket(row)
                total = {key: sum(b[key] for b in row["buckets"]) for key in
                         ("attempts", "retries", "timeouts", "elapsed_ms", "completed", "failed")}
                latest_evidence = row["last_real"]
                if row["diagnostic"] and row["diagnostic"]["outcome"] != "deferred":
                    latest_evidence = max(latest_evidence, row["last_probe"])
                state = "healthy"
                if row["probing"]:
                    state = "diagnosing"
                elif now - latest_evidence >= WINDOW_SECONDS:
                    state = "stale"
                elif row["outcome"] in ("restricted", "challenge", "proxy_auth", "response_error"):
                    state = row["outcome"]
                elif row["alert"]:
                    state = "suspect"
                elif row["outcome"] == "network_error":
                    state = "unstable"
                elif row["recovered_at"] is not None and now - row["recovered_at"] < WINDOW_SECONDS:
                    state = "recovered"
                rows.append(dict(source=self.source, proxy=self.proxy, target=row["target"], state=state,
                                 **total, timeout_rate=total["timeouts"] / total["attempts"] if total["attempts"] else None,
                                 average_ms=round(total["elapsed_ms"] / total["attempts"]) if total["attempts"] else None,
                                 consecutive_failures=row["consecutive"], last_attempt=row["last_attempt"],
                                 last_success=row["last_success"], last_error=row["last_error"],
                                 diagnostic=row["diagnostic"], quarantined=row["quarantined"],
                                 recovery_in=max(0, round(row["blocked_until"] - now))))
            return rows
