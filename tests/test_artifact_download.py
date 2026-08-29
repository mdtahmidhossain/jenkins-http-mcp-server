from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import jenkins_mcp_server.artifact_download as artifact_download
import jenkins_mcp_server.workspace_bundle as workspace_bundle
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import PathValidationError, WorkspaceBundleError
from jenkins_mcp_server.workspace_bundle import ProgressFile


def _set_artifact_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR", str(root))
    monkeypatch.setenv("JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS", "0.1")


def _wait_for_status(operation_id: str) -> dict[str, Any]:
    for _ in range(100):
        status = artifact_download.read_artifact_download_status(operation_id)
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("artifact download did not finish")


def test_artifact_download_streams_to_disk_with_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    _set_artifact_env(monkeypatch, root)
    seen_paths: list[str] = []
    payload = b"artifact-content"

    class FakeClient:
        def __init__(self, config: JenkinsConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params=None) -> dict[str, Any]:
            assert path == "job/folder/job/my%20job/lastBuild"
            return {"number": 123, "url": "https://jenkins.example.com/job/x/123/"}

        def stream_to_file(
            self,
            path: str,
            destination: Path,
            *,
            max_bytes: int,
            progress_callback,
            cancel_check,
        ) -> dict[str, Any]:
            seen_paths.append(path)
            assert len(payload) <= max_bytes
            assert not cancel_check()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            progress_callback(1, None)
            progress_callback(2, None)
            progress_callback(len(payload), len(payload))
            return {"bytes_downloaded": len(payload), "total_bytes": len(payload)}

    monkeypatch.setattr(artifact_download, "JenkinsClient", FakeClient)

    started = artifact_download.start_artifact_download(
        "folder/my job",
        "reports/report file.zip",
    )
    status = _wait_for_status(started["operation_id"])
    destination = Path(status["destination_path"])

    assert status["status"] == "succeeded"
    assert destination.read_bytes() == payload
    assert destination.name == "report_file.zip"
    assert status["download"]["complete"] is True
    assert status["download"]["speed_bytes_per_second"] > 0
    assert seen_paths == [
        "job/folder/job/my%20job/123/artifact/reports/report%20file.zip"
    ]
    assert json.loads(Path(status["metadata_path"]).read_text())["artifact_path"] == (
        "reports/report file.zip"
    )


@pytest.mark.parametrize(
    "artifact_path",
    [
        "",
        ".",
        "../secret",
        "/absolute",
        "https://example.com/file",
        "reports/file?raw=1",
        "reports/*zip*/file",
        "reports/*.zip",
    ],
)
def test_artifact_path_rejects_unsafe_values(artifact_path: str) -> None:
    with pytest.raises(PathValidationError):
        artifact_download.normalize_artifact_path(artifact_path)


def test_artifact_path_and_build_helpers() -> None:
    assert artifact_download.normalize_artifact_path("reports/./x.txt") == "reports/x.txt"
    assert artifact_download._artifact_endpoint("demo", 7, "a b/x.txt") == (
        "job/demo/7/artifact/a%20b/x.txt"
    )
    with pytest.raises(PathValidationError, match="build must"):
        artifact_download._build_path("demo", "bad/build")


def test_artifact_download_rejects_build_without_number(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_artifact_env(monkeypatch, tmp_path / "artifacts")

    class MissingBuildClient:
        def __init__(self, config: JenkinsConfig) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params=None) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(artifact_download, "JenkinsClient", MissingBuildClient)
    with pytest.raises(WorkspaceBundleError) as exc_info:
        artifact_download.start_artifact_download("demo", "report.txt")
    assert exc_info.value.code == "artifact_build_resolution_failed"


def test_artifact_status_recovers_interrupted_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    _set_artifact_env(monkeypatch, root)
    operation_id = "a" * 32
    output_dir = root / "demo1-artifact"
    destination = output_dir / "artifact" / "report.zip"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"incomplete")
    partial = destination.with_name("report.zip.partial")
    partial.write_bytes(b"partial")
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "status": "running",
            "destination_path": str(destination),
        },
    )
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)
    index_path = workspace_bundle.operation_index_path(root, operation_id)
    index = json.loads(index_path.read_text())
    index["server_instance_id"] = "previous-process"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    status = artifact_download.read_artifact_download_status(operation_id)

    assert status["status"] == "failed"
    assert status["error"]["code"] == "artifact_operation_interrupted"
    assert not destination.exists()
    assert not partial.exists()


def test_artifact_status_refreshes_terminal_progress_after_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    _set_artifact_env(monkeypatch, root)
    operation_id = "e" * 32
    output_dir = root / "operation"
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    ProgressFile(progress_path, {"status": "running"})
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)

    def finish_before_liveness_result(operation: str, index: dict[str, Any]) -> bool:
        ProgressFile(progress_path, {"status": "succeeded"})
        return True

    monkeypatch.setattr(
        artifact_download,
        "_operation_was_interrupted",
        finish_before_liveness_result,
    )
    assert artifact_download.read_artifact_download_status(operation_id)["status"] == "succeeded"


def test_artifact_status_cancel_and_missing_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    _set_artifact_env(monkeypatch, root)
    output_dir = root / "operation"
    output_dir.mkdir(parents=True)
    operation_id = "b" * 32
    progress_path = output_dir / ".progress.json"
    cancel_path = output_dir / ".cancel"
    ProgressFile(progress_path, {"status": "succeeded"})
    workspace_bundle._write_operation_index(root, operation_id, progress_path, cancel_path)

    assert artifact_download.read_artifact_download_status(operation_id)["status"] == "succeeded"
    cancelled = artifact_download.cancel_artifact_download(operation_id)
    assert cancelled["cancel_requested"] is True
    assert cancel_path.exists()

    running_id = "d" * 32
    running_dir = root / "running"
    running_progress = running_dir / ".progress.json"
    ProgressFile(running_progress, {"status": "running"})
    workspace_bundle._write_operation_index(
        root,
        running_id,
        running_progress,
        running_dir / ".cancel",
    )
    artifact_download.cancel_artifact_download(running_id)
    assert json.loads(running_progress.read_text())["cancel_requested"] is True

    missing_id = "c" * 32
    missing_progress = root / "missing" / ".progress.json"
    missing_progress.parent.mkdir()
    workspace_bundle._write_operation_index(
        root,
        missing_id,
        missing_progress,
        missing_progress.parent / ".cancel",
    )
    with pytest.raises(WorkspaceBundleError) as error:
        artifact_download.read_artifact_download_status(missing_id)
    assert error.value.code == "artifact_progress_not_found"
    no_progress_cancel = artifact_download.cancel_artifact_download(missing_id)
    assert no_progress_cancel["cancel_requested"] is True


def test_artifact_runner_cancellation_and_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = JenkinsConfig(
        url="https://jenkins.example.com/",
        user="u",
        api_token="t",
        artifact_progress_interval_seconds=1.0,
    )

    def run(behavior: str) -> ProgressFile:
        output_dir = tmp_path / behavior
        destination = output_dir / "artifact" / "report.zip"
        cancel_path = output_dir / ".cancel"
        progress = ProgressFile(
            output_dir / ".progress.json",
            {
                "operation_id": behavior,
                "status": "running",
                "phase": "queued",
                "download": {},
            },
        )

        class FakeClient:
            def __init__(self, received: JenkinsConfig) -> None:
                assert received is config

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def stream_to_file(self, path: str, partial: Path, **kwargs: Any) -> dict[str, Any]:
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"partial")
                if behavior == "cancelled":
                    cancel_path.write_text("cancel", encoding="utf-8")
                    return {}
                if behavior == "jenkins-error":
                    raise WorkspaceBundleError("jenkins_failure", "failed")
                raise RuntimeError("network failed")

        monkeypatch.setattr(artifact_download, "JenkinsClient", FakeClient)
        artifact_download._run_artifact_download(
            config=config,
            job="demo",
            build_number=1,
            artifact_path="report.zip",
            destination=destination,
            metadata_path=output_dir / "metadata.json",
            progress=progress,
            cancel_path=cancel_path,
        )
        assert not destination.exists()
        assert not destination.with_name("report.zip.partial").exists()
        return progress

    cancelled = run("cancelled")
    assert cancelled.data["status"] == "cancelled"
    assert cancelled.data["error"]["code"] == "operation_cancelled"

    jenkins_failure = run("jenkins-error")
    assert jenkins_failure.data["status"] == "failed"
    assert jenkins_failure.data["error"]["cause"]["code"] == "jenkins_failure"

    runtime_failure = run("runtime-error")
    assert runtime_failure.data["error"]["cause"]["code"] == "artifact_download_error"
