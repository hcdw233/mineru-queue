"""缓存 PDF 字节数。缺失文件记为 -1，下次重跑时重新 stat。"""

from __future__ import annotations

from pathlib import Path


class SizeCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._sizes: dict[str, int] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                task_key, _, raw_size = text.partition("\t")
                try:
                    self._sizes[task_key] = int(raw_size)
                except ValueError:
                    continue

    def bytes_for(self, task_key: str, pdf_path: Path) -> int | None:
        """文件存在时返回字节数。文件不存在时返回 None。"""
        cached = self._sizes.get(task_key)
        if cached is not None and cached >= 0:
            return cached
        if not pdf_path.is_file():
            self._remember(task_key, -1)
            return None
        size = pdf_path.stat().st_size
        self._remember(task_key, size)
        return size

    def _remember(self, task_key: str, size: int) -> None:
        previous = self._sizes.get(task_key)
        self._sizes[task_key] = size
        if previous == size:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{task_key}\t{size}\n")
