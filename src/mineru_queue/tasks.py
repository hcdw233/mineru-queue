"""读取任务表，并把 output_rel_dir 限制在结果根目录之内。"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParseTask:
    task_key: str
    pdf_path: Path
    output_rel_dir: str
    queue_order: int


def load_tasks(path: Path) -> list[ParseTask]:
    if not path.is_file():
        raise ValueError(f"找不到任务表：{path}")
    tasks: list[ParseTask] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        required = {"task_key", "pdf_primary_path", "output_rel_dir"}
        missing = required - fields
        if missing:
            raise ValueError(f"任务表缺少字段：{sorted(missing)}")
        for index, row in enumerate(reader, start=1):
            task_key = (row.get("task_key") or "").strip()
            pdf_path = (row.get("pdf_primary_path") or "").strip()
            output_rel = (row.get("output_rel_dir") or "").strip()
            if not task_key or not pdf_path or not output_rel:
                raise ValueError(f"任务表第 {index} 行缺少 task_key、pdf_primary_path 或 output_rel_dir")
            if task_key in seen:
                raise ValueError(f"任务表里 task_key 重复：{task_key}")
            seen.add(task_key)
            order_text = (row.get("queue_order") or "").strip()
            if order_text:
                try:
                    order = int(order_text)
                except ValueError as exc:
                    raise ValueError(f"任务 {task_key} 的 queue_order 不是整数") from exc
            else:
                order = index
            tasks.append(
                ParseTask(
                    task_key=task_key,
                    pdf_path=Path(pdf_path),
                    output_rel_dir=output_rel,
                    queue_order=order,
                )
            )
    tasks.sort(key=lambda item: (item.queue_order, item.task_key))
    return tasks


def output_dir(root: Path, output_rel_dir: str) -> Path:
    """把相对目录贴到结果根上，拒绝绝对路径和跳出根目录的片段。"""
    text = output_rel_dir.strip()
    if not text or text.startswith(("/", "\\")) or "\\" in text:
        raise ValueError(f"output_rel_dir 必须是相对路径：{output_rel_dir}")
    parts = Path(text).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"output_rel_dir 不能包含 . 或 ..：{output_rel_dir}")
    root_resolved = root.resolve()
    destination = root_resolved.joinpath(*parts).resolve()
    if os.path.commonpath([root_resolved, destination]) != str(root_resolved):
        raise ValueError(f"结果目录超出了 output_root：{output_rel_dir}")
    return destination
