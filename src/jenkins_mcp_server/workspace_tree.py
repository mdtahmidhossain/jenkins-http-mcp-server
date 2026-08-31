from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .client import JenkinsClient, job_path
from .errors import (
    JenkinsProtocolError,
    ResponseTooLargeError,
    ToolInputError,
    WorkspaceListingError,
)
from .workspace_bundle import WORKSPACE_MAGIC_SEGMENTS, normalize_workspace_path

DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_ENTRIES = 1_000
MAX_DEPTH = 10
MAX_ENTRIES = 2_000
MAX_WORKSPACE_PATH_BYTES = 4_096

JsonDict = dict[str, Any]


class _ListingLimitReached(Exception):
    pass


@dataclass(frozen=True)
class _Child:
    name: str
    is_directory: bool


class _ListingReader:
    def __init__(self, client: JenkinsClient, job_url_path: str) -> None:
        self.client = client
        self.job_url_path = job_url_path
        self.limit = client.config.max_response_bytes
        self.bytes_read = 0
        self.requests = 0

    def read(self, directory: str) -> list[_Child]:
        remaining = self.limit - self.bytes_read
        if remaining < 1:
            raise _ListingLimitReached

        path = _workspace_listing_path(self.job_url_path, directory)
        try:
            response = self.client.request("GET", path, max_bytes=remaining)
        except ResponseTooLargeError as exc:
            raise _ListingLimitReached from exc

        self.requests += 1
        self.bytes_read += len(response.content)
        return _parse_listing(response, path)


def _workspace_listing_path(job_url_path: str, directory: str) -> str:
    base = f"{job_url_path}/ws"
    if directory:
        encoded = "/".join(quote(part, safe="") for part in directory.split("/"))
        base = f"{base}/{encoded}"
    return f"{base}/*plain*"


def _parse_listing(response: httpx.Response, path: str) -> list[_Child]:
    content_type = response.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "text/plain":
        raise WorkspaceListingError(
            "Jenkins did not return a plain-text workspace directory listing; "
            "the job may have no workspace or its job type may not expose the core /ws endpoint"
        )

    content = response.content
    if not content:
        return []
    if not content.endswith(b"\n"):
        raise JenkinsProtocolError(
            f"Jenkins workspace listing at {path} did not end with a newline"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JenkinsProtocolError(
            f"Jenkins workspace listing at {path} was not valid UTF-8"
        ) from exc

    children: list[_Child] = []
    seen: set[str] = set()
    for line in text[:-1].split("\n"):
        is_directory = line.endswith("/")
        name = line[:-1] if is_directory else line
        if (
            not name
            or name in {".", ".."}
            or name != name.strip()
            or "/" in name
            or "\\" in name
            or "*" in name
            or "?" in name
            or name in WORKSPACE_MAGIC_SEGMENTS
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise JenkinsProtocolError(
                f"Jenkins workspace listing at {path} contained an unsafe entry name"
            )
        if name in seen:
            raise JenkinsProtocolError(
                f"Jenkins workspace listing at {path} contained a duplicate entry"
            )
        seen.add(name)
        children.append(_Child(name=name, is_directory=is_directory))

    return sorted(children, key=lambda child: (not child.is_directory, child.name))


def _normalize_directory_path(workspace_path: str) -> str:
    if not workspace_path.strip():
        return ""
    normalized = normalize_workspace_path(workspace_path)
    if len(normalized.encode("utf-8")) > MAX_WORKSPACE_PATH_BYTES:
        raise ToolInputError(
            f"workspace_path must be at most {MAX_WORKSPACE_PATH_BYTES} UTF-8 bytes"
        )
    return normalized


def _validate_limits(max_depth: int, max_entries: int) -> None:
    if not 1 <= max_depth <= MAX_DEPTH:
        raise ToolInputError(f"max_depth must be between 1 and {MAX_DEPTH}")
    if not 1 <= max_entries <= MAX_ENTRIES:
        raise ToolInputError(f"max_entries must be between 1 and {MAX_ENTRIES}")


def _resolve_directory(reader: _ListingReader, workspace_path: str) -> None:
    current = ""
    for segment in workspace_path.split("/") if workspace_path else ():
        try:
            children = reader.read(current)
        except _ListingLimitReached as exc:
            raise ResponseTooLargeError(reader.limit) from exc
        match = next((child for child in children if child.name == segment), None)
        candidate = f"{current}/{segment}" if current else segment
        if match is None:
            raise WorkspaceListingError(
                f"Workspace directory {candidate!r} was not present; "
                "it may not exist or /ws changed"
            )
        if not match.is_directory:
            raise WorkspaceListingError(f"Workspace path {candidate!r} is a file, not a directory")
        current = candidate


def get_workspace_tree(
    client: JenkinsClient,
    job: str | list[str],
    workspace_path: str = "",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> JsonDict:
    """Return a bounded tree from Jenkins core's line-based workspace listings."""
    _validate_limits(max_depth, max_entries)
    normalized_path = _normalize_directory_path(workspace_path)
    job_url_path = job_path(job)
    job_names = [part for part in job.split("/") if part] if isinstance(job, str) else job
    reader = _ListingReader(client, job_url_path)
    _resolve_directory(reader, normalized_path)

    pending = deque([(normalized_path, 0)])
    entries: list[JsonDict] = []
    truncation_reasons: set[str] = set()
    directories_scanned = 0
    directory_count = 0
    file_count = 0

    while pending:
        if len(entries) >= max_entries:
            truncation_reasons.add("max_entries")
            break
        directory, parent_depth = pending.popleft()
        try:
            children = reader.read(directory)
        except _ListingLimitReached as exc:
            if not entries:
                raise ResponseTooLargeError(reader.limit) from exc
            truncation_reasons.add("max_response_bytes")
            break
        directories_scanned += 1

        entry_limit_reached = False
        for child in children:
            if len(entries) >= max_entries:
                truncation_reasons.add("max_entries")
                entry_limit_reached = True
                break
            depth = parent_depth + 1
            child_path = f"{directory}/{child.name}" if directory else child.name
            entry_type = "directory" if child.is_directory else "file"
            entries.append({"path": child_path, "type": entry_type, "depth": depth})
            if child.is_directory:
                directory_count += 1
                if depth < max_depth:
                    pending.append((child_path, depth))
                else:
                    truncation_reasons.add("max_depth")
            else:
                file_count += 1
        if entry_limit_reached:
            break

    return {
        "job": "/".join(job_names),
        "workspace_path": normalized_path,
        "entries": entries,
        "entry_count": len(entries),
        "directory_count": directory_count,
        "file_count": file_count,
        "directories_scanned": directories_scanned,
        "listing_requests": reader.requests,
        "listing_bytes_read": reader.bytes_read,
        "limits": {
            "max_depth": max_depth,
            "max_entries": max_entries,
            "max_response_bytes": reader.limit,
        },
        "truncated": bool(truncation_reasons),
        "truncation_reasons": sorted(truncation_reasons),
        "workspace_freshness": "best_effort",
        "data_trust": "untrusted",
        "warning": (
            "Jenkins /ws is a dynamic job-level workspace. Entries are not bound to a build "
            "number; use archived artifacts for exact historical build files."
        ),
    }
