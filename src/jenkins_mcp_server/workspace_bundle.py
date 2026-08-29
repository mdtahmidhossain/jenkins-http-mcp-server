from __future__ import annotations

import json
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from .client import JenkinsClient, ensure_free_space, job_path, safe_segment
from .config import JenkinsConfig
from .errors import (
    JenkinsMCPError,
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
    ToolInputError,
    WorkspaceBundleError,
)

JsonDict = dict[str, Any]
WORKSPACE_MAGIC_SEGMENTS = {"*zip*", "*plain*", "*view*", "*fingerprint*"}
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "cancelled"}
WORKSPACE_OPERATION_TYPES = {None, "workspace_bundle", "workspace_path_download"}
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
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    pieces = [piece for piece in job.split("/") if piece] if isinstance(job, str) else job
    if not pieces:
        raise PathValidationError("job must include at least one path segment")
    return "__".join(_safe_name(piece) for piece in pieces)


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
    index_dir.mkdir(parents=True, exist_ok=True)
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


def read_workspace_bundle_status(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    index = _read_operation_index(root, operation_id)
    progress_path = _indexed_path(root, index, "progress_path")
    if not progress_path.exists():
        raise WorkspaceBundleError(
            "workspace_progress_not_found",
            f"Progress file is missing for operation {operation_id}",
        )
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    return _refresh_or_recover_interrupted_operation(
        root,
        operation_id,
        index,
        data,
        progress_path,
    )


def cancel_workspace_bundle(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    index = _read_operation_index(root, operation_id)
    cancel_path = _indexed_path(root, index, "cancel_path")
    cancel_path.write_text(_timestamp() + "\n", encoding="utf-8")

    progress_path = _indexed_path(root, index, "progress_path")
    if progress_path.exists():
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        if data.get("status") == "running":
            data["cancel_requested"] = True
            data["updated_at"] = _timestamp()
            tmp = progress_path.with_name(f"{progress_path.name}.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(progress_path)

    return {
        "operation_id": operation_id,
        "cancel_requested": True,
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
    index_dir = operation_index_dir(root)
    cutoff = time.time() - older_than_days * 24 * 60 * 60
    deleted: list[str] = []
    invalid: list[str] = []
    inspected_count = 0
    skipped_running = 0
    skipped_recent = 0
    skipped_invalid = 0
    skipped_non_workspace = 0

    for index_path in (sorted(index_dir.glob("*.json")) if index_dir.exists() else []):
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
        except (OSError, json.JSONDecodeError, WorkspaceBundleError):
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


def start_workspace_bundle_download(
    job: str | list[str],
    build: int | str = "lastBuild",
) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    operation_id = uuid.uuid4().hex

    with JenkinsClient(config) as client:
        build_info = client.get_json(
            f"{_build_path(job, build)}",
            params={"tree": "number,url,fullDisplayName,result,building"},
        )

    try:
        build_number = int(build_info["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "workspace_build_resolution_failed",
            "Jenkins build API response did not include a numeric build number",
        ) from exc
    name_prefix = f"{safe_job_name(job)}{build_number}"
    output_dir = _unique_output_dir(root, name_prefix, operation_id)
    output_dir.mkdir(parents=True, exist_ok=False)

    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    archive_path = output_dir / f"{name_prefix}.zip"
    workspace_dir = output_dir / "workspace"
    console_log_path = output_dir / f"{name_prefix}-console.log"
    metadata_path = output_dir / "metadata.json"

    progress = ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": "workspace_bundle",
            "status": "running",
            "phase": "queued",
            "job": job,
            "requested_build": build,
            "build_number": build_number,
            "build": build_info,
            "output_dir": str(output_dir),
            "archive_path": str(archive_path),
            "workspace_dir": str(workspace_dir),
            "console_log_path": str(console_log_path),
            "metadata_path": str(metadata_path),
            "cancel_requested": False,
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "workspace_archive": {},
            "extract": {},
            "console_log": {},
        },
    )
    _write_operation_index(root, operation_id, progress_path, cancel_path)

    thread = threading.Thread(
        target=_run_workspace_bundle,
        name=f"jenkins-workspace-bundle-{operation_id[:8]}",
        daemon=True,
        kwargs={
            "config": config,
            "job": job,
            "build_number": build_number,
            "name_prefix": name_prefix,
            "archive_path": archive_path,
            "workspace_dir": workspace_dir,
            "console_log_path": console_log_path,
            "metadata_path": metadata_path,
            "progress": progress,
            "cancel_path": cancel_path,
        },
    )
    _start_operation_thread(operation_id, thread)

    return {
        "operation_id": operation_id,
        "job": job,
        "build_number": build_number,
        "output_dir": str(output_dir),
        "progress_path": str(progress_path),
        "status": "running",
    }


def start_workspace_path_download(
    job: str | list[str],
    workspace_path: str,
    kind: str,
    build: int | str = "lastBuild",
) -> JsonDict:
    if kind not in {"file", "folder"}:
        raise WorkspaceBundleError(
            "invalid_workspace_path_kind",
            "kind must be either 'file' or 'folder'",
        )

    normalized_workspace_path = normalize_workspace_path(workspace_path)
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    operation_id = uuid.uuid4().hex

    with JenkinsClient(config) as client:
        build_info = client.get_json(
            f"{_build_path(job, build)}",
            params={"tree": "number,url,fullDisplayName,result,building"},
        )

    try:
        build_number = int(build_info["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "workspace_build_resolution_failed",
            "Jenkins build API response did not include a numeric build number",
        ) from exc

    name_prefix = f"{safe_job_name(job)}{build_number}"
    output_dir = _unique_output_dir(root, name_prefix, operation_id)
    output_dir.mkdir(parents=True, exist_ok=False)

    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    workspace_root = output_dir / "workspace"
    target_path = workspace_root.joinpath(*normalized_workspace_path.split("/"))
    archive_path = output_dir / f"{name_prefix}-{_safe_name(normalized_workspace_path)}.zip"
    console_log_path = output_dir / f"{name_prefix}-console.log"
    metadata_path = output_dir / "metadata.json"

    progress = ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "status": "running",
            "phase": "queued",
            "operation": "workspace_path_download",
            "job": job,
            "requested_build": build,
            "build_number": build_number,
            "build": build_info,
            "workspace_path": normalized_workspace_path,
            "kind": kind,
            "output_dir": str(output_dir),
            "workspace_dir": str(workspace_root),
            "target_path": str(target_path),
            "archive_path": str(archive_path) if kind == "folder" else None,
            "console_log_path": str(console_log_path),
            "metadata_path": str(metadata_path),
            "cancel_requested": False,
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "workspace_file": {},
            "workspace_archive": {},
            "extract": {},
            "console_log": {},
        },
    )
    _write_operation_index(root, operation_id, progress_path, cancel_path)

    thread = threading.Thread(
        target=_run_workspace_path_download,
        name=f"jenkins-workspace-path-{operation_id[:8]}",
        daemon=True,
        kwargs={
            "config": config,
            "job": job,
            "build_number": build_number,
            "workspace_path": normalized_workspace_path,
            "kind": kind,
            "archive_path": archive_path,
            "target_path": target_path,
            "console_log_path": console_log_path,
            "metadata_path": metadata_path,
            "progress": progress,
            "cancel_path": cancel_path,
        },
    )
    _start_operation_thread(operation_id, thread)

    return {
        "operation_id": operation_id,
        "job": job,
        "build_number": build_number,
        "workspace_path": normalized_workspace_path,
        "kind": kind,
        "output_dir": str(output_dir),
        "progress_path": str(progress_path),
        "status": "running",
    }


def _unique_output_dir(root: Path, name_prefix: str, operation_id: str) -> Path:
    candidate = root / name_prefix
    if not candidate.exists():
        return candidate
    return root / f"{name_prefix}-{operation_id[:8]}"


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

    def cancelled() -> bool:
        return cancel_path.exists()

    try:
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
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
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

    def cancelled() -> bool:
        return cancel_path.exists()

    try:
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
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
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
) -> None:
    partial_path.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)
    start = time.monotonic()
    last_update = 0.0

    progress.update(
        phase=phase,
        current_file=str(final_path),
        **{progress_key: {"path": str(final_path)}},
    )

    def on_progress(downloaded: int, total: int | None) -> None:
        nonlocal last_update
        now = time.monotonic()
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
