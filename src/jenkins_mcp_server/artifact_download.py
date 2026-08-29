from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .client import JenkinsClient, job_path, safe_segment
from .config import JenkinsConfig
from .errors import (
    JenkinsMCPError,
    OperationCancelledError,
    PathValidationError,
    WorkspaceBundleError,
)
from .workspace_bundle import (
    ProgressFile,
    _forget_operation_thread,
    _indexed_path,
    _operation_was_interrupted,
    _read_operation_index,
    _safe_name,
    _start_operation_thread,
    _timestamp,
    _transfer_progress,
    _unique_output_dir,
    _write_operation_index,
    safe_job_name,
)

JsonDict = dict[str, Any]
ARTIFACT_MAGIC_SEGMENTS = {"*zip*", "*plain*", "*view*", "*fingerprint*"}


def normalize_artifact_path(artifact_path: str) -> str:
    raw = artifact_path.strip().replace("\\", "/")
    if not raw:
        raise PathValidationError("artifact_path must not be empty")
    split = urlsplit(raw)
    if split.scheme or split.netloc or raw.startswith("//") or split.path.startswith("/"):
        raise PathValidationError("artifact_path must be relative")
    if split.query or split.fragment:
        raise PathValidationError("artifact_path must not include query or fragment")

    parts: list[str] = []
    for part in split.path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise PathValidationError("artifact_path traversal is not allowed")
        if part in ARTIFACT_MAGIC_SEGMENTS:
            raise PathValidationError(f"artifact_path must not include Jenkins token {part}")
        if "*" in part or "?" in part:
            raise PathValidationError("artifact_path wildcards are not allowed")
        parts.append(part)

    if not parts:
        raise PathValidationError("artifact_path must include a file path")
    return "/".join(parts)


def _build_path(job: str | list[str], build: int | str) -> str:
    build_id = str(build)
    if not build_id or build_id in {".", ".."} or "/" in build_id:
        raise PathValidationError("build must be a number or permalink path segment")
    return f"{job_path(job)}/{safe_segment(build_id, 'build')}"


def _artifact_endpoint(job: str | list[str], build_number: int, artifact_path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in artifact_path.split("/"))
    return f"{_build_path(job, build_number)}/artifact/{encoded_path}"


def _artifact_index(root: Path, operation_id: str) -> JsonDict:
    return _read_operation_index(
        root,
        operation_id,
        error_code="artifact_operation_not_found",
        label="artifact download operation",
    )


def _artifact_indexed_path(root: Path, index: JsonDict, key: str) -> Path:
    return _indexed_path(
        root,
        index,
        key,
        error_code="artifact_operation_index_invalid",
        label="Artifact operation",
    )


def start_artifact_download(
    job: str | list[str],
    artifact_path: str,
    build: int | str = "lastBuild",
) -> JsonDict:
    normalized_path = normalize_artifact_path(artifact_path)
    config = JenkinsConfig.from_env()
    root = config.require_artifact_download()
    operation_id = uuid.uuid4().hex

    with JenkinsClient(config) as client:
        build_info = client.get_json(
            _build_path(job, build),
            params={"tree": "number,url,fullDisplayName,result,building"},
        )
    try:
        build_number = int(build_info["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBundleError(
            "artifact_build_resolution_failed",
            "Jenkins build API response did not include a numeric build number",
        ) from exc

    name_prefix = f"{safe_job_name(job)}{build_number}-artifact"
    output_dir = _unique_output_dir(root, name_prefix, operation_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    destination = output_dir / "artifact" / _safe_name(Path(normalized_path).name)
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    metadata_path = output_dir / "metadata.json"
    progress = ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": "artifact_download",
            "status": "running",
            "phase": "queued",
            "job": job,
            "requested_build": build,
            "build_number": build_number,
            "build": build_info,
            "artifact_path": normalized_path,
            "output_dir": str(output_dir),
            "destination_path": str(destination),
            "metadata_path": str(metadata_path),
            "cancel_requested": False,
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "download": {},
        },
    )
    _write_operation_index(root, operation_id, progress_path, cancel_path)

    thread = threading.Thread(
        target=_run_artifact_download,
        name=f"jenkins-artifact-{operation_id[:8]}",
        daemon=True,
        kwargs={
            "config": config,
            "job": job,
            "build_number": build_number,
            "artifact_path": normalized_path,
            "destination": destination,
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
        "artifact_path": normalized_path,
        "output_dir": str(output_dir),
        "progress_path": str(progress_path),
        "status": "running",
    }


def read_artifact_download_status(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_artifact_download()
    index = _artifact_index(root, operation_id)
    progress_path = _artifact_indexed_path(root, index, "progress_path")
    if not progress_path.exists():
        raise WorkspaceBundleError(
            "artifact_progress_not_found",
            f"Progress file is missing for operation {operation_id}",
        )
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    if data.get("status") != "running" or not _operation_was_interrupted(operation_id, index):
        return data
    refreshed = json.loads(progress_path.read_text(encoding="utf-8"))
    if refreshed.get("status") != "running":
        return refreshed
    data = refreshed

    raw_destination = data.get("destination_path")
    if isinstance(raw_destination, str):
        destination = Path(raw_destination)
        root_resolved = root.resolve()
        destination_resolved = destination.resolve(strict=False)
        if destination_resolved != root_resolved and root_resolved in destination_resolved.parents:
            destination.unlink(missing_ok=True)
            destination.with_name(f"{destination.name}.partial").unlink(missing_ok=True)
    data.update(
        {
            "status": "failed",
            "phase": "failed",
            "error": {
                "code": "artifact_operation_interrupted",
                "message": "Artifact download stopped when its MCP server process exited",
            },
            "interrupted_at": _timestamp(),
            "updated_at": _timestamp(),
        }
    )
    tmp = progress_path.with_name(f"{progress_path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(progress_path)
    return data


def cancel_artifact_download(operation_id: str) -> JsonDict:
    config = JenkinsConfig.from_env()
    root = config.require_artifact_download()
    index = _artifact_index(root, operation_id)
    cancel_path = _artifact_indexed_path(root, index, "cancel_path")
    cancel_path.write_text(_timestamp() + "\n", encoding="utf-8")
    progress_path = _artifact_indexed_path(root, index, "progress_path")
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


def _run_artifact_download(
    *,
    config: JenkinsConfig,
    job: str | list[str],
    build_number: int,
    artifact_path: str,
    destination: Path,
    metadata_path: Path,
    progress: ProgressFile,
    cancel_path: Path,
) -> None:
    partial = destination.with_name(f"{destination.name}.partial")
    start = time.monotonic()
    last_update = 0.0

    def cancelled() -> bool:
        return cancel_path.exists()

    def on_progress(downloaded: int, total: int | None) -> None:
        nonlocal last_update
        now = time.monotonic()
        if (
            now - last_update < config.artifact_progress_interval_seconds
            and (total is None or downloaded != total)
        ):
            return
        last_update = now
        progress.update(download=_transfer_progress(start, downloaded, total))

    try:
        progress.update(
            phase="downloading_artifact",
            current_file=str(destination),
            download={"path": str(destination)},
        )
        with JenkinsClient(config) as client:
            client.stream_to_file(
                _artifact_endpoint(job, build_number, artifact_path),
                partial,
                max_bytes=config.max_artifact_bytes,
                progress_callback=on_progress,
                cancel_check=cancelled,
            )
        if cancelled():
            raise OperationCancelledError("Operation was cancelled")
        partial.replace(destination)
        completed_at = _timestamp()
        metadata = {
            "job": job,
            "build_number": build_number,
            "artifact_path": artifact_path,
            "destination_path": str(destination),
            "completed_at": completed_at,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        progress.update(
            status="succeeded",
            phase="completed",
            completed_at=completed_at,
            download={"path": str(destination), "complete": True},
        )
    except OperationCancelledError as exc:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        progress.update(
            status="cancelled",
            phase="cancelled",
            cancel_requested=True,
            error={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 - background task must persist errors to status.
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        cause = (
            exc.to_dict()["error"]
            if isinstance(exc, JenkinsMCPError)
            else {"code": "artifact_download_error", "message": str(exc)}
        )
        progress.update(
            status="failed",
            phase="failed",
            error={
                "code": "artifact_download_failed",
                "message": "Artifact download failed",
                "cause": cause,
            },
        )
    finally:
        _forget_operation_thread(str(progress.data["operation_id"]))
