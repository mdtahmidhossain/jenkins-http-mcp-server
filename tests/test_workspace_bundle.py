from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

import jenkins_mcp_server.workspace_bundle as workspace_bundle
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import WorkspaceBundleError
from jenkins_mcp_server.workspace_bundle import (
    ProgressFile,
    cancel_workspace_bundle,
    read_workspace_bundle_status,
    start_workspace_bundle_download,
)


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
            assert path == "job/my-job/lastBuild"
            return {"number": 123, "url": "https://jenkins.example.com/job/my-job/123/"}

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

    started = start_workspace_bundle_download("my-job", "lastBuild")
    status = _wait_for_status(started["operation_id"])

    output_dir = Path(started["output_dir"])
    assert status["status"] == "succeeded"
    assert not (output_dir / "my-job123.zip").exists()
    assert (output_dir / "workspace" / "README.txt").read_text() == "hello"
    assert (output_dir / "workspace" / "nested" / "file.txt").read_text() == "world"
    assert (output_dir / "my-job123-console.log").read_bytes() == console_log
    assert json.loads((output_dir / "metadata.json").read_text())["build_number"] == 123
    assert status["workspace_archive"]["complete"] is True
    assert status["console_log"]["speed_mib_per_second"] >= 0


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

    assert result["cancel_requested"] is True
    assert cancel_path.exists()
    assert status["cancel_requested"] is True


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


def _wait_for_status(operation_id: str) -> dict[str, Any]:
    for _ in range(100):
        status = read_workspace_bundle_status(operation_id)
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("workspace bundle operation did not finish")
