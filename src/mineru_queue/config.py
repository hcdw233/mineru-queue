"""读取 TOML 配置。服务自己的 target / max 覆盖全局值。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    base_url: str
    api_key: str
    target: int
    max_concurrency: int
    release_remote_jobs: bool


@dataclass(frozen=True)
class AppConfig:
    tasks_csv: Path
    output_root: Path
    progress_log: Path
    size_cache: Path
    tier: str
    output_formats: tuple[str, ...]
    poll_interval_seconds: float
    max_polls: int
    request_timeout_seconds: float
    transfer_timeout_seconds: float
    release_remote_jobs: bool
    services: tuple[ServiceConfig, ...]
    monitor_host: str
    monitor_port: int


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} 必须是大于 0 的整数")
    return value


def _positive_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{label} 必须是大于 0 的数")
    return float(value)


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve()
    if not config_path.is_file():
        raise ValueError(f"找不到配置文件：{config_path}")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    base_dir = config_path.parent

    tasks_csv = raw.get("tasks_csv")
    output_root = raw.get("output_root")
    if not isinstance(tasks_csv, str) or not tasks_csv.strip():
        raise ValueError("tasks_csv 必须是任务表路径")
    if not isinstance(output_root, str) or not output_root.strip():
        raise ValueError("output_root 必须是结果根目录")

    output_path = _resolve(base_dir, output_root.strip())
    progress_value = raw.get("progress_log")
    size_value = raw.get("size_cache")
    progress_log = (
        _resolve(base_dir, progress_value.strip())
        if isinstance(progress_value, str) and progress_value.strip()
        else output_path / "progress.txt"
    )
    size_cache = (
        _resolve(base_dir, size_value.strip())
        if isinstance(size_value, str) and size_value.strip()
        else output_path / "pdf_sizes.tsv"
    )

    formats = raw.get("output_formats", ["markdown", "middle_json", "structured_content", "zip"])
    if not isinstance(formats, list) or not formats or not all(isinstance(item, str) and item for item in formats):
        raise ValueError("output_formats 必须是非空字符串列表")
    if "zip" not in formats:
        raise ValueError("output_formats 必须包含 zip，结果需要下载并解压完整压缩包")
    release_remote_jobs = raw.get("release_remote_jobs", False)
    if not isinstance(release_remote_jobs, bool):
        raise ValueError("release_remote_jobs 必须是布尔值")

    tier = raw.get("tier", "standard")
    if not isinstance(tier, str) or not tier.strip():
        raise ValueError("tier 必须是非空字符串")

    concurrency = raw.get("concurrency", {})
    if not isinstance(concurrency, dict):
        raise ValueError("concurrency 必须是表")
    global_target = _positive_int(concurrency.get("target", 6), "concurrency.target")
    global_max = _positive_int(concurrency.get("max", 8), "concurrency.max")
    if global_max < global_target:
        raise ValueError("concurrency.max 不能小于 concurrency.target")

    service_rows = raw.get("services")
    if not isinstance(service_rows, list) or not service_rows:
        raise ValueError("services 至少要有一个 MinerU 服务")
    services: list[ServiceConfig] = []
    seen: set[str] = set()
    for index, row in enumerate(service_rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 个服务必须是表")
        name = row.get("name")
        base_url = row.get("base_url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 个服务缺少 name")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError(f"服务 {name} 的 base_url 必须以 http:// 或 https:// 开头")
        if name in seen:
            raise ValueError(f"服务名称重复：{name}")
        seen.add(name)
        target = _positive_int(row.get("target", global_target), f"服务 {name} 的 target")
        maximum = _positive_int(row.get("max", global_max), f"服务 {name} 的 max")
        if maximum < target:
            raise ValueError(f"服务 {name} 的 max 不能小于 target")
        api_key = row.get("api_key", "")
        if not isinstance(api_key, str):
            raise ValueError(f"服务 {name} 的 api_key 必须是字符串")
        service_release = row.get("release_remote_jobs", release_remote_jobs)
        if not isinstance(service_release, bool):
            raise ValueError(f"服务 {name} 的 release_remote_jobs 必须是布尔值")
        services.append(
            ServiceConfig(
                name=name.strip(),
                base_url=base_url.rstrip("/"),
                api_key=api_key.strip(),
                target=target,
                max_concurrency=maximum,
                release_remote_jobs=service_release,
            )
        )

    monitor = raw.get("monitor", {})
    if not isinstance(monitor, dict):
        raise ValueError("monitor 必须是表")
    host = monitor.get("host", "0.0.0.0")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("monitor.host 必须是非空字符串")
    port = _positive_int(monitor.get("port", 8765), "monitor.port")

    return AppConfig(
        tasks_csv=_resolve(base_dir, tasks_csv.strip()),
        output_root=output_path,
        progress_log=progress_log,
        size_cache=size_cache,
        tier=tier.strip(),
        output_formats=tuple(formats),
        poll_interval_seconds=_positive_float(raw.get("poll_interval_seconds", 3), "poll_interval_seconds"),
        max_polls=_positive_int(raw.get("max_polls", 1200), "max_polls"),
        request_timeout_seconds=_positive_float(
            raw.get("request_timeout_seconds", 60), "request_timeout_seconds"
        ),
        transfer_timeout_seconds=_positive_float(
            raw.get("transfer_timeout_seconds", 600), "transfer_timeout_seconds"
        ),
        release_remote_jobs=release_remote_jobs,
        services=tuple(services),
        monitor_host=host.strip(),
        monitor_port=port,
    )
