from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jenkins_mcp_server.workspace_worker as workspace_worker
from jenkins_mcp_server.workspace_bundle import ProgressFile, _operation_paths
from jenkins_mcp_server.workspace_registry import WorkspaceOperationRegistry


def _set_workspace_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR", str(root))


def _create_operation(root: Path, operation_id: str) -> None:
    registry = WorkspaceOperationRegistry(root)
    operation_dir, progress_path, cancel_path = _operation_paths(root, operation_id)
    registry.claim_or_join(
        operation_id=operation_id,
        request_key=operation_id,
        request={"operation": "workspace_bundle", "job": "demo"},
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    operation_dir.mkdir()
    ProgressFile(progress_path, {"operation_id": operation_id, "status": "running"})


def test_worker_rejects_invalid_or_unclaimable_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_workspace_env(monkeypatch, tmp_path)
    assert workspace_worker.run("invalid") == 2
    assert workspace_worker.run("a" * 32) == 3

    _create_operation(tmp_path, "b" * 32)
    registry = WorkspaceOperationRegistry(tmp_path)
    assert registry.claim_worker("b" * 32, "existing", 111) is not None
    assert workspace_worker.run("b" * 32) == 3


def test_worker_returns_success_only_for_succeeded_registry_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_workspace_env(monkeypatch, tmp_path)
    success_id = "c" * 32
    _create_operation(tmp_path, success_id)

    def succeed(config, registry, row, owner_id) -> None:
        assert registry.finish(row["operation_id"], owner_id, "succeeded")

    monkeypatch.setattr(workspace_worker, "run_registered_workspace_operation", succeed)
    assert workspace_worker.run(success_id) == 0

    unfinished_id = "d" * 32
    _create_operation(tmp_path, unfinished_id)
    monkeypatch.setattr(
        workspace_worker,
        "run_registered_workspace_operation",
        lambda config, registry, row, owner_id: None,
    )
    assert workspace_worker.run(unfinished_id) == 1


def test_worker_persists_unhandled_failure_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_workspace_env(monkeypatch, tmp_path)
    operation_id = "e" * 32
    _create_operation(tmp_path, operation_id)

    def fail(config, registry, row, owner_id) -> None:
        raise RuntimeError("secret failure detail")

    monkeypatch.setattr(workspace_worker, "run_registered_workspace_operation", fail)
    assert workspace_worker.run(operation_id) == 1
    row = WorkspaceOperationRegistry(tmp_path).get(operation_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "workspace_worker_failed"


def test_worker_main_uses_single_operation_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        workspace_worker, "run", lambda operation_id: calls.append(operation_id) or 7
    )
    monkeypatch.setattr(sys, "argv", ["workspace-worker", "f" * 32])
    with pytest.raises(SystemExit) as exit_info:
        workspace_worker.main()
    assert exit_info.value.code == 7
    assert calls == ["f" * 32]

    monkeypatch.setattr(sys, "argv", ["workspace-worker"])
    with pytest.raises(SystemExit):
        workspace_worker.main()
    assert calls[-1] == ""
