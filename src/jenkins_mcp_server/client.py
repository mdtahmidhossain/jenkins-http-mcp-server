from __future__ import annotations

import errno
import json
import shutil
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from .config import JenkinsConfig
from .crumbs import CrumbManager
from .errors import (
    InsufficientDiskSpaceError,
    JenkinsHTTPError,
    JenkinsProtocolError,
    JenkinsTransportError,
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
    ToolInputError,
)

Json = dict[str, Any] | list[Any]
T = TypeVar("T")

GET_MAX_ATTEMPTS = 3
GET_RETRY_BACKOFF_SECONDS = 0.25
RETRYABLE_GET_STATUSES = {429, 502, 503, 504}
ERROR_BODY_LIMIT = 1_000
SEARCH_PATTERN_MAX_BYTES = 1_024
SEARCH_MATCH_MAX = 100
SEARCH_SNIPPET_BYTES = 240


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


def ensure_free_space(path: Path, required_bytes: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(path).free
    if available < required_bytes:
        raise InsufficientDiskSpaceError(required_bytes, available, str(path))


def _transport_kind(exc: httpx.RequestError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError):
            return "tls"
        message = str(current).lower()
        if "certificate verify failed" in message or "ssl" in message or "tls" in message:
            return "tls"
        current = current.__cause__ or current.__context__

    if isinstance(exc, httpx.ConnectError):
        return "connection"
    return "transport"


class JenkinsClient:
    def __init__(
        self,
        config: JenkinsConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        crumb_manager: CrumbManager | None = None,
    ) -> None:
        self.config = config
        self.crumbs = crumb_manager or CrumbManager(
            max_bytes=min(config.max_response_bytes, 64_000)
        )
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

    def _transport_error(
        self,
        exc: httpx.RequestError,
        method: str,
        path: str,
        attempts: int,
    ) -> JenkinsTransportError:
        return JenkinsTransportError(_transport_kind(exc), method, path, attempts)

    @staticmethod
    def _detached_response(response: httpx.Response, content: bytes) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            request=response.request,
            extensions=response.extensions,
        )

    def _read_error_response(self, response: httpx.Response) -> httpx.Response:
        collected = bytearray()
        error_limit = min(ERROR_BODY_LIMIT, self.config.max_response_bytes)
        for chunk in response.iter_bytes():
            remaining = error_limit - len(collected)
            if remaining <= 0:
                break
            collected.extend(chunk[:remaining])
            if len(chunk) > remaining:
                break
        return self._detached_response(response, bytes(collected))

    def _read_bounded_response(self, response: httpx.Response, limit: int) -> httpx.Response:
        raw_length = response.headers.get("Content-Length")
        if raw_length and raw_length.isdigit() and int(raw_length) > limit:
            raise ResponseTooLargeError(limit)

        collected = bytearray()
        for chunk in response.iter_bytes():
            remaining = limit - len(collected)
            if len(chunk) > remaining:
                raise ResponseTooLargeError(limit)
            collected.extend(chunk)
        return self._detached_response(response, bytes(collected))

    @staticmethod
    def _sleep_before_retry(attempt: int) -> None:
        time.sleep(GET_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    def _stream_get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        consume: Callable[[httpx.Response], T],
    ) -> T:
        url, relative = self._url(path)
        for attempt in range(1, GET_MAX_ATTEMPTS + 1):
            try:
                with self.http.stream(
                    "GET",
                    url,
                    params=params,
                    headers=dict(headers or {}),
                ) as response:
                    if response.status_code >= 300:
                        error_response = self._read_error_response(response)
                    else:
                        return consume(response)
            except httpx.RequestError as exc:
                kind = _transport_kind(exc)
                if kind != "tls" and attempt < GET_MAX_ATTEMPTS:
                    self._sleep_before_retry(attempt)
                    continue
                raise self._transport_error(exc, "GET", relative, attempt) from exc

            if response.status_code in RETRYABLE_GET_STATUSES and attempt < GET_MAX_ATTEMPTS:
                self._sleep_before_retry(attempt)
                continue
            if 300 <= error_response.status_code < 400:
                raise JenkinsHTTPError(
                    error_response.status_code,
                    "GET",
                    relative,
                    "Unexpected redirect; external redirects are disabled",
                    _body_snippet(error_response),
                )
            self._raise_for_status(error_response, "GET", relative)

        raise AssertionError("GET retry loop exhausted")  # pragma: no cover

    def _post_bounded_once(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
        content: str | bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> httpx.Response:
        url, relative = self._url(path)
        try:
            with self.http.stream(
                "POST",
                url,
                params=params,
                data=data,
                content=content,
                headers=dict(headers),
            ) as response:
                if response.status_code >= 400:
                    return self._read_error_response(response)
                return self._read_bounded_response(response, limit)
        except httpx.RequestError as exc:
            raise self._transport_error(exc, "POST", relative, 1) from exc

    def _get_crumb(self, *, required: bool) -> Any:
        try:
            return self.crumbs.get(self.http, self.config.url)
        except httpx.HTTPStatusError:
            return None
        except (JenkinsProtocolError, ResponseTooLargeError):
            if required:
                raise
            return None
        except httpx.RequestError as exc:
            if required:
                raise self._transport_error(exc, "GET", "crumbIssuer/api/json", 1) from exc
            return None

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
        limit = self.config.max_response_bytes if max_bytes is None else max_bytes

        if method == "GET":
            return self._stream_get(
                path,
                params=params,
                headers=headers,
                consume=lambda response: self._read_bounded_response(response, limit),
            )

        request_headers = dict(headers or {})
        crumb = self._get_crumb(required=False)
        if crumb is not None:
            request_headers[crumb.request_field] = crumb.crumb

        response = self._post_bounded_once(
            path,
            params=params,
            data=data,
            content=content,
            headers=request_headers,
            limit=limit,
        )
        body = _body_snippet(response, ERROR_BODY_LIMIT) or ""
        if response.status_code == 403 and "crumb" in body.lower():
            self.crumbs.clear()
            crumb = self._get_crumb(required=True)
            retry_headers = dict(request_headers)
            if crumb is not None:
                retry_headers[crumb.request_field] = crumb.crumb
            response = self._post_bounded_once(
                path,
                params=params,
                data=data,
                content=content,
                headers=retry_headers,
                limit=limit,
            )

        _, relative = self._url(path)
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
        def consume(response: httpx.Response) -> dict[str, Any]:
            collected = bytearray()
            truncated = False
            for chunk in response.iter_bytes():
                remaining = limit - len(collected)
                if remaining <= 0:
                    truncated = True
                    break
                collected.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    break
            return {
                "text": collected.decode("utf-8", errors="replace"),
                "bytes_returned": len(collected),
                "truncated": truncated,
                "limit": limit,
            }

        return self._stream_get(path, params=None, headers=None, consume=consume)

    def get_progressive_text(self, path: str, *, start: int, limit: int) -> dict[str, Any]:
        if start < 0:
            raise ToolInputError("start must be >= 0")
        response = self.request("GET", path, params={"start": start}, max_bytes=limit)
        raw_next_start = response.headers.get("X-Text-Size")
        if raw_next_start is None:
            raise JenkinsProtocolError("Jenkins progressive log response omitted X-Text-Size")
        try:
            next_start = int(raw_next_start)
        except ValueError as exc:
            raise JenkinsProtocolError(
                "Jenkins progressive log response had an invalid X-Text-Size"
            ) from exc
        if next_start < 0:
            raise JenkinsProtocolError("Jenkins progressive log cursor must not be negative")

        more_data = response.headers.get("X-More-Data", "").lower() == "true"
        return {
            "text": response.content.decode("utf-8", errors="replace"),
            "bytes_returned": len(response.content),
            "start": start,
            "next_start": next_start,
            "more_data": more_data,
            "complete": not more_data,
            "cursor_reset": next_start < start,
            "limit": limit,
        }

    def search_text(
        self,
        path: str,
        *,
        pattern: str,
        max_scan_bytes: int,
        max_matches: int,
    ) -> dict[str, Any]:
        needle = pattern.encode("utf-8")
        if not needle:
            raise ToolInputError("pattern must not be empty")
        if len(needle) > SEARCH_PATTERN_MAX_BYTES:
            raise ToolInputError(
                f"pattern must be at most {SEARCH_PATTERN_MAX_BYTES} UTF-8 bytes"
            )
        if max_scan_bytes < 1:
            raise ToolInputError("max_scan_bytes must be >= 1")
        if not 1 <= max_matches <= SEARCH_MATCH_MAX:
            raise ToolInputError(f"max_matches must be between 1 and {SEARCH_MATCH_MAX}")

        def consume(response: httpx.Response) -> dict[str, Any]:
            matches: list[dict[str, Any]] = []
            bytes_scanned = 0
            newline_count = 0
            tail = b""
            tail_limit = max(len(needle) - 1, SEARCH_SNIPPET_BYTES)
            truncated = False
            match_limit_reached = False

            for chunk in response.iter_bytes():
                remaining = max_scan_bytes - bytes_scanned
                if remaining <= 0:
                    truncated = True
                    break
                part = chunk[:remaining]
                combined = tail + part
                combined_offset = bytes_scanned - len(tail)
                newlines_before_combined = newline_count - tail.count(b"\n")
                search_at = max(0, len(tail) - len(needle) + 1)

                while len(matches) < max_matches:
                    found = combined.find(needle, search_at)
                    if found < 0:
                        break
                    global_offset = combined_offset + found
                    search_at = found + len(needle)

                    line_start = combined.rfind(b"\n", 0, found) + 1
                    line_end = combined.find(b"\n", found + len(needle))
                    if line_end < 0:
                        line_end = len(combined)
                    snippet_start = max(line_start, found - SEARCH_SNIPPET_BYTES)
                    snippet_end = min(line_end, found + len(needle) + SEARCH_SNIPPET_BYTES)
                    snippet = combined[snippet_start:snippet_end].decode(
                        "utf-8",
                        errors="replace",
                    )
                    matches.append(
                        {
                            "byte_offset": global_offset,
                            "line_number": (
                                newlines_before_combined + combined[:found].count(b"\n") + 1
                            ),
                            "snippet": snippet,
                            "snippet_truncated": (
                                snippet_start > line_start or snippet_end < line_end
                            ),
                        }
                    )

                bytes_scanned += len(part)
                newline_count += part.count(b"\n")
                tail = combined[-tail_limit:]

                if len(matches) == max_matches:
                    match_limit_reached = True
                    break
                if len(chunk) > len(part):
                    truncated = True
                    break

            return {
                "pattern": pattern,
                "matches": matches,
                "match_count": len(matches),
                "bytes_scanned": bytes_scanned,
                "scan_limit": max_scan_bytes,
                "scan_truncated": truncated,
                "match_limit_reached": match_limit_reached,
            }

        return self._stream_get(path, params=None, headers=None, consume=consume)

    def stream_to_file(
        self,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        progress_callback: Callable[[int, int | None], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)

        def consume(response: httpx.Response) -> dict[str, Any]:
            raw_total = response.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            if total is not None and total > max_bytes:
                raise ResponseTooLargeError(max_bytes)
            ensure_free_space(destination.parent, total if total is not None else max_bytes)

            downloaded = 0
            destination.unlink(missing_ok=True)
            try:
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
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    available = shutil.disk_usage(destination.parent).free
                    raise InsufficientDiskSpaceError(
                        downloaded,
                        available,
                        str(destination.parent),
                    ) from exc
                raise

            if progress_callback:
                progress_callback(downloaded, total)
            return {
                "path": str(destination),
                "bytes_downloaded": downloaded,
                "total_bytes": total,
            }

        try:
            return self._stream_get(path, params=None, headers=None, consume=consume)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

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
