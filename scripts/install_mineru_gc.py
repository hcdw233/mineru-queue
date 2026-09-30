"""Install the job blob reclamation change into the exact MinerU 4.0.5 wheel.

Run with the MinerU virtualenv's Python while all API servers are stopped.
The source hash guard prevents patching a different release or a locally edited file.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import pathlib
import py_compile
import shutil
import sys
import tempfile

ORIGINAL_SHA256 = "333b94786cd651cce6bed0dbbca0874ad80a8e960719a95f805bacb572d410fa"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"MinerU source shape changed; expected one occurrence, found {source.count(old)}: {old[:80]!r}")
    return source.replace(old, new, 1)


def patch_source(source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("api_server.py 不是官方 MinerU 4.0.5 原始文件；没有修改")

    source = replace_once(
        source,
        """        del self._files[file_id]

    def list_files(""",
        """        self.release_files({file_id})

    def release_files(self, file_ids: set[str]) -> None:
        \"\"\"Remove file records and unlink blobs only after the last reference goes away.\"\"\"
        hashes = {self._files[file_id].sha256sum for file_id in file_ids if file_id in self._files}
        for file_id in file_ids:
            self._files.pop(file_id, None)
        for upload_id, upload in list(self._uploads.items()):
            if upload.file_id in file_ids:
                self._uploads.pop(upload_id, None)
        live_hashes = {rec.sha256sum for rec in self._files.values()}
        for sha in hashes - live_hashes:
            if sha is None:
                continue
            blob = self._blob_abs(sha)
            blob.unlink(missing_ok=True)
            try:
                blob.parent.rmdir()
            except OSError:
                pass

    def list_files(""",
    )
    source = replace_once(
        source,
        """class JobStore:
    def __init__(self, concurrency: int = 1) -> None:
        self._jobs: dict[str, _JobRecord] = {}""",
        """class JobStore:
    def __init__(self, concurrency: int = 1, file_store: FileStore | None = None) -> None:
        self._jobs: dict[str, _JobRecord] = {}
        self._file_store = file_store
        self._release_requested: set[str] = set()
        self._released_files: dict[str, set[str]] = {}""",
    )
    source = replace_once(
        source,
        """                    if rec.status != "canceled":
                        rec.status = "failed"

        task = asyncio.create_task(run_owned(), name=f"mineru-job-{rec.id}")""",
        """                    if rec.status != "canceled":
                        rec.status = "failed"
            if rec.status in ("failed", "partial", "canceled") or rec.id in self._release_requested:
                self._finish_release(rec)

        task = asyncio.create_task(run_owned(), name=f"mineru-job-{rec.id}")""",
    )
    source = replace_once(
        source,
        """    def list_jobs(
        self,
        *,
        status_filter: str | None,""",
        """    @staticmethod
    def _job_file_ids(rec: _JobRecord) -> set[str]:
        ids: set[str] = set()
        for file in rec.files:
            if file.file_id:
                ids.add(file.file_id)
            if file.output_files:
                ids.update(ref.file_id for ref in vars(file.output_files).values() if ref is not None)
        return ids

    def _finish_release(self, rec: _JobRecord) -> None:
        self._released_files[rec.id] = self._job_file_ids(rec)
        self._gc_released()

    def _gc_released(self) -> None:
        if self._file_store is None:
            return
        protected: set[str] = set()
        for job_id, rec in self._jobs.items():
            if job_id not in self._released_files:
                protected.update(self._job_file_ids(rec))
        for ids in self._released_files.values():
            removable = ids - protected
            self._file_store.release_files(removable)
            ids.difference_update(removable)

    def release(self, job_id: str) -> _JobRecord:
        \"\"\"Idempotent ACK for completed jobs; active jobs are canceled and discarded.\"\"\"
        rec = self.get(job_id)
        self._release_requested.add(job_id)
        if rec.status not in ("completed", "partial", "failed", "canceled"):
            self.cancel(job_id)
        if job_id not in self._tasks and rec.status in ("completed", "partial", "failed", "canceled"):
            self._finish_release(rec)
        return rec

    def list_jobs(
        self,
        *,
        status_filter: str | None,""",
    )
    source = replace_once(
        source,
        """    store: FileStore = Depends(_get_store),
) -> FileDeletionResponse:
    \"\"\"Delete a file from the current tenant view.\"\"\"
    store.delete_file(file_id)
    return FileDeletionResponse(id=file_id, deleted=True)""",
        """    store: FileStore = Depends(_get_store),
    job_store: JobStore = Depends(_get_job_store),
) -> FileDeletionResponse:
    \"\"\"Delete an unattached file and its unreferenced physical blob.\"\"\"
    if any(
        file_id in job_store._job_file_ids(rec)
        for job_id, rec in job_store._jobs.items()
        if job_id not in job_store._released_files
    ):
        _raise_api_error(
            409, error_type="invalid_request_error", code="file_in_use",
            message="File is still referenced by a parse job",
        )
    store.delete_file(file_id)
    return FileDeletionResponse(id=file_id, deleted=True)""",
    )
    source = replace_once(
        source,
        """# ── Usage ─────────────────────────────────────────────────────────────""",
        """@_router.post(
    "/parse/jobs/{job_id}/release",
    status_code=status.HTTP_200_OK,
    responses={**_ERR_404},
    tags=["Jobs"],
)
async def release_job(
    job_id: str = Path(description="Job ID"),
    job_store: JobStore = Depends(_get_job_store),
) -> dict[str, object]:
    \"\"\"Acknowledge a downloaded job or discard a job that should be retried.\"\"\"
    rec = job_store.release(job_id)
    task = job_store._tasks.get(job_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
        job_store._finish_release(rec)
    return {"job_id": rec.id, "released": True, "status": rec.status}


# ── Usage ─────────────────────────────────────────────────────────────""",
    )
    source = replace_once(
        source,
        """    FileStore(_upload_dir).install(application.state)
    JobStore(concurrency=concurrency).install(application.state)""",
        """    FileStore(_upload_dir).install(application.state)
    JobStore(concurrency=concurrency, file_store=application.state.file_store).install(application.state)""",
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch MinerU 4.0.5 API server blob reclamation")
    parser.add_argument("--target", type=pathlib.Path, help="api_server.py path; defaults to the active virtualenv")
    parser.add_argument("--check", action="store_true", help="validate without installing")
    args = parser.parse_args()
    if args.target:
        target = args.target.resolve()
    else:
        spec = importlib.util.find_spec("mineru")
        if spec is None or not spec.submodule_search_locations:
            parser.error("当前 Python 环境找不到 MinerU")
        target = pathlib.Path(next(iter(spec.submodule_search_locations))) / "parser" / "api_server.py"
    original = target.read_bytes()
    modified = patch_source(original.decode("utf-8")).encode("utf-8")
    with tempfile.TemporaryDirectory() as temp:
        candidate = pathlib.Path(temp) / "api_server.py"
        candidate.write_bytes(modified)
        py_compile.compile(str(candidate), doraise=True)
    if args.check:
        print(f"兼容 MinerU 4.0.5：{target}")
        return 0
    backup = target.with_name("api_server.py.mineru-queue-4.0.5.bak")
    if backup.exists():
        parser.error(f"备份已存在，未覆盖：{backup}")
    shutil.copy2(target, backup)
    fd, temporary = tempfile.mkstemp(prefix=".api_server.", suffix=".py", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(modified)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, target.stat().st_mode)
        os.replace(temporary, target)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    print(f"已更新 {target}；原文件备份于 {backup}。请重启所有 MinerU API 服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
