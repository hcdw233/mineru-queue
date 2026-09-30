import csv
import tempfile
import threading
import unittest
from pathlib import Path

from mineru_queue.config import ServiceConfig, load_config
from mineru_queue.progress import FAILED, SUCCESS, ProgressLog
from mineru_queue.run import run_queue


class HoldingParser:
    def __init__(self) -> None:
        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self.calls: list[str] = []

    def parse(self, service: ServiceConfig, pdf_path: Path, dest: Path) -> str:
        self.calls.append(service.name)
        self.started.release()
        if not self.release.wait(5):
            return FAILED
        dest.mkdir(parents=True, exist_ok=True)
        if pdf_path.name == "bad.pdf":
            return FAILED
        return SUCCESS


def _write_config(root: Path) -> object:
    path = root / "config.toml"
    path.write_text(
        """
tasks_csv = "tasks.csv"
output_root = "out"
[concurrency]
target = 1
max = 2
[[services]]
name = "a"
base_url = "http://127.0.0.1:1"
[[services]]
name = "b"
base_url = "http://127.0.0.1:2"
""",
        encoding="utf-8",
    )
    return load_config(path)


def _write_tasks(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["queue_order", "task_key", "pdf_primary_path", "output_rel_dir"])
        writer.writeheader()
        for index, (key, filename) in enumerate(rows, start=1):
            writer.writerow({
                "queue_order": index,
                "task_key": key,
                "pdf_primary_path": str(path.parent / filename),
                "output_rel_dir": f"batch/{Path(filename).stem}",
            })


class RunQueueTest(unittest.TestCase):
    def test_resume_skips_success_and_failed_only_reruns_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("one.pdf", "two.pdf", "bad.pdf", "gone.pdf"):
                if name != "gone.pdf":
                    (root / name).write_bytes(b"%PDF")
            _write_tasks(root / "tasks.csv", [("one", "one.pdf"), ("two", "two.pdf"), ("bad", "bad.pdf"), ("gone", "gone.pdf")])
            config = _write_config(root)
            parser = HoldingParser()

            def watch() -> None:
                self.assertTrue(parser.started.acquire(timeout=3))
                self.assertTrue(parser.started.acquire(timeout=3))
                self.assertFalse(parser.started.acquire(timeout=0.2))
                parser.release.set()

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            run_queue(config, parser=parser)
            watcher.join(5)
            self.assertFalse(watcher.is_alive())

            log = ProgressLog(config.progress_log)
            log.catch_up()
            self.assertEqual(log.latest["one"], SUCCESS)
            self.assertEqual(log.latest["two"], SUCCESS)
            self.assertEqual(log.latest["bad"], FAILED)
            self.assertEqual(log.latest["gone"], FAILED)
            self.assertEqual(parser.calls, ["a", "b", "a"])

            before = config.progress_log.read_text(encoding="utf-8")
            calls_before = len(parser.calls)
            self.assertEqual(run_queue(config, parser=parser), 2)
            self.assertEqual(len(parser.calls), calls_before + 1)
            self.assertTrue(config.progress_log.read_text(encoding="utf-8").startswith(before))
            log.catch_up()
            self.assertEqual(log.latest["one"], SUCCESS)
            self.assertEqual(log.latest["two"], SUCCESS)

            calls_before = len(parser.calls)
            self.assertEqual(run_queue(config, only_failed=True, parser=parser), 2)
            self.assertEqual(len(parser.calls), calls_before + 1)
            log.catch_up()
            self.assertEqual(log.latest["bad"], FAILED)
            self.assertEqual(log.latest["one"], SUCCESS)


if __name__ == "__main__":
    unittest.main()
