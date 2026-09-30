import tempfile
import unittest
import zipfile
from pathlib import Path

from mineru_queue.config import load_config
from mineru_queue.extract import extract_zip
from mineru_queue.tasks import load_tasks, output_dir


class ConfigTest(unittest.TestCase):
    def test_service_limits_override_the_global_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                """
tasks_csv = "tasks.csv"
output_root = "out"
tier = "standard"
output_formats = ["markdown", "zip"]
[concurrency]
target = 6
max = 8
[[services]]
name = "h100"
base_url = "http://127.0.0.1:8000"
[[services]]
name = "a100"
base_url = "http://127.0.0.1:8001/"
target = 2
max = 4
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.tasks_csv, (root / "tasks.csv").resolve())
            self.assertEqual(config.services[0].target, 6)
            self.assertEqual(config.services[0].max_concurrency, 8)
            self.assertEqual(config.services[1].target, 2)
            self.assertEqual(config.services[1].max_concurrency, 4)
            self.assertEqual(config.services[1].base_url, "http://127.0.0.1:8001")

    def test_zip_format_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
tasks_csv = "tasks.csv"
output_root = "out"
output_formats = ["markdown"]
[[services]]
name = "h100"
base_url = "http://127.0.0.1:8000"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(config_path)


class PathTest(unittest.TestCase):
    def test_output_dir_stays_under_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = output_dir(root, "user/10704yj01/paper")
            self.assertEqual(dest, (root / "user" / "10704yj01" / "paper").resolve())
            with self.assertRaises(ValueError):
                output_dir(root, "../outside")

    def test_extract_rejects_parent_segments_and_keeps_normal_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "result.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("paper.md", "hello")
                handle.writestr("../escape.txt", "no")
            with self.assertRaises(ValueError):
                extract_zip(archive, root / "out")

            safe = root / "safe.zip"
            with zipfile.ZipFile(safe, "w") as handle:
                handle.writestr("subdir/paper.md", "hello")
            dest = root / "safe-out"
            dest.mkdir()
            extract_zip(safe, dest)
            self.assertEqual((dest / "subdir" / "paper.md").read_text(encoding="utf-8"), "hello")

    def test_load_tasks_sorts_by_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.csv"
            path.write_text(
                "queue_order,task_key,pdf_primary_path,output_rel_dir\n"
                "2,b,/pdf/b.pdf,thesis_pdf/b\n"
                "1,a,/pdf/a.pdf,user/a\n",
                encoding="utf-8",
            )
            tasks = load_tasks(path)
            self.assertEqual([task.task_key for task in tasks], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
