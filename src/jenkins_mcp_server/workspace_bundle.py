from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from .client import GET_MAX_ATTEMPTS, JenkinsClient, ensure_free_space, job_path, safe_segment
from .config import JenkinsConfig
from .errors import (
    JenkinsMCPError,
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
    ToolInputError,
    WorkspaceBundleError,
)
from .workspace_registry import WorkspaceOperationRegistry

JsonDict = dict[str, Any]
WORKSPACE_MAGIC_SEGMENTS = {"*zip*", "*plain*", "*view*", "*fingerprint*"}
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "cancelled"}
WORKSPACE_OPERATION_TYPES = {None, "workspace_bundle", "workspace_path_download"}
WORKSPACE_STATE_POLL_SECONDS = 10.0
WORKSPACE_CAPTURE_MAX_ATTEMPTS = 2
WORKSPACE_HEARTBEAT_SECONDS = 2.0
WORKSPACE_STATE_REQUESTS_PER_PROBE = 2
_SERVER_INSTANCE_ID = uuid.uuid4().hex
_ACTIVE_OPERATIONS: dict[str, threading.Thread] = {}
_ACTIVE_OPERATIONS_LOCK = threading.Lock()


class ProgressFile:
    def __init__(self, path: Path, data: JsonDict) -> None:
        self.path = path
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write()

    def update(self, **patch: Any) -> None:
        _deep_update(self.data, patch)
        self.data["updated_at"] = _timestamp()
        self.write()

    def write(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)


class _WorkspaceStateChanged(Exception):
    def __init__(self, state: JsonDict) -> None:
        super().__init__("Jenkins workspace state changed during download")
        self.state = state


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stale_before(config: JenkinsConfig) -> float:
    state_probe_budget = (
        config.timeout_seconds * GET_MAX_ATTEMPTS * WORKSPACE_STATE_REQUESTS_PER_PROBE
    )
    stale_seconds = max(
        120.0,
        state_probe_budget + 30.0,
        WORKSPACE_STATE_POLL_SECONDS * 4,
    )
    return time.time() - stale_seconds


def _request_key(config: JenkinsConfig, request: JsonDict) -> str:
    normalized_request = dict(request)
    normalized_request["job"] = job_path(normalized_request["job"])
    normalized_request["build"] = str(normalized_request["build"])
    identity = {
        "jenkins_url": config.url,
        "jenkins_user": config.user,
        "request": normalized_request,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_paths(root: Path, operation_id: str) -> tuple[Path, Path, Path]:
    operation_dir = operation_index_dir(root) / operation_id
    return operation_dir, operation_dir / "progress.json", operation_dir / "cancel"


def _path_under_root(root: Path, raw: str | Path, label: str) -> Path:
    path = Path(raw)
    root_resolved = root.resolve()
    path_resolved = path.resolve(strict=False)
    if path_resolved == root_resolved or root_resolved not in path_resolved.parents:
        raise WorkspaceBundleError(
            "workspace_operation_index_invalid",
            f"Workspace operation has an unsafe {label}",
        )
    return path


def _normalize_object_url(base_url: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return urljoin(base_url, value).rstrip("/")


def _build_state(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins build state was not an object",
        )
    try:
        number = int(value["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins build state omitted a numeric build number",
        ) from exc
    in_progress = value.get("inProgress")
    building = value.get("building")
    if not isinstance(in_progress, bool) or not isinstance(building, bool):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            f"Jenkins build {number} omitted building or inProgress state",
        )
    return {
        "number": number,
        "url": value.get("url"),
        "queue_id": value.get("queueId"),
        "building": building,
        "in_progress": in_progress,
        "result": value.get("result"),
    }


def _probe_workspace_state(client: JenkinsClient, job: str | list[str]) -> JsonDict:
    job_data = client.get_json(
        job_path(job),
        params={
            "tree": (
                "url,inQueue,queueItem[id],"
                "lastBuild[number,url,queueId,building,inProgress,result],"
                "lastCompletedBuild[number],"
                "builds[number,url,queueId,building,inProgress,result]"
            )
        },
    )
    queue_data = client.get_json(
        "queue",
        params={
            "tree": (
                "items[id,url,why,blocked,buildable,stuck,cancelled,"
                "task[name,url],executable[number,url]]"
            )
        },
    )
    if not isinstance(job_data, dict) or not isinstance(queue_data, dict):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins job or queue state was not an object",
        )

    job_url = _normalize_object_url(client.config.url, job_data.get("url"))
    if job_url is None:
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins job state omitted its URL",
        )
    in_queue = job_data.get("inQueue")
    if not isinstance(in_queue, bool):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins job state omitted inQueue",
        )

    raw_builds = job_data.get("builds")
    if not isinstance(raw_builds, list):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins job state omitted its recent builds",
        )
    builds_by_number: dict[int, JsonDict] = {}
    for raw_build in raw_builds:
        build = _build_state(raw_build)
        builds_by_number[int(build["number"])] = build

    raw_last_build = job_data.get("lastBuild")
    last_build = None if raw_last_build is None else _build_state(raw_last_build)
    if last_build is not None:
        builds_by_number[int(last_build["number"])] = last_build
    active_builds = sorted(
        (build for build in builds_by_number.values() if build["in_progress"]),
        key=lambda build: int(build["number"]),
    )

    raw_items = queue_data.get("items")
    if not isinstance(raw_items, list):
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins queue state omitted its items",
        )
    queued_items: list[JsonDict] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("cancelled") is True:
            continue
        task = raw_item.get("task")
        task_url = (
            _normalize_object_url(client.config.url, task.get("url"))
            if isinstance(task, dict)
            else None
        )
        if task_url != job_url:
            continue
        queued_items.append(
            {
                "id": raw_item.get("id"),
                "why": raw_item.get("why"),
                "blocked": raw_item.get("blocked"),
                "buildable": raw_item.get("buildable"),
                "stuck": raw_item.get("stuck"),
            }
        )

    queue_unresolved = in_queue and not queued_items
    stable = not in_queue and not queued_items and not active_builds
    return {
        "checked_at": _timestamp(),
        "job_url": job_data.get("url"),
        "in_queue": in_queue,
        "queue_unresolved": queue_unresolved,
        "queued_items": queued_items,
        "active_builds": active_builds,
        "last_build": last_build,
        "last_completed_build": job_data.get("lastCompletedBuild"),
        "stable": stable,
    }


def _workspace_wait_phase(state: JsonDict) -> str:
    active = state.get("active_builds", [])
    if any(build.get("building") is True for build in active):
        return "waiting_for_build"
    if active:
        return "waiting_for_post_processing"
    return "waiting_for_queue"


def _state_anchor(state: JsonDict) -> int:
    last_build = state.get("last_build")
    if not isinstance(last_build, dict):
        raise WorkspaceBundleError(
            "workspace_no_build",
            "Jenkins job has no build that can anchor the current workspace",
        )
    try:
        return int(last_build["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "workspace_state_unavailable",
            "Jenkins lastBuild omitted a numeric build number",
        ) from exc


def _resolve_requested_build(
    client: JenkinsClient,
    job: str | list[str],
    build: int | str,
) -> int | None:
    if str(build) == "lastBuild":
        return None
    build_info = client.get_json(
        _build_path(job, build),
        params={"tree": "number"},
    )
    if not isinstance(build_info, dict):
        raise WorkspaceBundleError(
            "workspace_build_resolution_failed",
            "Jenkins build API response was not an object",
        )
    try:
        return int(build_info["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "workspace_build_resolution_failed",
            "Jenkins build API response did not include a numeric build number",
        ) from exc


def _require_current_build(desired_build: int | None, anchor_build: int) -> None:
    if desired_build is not None and desired_build != anchor_build:
        raise WorkspaceBundleError(
            "workspace_build_not_current",
            (
                f"Requested build {desired_build}, but Jenkins' current stable workspace is "
                f"anchored to build {anchor_build}; use archived artifacts for historical files"
            ),
        )


def _deep_update(target: JsonDict, patch: JsonDict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _transfer_progress(start: float, downloaded: int, total: int | None) -> JsonDict:
    elapsed = max(time.monotonic() - start, 0.001)
    speed = downloaded / elapsed
    return {
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "percent": round(downloaded * 100 / total, 2) if total else None,
        "speed_bytes_per_second": round(speed, 2),
        "speed_mib_per_second": round(speed / 1024 / 1024, 2),
        "elapsed_seconds": round(elapsed, 2),
    }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "jenkins-job"


def safe_job_name(job: str | list[str]) -> str:
    pieces = _safe_job_path_parts(job)
    return "__".join(pieces)


def _safe_job_path_parts(job: str | list[str]) -> list[str]:
    pieces = [piece for piece in job.split("/") if piece] if isinstance(job, str) else job
    if not pieces:
        raise PathValidationError("job must include at least one path segment")
    return [_safe_name(piece) for piece in pieces]


def _workspace_job_dir(root: Path, job: str | list[str]) -> Path:
    return root.joinpath(*_safe_job_path_parts(job))


def normalize_workspace_path(workspace_path: str) -> str:
    raw = workspace_path.strip().replace("\\", "/")
    if not raw:
        raise PathValidationError("workspace_path must not be empty")
    split = urlsplit(raw)
    if split.scheme or split.netloc or raw.startswith("//"):
        raise PathValidationError("workspace_path must be relative")
    if split.query or split.fragment:
        raise PathValidationError("workspace_path must not include query or fragment")
    if split.path.startswith("/"):
        raise PathValidationError("workspace_path must be relative")

    parts: list[str] = []
    for part in split.path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise PathValidationError("workspace_path traversal is not allowed")
        if part in WORKSPACE_MAGIC_SEGMENTS:
            raise PathValidationError(f"workspace_path must not include Jenkins token {part}")
        if "*" in part or "?" in part:
            raise PathValidationError("workspace_path wildcards are not allowed")
        parts.append(part)

    if not parts:
        raise PathValidationError("workspace_path must include a file or folder path")
    return "/".join(parts)


def _encoded_workspace_path(workspace_path: str) -> str:
    return "/".join(quote(part, safe="") for part in workspace_path.split("/"))


def operation_index_dir(root: Path) -> Path:
    return root / ".operations"


def operation_index_path(root: Path, operation_id: str) -> Path:
    return operation_index_dir(root) / f"{operation_id}.json"


def _write_operation_index(
    root: Path,
    operation_id: str,
    progress_path: Path,
    cancel_path: Path,
) -> None:
    index_dir = operation_index_dir(root)
    index_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        index_dir.chmod(0o700)
    path = operation_index_path(root, operation_id)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "progress_path": str(progress_path),
                "cancel_path": str(cancel_path),
                "server_instance_id": _SERVER_INSTANCE_ID,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_operation_index(
    root: Path,
    operation_id: str,
    *,
    error_code: str = "workspace_operation_not_found",
    label: str = "workspace bundle operation",
) -> JsonDict:
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise WorkspaceBundleError(error_code, "Invalid operation ID")
    path = operation_index_path(root, operation_id)
    if not path.exists():
        raise WorkspaceBundleError(
            error_code,
            f"No {label} found for {operation_id}",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _indexed_path(
    root: Path,
    index: JsonDict,
    key: str,
    *,
    error_code: str = "workspace_operation_index_invalid",
    label: str = "Workspace operation",
) -> Path:
    raw = index.get(key)
    if not isinstance(raw, str):
        raise WorkspaceBundleError(
            error_code,
            f"{label} index omitted {key}",
        )
    path = Path(raw)
    root_resolved = root.resolve()
    path_resolved = path.resolve(strict=False)
    if path_resolved == root_resolved or root_resolved not in path_resolved.parents:
        raise WorkspaceBundleError(
            error_code,
            f"{label} index has an unsafe {key}",
        )
    return path


def _start_operation_thread(operation_id: str, thread: threading.Thread) -> None:
    with _ACTIVE_OPERATIONS_LOCK:
        _ACTIVE_OPERATIONS[operation_id] = thread
    try:
        thread.start()
    except Exception:
        with _ACTIVE_OPERATIONS_LOCK:
            _ACTIVE_OPERATIONS.pop(operation_id, None)
        raise


def _operation_was_interrupted(operation_id: str, index: JsonDict) -> bool:
    instance_id = index.get("server_instance_id")
    if instance_id is None:
        return False
    if instance_id != _SERVER_INSTANCE_ID:
        return True
    with _ACTIVE_OPERATIONS_LOCK:
        thread = _ACTIVE_OPERATIONS.get(operation_id)
    return thread is None or not thread.is_alive()


def _forget_operation_thread(operation_id: str) -> None:
    with _ACTIVE_OPERATIONS_LOCK:
        _ACTIVE_OPERATIONS.pop(operation_id, None)


def _recover_interrupted_operation(root: Path, data: JsonDict, progress_path: Path) -> JsonDict:
    cleanup_paths: list[Path] = []
    for key in ("archive_path", "workspace_dir", "target_path", "console_log_path"):
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        path_resolved = path.resolve(strict=False)
        root_resolved = root.resolve()
        if path_resolved != root_resolved and root_resolved in path_resolved.parents:
            cleanup_paths.append(_partial_path(path))
            if key == "archive_path":
                cleanup_paths.append(path)
    _cleanup_partial(*cleanup_paths)

    data["status"] = "failed"
    data["phase"] = "failed"
    data["error"] = {
        "code": "workspace_operation_interrupted",
        "message": "Workspace operation stopped when its MCP server process exited",
    }
    data["interrupted_at"] = _timestamp()
    data["updated_at"] = data["interrupted_at"]
    tmp = progress_path.with_name(f"{progress_path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(progress_path)
    return data


def _refresh_or_recover_interrupted_operation(
    root: Path,
    operation_id: str,
    index: JsonDict,
    data: JsonDict,
    progress_path: Path,
) -> JsonDict:
    if data.get("status") != "running" or not _operation_was_interrupted(operation_id, index):
        return data
    refreshed = json.loads(progress_path.read_text(encoding="utf-8"))
    if refreshed.get("status") != "running":
        return refreshed
    return _recover_interrupted_operation(root, refreshed, progress_path)


def _registry_progress(root: Path, row: JsonDict) -> tuple[Path, JsonDict]:
    progress_path = _path_under_root(root, str(row["progress_path"]), "progress_path")
    if not progress_path.exists():
        try:
            request = json.loads(str(row.get("request_json", "{}")))
        except json.JSONDecodeError:
            request = {}
        if not isinstance(request, dict):
            request = {}
        return progress_path, {
            "operation_id": row["operation_id"],
            "operation": request.get("operation"),
            "job": request.get("job"),
            "requested_build": request.get("build"),
            "workspace_path": request.get("workspace_path"),
            "kind": request.get("kind"),
            "status": row["status"],
            "phase": "starting_worker" if row["status"] == "running" else row["status"],
            "output_dir": row.get("output_dir"),
            "build_number": row.get("anchor_build_number"),
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
        }
    try:
        value = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceBundleError(
            "workspace_progress_invalid",
            f"Progress data is invalid for operation {row['operation_id']}",
        ) from exc
    if not isinstance(value, dict):
        raise WorkspaceBundleError(
            "workspace_progress_invalid",
            f"Progress data is not an object for operation {row['operation_id']}",
        )
    return progress_path, value


def _remove_registry_output(root: Path, row: JsonDict, data: JsonDict) -> None:
    raw_output = row.get("output_dir") or data.get("output_dir")
    if not isinstance(raw_output, str):
        return
    output_dir = _path_under_root(root, raw_output, "output_dir")
    if output_dir.is_symlink():
        output_dir.unlink(missing_ok=True)
    elif output_dir.exists():
        shutil.rmtree(output_dir)


def _mark_registry_progress_interrupted(
    root: Path,
    row: JsonDict,
    progress_path: Path,
    data: JsonDict,
) -> JsonDict:
    _remove_registry_output(root, row, data)
    data.update(
        {
            "status": "failed",
            "phase": "failed",
            "error": {
                "code": "workspace_operation_interrupted",
                "message": "Detached workspace worker stopped before completing the operation",
            },
            "interrupted_at": _timestamp(),
            "updated_at": _timestamp(),
        }
    )
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    ProgressFile(progress_path, data)
    return data


def _read_registry_status(
    config: JenkinsConfig,
    root: Path,
    registry: WorkspaceOperationRegistry,
    row: JsonDict,
) -> JsonDict:
    if row["status"] == "running" and float(row["heartbeat_at"]) < _stale_before(config):
        refreshed = registry.mark_stale(str(row["operation_id"]), _stale_before(config))
        if refreshed is not None:
            row = refreshed

    progress_path, data = _registry_progress(root, row)
    if row["status"] == "failed" and row.get("error_code") == "workspace_operation_interrupted":
        if data.get("status") == "running":
            data = _mark_registry_progress_interrupted(root, row, progress_path, data)
    else:
        data["status"] = row["status"]
        if row["status"] in TERMINAL_OPERATION_STATUSES and data.get("phase") not in {
            "completed",
            "failed",
            "cancelled",
        }:
            data["phase"] = "completed" if row["status"] == "succeeded" else row["status"]
            if row.get("error_code") and not isinstance(data.get("error"), dict):
                data["error"] = {
                    "code": row["error_code"],
                    "message": "Detached workspace worker did not complete normally",
                }

    data["cancel_requested"] = bool(row["cancel_requested"])
    data["worker_pid"] = row.get("worker_pid")
    data["heartbeat_at_epoch"] = row.get("heartbeat_at")
    if row.get("anchor_build_number") is not None:
        data["build_number"] = row["anchor_build_number"]
    if row.get("output_dir") is not None:
        data["output_dir"] = row["output_dir"]
    registry.touch(str(row["operation_id"]))
    return data


def read_workspace_bundle_status(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    registry = WorkspaceOperationRegistry(root)
    registered = registry.get(operation_id)
    if registered is not None:
        return _read_registry_status(config, root, registry, registered)

    index = _read_operation_index(root, operation_id)
    progress_path = _indexed_path(root, index, "progress_path")
    if not progress_path.exists():
        raise WorkspaceBundleError(
            "workspace_progress_not_found",
            f"Progress file is missing for operation {operation_id}",
        )
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    data = _refresh_or_recover_interrupted_operation(
        root,
        operation_id,
        index,
        data,
        progress_path,
    )
    cancel_path = _indexed_path(root, index, "cancel_path")
    data["cancel_requested"] = cancel_path.exists()
    return data


def cancel_workspace_bundle(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    registry = WorkspaceOperationRegistry(root)
    registered = registry.get(operation_id)
    if registered is not None:
        row = registry.request_cancel(operation_id)
        cancel_requested = bool(
            row is not None and row["status"] == "running" and row["cancel_requested"]
        )
        marker_written = False
        if cancel_requested:
            cancel_path = _path_under_root(root, str(registered["cancel_path"]), "cancel_path")
            cancel_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                cancel_path.write_text(_timestamp() + "\n", encoding="utf-8")
                marker_written = True
            except OSError:
                pass
        return {
            "operation_id": operation_id,
            "cancel_requested": cancel_requested,
            "cancel_marker_written": marker_written,
            "status": row["status"] if row is not None else registered["status"],
            "progress_path": registered["progress_path"],
        }

    index = _read_operation_index(root, operation_id)
    progress_path = _indexed_path(root, index, "progress_path")
    if progress_path.exists():
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        status = data.get("status")
        if status != "running":
            return {
                "operation_id": operation_id,
                "cancel_requested": False,
                "status": status,
                "progress_path": str(progress_path),
            }

    cancel_path = _indexed_path(root, index, "cancel_path")
    cancel_path.write_text(_timestamp() + "\n", encoding="utf-8")
    status = "running" if progress_path.exists() else None
    if progress_path.exists():
        refreshed = json.loads(progress_path.read_text(encoding="utf-8"))
        status = refreshed.get("status")
        if status in {"succeeded", "failed"}:
            cancel_path.unlink(missing_ok=True)
            return {
                "operation_id": operation_id,
                "cancel_requested": False,
                "status": status,
                "progress_path": str(progress_path),
            }

    return {
        "operation_id": operation_id,
        "cancel_requested": True,
        "status": status,
        "progress_path": str(progress_path),
    }


def cleanup_workspace_bundle_operations(
    older_than_days: int = 30,
    max_operations: int = 100,
) -> JsonDict:
    if older_than_days < 1:
        raise ToolInputError("older_than_days must be >= 1")
    if not 1 <= max_operations <= 1_000:
        raise ToolInputError("max_operations must be between 1 and 1000")

    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    registry = WorkspaceOperationRegistry(root)
    index_dir = operation_index_dir(root)
    cutoff = time.time() - older_than_days * 24 * 60 * 60
    deleted: list[str] = []
    invalid: list[str] = []
    inspected_count = 0
    skipped_running = 0
    skipped_recent = 0
    skipped_invalid = 0
    skipped_non_workspace = 0

    for row in registry.cleanup_candidates(max_operations):
        inspected_count += 1
        operation_id = str(row["operation_id"])
        try:
            progress_path, data = _registry_progress(root, row)
            if row["status"] == "running":
                if float(row["heartbeat_at"]) >= _stale_before(config):
                    skipped_running += 1
                    continue
                refreshed = registry.mark_stale(operation_id, _stale_before(config))
                if refreshed is None:
                    skipped_invalid += 1
                    invalid.append(operation_id)
                    continue
                row = refreshed
                data = _mark_registry_progress_interrupted(root, row, progress_path, data)
            if float(row["last_accessed_at"]) > cutoff:
                skipped_recent += 1
                continue
            _remove_registry_output(root, row, data)
            operation_dir = _path_under_root(root, progress_path.parent, "operation directory")
            if operation_dir.exists():
                shutil.rmtree(operation_dir)
            registry.delete(operation_id)
            deleted.append(operation_id)
        except OSError, WorkspaceBundleError:
            skipped_invalid += 1
            invalid.append(operation_id)

    for index_path in sorted(index_dir.glob("*.json")) if index_dir.exists() else []:
        if inspected_count >= max_operations:
            break
        inspected_count += 1
        operation_id = index_path.stem
        try:
            index = _read_operation_index(root, operation_id)
            progress_path = _indexed_path(root, index, "progress_path")
            data = json.loads(progress_path.read_text(encoding="utf-8"))
            if data.get("operation") not in WORKSPACE_OPERATION_TYPES:
                skipped_non_workspace += 1
                continue
            data = _refresh_or_recover_interrupted_operation(
                root,
                operation_id,
                index,
                data,
                progress_path,
            )
            if data.get("status") not in TERMINAL_OPERATION_STATUSES:
                skipped_running += 1
                continue
            if progress_path.stat().st_mtime > cutoff:
                skipped_recent += 1
                continue

            raw_output_dir = data.get("output_dir")
            if not isinstance(raw_output_dir, str):
                raise WorkspaceBundleError(
                    "workspace_operation_index_invalid",
                    "Workspace progress omitted output_dir",
                )
            output_dir = Path(raw_output_dir)
            root_resolved = root.resolve()
            output_resolved = output_dir.resolve(strict=False)
            if output_resolved == root_resolved or root_resolved not in output_resolved.parents:
                raise WorkspaceBundleError(
                    "workspace_operation_index_invalid",
                    "Workspace progress has an unsafe output_dir",
                )

            if output_dir.is_symlink():
                output_dir.unlink()
            elif output_dir.exists():
                shutil.rmtree(output_dir)
            index_path.unlink(missing_ok=True)
            with _ACTIVE_OPERATIONS_LOCK:
                _ACTIVE_OPERATIONS.pop(operation_id, None)
            deleted.append(operation_id)
        except OSError, json.JSONDecodeError, WorkspaceBundleError:
            skipped_invalid += 1
            invalid.append(operation_id)

    return {
        "deleted_operation_ids": deleted,
        "deleted_count": len(deleted),
        "invalid_operation_ids": invalid,
        "inspected_count": inspected_count,
        "skipped_running": skipped_running,
        "skipped_recent": skipped_recent,
        "skipped_invalid": skipped_invalid,
        "skipped_non_workspace": skipped_non_workspace,
        "older_than_days": older_than_days,
        "max_operations": max_operations,
    }


def _cached_payload_exists(root: Path, row: JsonDict) -> bool:
    try:
        _, data = _registry_progress(root, row)
        if data.get("status") != "succeeded":
            return False
        required: list[tuple[Any, str]] = [
            (data.get("console_log_path"), "file"),
            (data.get("metadata_path"), "file"),
        ]
        if data.get("operation") == "workspace_bundle":
            required.append((data.get("workspace_dir"), "directory"))
        else:
            kind = data.get("kind")
            if kind not in {"file", "folder"}:
                return False
            required.append(
                (data.get("target_path"), "file" if kind == "file" else "directory")
            )
        for raw, expected_type in required:
            if not isinstance(raw, str):
                return False
            path = _path_under_root(root, raw, "cached payload path")
            if path.is_symlink():
                return False
            if expected_type == "file" and not path.is_file():
                return False
            if expected_type == "directory" and not path.is_dir():
                return False
        return True
    except OSError, WorkspaceBundleError:
        return False


def _start_response(
    root: Path,
    row: JsonDict,
    *,
    disposition: str,
) -> JsonDict:
    _, data = _registry_progress(root, row)
    return {
        "operation_id": row["operation_id"],
        "job": data.get("job"),
        "build_number": row.get("anchor_build_number") or data.get("build_number"),
        "workspace_path": data.get("workspace_path"),
        "kind": data.get("kind"),
        "output_dir": row.get("output_dir") or data.get("output_dir"),
        "progress_path": row["progress_path"],
        "status": row["status"],
        "phase": data.get("phase"),
        "disposition": disposition,
        "workspace_freshness": "best_effort",
    }


def _spawn_workspace_worker(operation_id: str) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "jenkins_mcp_server.workspace_worker",
        operation_id,
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - exercised on Windows CI/users.
        kwargs["creationflags"] = (  # pragma: no cover
            subprocess.CREATE_NEW_PROCESS_GROUP  # pragma: no cover
            | subprocess.DETACHED_PROCESS  # pragma: no cover
        )
    return subprocess.Popen(command, **kwargs)


def _start_workspace_operation(
    *,
    job: str | list[str],
    build: int | str,
    operation: str,
    workspace_path: str | None = None,
    kind: str | None = None,
    force_refresh: bool = False,
) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    job_path(job)
    request_identity: JsonDict = {
        "operation": operation,
        "job": job,
        "build": build,
        "workspace_path": workspace_path,
        "kind": kind,
    }
    request_key = _request_key(config, request_identity)
    registry = WorkspaceOperationRegistry(root)
    active = registry.find_active(request_key)
    if active is not None and float(active["heartbeat_at"]) >= _stale_before(config):
        registry.touch(str(active["operation_id"]))
        return _start_response(root, active, disposition="joined")

    with JenkinsClient(config) as client:
        initial_state = _probe_workspace_state(client, job)
        desired_build = _resolve_requested_build(client, job, build)

    if initial_state["stable"]:
        initial_anchor = _state_anchor(initial_state)
        _require_current_build(desired_build, initial_anchor)
        if not force_refresh:
            for reusable in registry.find_reusable(request_key, initial_anchor):
                if _cached_payload_exists(root, reusable):
                    registry.touch(str(reusable["operation_id"]))
                    return _start_response(root, reusable, disposition="reused")
                registry.invalidate_reusable(str(reusable["operation_id"]))

    operation_id = uuid.uuid4().hex
    operation_dir, progress_path, cancel_path = _operation_paths(root, operation_id)
    request = {
        **request_identity,
        "desired_build_number": desired_build,
        "initial_state": initial_state,
    }
    row, created, stale_rows = registry.claim_or_join(
        operation_id=operation_id,
        request_key=request_key,
        request=request,
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=_stale_before(config),
    )
    for stale in stale_rows:
        stale_progress_path, stale_data = _registry_progress(root, stale)
        _mark_registry_progress_interrupted(root, stale, stale_progress_path, stale_data)
    if not created:
        return _start_response(root, row, disposition="joined")

    try:
        operation_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        registry.fail_unowned_start(operation_id, "workspace_operation_setup_failed")
        raise WorkspaceBundleError(
            "workspace_operation_setup_failed",
            "Could not create local workspace operation files",
        ) from exc
    with suppress(OSError):
        operation_dir.chmod(0o700)
    progress = ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": operation,
            "status": "running",
            "phase": (
                "checking_workspace_state"
                if initial_state["stable"]
                else _workspace_wait_phase(initial_state)
            ),
            "job": job,
            "requested_build": build,
            "desired_build_number": desired_build,
            "build_number": None,
            "workspace_path": workspace_path,
            "kind": kind,
            "output_dir": None,
            "cancel_requested": False,
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "workspace_archive": {},
            "workspace_file": {},
            "extract": {},
            "console_log": {},
            "workspace_guard": {
                "mode": "guarded_dynamic_workspace",
                "freshness": "best_effort",
                "build_identity_guaranteed": False,
                "initial_state": initial_state,
                "capture_attempt": 0,
                "retry_count": 0,
            },
        },
    )
    try:
        worker = _spawn_workspace_worker(operation_id)
        registry.set_spawned_pid(operation_id, worker.pid)
    except Exception as exc:
        registry.fail_unowned_start(operation_id, "workspace_worker_start_failed")
        progress.update(
            status="failed",
            phase="failed",
            error={
                "code": "workspace_worker_start_failed",
                "message": "Could not start detached workspace worker",
                "type": type(exc).__name__,
            },
        )
        raise WorkspaceBundleError(
            "workspace_worker_start_failed",
            "Could not start detached workspace worker",
        ) from exc
    return _start_response(root, registry.get(operation_id) or row, disposition="started")


def start_workspace_bundle_download(
    job: str | list[str],
    build: int | str = "lastBuild",
    force_refresh: bool = False,
) -> JsonDict:
    return _start_workspace_operation(
        job=job,
        build=build,
        operation="workspace_bundle",
        force_refresh=force_refresh,
    )


def start_workspace_path_download(
    job: str | list[str],
    workspace_path: str,
    kind: str,
    build: int | str = "lastBuild",
    force_refresh: bool = False,
) -> JsonDict:
    if kind not in {"file", "folder"}:
        raise WorkspaceBundleError(
            "invalid_workspace_path_kind",
            "kind must be either 'file' or 'folder'",
        )
    normalized_workspace_path = normalize_workspace_path(workspace_path)
    return _start_workspace_operation(
        job=job,
        build=build,
        operation="workspace_path_download",
        workspace_path=normalized_workspace_path,
        kind=kind,
        force_refresh=force_refresh,
    )


def _build_path(job: str | list[str], build: int | str) -> str:
    build_id = str(build)
    if not build_id or build_id in {".", ".."} or "/" in build_id:
        raise PathValidationError("build must be a number or permalink path segment")
    return f"{job_path(job)}/{safe_segment(build_id, 'build')}"


def _workspace_archive_path(job: str | list[str], filename: str) -> str:
    # The ** glob avoids Jenkins' default zip prefix so files extract directly under workspace/.
    return f"{job_path(job)}/ws/**/*zip*/{safe_segment(filename, 'archive filename')}"


def _workspace_folder_archive_path(
    job: str | list[str],
    workspace_path: str,
    filename: str,
) -> str:
    encoded_path = _encoded_workspace_path(workspace_path)
    encoded_filename = safe_segment(filename, "archive filename")
    return f"{job_path(job)}/ws/{encoded_path}/**/*zip*/{encoded_filename}"


def _workspace_file_path(job: str | list[str], workspace_path: str) -> str:
    return f"{job_path(job)}/ws/{_encoded_workspace_path(workspace_path)}"


def _reserve_output_dir(parent: Path, directory_name: str, operation_id: str) -> Path:
    candidates = [
        parent / directory_name,
        parent / f"{directory_name}-{operation_id[:8]}",
    ]
    candidates.extend(
        parent / f"{directory_name}-{operation_id[:8]}-{index}" for index in range(2, 100)
    )
    for candidate in candidates:
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise WorkspaceBundleError(
        "workspace_output_reservation_failed",
        f"Could not reserve an output directory for build {directory_name}",
    )


def _discard_output_dir(root: Path, output_dir: Path | None) -> None:
    if output_dir is None:
        return
    safe = _path_under_root(root, output_dir, "output_dir")
    if safe.is_symlink():
        safe.unlink(missing_ok=True)
    elif safe.exists():
        shutil.rmtree(safe)


def _configure_capture_paths(
    *,
    root: Path,
    registry: WorkspaceOperationRegistry,
    operation_id: str,
    owner_id: str,
    request: JsonDict,
    anchor_build: int,
    progress: ProgressFile,
) -> JsonDict:
    job = request["job"]
    name_prefix = f"{safe_job_name(job)}{anchor_build}"
    job_dir = _workspace_job_dir(root, job)
    _path_under_root(root, job_dir, "job output directory")
    job_dir.mkdir(parents=True, exist_ok=True)
    _path_under_root(root, job_dir, "job output directory")
    output_dir = _reserve_output_dir(job_dir, str(anchor_build), operation_id)
    if not registry.set_capture(
        operation_id,
        owner_id,
        output_dir=output_dir,
        anchor_build_number=anchor_build,
    ):
        _discard_output_dir(root, output_dir)
        raise OperationCancelledError("Workspace operation ownership was lost")

    workspace_root = output_dir / "workspace"
    console_log_path = output_dir / f"{name_prefix}-console.log"
    metadata_path = output_dir / "metadata.json"
    if request["operation"] == "workspace_bundle":
        archive_path = output_dir / f"{name_prefix}.zip"
        target_path = None
    else:
        workspace_path = str(request["workspace_path"])
        target_path = workspace_root.joinpath(*workspace_path.split("/"))
        archive_path = output_dir / f"{name_prefix}-{_safe_name(workspace_path)}.zip"

    progress.update(
        build_number=anchor_build,
        output_dir=str(output_dir),
        archive_path=(
            str(archive_path)
            if request["operation"] == "workspace_bundle" or request["kind"] == "folder"
            else None
        ),
        workspace_dir=str(workspace_root),
        target_path=str(target_path) if target_path is not None else None,
        console_log_path=str(console_log_path),
        metadata_path=str(metadata_path),
        archive_deleted=False,
        workspace_archive={},
        workspace_file={},
        extract={},
        console_log={},
    )
    return {
        "name_prefix": name_prefix,
        "output_dir": output_dir,
        "archive_path": archive_path,
        "workspace_dir": workspace_root,
        "target_path": target_path,
        "console_log_path": console_log_path,
        "metadata_path": metadata_path,
    }


def _sleep_with_cancel(seconds: float, cancel_check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _raise_if_cancelled(cancel_check)
        time.sleep(min(0.25, max(deadline - time.monotonic(), 0.0)))


def _worker_cancel_check(
    registry: WorkspaceOperationRegistry,
    operation_id: str,
    owner_id: str,
    cancel_path: Path,
) -> Callable[[], bool]:
    state = {"last_heartbeat": 0.0, "lost": False}

    def cancelled() -> bool:
        if state["lost"] or cancel_path.exists():
            return True
        now = time.monotonic()
        if now - state["last_heartbeat"] >= WORKSPACE_HEARTBEAT_SECONDS:
            state["last_heartbeat"] = now
            if not registry.heartbeat(operation_id, owner_id):
                state["lost"] = True
                return True
        return False

    return cancelled


def _wait_for_stable_workspace(
    *,
    client: JenkinsClient,
    job: str | list[str],
    progress: ProgressFile,
    cancel_check: Callable[[], bool],
) -> JsonDict:
    checks = 0
    while True:
        _raise_if_cancelled(cancel_check)
        state = _probe_workspace_state(client, job)
        checks += 1
        progress.update(
            phase="checking_workspace_state" if state["stable"] else _workspace_wait_phase(state),
            workspace_guard={
                "last_state": state,
                "state_checks": checks,
            },
        )
        if state["stable"]:
            return state
        _sleep_with_cancel(WORKSPACE_STATE_POLL_SECONDS, cancel_check)


def _operation_is_owned(
    registry: WorkspaceOperationRegistry,
    operation_id: str,
    owner_id: str,
) -> bool:
    row = registry.get(operation_id)
    return row is not None and row["status"] == "running" and row["owner_id"] == owner_id


def _finish_registered_operation(
    *,
    registry: WorkspaceOperationRegistry,
    operation_id: str,
    owner_id: str,
    progress: ProgressFile,
    status: str,
    phase: str,
    error: JsonDict | None = None,
    **extra: Any,
) -> None:
    if not _operation_is_owned(registry, operation_id, owner_id):
        return
    patch: JsonDict = {"status": status, "phase": phase, **extra}
    if error is not None:
        patch["error"] = error
    progress.update(**patch)
    registry.finish(
        operation_id,
        owner_id,
        status,
        error_code=error.get("code") if error is not None else None,
    )


def run_registered_workspace_operation(
    config: JenkinsConfig,
    registry: WorkspaceOperationRegistry,
    row: JsonDict,
    owner_id: str,
) -> None:
    operation_id = str(row["operation_id"])
    root = registry.root
    progress_path = _path_under_root(root, str(row["progress_path"]), "progress_path")
    cancel_path = _path_under_root(root, str(row["cancel_path"]), "cancel_path")
    progress_data = json.loads(progress_path.read_text(encoding="utf-8"))
    if not isinstance(progress_data, dict):
        raise WorkspaceBundleError(
            "workspace_progress_invalid",
            f"Progress data is not an object for operation {operation_id}",
        )
    progress = ProgressFile(progress_path, progress_data)
    request = registry.request(row)
    job = request.get("job")
    if not isinstance(job, (str, list)):
        raise WorkspaceBundleError(
            "workspace_operation_request_invalid",
            "Workspace operation request omitted job",
        )
    job_path(job)
    desired_build = request.get("desired_build_number")
    if desired_build is not None:
        try:
            desired_build = int(desired_build)
        except (TypeError, ValueError) as exc:
            raise WorkspaceBundleError(
                "workspace_operation_request_invalid",
                "Workspace operation request has an invalid desired build",
            ) from exc

    cancel_check = _worker_cancel_check(
        registry,
        operation_id,
        owner_id,
        cancel_path,
    )
    output_dir: Path | None = None
    try:
        with JenkinsClient(config) as state_client:
            for attempt in range(1, WORKSPACE_CAPTURE_MAX_ATTEMPTS + 1):
                stable_state = _wait_for_stable_workspace(
                    client=state_client,
                    job=job,
                    progress=progress,
                    cancel_check=cancel_check,
                )
                anchor_build = _state_anchor(stable_state)
                _require_current_build(desired_build, anchor_build)
                paths = _configure_capture_paths(
                    root=root,
                    registry=registry,
                    operation_id=operation_id,
                    owner_id=owner_id,
                    request=request,
                    anchor_build=anchor_build,
                    progress=progress,
                )
                output_dir = paths["output_dir"]
                guard_metadata: JsonDict = {
                    "mode": "guarded_dynamic_workspace",
                    "freshness": "best_effort",
                    "build_identity_guaranteed": False,
                    "anchor_build_number": anchor_build,
                    "pre_download_state": stable_state,
                    "capture_attempt": attempt,
                    "retry_count": attempt - 1,
                }
                progress.update(workspace_guard=guard_metadata)

                def state_check(
                    anchor: int = anchor_build,
                    metadata: JsonDict = guard_metadata,
                ) -> JsonDict:
                    _raise_if_cancelled(cancel_check)
                    current = _probe_workspace_state(state_client, job)
                    metadata["last_checked_state"] = current
                    progress.update(workspace_guard=metadata)
                    if not current["stable"] or _state_anchor(current) != anchor:
                        raise _WorkspaceStateChanged(current)
                    metadata["post_download_state"] = current
                    return current

                try:
                    state_check()
                    metadata_extra = {"workspace_guard": guard_metadata}
                    if request["operation"] == "workspace_bundle":
                        metadata = _capture_workspace_bundle(
                            config=config,
                            job=job,
                            build_number=anchor_build,
                            name_prefix=str(paths["name_prefix"]),
                            archive_path=paths["archive_path"],
                            workspace_dir=paths["workspace_dir"],
                            console_log_path=paths["console_log_path"],
                            metadata_path=paths["metadata_path"],
                            progress=progress,
                            cancel_path=cancel_path,
                            cancel_check=cancel_check,
                            state_check=state_check,
                            metadata_extra=metadata_extra,
                        )
                    else:
                        metadata = _capture_workspace_path_download(
                            config=config,
                            job=job,
                            build_number=anchor_build,
                            workspace_path=str(request["workspace_path"]),
                            kind=str(request["kind"]),
                            archive_path=paths["archive_path"],
                            target_path=paths["target_path"],
                            console_log_path=paths["console_log_path"],
                            metadata_path=paths["metadata_path"],
                            progress=progress,
                            cancel_path=cancel_path,
                            cancel_check=cancel_check,
                            state_check=state_check,
                            metadata_extra=metadata_extra,
                        )
                    _finish_registered_operation(
                        registry=registry,
                        operation_id=operation_id,
                        owner_id=owner_id,
                        progress=progress,
                        status="succeeded",
                        phase="completed",
                        metadata_path=str(paths["metadata_path"]),
                        completed_at=metadata["completed_at"],
                    )
                    return
                except _WorkspaceStateChanged as exc:
                    _discard_output_dir(root, output_dir)
                    registry.clear_capture(operation_id, owner_id)
                    output_dir = None
                    if attempt >= WORKSPACE_CAPTURE_MAX_ATTEMPTS:
                        raise WorkspaceBundleError(
                            "workspace_changed_during_download",
                            "Jenkins workspace changed during both capture attempts",
                        ) from exc
                    progress.update(
                        phase="workspace_changed_retrying",
                        output_dir=None,
                        workspace_guard={
                            "last_state": exc.state,
                            "retry_count": attempt,
                        },
                    )
    except OperationCancelledError as exc:
        _discard_output_dir(root, output_dir)
        registry.clear_capture(operation_id, owner_id)
        _finish_registered_operation(
            registry=registry,
            operation_id=operation_id,
            owner_id=owner_id,
            progress=progress,
            status="cancelled",
            phase="cancelled",
            error={"code": exc.code, "message": str(exc)},
            cancel_requested=True,
            output_dir=None,
        )
    except Exception as exc:  # noqa: BLE001 - detached worker must persist failures.
        _discard_output_dir(root, output_dir)
        registry.clear_capture(operation_id, owner_id)
        _finish_registered_operation(
            registry=registry,
            operation_id=operation_id,
            owner_id=owner_id,
            progress=progress,
            status="failed",
            phase="failed",
            error=_error_payload(exc, str(progress.data.get("phase", ""))),
            output_dir=None,
        )


def _write_json_atomic(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _capture_workspace_bundle(
    *,
    config: JenkinsConfig,
    job: str | list[str],
    build_number: int,
    name_prefix: str,
    archive_path: Path,
    workspace_dir: Path,
    console_log_path: Path,
    metadata_path: Path,
    progress: ProgressFile,
    cancel_path: Path,
    cancel_check: Callable[[], bool] | None = None,
    state_check: Callable[[], JsonDict] | None = None,
    metadata_extra: JsonDict | None = None,
) -> JsonDict:
    archive_partial = archive_path.with_suffix(f"{archive_path.suffix}.partial")
    workspace_partial = workspace_dir.with_name(f"{workspace_dir.name}.partial")
    log_partial = console_log_path.with_suffix(f"{console_log_path.suffix}.partial")

    def cancelled() -> bool:
        return cancel_path.exists() or (cancel_check is not None and cancel_check())

    with JenkinsClient(config) as client:
        _download_with_progress(
            client=client,
            source_path=_workspace_archive_path(job, archive_path.name),
            partial_path=archive_partial,
            final_path=archive_path,
            max_bytes=config.max_workspace_archive_bytes,
            progress=progress,
            progress_key="workspace_archive",
            phase="downloading_workspace_archive",
            cancel_check=cancelled,
            interval_seconds=config.workspace_progress_interval_seconds,
            state_check=state_check,
        )

        _raise_if_cancelled(cancelled)
        _extract_zip_safely(
            archive_path=archive_path,
            partial_dir=workspace_partial,
            final_dir=workspace_dir,
            max_bytes=config.max_workspace_extract_bytes,
            max_files=config.max_workspace_files,
            progress=progress,
            cancel_check=cancelled,
            interval_seconds=config.workspace_progress_interval_seconds,
        )

        archive_path.unlink(missing_ok=True)
        progress.update(archive_deleted=True)

        _download_with_progress(
            client=client,
            source_path=f"{_build_path(job, build_number)}/consoleText",
            partial_path=log_partial,
            final_path=console_log_path,
            max_bytes=config.max_bundle_log_bytes,
            progress=progress,
            progress_key="console_log",
            phase="downloading_console_log",
            cancel_check=cancelled,
            interval_seconds=config.workspace_progress_interval_seconds,
        )

    metadata = {
        "job": job,
        "build_number": build_number,
        "archive_deleted": True,
        "workspace_dir": str(workspace_dir),
        "console_log_path": str(console_log_path),
        "completed_at": _timestamp(),
    }
    if metadata_extra:
        _deep_update(metadata, metadata_extra)
    _write_json_atomic(metadata_path, metadata)
    return metadata


def _run_workspace_bundle(
    *,
    config: JenkinsConfig,
    job: str | list[str],
    build_number: int,
    name_prefix: str,
    archive_path: Path,
    workspace_dir: Path,
    console_log_path: Path,
    metadata_path: Path,
    progress: ProgressFile,
    cancel_path: Path,
) -> None:
    archive_partial = archive_path.with_suffix(f"{archive_path.suffix}.partial")
    workspace_partial = workspace_dir.with_name(f"{workspace_dir.name}.partial")
    log_partial = console_log_path.with_suffix(f"{console_log_path.suffix}.partial")
    try:
        metadata = _capture_workspace_bundle(
            config=config,
            job=job,
            build_number=build_number,
            name_prefix=name_prefix,
            archive_path=archive_path,
            workspace_dir=workspace_dir,
            console_log_path=console_log_path,
            metadata_path=metadata_path,
            progress=progress,
            cancel_path=cancel_path,
        )
        progress.update(
            status="succeeded",
            phase="completed",
            metadata_path=str(metadata_path),
            completed_at=metadata["completed_at"],
        )
    except OperationCancelledError as exc:
        _cleanup_partial(archive_partial, workspace_partial, log_partial)
        archive_path.unlink(missing_ok=True)
        progress.update(
            status="cancelled",
            phase="cancelled",
            cancel_requested=True,
            error={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 - background task must persist errors to status.
        _cleanup_partial(archive_partial, workspace_partial, log_partial)
        archive_path.unlink(missing_ok=True)
        progress.update(
            status="failed",
            phase="failed",
            error=_error_payload(exc, str(progress.data.get("phase", ""))),
        )
    finally:
        _forget_operation_thread(str(progress.data["operation_id"]))


def _capture_workspace_path_download(
    *,
    config: JenkinsConfig,
    job: str | list[str],
    build_number: int,
    workspace_path: str,
    kind: str,
    archive_path: Path,
    target_path: Path,
    console_log_path: Path,
    metadata_path: Path,
    progress: ProgressFile,
    cancel_path: Path,
    cancel_check: Callable[[], bool] | None = None,
    state_check: Callable[[], JsonDict] | None = None,
    metadata_extra: JsonDict | None = None,
) -> JsonDict:
    archive_partial = _partial_path(archive_path)
    target_partial = _partial_path(target_path)
    folder_partial = _partial_path(target_path)
    log_partial = _partial_path(console_log_path)

    def cancelled() -> bool:
        return cancel_path.exists() or (cancel_check is not None and cancel_check())

    with JenkinsClient(config) as client:
        if kind == "file":
            _download_with_progress(
                client=client,
                source_path=_workspace_file_path(job, workspace_path),
                partial_path=target_partial,
                final_path=target_path,
                max_bytes=config.max_workspace_archive_bytes,
                progress=progress,
                progress_key="workspace_file",
                phase="downloading_workspace_file",
                cancel_check=cancelled,
                interval_seconds=config.workspace_progress_interval_seconds,
                state_check=state_check,
            )
        else:
            _download_with_progress(
                client=client,
                source_path=_workspace_folder_archive_path(
                    job,
                    workspace_path,
                    archive_path.name,
                ),
                partial_path=archive_partial,
                final_path=archive_path,
                max_bytes=config.max_workspace_archive_bytes,
                progress=progress,
                progress_key="workspace_archive",
                phase="downloading_workspace_archive",
                cancel_check=cancelled,
                interval_seconds=config.workspace_progress_interval_seconds,
                state_check=state_check,
            )
            _raise_if_cancelled(cancelled)
            _extract_zip_safely(
                archive_path=archive_path,
                partial_dir=folder_partial,
                final_dir=target_path,
                max_bytes=config.max_workspace_extract_bytes,
                max_files=config.max_workspace_files,
                progress=progress,
                cancel_check=cancelled,
                interval_seconds=config.workspace_progress_interval_seconds,
            )
            archive_path.unlink(missing_ok=True)
            progress.update(archive_deleted=True)

        _download_with_progress(
            client=client,
            source_path=f"{_build_path(job, build_number)}/consoleText",
            partial_path=log_partial,
            final_path=console_log_path,
            max_bytes=config.max_bundle_log_bytes,
            progress=progress,
            progress_key="console_log",
            phase="downloading_console_log",
            cancel_check=cancelled,
            interval_seconds=config.workspace_progress_interval_seconds,
        )

    metadata = {
        "job": job,
        "build_number": build_number,
        "kind": kind,
        "workspace_path": workspace_path,
        "target_path": str(target_path),
        "archive_deleted": kind == "folder",
        "console_log_path": str(console_log_path),
        "completed_at": _timestamp(),
    }
    if metadata_extra:
        _deep_update(metadata, metadata_extra)
    _write_json_atomic(metadata_path, metadata)
    return metadata


def _run_workspace_path_download(
    *,
    config: JenkinsConfig,
    job: str | list[str],
    build_number: int,
    workspace_path: str,
    kind: str,
    archive_path: Path,
    target_path: Path,
    console_log_path: Path,
    metadata_path: Path,
    progress: ProgressFile,
    cancel_path: Path,
) -> None:
    archive_partial = _partial_path(archive_path)
    target_partial = _partial_path(target_path)
    folder_partial = _partial_path(target_path)
    log_partial = _partial_path(console_log_path)
    try:
        metadata = _capture_workspace_path_download(
            config=config,
            job=job,
            build_number=build_number,
            workspace_path=workspace_path,
            kind=kind,
            archive_path=archive_path,
            target_path=target_path,
            console_log_path=console_log_path,
            metadata_path=metadata_path,
            progress=progress,
            cancel_path=cancel_path,
        )
        progress.update(
            status="succeeded",
            phase="completed",
            metadata_path=str(metadata_path),
            completed_at=metadata["completed_at"],
        )
    except OperationCancelledError as exc:
        _cleanup_partial(archive_partial, target_partial, folder_partial, log_partial)
        archive_path.unlink(missing_ok=True)
        progress.update(
            status="cancelled",
            phase="cancelled",
            cancel_requested=True,
            error={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 - background task must persist errors to status.
        _cleanup_partial(archive_partial, target_partial, folder_partial, log_partial)
        archive_path.unlink(missing_ok=True)
        progress.update(
            status="failed",
            phase="failed",
            error=_error_payload(exc, str(progress.data.get("phase", ""))),
        )
    finally:
        _forget_operation_thread(str(progress.data["operation_id"]))


def _download_with_progress(
    *,
    client: JenkinsClient,
    source_path: str,
    partial_path: Path,
    final_path: Path,
    max_bytes: int,
    progress: ProgressFile,
    progress_key: str,
    phase: str,
    cancel_check: Callable[[], bool],
    interval_seconds: float,
    state_check: Callable[[], JsonDict] | None = None,
    state_check_interval_seconds: float = WORKSPACE_STATE_POLL_SECONDS,
) -> None:
    partial_path.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)
    start = time.monotonic()
    last_update = 0.0
    last_state_check = start

    progress.update(
        phase=phase,
        current_file=str(final_path),
        **{progress_key: {"path": str(final_path)}},
    )

    def on_progress(downloaded: int, total: int | None) -> None:
        nonlocal last_state_check, last_update
        now = time.monotonic()
        if state_check is not None and now - last_state_check >= state_check_interval_seconds:
            state_check()
            last_state_check = time.monotonic()
        if now - last_update < interval_seconds and (total is None or downloaded != total):
            return
        last_update = now
        progress.update(**{progress_key: _transfer_progress(start, downloaded, total)})

    client.stream_to_file(
        source_path,
        partial_path,
        max_bytes=max_bytes,
        progress_callback=on_progress,
        cancel_check=cancel_check,
    )
    if state_check is not None:
        state_check()
    partial_path.replace(final_path)
    progress.update(**{progress_key: {"path": str(final_path), "complete": True}})


def _extract_zip_safely(
    *,
    archive_path: Path,
    partial_dir: Path,
    final_dir: Path,
    max_bytes: int,
    max_files: int,
    progress: ProgressFile,
    cancel_check: Callable[[], bool],
    interval_seconds: float,
) -> None:
    _raise_if_cancelled(cancel_check)
    shutil.rmtree(partial_dir, ignore_errors=True)
    shutil.rmtree(final_dir, ignore_errors=True)
    partial_dir.mkdir(parents=True)

    start = time.monotonic()
    last_update = 0.0
    extracted_bytes = 0
    files_extracted = 0
    seen: set[str] = set()
    progress.update(
        phase="extracting_workspace_archive",
        extract={"files_extracted": 0, "extracted_bytes": 0, "complete": False},
    )

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            total_files = sum(1 for member in members if not member.is_dir())
            total_bytes = sum(member.file_size for member in members if not member.is_dir())
            if total_files > max_files:
                raise WorkspaceBundleError(
                    "workspace_extract_file_limit_exceeded",
                    f"Workspace archive contains more than {max_files} files",
                )
            if total_bytes > max_bytes:
                raise ResponseTooLargeError(max_bytes)
            ensure_free_space(partial_dir, total_bytes)
            for member in members:
                _raise_if_cancelled(cancel_check)
                target = _safe_zip_target(partial_dir, member, seen)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        _raise_if_cancelled(cancel_check)
                        if extracted_bytes + len(chunk) > max_bytes:
                            raise ResponseTooLargeError(max_bytes)
                        dst.write(chunk)
                        extracted_bytes += len(chunk)

                        now = time.monotonic()
                        if now - last_update >= interval_seconds:
                            last_update = now
                            elapsed = max(now - start, 0.001)
                            progress.update(
                                extract={
                                    "current_entry": member.filename,
                                    "files_extracted": files_extracted,
                                    "total_files": total_files,
                                    "extracted_bytes": extracted_bytes,
                                    "speed_bytes_per_second": round(
                                        extracted_bytes / elapsed,
                                        2,
                                    ),
                                    "speed_mib_per_second": round(
                                        extracted_bytes / elapsed / 1024 / 1024,
                                        2,
                                    ),
                                    "elapsed_seconds": round(elapsed, 2),
                                }
                            )

                files_extracted += 1
                now = time.monotonic()
                if now - last_update >= interval_seconds or files_extracted == total_files:
                    last_update = now
                    elapsed = max(now - start, 0.001)
                    progress.update(
                        extract={
                            "files_extracted": files_extracted,
                            "total_files": total_files,
                            "extracted_bytes": extracted_bytes,
                            "percent": round(files_extracted * 100 / total_files, 2)
                            if total_files
                            else 100.0,
                            "speed_bytes_per_second": round(extracted_bytes / elapsed, 2),
                            "speed_mib_per_second": round(
                                extracted_bytes / elapsed / 1024 / 1024,
                                2,
                            ),
                            "elapsed_seconds": round(elapsed, 2),
                        }
                    )

        partial_dir.replace(final_dir)
        progress.update(
            extract={
                "files_extracted": files_extracted,
                "extracted_bytes": extracted_bytes,
                "complete": True,
            }
        )
    except Exception:
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise


def _safe_zip_target(root: Path, member: zipfile.ZipInfo, seen: set[str]) -> Path:
    raw_name = member.filename.replace("\\", "/")
    pure = PurePosixPath(raw_name)
    if raw_name.startswith("/") or pure.is_absolute() or not pure.parts:
        raise WorkspaceBundleError(
            "unsafe_zip_entry",
            f"Unsafe absolute zip entry: {member.filename}",
        )
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise WorkspaceBundleError("unsafe_zip_entry", f"Unsafe zip entry path: {member.filename}")

    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode):
        raise WorkspaceBundleError(
            "unsafe_zip_entry",
            f"Refusing symlink zip entry: {member.filename}",
        )
    if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise WorkspaceBundleError(
            "unsafe_zip_entry",
            f"Refusing special zip entry: {member.filename}",
        )

    normalized = "/".join(pure.parts)
    if normalized in seen and not member.is_dir():
        raise WorkspaceBundleError("unsafe_zip_entry", f"Duplicate zip entry: {member.filename}")
    seen.add(normalized)

    target = root.joinpath(*pure.parts)
    root_resolved = root.resolve()
    target_parent = target.parent.resolve()
    if root_resolved != target_parent and root_resolved not in target_parent.parents:
        raise WorkspaceBundleError(
            "unsafe_zip_entry",
            f"Zip entry escapes target: {member.filename}",
        )
    return target


def _raise_if_cancelled(cancel_check: Callable[[], bool]) -> None:
    if cancel_check():
        raise OperationCancelledError("Operation was cancelled")


def _cleanup_partial(*paths: Path) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _partial_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.partial")


def _error_payload(exc: Exception, phase: str = "") -> JsonDict:
    if isinstance(exc, JenkinsMCPError):
        cause = exc.to_dict()["error"]
    else:
        cause = {
            "code": "workspace_bundle_failed",
            "message": str(exc),
            "type": type(exc).__name__,
        }

    if isinstance(exc, zipfile.BadZipFile):
        cause = {
            "code": "workspace_archive_extract_failed",
            "message": "Workspace archive was not a valid zip file",
        }

    if phase == "downloading_workspace_archive":
        return {
            "code": "workspace_archive_download_failed",
            "message": "Workspace archive download failed",
            "cause": cause,
        }
    if phase == "downloading_workspace_file":
        return {
            "code": "workspace_file_download_failed",
            "message": "Workspace file download failed",
            "cause": cause,
        }
    if phase == "downloading_console_log":
        return {
            "code": "console_log_download_failed",
            "message": "Console log download failed",
            "cause": cause,
        }
    if phase == "extracting_workspace_archive":
        return {
            "code": "workspace_archive_extract_failed",
            "message": "Workspace archive extraction failed",
            "cause": cause,
        }
    return cause
