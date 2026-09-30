"""少量 HTTP 调用。上传按文件流式发送，下载重定向时不把 API key 带到别的源。"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection, HTTPResponse
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class HttpError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


def origin_of(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise HttpError(f"不是 http(s) 地址：{url}")
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def same_origin(left: str, right: str) -> bool:
    return origin_of(left) == origin_of(right)


def join_url(base: str, url: str) -> str:
    return urljoin(base.rstrip("/") + "/", url)


def perform(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: object | None = None,
    body_path: Path | None = None,
    timeout: float,
    dest_path: Path | None = None,
    follow_redirects: bool = False,
    auth_origin: str | None = None,
) -> HttpResult:
    """发送请求。dest_path 给定时把最终响应体流式写入该文件。"""
    current_method = method.upper()
    current_url = url
    current_headers = dict(headers or {})
    send_json = json_body
    send_path = body_path
    for _ in range(6):
        if send_json is not None and send_path is not None:
            raise HttpError("json_body 和 body_path 不能同时使用")
        try:
            status, response_headers, body = _exchange(
                current_method,
                current_url,
                current_headers,
                send_json,
                send_path,
                timeout,
                dest_path,
                follow_redirects,
            )
        except (OSError, HTTPException, TimeoutError) as exc:
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)
            raise HttpError(f"{current_method} {current_url} 失败：{exc}") from exc

        if follow_redirects and status in {301, 302, 303, 307, 308}:
            location = response_headers.get("location")
            if not location:
                raise HttpError(f"{current_url} 返回 {status} 但没有 Location")
            next_url = join_url(current_url, location)
            if status in {301, 302, 303} and current_method != "GET":
                current_method = "GET"
            if auth_origin is not None and not same_origin(auth_origin, next_url):
                current_headers.pop("Authorization", None)
            current_url = next_url
            send_json = None
            send_path = None
            continue

        if status < 200 or status >= 300:
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)
            detail = body.decode("utf-8", "replace")[:500]
            raise HttpError(f"{current_method} {current_url} 返回 {status}：{detail}")
        return HttpResult(status, response_headers, body, current_url)
    raise HttpError(f"重定向次数过多：{url}")


def _exchange(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: object | None,
    body_path: Path | None,
    timeout: float,
    dest_path: Path | None,
    defer_download: bool,
) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(url)
    scheme, host, port = origin_of(url)
    connection: HTTPConnection
    if scheme == "https":
        connection = HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        connection = HTTPConnection(host, port, timeout=timeout)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    request_headers = {"User-Agent": "mineru-queue", **headers}
    file_handle = None
    try:
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(payload))
            connection.request(method, target, body=payload, headers=request_headers)
        elif body_path is not None:
            request_headers["Content-Length"] = str(body_path.stat().st_size)
            file_handle = body_path.open("rb")
            connection.request(method, target, body=file_handle, headers=request_headers)
        else:
            connection.request(method, target, headers=request_headers)
        response: HTTPResponse = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        # 重定向时先把响应体读完丢掉，避免把跳转页写进目标文件。
        if defer_download and response.status in {301, 302, 303, 307, 308}:
            response.read()
            return response.status, response_headers, b""
        if dest_path is None:
            return response.status, response_headers, response.read()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise
        return response.status, response_headers, b""
    finally:
        if file_handle is not None:
            file_handle.close()
        connection.close()
