"""命令行入口。解析和进度网页分开启动。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .run import run_queue
from .serve import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MinerU 多服务队列调度和进度查看")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="按任务表调用 MinerU，并把结果追加到进度文件")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument(
        "--only",
        choices=("failed",),
        help="只重跑进度文件里最新状态为 failed 的任务",
    )

    serve_parser = sub.add_parser("serve", help="启动进度 API，供网页读取")
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.command == "serve":
        if args.host:
            config = _replace_monitor(config, host=args.host)
        if args.port:
            config = _replace_monitor(config, port=args.port)
        serve(config)
        return 0

    run_queue(config, only_failed=args.only == "failed")
    return 0


def _replace_monitor(config, *, host: str | None = None, port: int | None = None):
    from dataclasses import replace

    return replace(
        config,
        monitor_host=host or config.monitor_host,
        monitor_port=port or config.monitor_port,
    )


if __name__ == "__main__":
    raise SystemExit(main())
