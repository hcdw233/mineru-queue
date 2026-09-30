"""把 MinerU 返回的 zip 解压到任务目录，并挡住压缩包路径穿越。"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path


def extract_zip(zip_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            target = _member_path(dest_resolved, info.filename)
            if target is None:
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _member_path(dest: Path, name: str) -> Path | None:
    if not name or name.startswith(("/", "\\")):
        raise ValueError(f"压缩包包含绝对路径：{name}")
    parts = Path(name).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"压缩包包含越界路径：{name}")
    # 跳过我们自己保存的压缩包，避免解压时把它覆盖掉。
    if parts == ("result.zip",):
        return None
    target = dest.joinpath(*parts).resolve()
    if os.path.commonpath([dest, target]) != str(dest):
        raise ValueError(f"压缩包成员超出任务目录：{name}")
    return target
