"""进度页面和 API 走同一个端口。只读进度文件和任务表，不负责启动解析。"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .progress import (
    STATUS_LABELS,
    SUCCESS,
    TERMINAL_STATUSES,
    ProgressLog,
    average_success_per_hour,
    latest_counts,
    outcome_group,
    rates_for_events,
    series_for_day,
    series_for_hour,
)
from .tasks import load_tasks

# 开发布局是仓库根目录下的 web/。安装后仍从源码树定位，避免页面和接口分成两个端口。
WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def resolve_web_file(root: Path, url_path: str) -> Path | None:
    """把 URL 路径限制在 web 目录内。拒绝绝对路径和 .. 。"""
    if url_path in {"", "/"}:
        url_path = "/index.html"
    relative = url_path.lstrip("/")
    if not relative or "\\" in relative or relative.startswith("/"):
        return None
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    root_resolved = root.resolve()
    destination = root_resolved.joinpath(*parts).resolve()
    if os.path.commonpath([root_resolved, destination]) != str(root_resolved):
        return None
    if not destination.is_file():
        return None
    return destination


class Monitor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.progress = ProgressLog(config.progress_log)
        self._task_keys: list[str] = []
        self._csv_mtime: float | None = None
        self._lock = threading.Lock()

    def summary(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            counts = latest_counts(self.progress.latest)
            event_counts: Counter[str] = Counter(outcome_group(event.status) for event in self.progress.events)
            known = set(self._task_keys)
            pending = sum(1 for key in known if key not in self.progress.latest)
            completed = sum(1 for key in known if self.progress.latest.get(key) == SUCCESS)
            return {
                "total_tasks": len(known),
                "completed_tasks": completed,
                "remaining_tasks": len(known) - completed,
                "pending": pending,
                "latest": counts,
                "events": {
                    "success": event_counts["success"],
                    "failed": event_counts["failed"],
                    "other": event_counts["other"],
                },
                "speed": average_success_per_hour(self.progress.events),
                "server_now": datetime.now().astimezone().isoformat(timespec="seconds"),
                "ignored_lines": self.progress.ignored_lines,
                "status_labels": STATUS_LABELS,
            }

    def series(self, view: str, day_text: str, hour: int | None) -> dict[str, object]:
        with self._lock:
            self._refresh()
            day = datetime.strptime(day_text, "%Y-%m-%d")
            if view == "hour":
                if hour is None or not 0 <= hour <= 23:
                    raise ValueError("hour 必须是 0 到 23")
                buckets = series_for_hour(self.progress.events, day, hour)
            elif view == "day":
                buckets = series_for_day(self.progress.events, day)
            else:
                raise ValueError("view 只能是 hour 或 day")
            # 与柱状图同一套完成时间：用事件自己的日期和钟点，避免首条记录的时区把窗口挪走。
            if view == "hour":
                matched = [
                    event for event in self.progress.events
                    if event.finished_at.date() == day.date() and event.finished_at.hour == hour
                ]
            else:
                matched = [
                    event for event in self.progress.events
                    if event.finished_at.date() == day.date()
                ]
            return {
                "view": view,
                "date": day_text,
                "hour": hour,
                "buckets": buckets,
                "speed": rates_for_events(matched),
            }

    def _refresh(self) -> None:
        if self.config.tasks_csv.is_file():
            mtime = self.config.tasks_csv.stat().st_mtime
            if mtime != self._csv_mtime:
                self._task_keys = [task.task_key for task in load_tasks(self.config.tasks_csv)]
                self._csv_mtime = mtime
        self.progress.catch_up()


def serve(config: AppConfig) -> None:
    monitor = Monitor(config)

    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_file(parsed.path)
                return
            try:
                if parsed.path == "/api/summary":
                    payload = monitor.summary()
                elif parsed.path == "/api/series":
                    query = parse_qs(parsed.query)
                    view = (query.get("view") or ["day"])[0]
                    day = (query.get("date") or [datetime.now().astimezone().date().isoformat()])[0]
                    hour_text = (query.get("hour") or [None])[0]
                    hour = int(hour_text) if hour_text is not None else None
                    payload = monitor.series(view, day, hour)
                elif parsed.path == "/api/health":
                    payload = {"ok": True, "statuses": list(TERMINAL_STATUSES)}
                else:
                    self._send(404, {"error": "not found"})
                    return
            except (ValueError, OSError) as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, payload)

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, url_path: str) -> None:
            path = resolve_web_file(WEB_ROOT, url_path)
            if path is None:
                self._send(404, {"error": "not found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((config.monitor_host, config.monitor_port), Handler)
    origin = f"http://{config.monitor_host}:{config.monitor_port}"
    print(f"进度页面：{origin}/", flush=True)
    print(f"进度 API：{origin}/api/summary", flush=True)
    if config.monitor_host in {"0.0.0.0", "::"}:
        print("从笔记本打开时，把地址里的 0.0.0.0 换成这台机器的 IP。", flush=True)
    server.serve_forever()
