"""管理页 Web 服务。

单页管理界面 + JSON API：运行状态、手动触发、失败台账、
分页补抓、配置文件编辑和进程重启。设计原则是薄封装——
所有能力复用现有 CLI 走的同一套入口（source_registry / main.backfill_pages），
仅新增展示与触发，不引入独立的任务系统。
"""
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from util.log_util import log

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_INDEX_PATH = Path(__file__).resolve().parent / "index.html"


class ActionTracker:
    """跟踪 web 触发的后台动作，防止同类动作并发重复。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._actions = {}

    def try_start(self, key: str, description: str) -> bool:
        with self._lock:
            current = self._actions.get(key)
            if current and (current["thread"] is None or current["thread"].is_alive()):
                return False
            self._actions[key] = {
                "description": description,
                "started_at": time.time(),
                "thread": None,
            }
            return True

    def attach(self, key: str, thread: threading.Thread) -> None:
        with self._lock:
            if key in self._actions:
                self._actions[key]["thread"] = thread

    def snapshot(self):
        with self._lock:
            return {
                key: {
                    "description": entry["description"],
                    "started_at": entry["started_at"],
                    "running": bool(
                        entry["thread"] and entry["thread"].is_alive()
                    ),
                }
                for key, entry in self._actions.items()
            }


def _spawn(tracker: ActionTracker, key: str, description: str, target) -> bool:
    """在后台线程执行动作；同 key 动作在跑时返回 False。"""
    if not tracker.try_start(key, description):
        return False

    def runner():
        try:
            target()
        except Exception as exc:
            log.error(f"管理页动作执行失败: {description} error={exc}")

    thread = threading.Thread(target=runner, name=f"web-{key}", daemon=True)
    tracker.attach(key, thread)
    thread.start()
    return True


def _iso_utc(value):
    """把 Mongo 返回的 naive UTC datetime 序列化成带时区的 ISO 字符串。

    pymongo 默认返回不带 tzinfo 的 UTC 时间，直接 str() 会让前端
    无从判断时区；统一补上 UTC 标记，由浏览器转成本地时间显示。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _restart_process() -> None:
    log.info("管理页触发重启，等待通知队列收尾后重新执行")
    from scrapers.core.pool import stop_shared_clients
    from scrapers.core.storage import drain_writers
    from notifications.memory_queue import shutdown_notifications
    # execv 不执行 finally/atexit；先关闭常驻浏览器、HTTP 和已入队写入。
    stop_shared_clients()
    drain_writers()
    shutdown_notifications()
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        # docker restart 策略会拉起退出的容器
        log.error(f"execv 重启失败，改为退出进程: {exc}")
        os._exit(3)


def create_app(
    *,
    token: Optional[str] = None,
    config_path: Optional[str] = None,
    scheduler_manager=None,
    restart_func=_restart_process,
) -> FastAPI:
    from scrapers.registry import source_registry
    from util.read_config import get_config, _config_manager

    app = FastAPI(title="sehuatang crawler admin", docs_url=None, redoc_url=None)
    tracker = ActionTracker()
    resolved_token = (
        token
        if token is not None
        else str(
            os.getenv("SHT_WEB_TOKEN") or get_config("web.token", "") or ""
        ).strip()
    )
    resolved_config_path = Path(config_path or _config_manager._config_path)
    config_lock = threading.Lock()

    def require_auth(request: Request):
        if resolved_token:
            provided = (
                request.headers.get("x-token")
                or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or request.query_params.get("token", "")
            )
            if provided != resolved_token:
                raise HTTPException(status_code=401, detail="token 无效")
            return
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=401,
                detail="未配置访问 token，仅允许本机访问；请设置 web.token 或 SHT_WEB_TOKEN",
            )

    def _mongodb_enabled() -> bool:
        return bool(get_config("mongodb.enable", False))

    # ---------- 页面 ----------

    @app.get("/")
    def index():
        if not _INDEX_PATH.exists():
            raise HTTPException(
                status_code=503,
                detail="管理页文件缺失: web/index.html（镜像构建不完整）",
            )
        return FileResponse(_INDEX_PATH, media_type="text/html")

    # ---------- 状态 ----------

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status():
        config = get_config() or {}
        jobs = []
        if scheduler_manager and scheduler_manager.scheduler:
            for job in scheduler_manager.scheduler.get_jobs():
                jobs.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": (
                        job.next_run_time.isoformat()
                        if job.next_run_time
                        else None
                    ),
                })

        last_runs = {}
        if _mongodb_enabled():
            try:
                from util.mongo import find_recent_crawl_runs

                for name in source_registry.names():
                    rows = find_recent_crawl_runs(source=name, limit=1)
                    if rows:
                        row = rows[0]
                        row["created_at"] = _iso_utc(row.get("created_at", ""))
                        last_runs[name] = row
            except Exception as exc:
                log.warning(f"读取运行历史失败: {exc}")

        running = set(source_registry.running_sources())
        return {
            "mongodb_enabled": _mongodb_enabled(),
            "config_path": str(resolved_config_path),
            "sources": [
                {
                    "name": name,
                    "enabled": source_registry.is_enabled(config, name),
                    "running": name in running,
                    "last_run": last_runs.get(name),
                }
                for name in source_registry.names()
            ],
            "scheduler_jobs": jobs,
            "actions": tracker.snapshot(),
            "active_tasks": source_registry.active_tasks(),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/network", dependencies=[Depends(require_auth)])
    def network_status():
        from scrapers.core.network_management import configured_snapshot
        return configured_snapshot(get_config() or {})

    @app.post("/api/network/check", dependencies=[Depends(require_auth)])
    def network_check(payload: dict):
        from scrapers.core.network_management import check_configured_network
        try:
            return check_configured_network(get_config() or {}, payload.get("source") or None,
                                            payload.get("proxy_slot"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/notifications", dependencies=[Depends(require_auth)])
    def notification_status():
        from notifications.memory_queue import get_notification_queue
        return {"enabled": bool(get_config("sendMessage.send_telegram_enable", False)),
                **get_notification_queue().snapshot()}

    @app.post("/api/notifications/retry", dependencies=[Depends(require_auth)])
    def retry_notification(payload: dict):
        from notifications.memory_queue import get_notification_queue
        if not get_config("sendMessage.send_telegram_enable", False):
            raise HTTPException(status_code=409, detail="Telegram 通知未启用")
        if not get_notification_queue().retry(str(payload.get("id") or "")):
            raise HTTPException(status_code=409, detail="任务已不在失败记录中，或队列已满/关闭")
        return {"ok": True}

    @app.get("/api/runs", dependencies=[Depends(require_auth)])
    def runs(source: Optional[str] = None, limit: int = 20):
        if not _mongodb_enabled():
            return {"runs": []}
        from util.mongo import find_recent_crawl_runs

        rows = find_recent_crawl_runs(source=source, limit=limit)
        for row in rows:
            row["created_at"] = _iso_utc(row.get("created_at", ""))
        return {"runs": rows}

    def failure_store():
        from scrapers.infrastructure import build_failure_store
        return build_failure_store(mongodb_enabled=_mongodb_enabled())

    @app.get("/api/failures", dependencies=[Depends(require_auth)])
    def failures(source: Optional[str] = None, limit: int = 100, state: Optional[str] = None):
        if state and state not in {"due", "waiting", "exhausted"}:
            raise HTTPException(status_code=400, detail="未知失败状态")
        data = failure_store().snapshot(source=source, state=state, limit=limit)
        for row in data["failures"]:
            for key in ("last_failed_at", "next_retry_at", "created_at", "resolved_at", "requeued_at"):
                if row.get(key) is not None:
                    row[key] = _iso_utc(row[key])
        return data

    @app.post("/api/failures/requeue", dependencies=[Depends(require_auth)])
    def requeue_failure(payload: dict):
        source = str(payload.get("source") or "")
        key = str(payload.get("key") or "")
        stage = str(payload.get("stage") or "")
        if source not in source_registry.names() or not key or not stage:
            raise HTTPException(status_code=400, detail="缺少有效的来源、key 或阶段")
        with source_registry.activity(source, "requeue", f"{source} 重新入队") as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="来源正在运行，请完成后重新入队")
            if not failure_store().requeue(source, key, stage):
                raise HTTPException(status_code=404, detail="失败记录已不存在")
        return {"ok": True}

    @app.post("/api/failures/requeue-exhausted", dependencies=[Depends(require_auth)])
    def requeue_exhausted(payload: dict):
        source = str(payload.get("source") or "")
        if source not in source_registry.names():
            raise HTTPException(status_code=400, detail="请选择一个来源")
        with source_registry.activity(source, "requeue", f"{source} 批量重新入队") as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="来源正在运行，请完成后重新入队")
            count = failure_store().requeue_exhausted(source)
        return {"ok": True, "requeued": count}

    @app.get("/api/backfill-progress", dependencies=[Depends(require_auth)])
    def backfill_progress():
        from scrapers.page_backfill import checkpoint_snapshot
        return checkpoint_snapshot()

    @app.get("/api/logs", dependencies=[Depends(require_auth)])
    def tail_logs(file: str = "crawler", lines: int = 200, level: str = "", module: str = ""):
        from util.log_util import logs_dir, LOG_MODULES
        from util.log_reader import read_log_tail, LOG_LEVELS

        if file not in {"crawler", "error"}:
            raise HTTPException(status_code=400, detail=f"未知日志文件: {file}")
        level = level.upper()
        if level and level not in LOG_LEVELS:
            raise HTTPException(status_code=400, detail="未知日志级别")
        if module and module not in LOG_MODULES:
            raise HTTPException(status_code=400, detail="未知日志模块")
        lines = max(1, min(1000, int(lines)))
        log_path = logs_dir / f"{file}.log"
        try:
            result = read_log_tail(log_path, lines, level=level, module=module)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读取日志失败: {exc}")
        return {"file": file, "level": level, "module": module, **result}

    # ---------- 动作 ----------

    @app.post("/api/actions/crawl", dependencies=[Depends(require_auth)])
    async def action_crawl(payload: dict):
        source = str(payload.get("source") or "")
        if source not in source_registry.names():
            raise HTTPException(status_code=400, detail=f"未知来源: {source}")
        dry_run = bool(payload.get("dry_run", False))

        if source in source_registry.running_sources():
            raise HTTPException(status_code=409, detail=f"{source} 已有任务正在运行")

        def task():
            from main import crawl_sources

            asyncio.run(crawl_sources([source], force=True, dry_run=dry_run))

        if not _spawn(tracker, f"source:{source}", f"抓取 {source}", task):
            raise HTTPException(status_code=409, detail=f"{source} 抓取已在进行中")
        return {"ok": True}

    @app.post("/api/actions/retry-failed", dependencies=[Depends(require_auth)])
    async def action_retry_failed(payload: dict):
        source = str(payload.get("source") or "")
        if source not in source_registry.names():
            raise HTTPException(status_code=400, detail=f"未知来源: {source}")

        if source in source_registry.running_sources():
            raise HTTPException(status_code=409, detail=f"{source} 已有任务正在运行")

        def task():
            from main import crawl_sources

            asyncio.run(
                crawl_sources([source], force=True, retry_failed=True)
            )

        if not _spawn(tracker, f"source:{source}", f"重试失败 {source}", task):
            raise HTTPException(status_code=409, detail=f"{source} 重试已在进行中")
        return {"ok": True}

    @app.post("/api/actions/backfill-pages", dependencies=[Depends(require_auth)])
    async def action_backfill(payload: dict):
        source = str(payload.get("source") or "")
        if source not in {"sehuatang", "x1080x"}:
            raise HTTPException(status_code=400, detail=f"来源不支持分页补抓: {source}")
        try:
            start_page = int(payload.get("start_page", 1))
            end_page = int(payload["end_page"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="缺少有效的 start_page/end_page")
        if end_page < start_page or start_page < 1:
            raise HTTPException(status_code=400, detail="页区间不合法")
        fids = payload.get("fids") or None
        typeids = payload.get("typeids") or None
        resume = bool(payload.get("resume", False))
        dry_run = bool(payload.get("dry_run", False))

        if source in source_registry.running_sources():
            raise HTTPException(status_code=409, detail=f"{source} 已有任务正在运行")

        def task():
            from main import backfill_pages

            asyncio.run(
                backfill_pages(
                    source,
                    start_page,
                    end_page,
                    fids=fids,
                    typeids=typeids,
                    resume=resume,
                    dry_run=dry_run,
                )
            )

        if not _spawn(
            tracker,
            f"source:{source}",
            f"分页补抓 {source} {start_page}-{end_page}",
            task,
        ):
            raise HTTPException(status_code=409, detail=f"{source} 补抓已在进行中")
        return {"ok": True}

    # ---------- 配置 ----------

    @app.get("/api/config", dependencies=[Depends(require_auth)])
    def read_config_file():
        try:
            content = resolved_config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读取配置失败: {exc}")
        return {"path": str(resolved_config_path), "content": content}

    def save_config_content(content):
        try:
            if resolved_config_path.exists():
                backup_path = resolved_config_path.with_suffix(".yaml.bak")
                backup_path.write_text(
                    resolved_config_path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            temp_path = resolved_config_path.with_suffix(".yaml.tmp")
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(resolved_config_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入配置失败: {exc}")

        log.info("管理页更新了配置文件（重启后生效）")
        return {"ok": True, "restart_required": True}

    @app.put("/api/config", dependencies=[Depends(require_auth)])
    def write_config_file(payload: dict):
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail="配置内容不能为空")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"YAML 语法错误: {exc}")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="配置必须是 YAML 映射")
        with config_lock:
            return save_config_content(content)

    def load_config_mapping():
        try:
            parsed = yaml.safe_load(resolved_config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raise HTTPException(status_code=400, detail="无法读取配置，请先检查配置文件") from None
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="配置必须是 YAML 映射")
        return parsed

    @app.get("/api/config/x1080x", dependencies=[Depends(require_auth)])
    def read_x1080x_config():
        from .x1080x_config import form_state
        try:
            return form_state(load_config_mapping())
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=400, detail="x1080x 配置结构无效，请检查 YAML 配置") from None

    @app.put("/api/config/x1080x", dependencies=[Depends(require_auth)])
    def write_x1080x_config(payload: dict):
        from .x1080x_config import update_form
        with config_lock:
            try:
                parsed = update_form(load_config_mapping(), payload)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            except (TypeError, AttributeError):
                raise HTTPException(status_code=400, detail="x1080x 配置结构无效，请检查 YAML 配置") from None
            return save_config_content(yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False))

    # ---------- 重启 ----------

    @app.post("/api/actions/restart", dependencies=[Depends(require_auth)])
    def action_restart():
        threading.Timer(0.8, restart_func).start()
        return {"ok": True, "message": "进程将在 1 秒内重启"}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error(f"管理页接口异常: path={request.url.path} error={exc}")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app
