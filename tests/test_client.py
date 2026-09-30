import io
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mineru_queue.config import load_config
from mineru_queue.http_util import perform
from mineru_queue.mineru_client import MinerUClient
from mineru_queue.progress import CANCELED, FAILED, PARTIAL, SUCCESS, TIMEOUT


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("paper.md", "hello")
        archive.writestr("assets/fig.txt", "img")
    return buffer.getvalue()


class MinerUHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._read_body()
        if self.path == "/v1/uploads":
            self._json({"id": "up1", "status": "pending", "upload_url": "/v1/uploads/up1/content", "upload_method": "PUT"})
            return
        if self.path == "/v1/uploads/up1/complete":
            self._json({"status": "completed", "file": {"id": "file1"}})
            return
        if self.path == "/v1/parse/jobs":
            self.server.submit_count += 1
            mode = self.server.mode
            if mode == "failed":
                self._json({"job_id": "job1", "status": "failed", "files": [{"status": "failed", "error": {"message": "boom"}}]})
                return
            if mode == "canceled":
                self._json({"job_id": "job1", "status": "canceled", "files": []})
                return
            if mode == "partial":
                self._json({"job_id": "job1", "status": "partial", "files": [{"status": "completed", "output_files": {"zip": {"file_id": "zip1"}}}]})
                return
            if mode == "timeout":
                self._json({"job_id": "job1", "status": "queued", "files": []})
                return
            self._json({
                "job_id": "job1",
                "status": "completed",
                "files": [{"status": "completed", "name": "paper.pdf", "output_files": {"zip": {"file_id": "zip1"}}}],
            })
            return
        if self.path == "/v1/parse/jobs/job1/release":
            if self.server.release_fail:
                self.send_error(503)
                return
            self.server.released.append("job1")
            self._json({"job_id": "job1", "released": True})
            return
        self.send_error(404)

    def do_PUT(self):
        self.server.auth = self.headers.get("Authorization")
        self._read_body()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/parse/jobs/job1":
            if self.server.mode == "timeout":
                self._json({"job_id": "job1", "status": "queued", "files": []})
            else:
                self._json({"job_id": "job1", "status": "completed", "files": [{"status": "completed", "output_files": {"zip": {"file_id": "zip1"}}}]})
            return
        if self.path == "/v1/files/zip1/content":
            if self.server.mode == "download_fail":
                self.send_error(503)
                return
            payload = _zip_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/zip")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class BlobHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_auth = self.headers.get("Authorization")
        body = _zip_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _start(handler, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.mode = "completed"
    server.auth = None
    server.redirect_to = ""
    server.seen_auth = "unset"
    server.released = []
    server.release_fail = False
    server.submit_count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _config(root: Path, base_url: str, *, max_polls: int = 5) -> object:
    path = root / "config.toml"
    path.write_text(
        f"""
tasks_csv = "tasks.csv"
output_root = "out"
poll_interval_seconds = 0.01
max_polls = {max_polls}
request_timeout_seconds = 5
transfer_timeout_seconds = 5
release_remote_jobs = true
[[services]]
name = "local"
base_url = "{base_url}"
api_key = "secret"
""",
        encoding="utf-8",
    )
    return load_config(path)


class ClientTest(unittest.TestCase):
    def test_completed_job_saves_zip_and_extracted_files(self):
        server = _start(MinerUHandler)
        self.addCleanup(_stop, server)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            config = _config(root, f"http://127.0.0.1:{server.server_port}")
            dest = root / "out" / "user" / "paper"
            status = MinerUClient(config).parse(config.services[0], pdf, dest)
            self.assertEqual(status, SUCCESS)
            self.assertEqual(server.auth, "Bearer secret")
            self.assertTrue((dest / "result.zip").is_file())
            self.assertEqual((dest / "paper.md").read_text(encoding="utf-8"), "hello")
            self.assertEqual((dest / "assets" / "fig.txt").read_text(encoding="utf-8"), "img")
            self.assertFalse((dest / "mineru_error.txt").exists())
            self.assertEqual(server.released, ["job1"])
            self.assertFalse((dest / ".mineru_job.json").exists())

    def test_failed_and_canceled_and_timeout(self):
        server = _start(MinerUHandler)
        self.addCleanup(_stop, server)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            config = _config(root, f"http://127.0.0.1:{server.server_port}", max_polls=1)
            client = MinerUClient(config)
            dest = root / "failed"
            server.mode = "failed"
            self.assertEqual(client.parse(config.services[0], pdf, dest), FAILED)
            self.assertIn("boom", (dest / "mineru_error.txt").read_text(encoding="utf-8"))
            server.mode = "canceled"
            self.assertEqual(client.parse(config.services[0], pdf, root / "canceled"), CANCELED)
            server.mode = "partial"
            partial_dir = root / "partial"
            self.assertEqual(client.parse(config.services[0], pdf, partial_dir), PARTIAL)
            self.assertFalse((partial_dir / "result.zip").exists())
            server.mode = "timeout"
            self.assertEqual(client.parse(config.services[0], pdf, root / "timeout"), TIMEOUT)
            self.assertEqual(server.released, ["job1", "job1", "job1", "job1"])

    def test_failed_release_is_retried_and_download_failure_resumes_job(self):
        server = _start(MinerUHandler)
        self.addCleanup(_stop, server)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            config = _config(root, f"http://127.0.0.1:{server.server_port}")
            client = MinerUClient(config)
            dest = root / "out" / "paper"
            server.mode = "download_fail"
            self.assertEqual(client.parse(config.services[0], pdf, dest), FAILED)
            self.assertTrue((dest / ".mineru_job.json").exists())
            self.assertEqual(server.submit_count, 1)
            server.mode = "completed"
            server.release_fail = True
            self.assertEqual(client.parse(config.services[0], pdf, dest), SUCCESS)
            self.assertEqual(server.submit_count, 1)
            self.assertTrue((config.output_root / ".mineru_pending_releases" / "job1.json").exists())
            server.release_fail = False
            client.retry_pending_releases()
            self.assertFalse((config.output_root / ".mineru_pending_releases" / "job1.json").exists())
            self.assertEqual(server.released, ["job1"])

    def test_download_redirect_does_not_forward_the_api_key(self):
        blob = _start(BlobHandler)
        server = _start(MinerUHandler)
        self.addCleanup(_stop, blob)
        self.addCleanup(_stop, server)
        server.redirect_to = f"http://127.0.0.1:{blob.server_port}/blob"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.part"
            perform(
                "GET",
                f"http://127.0.0.1:{server.server_port}/redirect",
                headers={"Authorization": "Bearer secret"},
                timeout=5,
                dest_path=dest,
                follow_redirects=True,
                auth_origin=f"http://127.0.0.1:{server.server_port}",
            )
            self.assertIsNone(blob.seen_auth)
            self.assertTrue(dest.is_file())


if __name__ == "__main__":
    unittest.main()
