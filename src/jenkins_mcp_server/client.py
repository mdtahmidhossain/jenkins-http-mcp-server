from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from .config import JenkinsConfig
from .crumbs import CrumbManager
from .errors import (
    JenkinsHTTPError,
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
)

Json = dict[str, Any] | list[Any]


def _body_snippet(response: httpx.Response, limit: int = 500) -> str | None:
    if not response.content:
        return None
    text = response.text.replace("\r", "")
    return text[:limit]


def normalize_relative_path(path: str) -> str:
    raw = path.strip()
    if not raw:
        raise PathValidationError("Jenkins path must not be empty")
    split = urlsplit(raw)
    if split.scheme or split.netloc:
        raise PathValidationError("Only relative Jenkins paths are accepted")
    if raw.startswith("//"):
        raise PathValidationError("Protocol-relative URLs are not accepted")

    clean_path = split.path.lstrip("/")
    if not clean_path:
        raise PathValidationError("Jenkins path must include a path component")

    segments = []
    for segment in clean_path.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            raise PathValidationError("Path traversal is not allowed")
        decoded = segment.replace("%2e", ".").replace("%2E", ".")
        if decoded == "..":
            raise PathValidationError("Encoded path traversal is not allowed")
        segments.append(segment)

    if not segments:
        raise PathValidationError("Jenkins path must include a path component")

    normalized = "/".join(segments)
    query_pairs = parse_qsl(split.query, keep_blank_values=True)
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit(("", "", normalized, query, ""))


def append_api_json(path: str) -> str:
    path = normalize_relative_path(path)
    split = urlsplit(path)
    clean = split.path.rstrip("/")
    if not clean.endswith("/api/json") and clean != "api/json":
        clean = f"{clean}/api/json"
    return urlunsplit(("", "", clean, split.query, ""))


def job_path(job: str | list[str]) -> str:
    pieces = [piece for piece in job.split("/") if piece] if isinstance(job, str) else job
    if not pieces:
        raise PathValidationError("job must include at least one path segment")

    encoded: list[str] = []
    for piece in pieces:
        if not piece or piece in {".", ".."} or "/" in piece:
            raise PathValidationError("job path segments must be non-empty names")
        encoded.extend(["job", quote(piece, safe="")])
    return "/".join(encoded)


def safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value:
        raise PathValidationError(f"{label} must be a single Jenkins path segment")
    return quote(value, safe="")


class JenkinsClient:
    def __init__(
        self,
        config: JenkinsConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        crumb_manager: CrumbManager | None = None,
    ) -> None:
        self.config = config
        self.crumbs = crumb_manager or CrumbManager()
        auth = None
        if config.user and config.api_token:
            auth = httpx.BasicAuth(config.user, config.api_token)
        self.http = httpx.Client(
            auth=auth,
            verify=config.verify_ssl,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> JenkinsClient:
        return cls(JenkinsConfig.from_env())

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> JenkinsClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _url(self, path: str) -> tuple[str, str]:
        relative = normalize_relative_path(path)
        return self.config.url + relative, relative

    def _raise_for_status(self, response: httpx.Response, method: str, path: str) -> None:
        if response.status_code < 400:
            return
        reason = response.reason_phrase or "Jenkins request failed"
        raise JenkinsHTTPError(
            status_code=response.status_code,
            method=method,
            path=path,
            message=reason,
            body=_body_snippet(response),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        content: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise PathValidationError("Only GET and POST are supported internally")
        url, relative = self._url(path)
        limit = max_bytes or self.config.max_response_bytes
        request_headers = dict(headers or {})

        if method == "POST":
            try:
                crumb = self.crumbs.get(self.http, self.config.url)
            except httpx.HTTPStatusError:
                crumb = None
            if crumb is not None:
                request_headers[crumb.request_field] = crumb.crumb

        response = self.http.request(
            method,
            url,
            params=params,
            data=data,
            content=content,
            headers=request_headers,
        )
        if method == "POST" and response.status_code == 403 and _body_snippet(response):
            body = _body_snippet(response, 1000) or ""
            if "crumb" in body.lower():
                self.crumbs.clear()
                crumb = self.crumbs.get(self.http, self.config.url)
                retry_headers = dict(request_headers)
                if crumb is not None:
                    retry_headers[crumb.request_field] = crumb.crumb
                response = self.http.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    content=content,
                    headers=retry_headers,
                )

        if len(response.content) > limit:
            raise ResponseTooLargeError(limit)
        self._raise_for_status(response, method, relative)
        return response

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Json:
        response = self.request("GET", append_api_json(path), params=params)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise JenkinsHTTPError(
                response.status_code,
                "GET",
                normalize_relative_path(path),
                "Response was not JSON",
                _body_snippet(response),
            ) from exc
        return payload

    def get_text(self, path: str, *, params: Mapping[str, Any] | None = None) -> str:
        response = self.request("GET", path, params=params)
        return response.text

    def get_text_limited(self, path: str, *, limit: int) -> dict[str, Any]:
        url, relative = self._url(path)
        collected = bytearray()
        truncated = False
        with self.http.stream("GET", url) as response:
            if response.status_code >= 400:
                response.read()
            self._raise_for_status(response, "GET", relative)
            for chunk in response.iter_bytes():
                remaining = limit - len(collected)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    collected.extend(chunk[:remaining])
                    truncated = True
                    break
                collected.extend(chunk)
        return {
            "text": collected.decode("utf-8", errors="replace"),
            "bytes_returned": len(collected),
            "truncated": truncated,
            "limit": limit,
        }

    def stream_to_file(
        self,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        progress_callback: Callable[[int, int | None], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        url, relative = self._url(path)
        downloaded = 0
        destination.parent.mkdir(parents=True, exist_ok=True)

        with self.http.stream("GET", url) as response:
            self._raise_for_status(response, "GET", relative)
            raw_total = response.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            if total is not None and total > max_bytes:
                raise ResponseTooLargeError(max_bytes)

            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if cancel_check and cancel_check():
                        raise OperationCancelledError("Operation was cancelled")
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ResponseTooLargeError(max_bytes)
                    handle.write(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        if progress_callback:
            progress_callback(downloaded, total)
        return {
            "path": str(destination),
            "bytes_downloaded": downloaded,
            "total_bytes": total,
        }

    def post(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        content: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            path,
            params=params,
            data=data,
            content=content,
            headers=headers,
        )
        return {
            "status_code": response.status_code,
            "location": response.headers.get("Location"),
            "ok": True,
        }
