"""按在途 PDF 字节选择下一个 MinerU 服务。

先把每个还没到最高并发的服务补到它自己的普遍并发。全部到达普遍并发之后，
只有新任务字节数不超过「最重服务字节 − 最轻服务字节」时，才把任务交给最轻的服务。
已经到达最高并发的服务不接收任务，也不参与最重/最轻的比较。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceLoad:
    name: str
    inflight_count: int
    inflight_bytes: int
    target: int
    max_concurrency: int


def choose_service(services: list[ServiceLoad], task_bytes: int) -> str | None:
    """返回应接收该任务的服务名。返回 None 表示先等待，不把任务塞给别的服务。"""
    if task_bytes < 0:
        raise ValueError(f"任务字节数不能为负：{task_bytes}")
    active = [item for item in services if item.inflight_count < item.max_concurrency]
    if not active:
        return None

    below_target = [item for item in active if item.inflight_count < item.target]
    if below_target:
        # 先把各服务一起抬到普遍并发。数量相同再照顾当前更轻的服务。
        chosen = min(below_target, key=lambda item: (item.inflight_count, item.inflight_bytes, item.name))
        return chosen.name

    # 只剩一个服务还没到最高并发时，没有第二份负载可以比较，让它继续接到自己的上限。
    if len(active) == 1:
        return active[0].name

    lightest = min(active, key=lambda item: (item.inflight_bytes, item.inflight_count, item.name))
    heaviest_bytes = max(item.inflight_bytes for item in active)
    gap = heaviest_bytes - lightest.inflight_bytes
    if task_bytes <= gap:
        return lightest.name
    return None
