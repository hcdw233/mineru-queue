"""按 MinerU V1 的上传、解析、轮询、下载流程处理一个 PDF。

轮询中的状态是 queued、running，不写进度文件。
终态是 completed、partial、failed、canceled。
启用远端释放后，轮询超时会取消远端任务；成功任务在 ZIP 下载并解压后确认释放。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig, ServiceConfig
from .extract import extract_zip
from .http_util import HttpError, join_url, perform, same_origin
from .progress import CANCELED, FAILED, PARTIAL, SUCCESS, TIMEOUT

JOB_STATUSES = {"queued", "running", "completed", "partial", "failed", "canceled"}
TERMINAL_JOB_STATUSES = {"completed", "partial", "failed", "canceled"}
UPLOAD_STATUSES = {"pending", "completed"}
RESULT_ZIP = "result.zip"
LOGGER = logging.getLogger("mineru_queue.cleanup")


class ParseFailure(RuntimeError):
    def __init__(self, status: str, message: str, downloaded_at: datetime | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.downloaded_at = downloaded_at


class RetryableDownloadError(RuntimeError):
    """Keep the completed remote job so a later run can retry its ZIP download."""


@dataclass(frozen=True)
class ParseOutcome:
    status: str
    started_at: datetime
    downloaded_at: datetime | None = None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MinerUClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def parse(self, service: ServiceConfig, pdf_path: Path, dest: Path) -> str:
        """解析一个 PDF。返回要写入进度文件的状态。"""
        return self.parse_outcome(service, pdf_path, dest).status

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.part")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _resume_job_id(path: Path, services: tuple[ServiceConfig, ...]) -> tuple[ServiceConfig | None, str]:
        if not path.exists():
            return None, ""
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ParseFailure(FAILED, f"远端任务记录无效：{path}")
        service = next((item for item in services if item.name == record.get("service")), None)
        if service is None:
            raise ParseFailure(FAILED, f"远端任务所属服务不在配置中：{path}")
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", job_id) is None:
            raise ParseFailure(FAILED, f"远端任务记录无效：{path}")
        return service, job_id

    def _pending_path(self, job_id: str) -> Path:
        return self.config.output_root / ".mineru_pending_releases" / f"{job_id}.json"

    def _release_or_queue(self, service: ServiceConfig, job_id: str) -> None:
        if not service.release_remote_jobs:
            return
        pending = self._pending_path(job_id)
        try:
            self._write_json_atomic(pending, {"service": service.name, "job_id": job_id})
        except OSError as exc:
            LOGGER.error("无法记录待释放任务 %s：%s", job_id, exc)
        try:
            response = self._json(
                service, "POST", f"/v1/parse/jobs/{job_id}/release",
                timeout=self.config.request_timeout_seconds,
            )
            if response.get("released") is not True:
                raise ValueError(f"释放响应缺少 released=true：{response}")
        except (HttpError, OSError, ValueError, ParseFailure) as exc:
            LOGGER.warning("远端任务 %s 暂未释放，已记录待重试：%s", job_id, exc)
        else:
            try:
                pending.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning("任务 %s 已释放，但无法删除待重试记录：%s", job_id, exc)

    def retry_pending_releases(self) -> None:
        if not any(service.release_remote_jobs for service in self.config.services):
            return
        by_name = {service.name: service for service in self.config.services}
        for pending in sorted((self.config.output_root / ".mineru_pending_releases").glob("*.json")):
            try:
                record = json.loads(pending.read_text(encoding="utf-8"))
                service = by_name[record["service"]]
                if not service.release_remote_jobs:
                    continue
                job_id = record["job_id"]
                if not isinstance(job_id, str) or pending.stem != job_id:
                    raise ValueError("job_id 不匹配")
                response = self._json(
                    service, "POST", f"/v1/parse/jobs/{job_id}/release",
                    timeout=self.config.request_timeout_seconds,
                )
                if response.get("released") is not True:
                    raise ValueError("释放响应缺少 released=true")
            except (KeyError, TypeError, ValueError, OSError, HttpError, ParseFailure) as exc:
                LOGGER.warning("待释放任务 %s 仍未释放：%s", pending, exc)
            else:
                try:
                    pending.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("任务 %s 已释放，但无法删除待重试记录：%s", job_id, exc)

    def parse_outcome(self, service: ServiceConfig, pdf_path: Path, dest: Path) -> ParseOutcome:
        """解析一个 PDF，并记下开始时间与 zip 下载完成时间。"""
        dest.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().astimezone()
        downloaded_at: datetime | None = None
        try:
            status, downloaded_at = self._parse(service, pdf_path, dest)
        except ParseFailure as exc:
            _write_error(dest, str(exc))
            return ParseOutcome(exc.status, started_at, exc.downloaded_at)
        except (OSError, HttpError, ValueError) as exc:
            _write_error(dest, str(exc))
            return ParseOutcome(FAILED, started_at, downloaded_at)
        if status == SUCCESS:
            (dest / "mineru_error.txt").unlink(missing_ok=True)
        return ParseOutcome(status, started_at, downloaded_at)

    def _parse(self, service: ServiceConfig, pdf_path: Path, dest: Path) -> tuple[str, datetime | None]:
        if not pdf_path.is_file():
            raise ParseFailure(FAILED, f"PDF 不存在：{pdf_path}")
        resume_path = dest / ".mineru_job.json"
        resume_service, job_id = self._resume_job_id(resume_path, self.config.services)
        if job_id:
            service = resume_service or service
            try:
                job = self._json(service, "GET", f"/v1/parse/jobs/{job_id}", timeout=self.config.request_timeout_seconds)
            except HttpError as exc:
                if "返回 404" in str(exc):
                    resume_path.unlink(missing_ok=True)
                    return self._parse(service, pdf_path, dest)
                raise ParseFailure(FAILED, f"无法恢复远端任务 job_id={job_id}：{exc}") from exc
            status = _required_string(job, "status", "恢复解析")
        else:
            file_id = self._upload(service, pdf_path)
            try:
                job = self._submit(service, file_id)
            except (ParseFailure, HttpError, OSError, ValueError):
                if service.release_remote_jobs:
                    try:
                        self._json(service, "DELETE", f"/v1/files/{file_id}", timeout=self.config.request_timeout_seconds)
                    except (ParseFailure, HttpError, OSError, ValueError) as exc:
                        LOGGER.warning("提交失败后，上传文件 %s 暂未释放：%s", file_id, exc)
                raise
            job_id = _required_string(job, "job_id", "提交解析")
            if re.fullmatch(r"[A-Za-z0-9_-]+", job_id) is None:
                raise ParseFailure(FAILED, f"提交解析返回了无效 job_id：{job_id!r}")
            status = _required_string(job, "status", "提交解析")
            try:
                self._write_json_atomic(resume_path, {"service": service.name, "job_id": job_id})
            except OSError:
                self._release_or_queue(service, job_id)
                raise
        _ensure_job_status(status, "提交解析")
        return self._finish_job(service, job, job_id, status, dest)

    def _finish_job(
        self, service: ServiceConfig, job: dict[str, object], job_id: str, status: str, dest: Path,
    ) -> tuple[str, datetime | None]:
        release = False
        resume_path = dest / ".mineru_job.json"
        try:
            result = self._poll_and_download(service, job, job_id, status, dest)
            release = True
            return result
        except RetryableDownloadError as exc:
            raise ParseFailure(FAILED, f"ZIP 下载失败，保留远端 job_id={job_id} 供下次重试：{exc}") from exc
        except (ParseFailure, HttpError, OSError, ValueError):
            release = True
            raise
        finally:
            if release:
                self._release_or_queue(service, job_id)
                try:
                    resume_path.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("任务 %s 已处理，但无法删除恢复记录：%s", job_id, exc)

    def _poll_and_download(
        self, service: ServiceConfig, job: dict[str, object], job_id: str, status: str, dest: Path
    ) -> tuple[str, datetime | None]:
        polls = 0
        while status not in TERMINAL_JOB_STATUSES:
            polls += 1
            if polls > self.config.max_polls:
                raise ParseFailure(TIMEOUT, f"轮询超过 {self.config.max_polls} 次，任务仍是 {status}，job_id={job_id}")
            time.sleep(self.config.poll_interval_seconds)
            job = self._json(
                service,
                "GET",
                f"/v1/parse/jobs/{job_id}",
                timeout=self.config.request_timeout_seconds,
            )
            status = _required_string(job, "status", "轮询解析")
            _ensure_job_status(status, "轮询解析")

        if status == "failed":
            raise ParseFailure(FAILED, f"MinerU 任务失败，job_id={job_id}，{_job_error(job)}")
        if status == "canceled":
            raise ParseFailure(CANCELED, f"MinerU 任务已取消，job_id={job_id}")
        if status == "partial":
            raise ParseFailure(PARTIAL, f"MinerU 返回 partial，job_id={job_id}，{_job_error(job)}")

        zip_id = _zip_file_id(job)
        if not zip_id:
            raise ParseFailure(FAILED if status == "completed" else PARTIAL, f"结果里没有 zip，job_id={job_id}")
        try:
            self._download_zip(service, zip_id, dest)
        except (HttpError, OSError) as exc:
            raise RetryableDownloadError(str(exc)) from exc
        downloaded_at = datetime.now().astimezone()
        try:
            extract_zip(dest / RESULT_ZIP, dest)
        except (OSError, ValueError) as exc:
            raise ParseFailure(FAILED, f"解压失败：{exc}", downloaded_at) from exc
        return SUCCESS, downloaded_at

    def _upload(self, service: ServiceConfig, pdf_path: Path) -> str:
        created = self._json(
            service,
            "POST",
            "/v1/uploads",
            json_body={
                "filename": pdf_path.name,
                "bytes": pdf_path.stat().st_size,
                "mime_type": "application/pdf",
                "purpose": "parse",
                "sha256sum": file_sha256(pdf_path),
            },
            timeout=self.config.request_timeout_seconds,
        )
        upload_status = _required_string(created, "status", "创建上传")
        if upload_status not in UPLOAD_STATUSES:
            raise ParseFailure(FAILED, f"创建上传返回了不支持的状态：{upload_status}")
        if upload_status == "pending":
            upload_url = _required_string(created, "upload_url", "创建上传")
            upload_method = str(created.get("upload_method") or "PUT")
            if upload_method != "PUT":
                raise ParseFailure(FAILED, f"不支持的上传方法：{upload_method}")
            absolute = join_url(service.base_url, upload_url)
            headers = _upload_headers(created)
            if same_origin(service.base_url, absolute):
                _add_auth(headers, service.api_key)
            perform(
                "PUT",
                absolute,
                headers=headers,
                body_path=pdf_path,
                timeout=self.config.transfer_timeout_seconds,
            )
            upload_id = _required_string(created, "id", "创建上传")
            created = self._json(
                service,
                "POST",
                f"/v1/uploads/{upload_id}/complete",
                timeout=self.config.request_timeout_seconds,
            )
            complete_status = _required_string(created, "status", "完成上传")
            if complete_status != "completed":
                raise ParseFailure(FAILED, f"完成上传返回了意外状态：{complete_status}")
        file_obj = created.get("file")
        if not isinstance(file_obj, dict):
            raise ParseFailure(FAILED, "上传响应缺少 file")
        file_id = file_obj.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ParseFailure(FAILED, "上传响应缺少 file.id")
        return file_id

    def _submit(self, service: ServiceConfig, file_id: str) -> dict[str, object]:
        return self._json(
            service,
            "POST",
            "/v1/parse/jobs",
            json_body={
                "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                "tier": self.config.tier,
                "output_formats": list(self.config.output_formats),
            },
            timeout=self.config.request_timeout_seconds,
        )

    def _download_zip(self, service: ServiceConfig, file_id: str, dest: Path) -> None:
        part = dest / f"{RESULT_ZIP}.part"
        headers: dict[str, str] = {}
        _add_auth(headers, service.api_key)
        perform(
            "GET",
            join_url(service.base_url, f"/v1/files/{file_id}/content"),
            headers=headers,
            timeout=self.config.transfer_timeout_seconds,
            dest_path=part,
            follow_redirects=True,
            auth_origin=service.base_url,
        )
        part.replace(dest / RESULT_ZIP)

    def _json(
        self,
        service: ServiceConfig,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        timeout: float,
    ) -> dict[str, object]:
        headers: dict[str, str] = {}
        _add_auth(headers, service.api_key)
        result = perform(
            method,
            join_url(service.base_url, path),
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )
        try:
            payload = json_loads(result.body)
        except ValueError as exc:
            raise ParseFailure(FAILED, f"{method} {path} 的响应不是 JSON") from exc
        if not isinstance(payload, dict):
            raise ParseFailure(FAILED, f"{method} {path} 的响应不是对象")
        return payload


def json_loads(body: bytes) -> object:
    import json

    return json.loads(body.decode("utf-8"))


def _add_auth(headers: dict[str, str], api_key: str) -> None:
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"


def _upload_headers(payload: dict[str, object]) -> dict[str, str]:
    raw = payload.get("upload_headers") or {}
    if not isinstance(raw, dict):
        raise ParseFailure(FAILED, "upload_headers 不是对象")
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ParseFailure(FAILED, "upload_headers 必须是字符串映射")
        if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
            raise ParseFailure(FAILED, "upload_headers 包含换行")
        headers[key] = value
    return headers


def _required_string(payload: dict[str, object], field: str, stage: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ParseFailure(FAILED, f"{stage} 的响应缺少 {field}")
    return value


def _ensure_job_status(status: str, stage: str) -> None:
    if status not in JOB_STATUSES:
        raise ParseFailure(FAILED, f"{stage} 返回了不支持的任务状态：{status}")


def _zip_file_id(job: dict[str, object]) -> str:
    files = job.get("files")
    if not isinstance(files, list):
        return ""
    for item in files:
        if not isinstance(item, dict) or item.get("status") not in {None, "completed"}:
            continue
        outputs = item.get("output_files")
        if not isinstance(outputs, dict):
            continue
        zip_info = outputs.get("zip")
        if isinstance(zip_info, dict):
            file_id = zip_info.get("file_id")
            if isinstance(file_id, str) and file_id:
                return file_id
    return ""


def _job_error(job: dict[str, object]) -> str:
    files = job.get("files")
    if not isinstance(files, list):
        return ""
    messages: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        error = item.get("error")
        if isinstance(error, dict):
            messages.append(str(error.get("message") or error.get("code") or ""))
        elif item.get("status"):
            messages.append(str(item.get("status")))
    return "；".join(message for message in messages if message)


def _write_error(dest: Path, message: str) -> None:
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "mineru_error.txt").write_text(message + "\n", encoding="utf-8")
    except OSError:
        return
