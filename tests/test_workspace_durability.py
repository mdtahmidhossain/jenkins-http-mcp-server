from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

import jenkins_mcp_server.workspace_bundle as workspace_bundle
from jenkins_mcp_server.errors import (
    InsufficientDiskSpaceError,
    ResponseTooLargeError,
    ToolInputError,
    WorkspaceBundleError,
)
from jenkins_mcp_server.workspace_bundle import ProgressFile


def _set_workspace_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR", str(root))


def _write_indexed_progress(
    root: Path,
    operation_id: str,
    output_dir: Path,
    status: str,
    operation: str | None = None,
) -> Path:
    progress_path = root / "progress" / f"{operation_id}.json"
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": operation,
            "status": status,
            "output_dir": str(output_dir),
        },
    )
    workspace_bundle._write_operation_index(
        root,
        operation_id,
        progress_path,
        root / "cancel" / operation_id,
    )
    return progress_path


def test_operation_index_paths_are_confined_to_download_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(WorkspaceBundleError, match="omitted progress_path"):
        workspace_bundle._indexed_path(root, {}, "progress_path")
    with pytest.raises(WorkspaceBundleError, match="unsafe progress_path"):
        workspace_bundle._indexed_path(
            root,
            {"progress_path": str(tmp_path / "outside.json")},
            "progress_path",
        )


def test_operation_thread_registration_and_interruption_detection() -> None:
    operation_id = "a" * 32

    class BrokenThread:
        def start(self) -> None:
            raise RuntimeError("cannot start")

    with pytest.raises(RuntimeError, match="cannot start"):
        workspace_bundle._start_operation_thread(operation_id, BrokenThread())  # type: ignore[arg-type]
    assert operation_id not in workspace_bundle._ACTIVE_OPERATIONS
    assert workspace_bundle._operation_was_interrupted(operation_id, {}) is False
    assert workspace_bundle._operation_was_interrupted(
        operation_id,
        {"server_instance_id": "old"},
    ) is True
    assert workspace_bundle._operation_was_interrupted(
        operation_id,
        {"server_instance_id": workspace_bundle._SERVER_INSTANCE_ID},
    ) is True

    class ThreadState:
        def __init__(self, alive: bool) -> None:
            self.alive = alive

        def is_alive(self) -> bool:
            return self.alive

    workspace_bundle._ACTIVE_OPERATIONS[operation_id] = ThreadState(True)  # type: ignore[assignment]
    assert workspace_bundle._operation_was_interrupted(
        operation_id,
        {"server_instance_id": workspace_bundle._SERVER_INSTANCE_ID},
    ) is False
    workspace_bundle._ACTIVE_OPERATIONS[operation_id] = ThreadState(False)  # type: ignore[assignment]
    assert workspace_bundle._operation_was_interrupted(
        operation_id,
        {"server_instance_id": workspace_bundle._SERVER_INSTANCE_ID},
    ) is True
    workspace_bundle._forget_operation_thread(operation_id)
    assert operation_id not in workspace_bundle._ACTIVE_OPERATIONS


def test_workspace_status_recovers_interrupted_worker_and_removes_partials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    operation_id = "b" * 32
    output_dir = root / "demo1"
    output_dir.mkdir(parents=True)
    archive = output_dir / "demo1.zip"
    archive.write_bytes(b"archive")
    workspace_dir = output_dir / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "kept.txt").write_text("complete", encoding="utf-8")
    paths = {
        "archive_path": str(archive),
        "workspace_dir": str(workspace_dir),
        "target_path": str(output_dir / "workspace" / "target.txt"),
        "console_log_path": str(output_dir / "console.log"),
    }
    for raw in paths.values():
        partial = workspace_bundle._partial_path(Path(raw))
        if Path(raw) == workspace_dir:
            partial.mkdir()
        else:
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
    progress_path = output_dir / ".progress.json"
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "status": "running",
            "output_dir": str(output_dir),
            **paths,
        },
    )
    workspace_bundle._write_operation_index(
        root,
        operation_id,
        progress_path,
        output_dir / ".cancel",
    )
    index_path = workspace_bundle.operation_index_path(root, operation_id)
    index = json.loads(index_path.read_text())
    index["server_instance_id"] = "previous-process"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    status = workspace_bundle.read_workspace_bundle_status(operation_id)

    assert status["status"] == "failed"
    assert status["error"]["code"] == "workspace_operation_interrupted"
    assert not archive.exists()
    assert (workspace_dir / "kept.txt").exists()
    for raw in paths.values():
        assert not workspace_bundle._partial_path(Path(raw)).exists()


def test_workspace_interruption_check_refreshes_terminal_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    root.mkdir()
    progress_path = root / "operation" / ".progress.json"
    ProgressFile(progress_path, {"status": "succeeded"})
    monkeypatch.setattr(workspace_bundle, "_operation_was_interrupted", lambda op, index: True)

    result = workspace_bundle._refresh_or_recover_interrupted_operation(
        root,
        "a" * 32,
        {},
        {"status": "running"},
        progress_path,
    )

    assert result["status"] == "succeeded"


def test_workspace_cleanup_validates_bounds_and_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    with pytest.raises(ToolInputError, match="older_than_days"):
        workspace_bundle.cleanup_workspace_bundle_operations(0)
    with pytest.raises(ToolInputError, match="max_operations"):
        workspace_bundle.cleanup_workspace_bundle_operations(1, 0)
    assert workspace_bundle.cleanup_workspace_bundle_operations()["deleted_count"] == 0


def test_workspace_cleanup_deletes_only_old_terminal_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    now = time.time()

    old_output = root / "old-output"
    old_output.mkdir(parents=True)
    (old_output / "data").write_text("x", encoding="utf-8")
    old_progress = _write_indexed_progress(root, "1" * 32, old_output, "succeeded")
    os.utime(old_progress, (now - 40 * 86400, now - 40 * 86400))

    running_output = root / "running-output"
    running_output.mkdir()
    running_progress = _write_indexed_progress(root, "2" * 32, running_output, "running")
    os.utime(running_progress, (now - 40 * 86400, now - 40 * 86400))

    class ActiveThread:
        def is_alive(self) -> bool:
            return True

    monkeypatch.setitem(
        workspace_bundle._ACTIVE_OPERATIONS,
        "2" * 32,
        ActiveThread(),  # type: ignore[arg-type]
    )

    recent_output = root / "recent-output"
    recent_output.mkdir()
    _write_indexed_progress(root, "3" * 32, recent_output, "failed")

    unsafe_progress = _write_indexed_progress(root, "4" * 32, root, "cancelled")
    os.utime(unsafe_progress, (now - 40 * 86400, now - 40 * 86400))

    malformed = workspace_bundle.operation_index_dir(root) / f"{'5' * 32}.json"
    malformed.write_text("not-json", encoding="utf-8")

    missing_output_id = "8" * 32
    missing_output_progress = root / "progress" / f"{missing_output_id}.json"
    ProgressFile(missing_output_progress, {"status": "failed"})
    os.utime(missing_output_progress, (now - 40 * 86400, now - 40 * 86400))
    workspace_bundle._write_operation_index(
        root,
        missing_output_id,
        missing_output_progress,
        root / "cancel" / missing_output_id,
    )

    interrupted_id = "9" * 32
    interrupted_output = root / "interrupted-output"
    interrupted_output.mkdir()
    interrupted_progress = _write_indexed_progress(
        root,
        interrupted_id,
        interrupted_output,
        "running",
    )
    os.utime(interrupted_progress, (now - 40 * 86400, now - 40 * 86400))
    interrupted_index_path = workspace_bundle.operation_index_path(root, interrupted_id)
    interrupted_index = json.loads(interrupted_index_path.read_text())
    interrupted_index["server_instance_id"] = "previous-process"
    interrupted_index_path.write_text(json.dumps(interrupted_index), encoding="utf-8")

    artifact_output = root / "artifact-output"
    artifact_output.mkdir()
    artifact_progress = _write_indexed_progress(
        root,
        "a" * 32,
        artifact_output,
        "running",
        operation="artifact_download",
    )
    os.utime(artifact_progress, (now - 40 * 86400, now - 40 * 86400))
    artifact_index_path = workspace_bundle.operation_index_path(root, "a" * 32)
    artifact_index = json.loads(artifact_index_path.read_text())
    artifact_index["server_instance_id"] = "previous-process"
    artifact_index_path.write_text(json.dumps(artifact_index), encoding="utf-8")

    result = workspace_bundle.cleanup_workspace_bundle_operations(older_than_days=30)

    assert result["deleted_operation_ids"] == ["1" * 32]
    assert result["skipped_running"] == 1
    assert result["skipped_recent"] == 2
    assert result["skipped_invalid"] == 3
    assert result["skipped_non_workspace"] == 1
    assert not old_output.exists()
    assert running_output.exists()
    assert recent_output.exists()
    assert json.loads(interrupted_progress.read_text())["error"]["code"] == (
        "workspace_operation_interrupted"
    )
    assert json.loads(artifact_progress.read_text())["status"] == "running"
    assert artifact_output.exists()


def test_workspace_cleanup_handles_symlink_missing_output_and_max_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    root.mkdir()
    old = time.time() - 40 * 86400

    target = root / "target"
    target.mkdir()
    symlink_output = root / "linked-output"
    symlink_output.symlink_to(target, target_is_directory=True)
    progress = _write_indexed_progress(root, "6" * 32, symlink_output, "succeeded")
    os.utime(progress, (old, old))

    missing_output = root / "already-missing"
    progress = _write_indexed_progress(root, "7" * 32, missing_output, "failed")
    os.utime(progress, (old, old))

    result = workspace_bundle.cleanup_workspace_bundle_operations(30, max_operations=1)
    assert result["deleted_count"] == 1
    assert not symlink_output.exists()
    assert target.exists()
    assert workspace_bundle.operation_index_path(root, "7" * 32).exists()

    second = workspace_bundle.cleanup_workspace_bundle_operations(30, max_operations=1)
    assert second["deleted_operation_ids"] == ["7" * 32]


def test_workspace_extract_preflights_uncompressed_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("report.txt", b"content")
    archive_path = tmp_path / "workspace.zip"
    archive_path.write_bytes(data.getvalue())
    progress = ProgressFile(tmp_path / "progress.json", {"status": "running"})
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        workspace_bundle,
        "ensure_free_space",
        lambda path, required: calls.append((path, required)),
    )

    workspace_bundle._extract_zip_safely(
        archive_path=archive_path,
        partial_dir=tmp_path / "workspace.partial",
        final_dir=tmp_path / "workspace",
        max_bytes=100,
        max_files=10,
        progress=progress,
        cancel_check=lambda: False,
        interval_seconds=0,
    )
    assert calls == [(tmp_path / "workspace.partial", len(b"content"))]

    def no_space(path: Path, required: int) -> None:
        raise InsufficientDiskSpaceError(required, 0, str(path))

    monkeypatch.setattr(workspace_bundle, "ensure_free_space", no_space)
    with pytest.raises(InsufficientDiskSpaceError):
        workspace_bundle._extract_zip_safely(
            archive_path=archive_path,
            partial_dir=tmp_path / "second.partial",
            final_dir=tmp_path / "second",
            max_bytes=100,
            max_files=10,
            progress=progress,
            cancel_check=lambda: False,
            interval_seconds=0,
        )
    assert not (tmp_path / "second.partial").exists()


def test_workspace_extract_enforces_actual_streamed_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Member:
        filename = "report.txt"
        file_size = 1
        external_attr = 0

        def is_dir(self) -> bool:
            return False

    class Source:
        def __init__(self) -> None:
            self.read_once = False

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            if self.read_once:
                return b""
            self.read_once = True
            return b"too large"

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def infolist(self) -> list[Member]:
            return [Member()]

        def open(self, member: Member) -> Source:
            return Source()

    monkeypatch.setattr(workspace_bundle.zipfile, "ZipFile", lambda path: Archive())
    progress = ProgressFile(tmp_path / "progress.json", {"status": "running"})
    with pytest.raises(ResponseTooLargeError) as exc_info:
        workspace_bundle._extract_zip_safely(
            archive_path=tmp_path / "unused.zip",
            partial_dir=tmp_path / "workspace.partial",
            final_dir=tmp_path / "workspace",
            max_bytes=1,
            max_files=1,
            progress=progress,
            cancel_check=lambda: False,
            interval_seconds=0,
        )
    assert exc_info.value.code == "response_too_large"
    assert not (tmp_path / "workspace.partial").exists()
