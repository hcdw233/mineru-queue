import tempfile
import unittest
from pathlib import Path

from mineru_queue.serve import resolve_web_file


class WebFileTest(unittest.TestCase):
    def test_index_and_assets_stay_inside_the_web_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("ok", encoding="utf-8")
            (root / "app.js").write_text("ok", encoding="utf-8")
            self.assertEqual(resolve_web_file(root, "/"), (root / "index.html").resolve())
            self.assertEqual(resolve_web_file(root, "/app.js"), (root / "app.js").resolve())
            self.assertIsNone(resolve_web_file(root, "/../secret"))
            self.assertIsNone(resolve_web_file(root, "/missing.css"))


if __name__ == "__main__":
    unittest.main()
