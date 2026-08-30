from __future__ import annotations

import io
import json
import stat
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jenkins_mcp_server.workspace_bundle as workspace_bundle
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import (
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
    WorkspaceBundleError,
)
from jenkins_mcp_server.workspace_bundle import (
    ProgressFile,
    cancel_workspace_bundle,
    normalize_workspace_path,
    read_workspace_bundle_status,
    start_workspace_bundle_download,
    start_workspace_path_download,
)
from jenkins_mcp_server.workspace_registry import WorkspaceOperationRegistry


def _set_workspace_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR", str(root))
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_PROGRESS_INTERVAL_SECONDS", "0.1")


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return data.getvalue()


def _stable_job_state(build_number: int = 123) -> dict[str, Any]:
    build = {
        "number": build_number,
        "url": f"https://jenkins.example.com/job/my-job/{build_number}/",
        "queueId": 42,
        "building": False,
        "inProgress": False,
        "result": "SUCCESS",
    }
    return {
        "url": "https://jenkins.example.com/job/my-job/",
        "inQueue": False,
        "queueItem": None,
        "lastBuild": build,
        "lastCompletedBuild": {"number": build_number},
        "builds": [build],
    }


def _install_inline_worker(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def spawn(operation_id: str) -> SimpleNamespace:
        config = JenkinsConfig.from_env()
        registry = WorkspaceOperationRegistry(root)
        owner_id = "inline-worker"
        row = registry.claim_worker(operation_id, owner_id, 12345)
        assert row is not None
        workspace_bundle.run_registered_workspace_operation(config, registry, row, owner_id)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(workspace_bundle, "_spawn_workspace_worker", spawn)


def test_workspace_bundle_download_extracts_deletes_archive_and_saves_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    workspace_zip = _zip_bytes({"README.txt": b"hello", "nested/file.txt": b"world"})
    console_log = b"build log\nline 2\n"

    class FakeClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "job/my-job":
                return _stable_job_state()
            if path == "queue":
                return {"items": []}
            raise AssertionError(path)

        def stream_to_file(
            self,
            path: str,
            destination: Path,
            *,
            max_bytes: int,
            progress_callback,
            cancel_check,
        ) -> dict[str, Any]:
            if path == "job/my-job/ws/**/*zip*/my-job123.zip":
                payload = workspace_zip
            elif path == "job/my-job/123/consoleText":
                payload = console_log
            else:  # pragma: no cover - assertion aid
                raise AssertionError(path)
            assert len(payload) <= max_bytes
            destination.parent.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            with destination.open("wb") as handle:
                for index in range(0, len(payload), 3):
                    assert not cancel_check()
                    chunk = payload[index : index + 3]
                    handle.write(chunk)
                    downloaded += len(chunk)
                    progress_callback(downloaded, len(payload))
            return {"bytes_downloaded": downloaded, "total_bytes": len(payload)}

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", FakeClient)
    _install_inline_worker(monkeypatch, root)

    started = start_workspace_bundle_download("my-job", "lastBuild")
    status = _wait_for_status(started["operation_id"])

    output_dir = Path(started["output_dir"])
    assert output_dir == root / "my-job" / "123"
    assert status["status"] == "succeeded"
    assert not (output_dir / "my-job123.zip").exists()
    assert (output_dir / "workspace" / "README.txt").read_text() == "hello"
    assert (output_dir / "workspace" / "nested" / "file.txt").read_text() == "world"
    assert (output_dir / "my-job123-console.log").read_bytes() == console_log
    assert json.loads((output_dir / "metadata.json").read_text())["build_number"] == 123
    assert status["workspace_archive"]["complete"] is True
    assert status["console_log"]["speed_mib_per_second"] >= 0


def test_workspace_folder_path_download_extracts_under_requested_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    folder_zip = _zip_bytes({"report.xml": b"<testsuite />"})
    console_log = b"folder log\n"

    class FakeClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "job/my-job":
                return _stable_job_state()
            if path == "queue":
                return {"items": []}
            raise AssertionError(path)

        def stream_to_file(
            self,
            path: str,
            destination: Path,
            *,
            max_bytes: int,
            progress_callback,
            cancel_check,
        ) -> dict[str, Any]:
            if path == "job/my-job/ws/target/reports/**/*zip*/my-job123-target_reports.zip":
                payload = folder_zip
            elif path == "job/my-job/123/consoleText":
                payload = console_log
            else:  # pragma: no cover - assertion aid
                raise AssertionError(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            progress_callback(len(payload), len(payload))
            assert not cancel_check()
            return {"bytes_downloaded": len(payload), "total_bytes": len(payload)}

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", FakeClient)
    _install_inline_worker(monkeypatch, root)

    started = start_workspace_path_download("my-job", "target/reports", "folder")
    status = _wait_for_status(started["operation_id"])
    output_dir = Path(started["output_dir"])

    assert status["status"] == "succeeded"
    assert not (output_dir / "my-job123-target_reports.zip").exists()
    assert (output_dir / "workspace" / "target" / "reports" / "report.xml").read_bytes() == (
        b"<testsuite />"
    )
    assert (output_dir / "my-job123-console.log").read_bytes() == console_log
    assert status["workspace_path"] == "target/reports"
    assert status["kind"] == "folder"


def test_workspace_file_path_download_saves_file_and_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    file_content = b"<html>report</html>"
    console_log = b"file log\n"

    class FakeClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "job/my-job":
                return _stable_job_state()
            if path == "queue":
                return {"items": []}
            raise AssertionError(path)

        def stream_to_file(
            self,
            path: str,
            destination: Path,
            *,
            max_bytes: int,
            progress_callback,
            cancel_check,
        ) -> dict[str, Any]:
            if path == "job/my-job/ws/target/report.html":
                payload = file_content
            elif path == "job/my-job/123/consoleText":
                payload = console_log
            else:  # pragma: no cover - assertion aid
                raise AssertionError(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            progress_callback(len(payload), len(payload))
            assert not cancel_check()
            return {"bytes_downloaded": len(payload), "total_bytes": len(payload)}

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", FakeClient)
    _install_inline_worker(monkeypatch, root)

    started = start_workspace_path_download("my-job", "target/report.html", "file")
    status = _wait_for_status(started["operation_id"])
    output_dir = Path(started["output_dir"])

    assert status["status"] == "succeeded"
    assert (output_dir / "workspace" / "target" / "report.html").read_bytes() == file_content
    assert (output_dir / "my-job123-console.log").read_bytes() == console_log
    assert status["workspace_file"]["complete"] is True


def test_workspace_bundle_cancel_writes_cancel_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    operation_id = "a" * 32
    output_dir = root / "my-job123"
    output_dir.mkdir(parents=True)
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    progress_path.write_text(
        json.dumps({"operation_id": operation_id, "status": "running"}),
        encoding="utf-8",
    )
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)

    result = cancel_workspace_bundle(operation_id)
    status = read_workspace_bundle_status(operation_id)

    assert workspace_bundle.operation_index_dir(root).stat().st_mode & 0o777 == 0o700
    assert result["cancel_requested"] is True
    assert cancel_path.exists()
    assert status["cancel_requested"] is True


def test_legacy_workspace_cancel_does_not_change_completed_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    operation_id = "c" * 32
    output_dir = root / "my-job123"
    output_dir.mkdir(parents=True)
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    progress_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)

    result = cancel_workspace_bundle(operation_id)

    assert result["cancel_requested"] is False
    assert result["status"] == "succeeded"
    assert not cancel_path.exists()


def test_legacy_workspace_cancel_reports_concurrent_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    operation_id = "d" * 32
    output_dir = root / "my-job123"
    output_dir.mkdir(parents=True)
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    progress_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)
    original_write_text = Path.write_text

    def complete_then_write_marker(path: Path, *args: Any, **kwargs: Any) -> int:
        if path == cancel_path:
            progress_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", complete_then_write_marker)
    result = cancel_workspace_bundle(operation_id)

    assert result["cancel_requested"] is False
    assert result["status"] == "succeeded"
    assert not cancel_path.exists()


@pytest.mark.parametrize(
    "workspace_path",
    [
        "../secret.txt",
        "/absolute/path",
        "https://example.com/file",
        "target/*zip*/x",
        "target/*.xml",
        "target/report.html?raw=1",
    ],
)
def test_workspace_path_validation_rejects_unsafe_paths(workspace_path: str) -> None:
    with pytest.raises(PathValidationError):
        normalize_workspace_path(workspace_path)


def test_workspace_path_download_rejects_invalid_kind() -> None:
    with pytest.raises(WorkspaceBundleError) as error:
        start_workspace_path_download("my-job", "target/report.html", "directory")

    assert error.value.code == "invalid_workspace_path_kind"


def test_safe_extract_rejects_zip_slip(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    archive_path.write_bytes(_zip_bytes({"../evil.txt": b"no"}))
    progress = ProgressFile(
        tmp_path / ".progress.json",
        {"status": "running", "phase": "extracting_workspace_archive"},
    )

    with pytest.raises(WorkspaceBundleError):
        workspace_bundle._extract_zip_safely(
            archive_path=archive_path,
            partial_dir=tmp_path / "workspace.partial",
            final_dir=tmp_path / "workspace",
            max_bytes=1000,
            max_files=100,
            progress=progress,
            cancel_check=lambda: False,
            interval_seconds=0.0,
        )

    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "workspace").exists()


def test_workspace_name_path_and_output_edge_cases(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="job must include"):
        workspace_bundle.safe_job_name([])
    with pytest.raises(PathValidationError, match="must not be empty"):
        normalize_workspace_path("")
    assert normalize_workspace_path("target/./reports/file.xml") == "target/reports/file.xml"
    with pytest.raises(PathValidationError, match="must include"):
        normalize_workspace_path(".")

    with pytest.raises(PathValidationError, match="build must"):
        workspace_bundle._build_path("my-job", "bad/build")


def test_start_downloads_reject_build_without_numeric_number(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)

    class MissingBuildClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "job/my-job":
                return _stable_job_state()
            if path == "queue":
                return {"items": []}
            return {}

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", MissingBuildClient)

    with pytest.raises(WorkspaceBundleError, match="numeric build number") as bundle_error:
        start_workspace_bundle_download("my-job", "missing")
    with pytest.raises(WorkspaceBundleError, match="numeric build number") as path_error:
        start_workspace_path_download("my-job", "reports/result.xml", "file", "missing")

    assert bundle_error.value.code == "workspace_build_resolution_failed"
    assert path_error.value.code == "workspace_build_resolution_failed"


def test_operation_lookup_reports_invalid_missing_and_missing_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    root.mkdir(parents=True)

    with pytest.raises(WorkspaceBundleError, match="Invalid operation ID"):
        workspace_bundle._read_operation_index(root, "invalid")
    with pytest.raises(WorkspaceBundleError, match="No workspace bundle operation"):
        workspace_bundle._read_operation_index(root, "a" * 32)

    operation_id = "b" * 32
    progress_path = root / "missing" / ".progress.json"
    workspace_bundle._write_operation_index(
        root,
        operation_id,
        progress_path,
        root / "missing" / ".cancel",
    )
    with pytest.raises(WorkspaceBundleError, match="Progress file is missing") as error:
        read_workspace_bundle_status(operation_id)
    assert error.value.code == "workspace_progress_not_found"


def test_workspace_bundle_runner_persists_cancellation_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ContextClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", ContextClient)
    config = JenkinsConfig(url="https://jenkins.example.com/", user="u", api_token="t")

    cancel_dir = tmp_path / "cancel-bundle"
    cancel_progress = ProgressFile(
        cancel_dir / ".progress.json",
        {"operation_id": "cancel-bundle", "status": "running", "phase": "queued"},
    )

    def cancel_download(**kwargs: Any) -> None:
        raise OperationCancelledError("Operation was cancelled")

    monkeypatch.setattr(workspace_bundle, "_download_with_progress", cancel_download)
    workspace_bundle._run_workspace_bundle(
        config=config,
        job="my-job",
        build_number=123,
        name_prefix="my-job123",
        archive_path=cancel_dir / "my-job123.zip",
        workspace_dir=cancel_dir / "workspace",
        console_log_path=cancel_dir / "console.log",
        metadata_path=cancel_dir / "metadata.json",
        progress=cancel_progress,
        cancel_path=cancel_dir / ".cancel",
    )
    assert cancel_progress.data["status"] == "cancelled"
    assert cancel_progress.data["error"]["code"] == "operation_cancelled"

    failed_dir = tmp_path / "failed-bundle"
    failed_progress = ProgressFile(
        failed_dir / ".progress.json",
        {"operation_id": "failed-bundle", "status": "running", "phase": "queued"},
    )

    def fail_download(**kwargs: Any) -> None:
        kwargs["progress"].update(phase="downloading_workspace_archive")
        raise RuntimeError("network failed")

    monkeypatch.setattr(workspace_bundle, "_download_with_progress", fail_download)
    workspace_bundle._run_workspace_bundle(
        config=config,
        job="my-job",
        build_number=123,
        name_prefix="my-job123",
        archive_path=failed_dir / "my-job123.zip",
        workspace_dir=failed_dir / "workspace",
        console_log_path=failed_dir / "console.log",
        metadata_path=failed_dir / "metadata.json",
        progress=failed_progress,
        cancel_path=failed_dir / ".cancel",
    )
    assert failed_progress.data["status"] == "failed"
    assert failed_progress.data["error"]["code"] == "workspace_archive_download_failed"


def test_workspace_path_runner_persists_cancellation_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ContextClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", ContextClient)
    config = JenkinsConfig(url="https://jenkins.example.com/", user="u", api_token="t")

    def run_with(download, output_name: str) -> ProgressFile:
        output_dir = tmp_path / output_name
        progress = ProgressFile(
            output_dir / ".progress.json",
            {"operation_id": output_name, "status": "running", "phase": "queued"},
        )
        monkeypatch.setattr(workspace_bundle, "_download_with_progress", download)
        workspace_bundle._run_workspace_path_download(
            config=config,
            job="my-job",
            build_number=123,
            workspace_path="reports/result.xml",
            kind="file",
            archive_path=output_dir / "unused.zip",
            target_path=output_dir / "workspace" / "reports" / "result.xml",
            console_log_path=output_dir / "console.log",
            metadata_path=output_dir / "metadata.json",
            progress=progress,
            cancel_path=output_dir / ".cancel",
        )
        return progress

    def cancel_download(**kwargs: Any) -> None:
        raise OperationCancelledError("Operation was cancelled")

    cancel_progress = run_with(cancel_download, "cancel-path")
    assert cancel_progress.data["status"] == "cancelled"
    assert cancel_progress.data["error"]["code"] == "operation_cancelled"

    def fail_download(**kwargs: Any) -> None:
        kwargs["progress"].update(phase="downloading_workspace_file")
        raise RuntimeError("network failed")

    failed_progress = run_with(fail_download, "failed-path")
    assert failed_progress.data["status"] == "failed"
    assert failed_progress.data["error"]["code"] == "workspace_file_download_failed"


def test_safe_extract_handles_directories_and_enforces_limits(tmp_path: Path) -> None:
    directory_archive = tmp_path / "directory.zip"
    with zipfile.ZipFile(directory_archive, "w") as archive:
        archive.writestr("empty/", b"")
    directory_progress = ProgressFile(
        tmp_path / "directory-progress.json",
        {"status": "running", "phase": "queued"},
    )
    workspace_bundle._extract_zip_safely(
        archive_path=directory_archive,
        partial_dir=tmp_path / "directory.partial",
        final_dir=tmp_path / "directory",
        max_bytes=100,
        max_files=10,
        progress=directory_progress,
        cancel_check=lambda: False,
        interval_seconds=0.0,
    )
    assert (tmp_path / "directory" / "empty").is_dir()

    file_archive = tmp_path / "file.zip"
    file_archive.write_bytes(_zip_bytes({"result.txt": b"content"}))
    limit_progress = ProgressFile(
        tmp_path / "limit-progress.json",
        {"status": "running", "phase": "queued"},
    )
    with pytest.raises(WorkspaceBundleError) as file_limit_error:
        workspace_bundle._extract_zip_safely(
            archive_path=file_archive,
            partial_dir=tmp_path / "file-limit.partial",
            final_dir=tmp_path / "file-limit",
            max_bytes=100,
            max_files=0,
            progress=limit_progress,
            cancel_check=lambda: False,
            interval_seconds=0.0,
        )
    assert file_limit_error.value.code == "workspace_extract_file_limit_exceeded"

    with pytest.raises(ResponseTooLargeError):
        workspace_bundle._extract_zip_safely(
            archive_path=file_archive,
            partial_dir=tmp_path / "byte-limit.partial",
            final_dir=tmp_path / "byte-limit",
            max_bytes=0,
            max_files=10,
            progress=limit_progress,
            cancel_check=lambda: False,
            interval_seconds=0.0,
        )


def test_safe_zip_target_rejects_absolute_links_special_duplicates_and_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extract"
    root.mkdir()

    with pytest.raises(WorkspaceBundleError, match="absolute zip entry"):
        workspace_bundle._safe_zip_target(root, zipfile.ZipInfo("/absolute.txt"), set())

    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(WorkspaceBundleError, match="symlink zip entry"):
        workspace_bundle._safe_zip_target(root, symlink, set())

    special = zipfile.ZipInfo("pipe")
    special.create_system = 3
    special.external_attr = (stat.S_IFIFO | 0o600) << 16
    with pytest.raises(WorkspaceBundleError, match="special zip entry"):
        workspace_bundle._safe_zip_target(root, special, set())

    duplicate = zipfile.ZipInfo("duplicate.txt")
    seen: set[str] = set()
    workspace_bundle._safe_zip_target(root, duplicate, seen)
    with pytest.raises(WorkspaceBundleError, match="Duplicate zip entry"):
        workspace_bundle._safe_zip_target(root, duplicate, seen)

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceBundleError, match="escapes target"):
        workspace_bundle._safe_zip_target(root, zipfile.ZipInfo("linked/file.txt"), set())


def test_cancel_cleanup_partial_path_and_error_payloads(tmp_path: Path) -> None:
    with pytest.raises(OperationCancelledError):
        workspace_bundle._raise_if_cancelled(lambda: True)

    partial_dir = tmp_path / "directory.partial"
    partial_dir.mkdir()
    (partial_dir / "file.txt").write_text("data", encoding="utf-8")
    partial_file = tmp_path / "file.partial"
    partial_file.write_text("data", encoding="utf-8")
    workspace_bundle._cleanup_partial(partial_dir, partial_file, tmp_path / "missing")
    assert not partial_dir.exists()
    assert not partial_file.exists()
    assert workspace_bundle._partial_path(tmp_path / "file.txt").name == "file.txt.partial"

    jenkins_error = WorkspaceBundleError("jenkins_failure", "failed")
    assert workspace_bundle._error_payload(jenkins_error)["code"] == "jenkins_failure"
    assert workspace_bundle._error_payload(RuntimeError("failed"))["code"] == (
        "workspace_bundle_failed"
    )
    assert workspace_bundle._error_payload(zipfile.BadZipFile())["code"] == (
        "workspace_archive_extract_failed"
    )

    phases = {
        "downloading_workspace_archive": "workspace_archive_download_failed",
        "downloading_workspace_file": "workspace_file_download_failed",
        "downloading_console_log": "console_log_download_failed",
        "extracting_workspace_archive": "workspace_archive_extract_failed",
    }
    for phase, expected_code in phases.items():
        assert workspace_bundle._error_payload(RuntimeError("failed"), phase)["code"] == (
            expected_code
        )


def _wait_for_status(operation_id: str) -> dict[str, Any]:
    for _ in range(100):
        status = read_workspace_bundle_status(operation_id)
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("workspace bundle operation did not finish")
