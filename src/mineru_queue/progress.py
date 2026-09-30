"""追加式进度文件。

每一行是一次任务结束，格式固定为「task_key\\t状态\\t完成时间」。
文件只追加。同一个任务重跑会再追加一行，读取时每个任务取最后一行作为当前结果，
时间分布则按每一行的完成时间计数。
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SUCCESS = "success"
FAILED = "failed"
PARTIAL = "partial"
CANCELED = "canceled"
TIMEOUT = "timeout"

# 写入进度文件的全部终态。进行中的 MinerU 状态不会写进来。
TERMINAL_STATUSES = (SUCCESS, FAILED, PARTIAL, CANCELED, TIMEOUT)
OTHER_STATUSES = (PARTIAL, CANCELED, TIMEOUT)

STATUS_LABELS = {
    SUCCESS: "成功",
    FAILED: "失败",
    PARTIAL: "部分成功",
    CANCELED: "已取消",
    TIMEOUT: "轮询超时",
}


def outcome_group(status: str) -> str:
    """把终态收成网页上的三类：成功、失败、其他。"""
    if status == SUCCESS:
        return "success"
    if status == FAILED:
        return "failed"
    return "other"


def format_finished_at(moment: datetime | None = None) -> str:
    current = moment or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    return current.isoformat(timespec="seconds")


def append_progress(
    path: Path,
    task_key: str,
    status: str,
    moment: datetime | None = None,
    started_at: datetime | None = None,
) -> None:
    """用一次 write 追加一行。短行在 O_APPEND 下不会和别的行交错。

    有开始时间时写成四列：任务、状态、下载完成时间、开始时间。
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"不能写入未知状态：{status}")
    if not task_key or any(char in task_key for char in "\t\r\n"):
        raise ValueError(f"任务主键不能包含制表符或换行：{task_key!r}")
    line = f"{task_key}\t{status}\t{format_finished_at(moment)}"
    if started_at is not None:
        line += f"\t{format_finished_at(started_at)}"
    line += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ProgressEvent:
    task_key: str
    status: str
    finished_at: datetime
    # 开始处理的时间。旧进度行没有这一列，无法计算单篇耗时。
    started_at: datetime | None = None


class ProgressLog:
    """增量读取进度文件。不完整的最后一行会留到下一次读取。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[ProgressEvent] = []
        self.latest: dict[str, str] = {}
        self.ignored_lines = 0
        self._offset = 0
        self._pending = b""

    def catch_up(self) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self._offset:
            self.events.clear()
            self.latest.clear()
            self.ignored_lines = 0
            self._offset = 0
            self._pending = b""
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            data = handle.read()
            self._offset = handle.tell()
        if not data and not self._pending:
            return
        blob = self._pending + data
        parts = blob.split(b"\n")
        self._pending = parts.pop()
        for raw in parts:
            self._consume(raw.decode("utf-8", "replace"))

    def _consume(self, line: str) -> None:
        text = line.strip("\ufeff").strip()
        if not text:
            return
        fields = text.split("\t")
        if len(fields) not in (3, 4):
            self.ignored_lines += 1
            return
        task_key, status, raw_time = fields[:3]
        try:
            finished_at = _aware(datetime.fromisoformat(raw_time))
            started_at = _aware(datetime.fromisoformat(fields[3])) if len(fields) == 4 else None
        except ValueError:
            self.ignored_lines += 1
            return
        self.events.append(ProgressEvent(task_key, status, finished_at, started_at))
        self.latest[task_key] = status


def _empty_bucket(label: str) -> dict[str, object]:
    bucket: dict[str, object] = {
        "label": label,
        "success": 0,
        "failed": 0,
        "other": 0,
    }
    for status in OTHER_STATUSES:
        bucket[status] = 0
    return bucket


def _add_event(bucket: dict[str, object], event: ProgressEvent) -> None:
    group = outcome_group(event.status)
    bucket[group] = int(bucket[group]) + 1
    if event.status in OTHER_STATUSES:
        bucket[event.status] = int(bucket[event.status]) + 1
    if event.status != SUCCESS or event.started_at is None:
        return
    seconds = (event.finished_at - event.started_at).total_seconds()
    if seconds > 0:
        durations = bucket.setdefault("_durations", [])
        assert isinstance(durations, list)
        durations.append(seconds)


def _finish_buckets(buckets: list[dict[str, object]]) -> list[dict[str, object]]:
    """把柱内成功任务的耗时折成单篇速度。开始时间可以早于这根柱。"""
    for bucket in buckets:
        raw = bucket.pop("_durations", [])
        durations = [float(item) for item in raw] if isinstance(raw, list) else []
        rates = _rates_from_durations(durations)
        bucket["timed_success_events"] = rates["timed_success_events"]
        bucket["average_duration_seconds"] = rates["average_duration_seconds"]
        bucket["success_per_hour"] = rates["success_per_hour"]
        bucket["success_per_10_minutes"] = rates["success_per_10_minutes"]
    return buckets


def series_for_hour(events: list[ProgressEvent], day: datetime, hour: int) -> list[dict[str, object]]:
    """某一小时内按 5 分钟分 12 段。day 只使用日期部分。按完成时间归柱。"""
    buckets = [_empty_bucket(f"{hour:02d}:{minute:02d}") for minute in range(0, 60, 5)]
    for event in events:
        moment = event.finished_at
        if moment.date() != day.date() or moment.hour != hour:
            continue
        _add_event(buckets[moment.minute // 5], event)
    return _finish_buckets(buckets)


def series_for_day(events: list[ProgressEvent], day: datetime) -> list[dict[str, object]]:
    """某一天按小时分 24 段。按完成时间归柱。"""
    buckets = [_empty_bucket(f"{hour:02d}:00") for hour in range(24)]
    for event in events:
        if event.finished_at.date() != day.date():
            continue
        _add_event(buckets[event.finished_at.hour], event)
    return _finish_buckets(buckets)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return moment


def _success_durations(
    events: list[ProgressEvent],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[float]:
    """成功任务从开始处理到 zip 下载完成的秒数。没有开始时间的旧行不参与。"""
    durations: list[float] = []
    for event in events:
        if event.status != SUCCESS or event.started_at is None:
            continue
        if start is not None and end is not None and not start <= event.finished_at < end:
            continue
        seconds = (event.finished_at - event.started_at).total_seconds()
        if seconds > 0:
            durations.append(seconds)
    return durations


def _rates_from_durations(durations: list[float]) -> dict[str, object]:
    """一个并发槽的速度：3600 秒除以单篇平均耗时。"""
    if not durations:
        return {
            "timed_success_events": 0,
            "average_duration_seconds": None,
            "success_per_hour": None,
            "success_per_10_minutes": None,
        }
    average = sum(durations) / len(durations)
    return {
        "timed_success_events": len(durations),
        "average_duration_seconds": round(average, 1),
        "success_per_hour": round(3600 / average, 2),
        "success_per_10_minutes": round(600 / average, 2),
    }


def _finished_between(events: list[ProgressEvent], start: datetime, end: datetime | None = None) -> list[ProgressEvent]:
    """完成时间落在 [start, end) 的事件。end 为空时只要求不早于 start。"""
    matched: list[ProgressEvent] = []
    for event in events:
        if event.finished_at < start:
            continue
        if end is not None and event.finished_at >= end:
            continue
        matched.append(event)
    return matched


def average_success_per_hour(events: list[ProgressEvent], *, now: datetime | None = None) -> dict[str, object]:
    """实际完成篇数按完成时间统计；单篇耗时只用于辅助展示。"""
    successes = [event for event in events if event.status == SUCCESS]
    current = now or datetime.now().astimezone()
    hour_start = current.replace(minute=0, second=0, microsecond=0)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    recent = _finished_between(successes, current - timedelta(hours=1), current)
    payload = _rates_from_durations(_success_durations(recent))
    finished_at = [event.finished_at for event in successes]
    payload.update({
        "success_events": len(successes),
        "recent_hour_success": len(recent),
        "previous_rolling_hour_success": len(_finished_between(
            successes, current - timedelta(hours=2), current - timedelta(hours=1)
        )),
        "recent_10_minutes_success": len(_finished_between(successes, current - timedelta(minutes=10), current)),
        "current_clock_hour_success": len(_finished_between(successes, hour_start, current)),
        "previous_clock_hour_success": len(_finished_between(
            successes, hour_start - timedelta(hours=1), hour_start
        )),
        "today_success": len(_finished_between(successes, day_start, current)),
        "recent_hour_failed": len(_finished_between(
            [event for event in events if event.status == FAILED], current - timedelta(hours=1), current
        )),
        "recent_hour_other": len(_finished_between(
            [event for event in events if event.status in OTHER_STATUSES], current - timedelta(hours=1), current
        )),
        "first_success_at": format_finished_at(min(finished_at)) if finished_at else None,
        "last_success_at": format_finished_at(max(finished_at)) if finished_at else None,
    })
    return payload


def view_window(view: str, day: datetime, hour: int | None, tzinfo: datetime.tzinfo) -> tuple[datetime, datetime]:
    """按小时或按天 tab 选中的时间范围，右边界不包含。"""
    if view == "hour":
        start = datetime(day.year, day.month, day.day, hour or 0, tzinfo=tzinfo)
        return start, start + timedelta(hours=1)
    start = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
    return start, start + timedelta(days=1)


def rates_for_events(events: list[ProgressEvent]) -> dict[str, object]:
    """events 已按完成时间筛过。耗时用开始时间到完成时间，开始时间可以更早。"""
    payload = _rates_from_durations(_success_durations(events))
    payload["success_events"] = sum(1 for event in events if event.status == SUCCESS)
    payload["failed_events"] = sum(1 for event in events if event.status == FAILED)
    payload["other_events"] = sum(1 for event in events if event.status in OTHER_STATUSES)
    return payload


def success_rates_in_window(events: list[ProgressEvent], start: datetime, end: datetime) -> dict[str, object]:
    """所选小时或日期内，按下载完成时间落入窗口的成功任务计算单篇速度。"""
    return rates_for_events(_finished_between(events, start, end))


def latest_counts(latest: dict[str, str]) -> dict[str, int]:
    counts: Counter[str] = Counter(latest.values())
    payload = {status: counts[status] for status in TERMINAL_STATUSES}
    payload["other"] = sum(counts[status] for status in OTHER_STATUSES)
    return payload
