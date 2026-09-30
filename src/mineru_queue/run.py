"""调度循环：补齐普遍并发，再用字节差决定谁可以超过普遍并发。"""

from __future__ import annotations

import logging
import queue
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import AppConfig, ServiceConfig
from .mineru_client import MinerUClient
from .progress import FAILED, SUCCESS, TERMINAL_STATUSES, ProgressLog, append_progress
from .schedule import ServiceLoad, choose_service
from .sizes import SizeCache
from .tasks import ParseTask, load_tasks, output_dir

LOGGER = logging.getLogger("mineru_queue")


@dataclass
class RunningService:
    config: ServiceConfig
    inflight: dict[str, int] = field(default_factory=dict)

    def load(self) -> ServiceLoad:
        return ServiceLoad(
            name=self.config.name,
            inflight_count=len(self.inflight),
            inflight_bytes=sum(self.inflight.values()),
            target=self.config.target,
            max_concurrency=self.config.max_concurrency,
        )


@dataclass(frozen=True)
class Completion:
    service_name: str
    task_key: str
    status: str
    started_at: datetime | None = None
    # zip 下载完成的时间。失败且没有下到 zip 时为空，进度行用写入时刻。
    downloaded_at: datetime | None = None


class Parser:
    """测试可以换成不访问网络的实现。"""

    def parse(self, service: ServiceConfig, pdf_path: Path, dest: Path) -> str:
        raise NotImplementedError


def run_queue(config: AppConfig, *, only_failed: bool = False, parser: Parser | None = None) -> int:
    tasks = load_tasks(config.tasks_csv)
    if parser is None:
        MinerUClient(config).retry_pending_releases()
    progress = ProgressLog(config.progress_log)
    progress.catch_up()
    pending = _select_pending(tasks, progress, only_failed=only_failed)
    LOGGER.info(
        "任务 %d，跳过已成功 %d，本次准备解析 %d",
        len(tasks),
        sum(1 for task in tasks if progress.latest.get(task.task_key) == SUCCESS),
        len(pending),
    )
    if not pending:
        return 0

    sizes = SizeCache(config.size_cache)
    services = [RunningService(item) for item in config.services]
    by_name = {item.config.name: item for item in services}
    client = parser or MinerUClient(config)
    completions: queue.Queue[Completion] = queue.Queue()
    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        if stop.is_set():
            raise SystemExit(128 + signum)
        stop.set()
        LOGGER.warning("已停止接收新任务，等待在途任务写完进度")

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    attempted = 0
    announced_wait = False
    try:
        while pending or _inflight_count(services) or not completions.empty():
            _drain(completions, by_name, config.progress_log)
            if stop.is_set():
                if _inflight_count(services) == 0 and completions.empty():
                    break
                _wait(completions)
                continue
            if not pending:
                if _inflight_count(services) == 0:
                    break
                _wait(completions)
                continue

            task = pending[0]
            pdf_bytes = sizes.bytes_for(task.task_key, task.pdf_path)
            if pdf_bytes is None:
                LOGGER.error("PDF 不存在，记为失败：%s", task.task_key)
                _finish(config, task, FAILED, "PDF 不存在")
                pending.pop(0)
                attempted += 1
                announced_wait = False
                continue

            service_name = choose_service([item.load() for item in services], pdf_bytes)
            if service_name is None:
                if not announced_wait:
                    LOGGER.info(
                        "队首 %s（%d 字节）放不进当前字节差，等待在途任务完成",
                        task.task_key,
                        pdf_bytes,
                    )
                    announced_wait = True
                _wait(completions)
                continue

            announced_wait = False
            pending.pop(0)
            service = by_name[service_name]
            service.inflight[task.task_key] = pdf_bytes
            attempted += 1
            LOGGER.info("提交 %s -> %s（%d 字节，在途 %d）", task.task_key, service_name, pdf_bytes, len(service.inflight))
            thread = threading.Thread(
                target=_execute,
                name=f"mineru-{service_name}",
                args=(client, service.config, task, config, completions),
                daemon=True,
            )
            thread.start()
        _drain(completions, by_name, config.progress_log)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    LOGGER.info("本次结束，处理 %d 个任务", attempted)
    return attempted


def _select_pending(tasks: list[ParseTask], progress: ProgressLog, *, only_failed: bool) -> list[ParseTask]:
    selected: list[ParseTask] = []
    for task in tasks:
        latest = progress.latest.get(task.task_key)
        if only_failed:
            if latest == FAILED:
                selected.append(task)
            continue
        if latest != SUCCESS:
            selected.append(task)
    return selected


def _execute(
    client: Parser,
    service: ServiceConfig,
    task: ParseTask,
    config: AppConfig,
    completions: queue.Queue[Completion],
) -> None:
    status = FAILED
    started_at: datetime | None = None
    downloaded_at: datetime | None = None
    try:
        dest = output_dir(config.output_root, task.output_rel_dir)
        if hasattr(client, "parse_outcome"):
            outcome = client.parse_outcome(service, task.pdf_path, dest)
            status = outcome.status
            started_at = outcome.started_at
            downloaded_at = outcome.downloaded_at
        else:
            status = client.parse(service, task.pdf_path, dest)
        if status not in TERMINAL_STATUSES:
            _write_task_error(dest, f"解析返回了未知状态：{status}")
            status = FAILED
    except Exception as exc:
        LOGGER.exception("任务 %s 执行失败", task.task_key)
        try:
            dest = output_dir(config.output_root, task.output_rel_dir)
            _write_task_error(dest, str(exc))
        except Exception:
            LOGGER.exception("任务 %s 的错误信息没有写下来", task.task_key)
        status = FAILED
    completions.put(Completion(service.name, task.task_key, status, started_at, downloaded_at))


def _drain(
    completions: queue.Queue[Completion],
    services: dict[str, RunningService],
    progress_log: Path,
) -> None:
    while True:
        try:
            item = completions.get_nowait()
        except queue.Empty:
            return
        service = services[item.service_name]
        service.inflight.pop(item.task_key, None)
        append_progress(
            progress_log,
            item.task_key,
            item.status,
            item.downloaded_at,
            item.started_at,
        )
        LOGGER.info("完成 %s %s", item.task_key, item.status)


def _finish(config: AppConfig, task: ParseTask, status: str, message: str) -> None:
    try:
        dest = output_dir(config.output_root, task.output_rel_dir)
        _write_task_error(dest, message)
    except Exception:
        LOGGER.exception("任务 %s 的错误信息没有写下来", task.task_key)
    append_progress(config.progress_log, task.task_key, status)


def _write_task_error(dest: Path, message: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mineru_error.txt").write_text(message + "\n", encoding="utf-8")


def _inflight_count(services: list[RunningService]) -> int:
    return sum(len(item.inflight) for item in services)


def _wait(completions: queue.Queue[Completion]) -> None:
    try:
        item = completions.get(timeout=0.5)
    except queue.Empty:
        return
    completions.put(item)
