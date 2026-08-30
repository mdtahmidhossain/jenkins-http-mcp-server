from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jenkins_mcp_server.workspace_registry import (
    WorkspaceOperationRegistry,
    current_process_id,
)


def _claim(
    registry: WorkspaceOperationRegistry,
    operation_id: str,
    request_key: str = "request-key",
    *,
    stale_before: float = 0.0,
):
    operation_dir = registry.directory / operation_id
    return registry.claim_or_join(
        operation_id=operation_id,
        request_key=request_key,
        request={"operation": "workspace_bundle", "job": "demo"},
        progress_path=operation_dir / "progress.json",
        cancel_path=operation_dir / "cancel",
        stale_before=stale_before,
    )


def test_registry_coordinates_lifecycle_and_reusable_captures(tmp_path: Path) -> None:
    registry = WorkspaceOperationRegistry(tmp_path)
    operation_id = "a" * 32
    row, created, stale = _claim(registry, operation_id)

    assert created is True
    assert stale == []
    assert registry.request(row)["job"] == "demo"
    assert registry.find_active("request-key")["operation_id"] == operation_id

    joined, created, stale = _claim(registry, "b" * 32)
    assert created is False
    assert stale == []
    assert joined["operation_id"] == operation_id

    assert registry.claim_worker(operation_id, "owner", 123) is not None
    assert registry.claim_worker(operation_id, "other", 456) is None
    registry.set_spawned_pid(operation_id, 789)
    assert registry.get(operation_id)["worker_pid"] == 789
    assert registry.heartbeat(operation_id, "other") is False
    assert registry.heartbeat(operation_id, "owner") is True

    output_dir = tmp_path / "demo1"
    output_dir.mkdir()
    assert (
        registry.set_capture(
            operation_id,
            "other",
            output_dir=output_dir,
            anchor_build_number=1,
        )
        is False
    )
    assert (
        registry.set_capture(
            operation_id,
            "owner",
            output_dir=output_dir,
            anchor_build_number=1,
        )
        is True
    )
    assert registry.clear_capture(operation_id, "other") is False
    assert registry.clear_capture(operation_id, "owner") is True
    assert (
        registry.set_capture(
            operation_id,
            "owner",
            output_dir=output_dir,
            anchor_build_number=1,
        )
        is True
    )

    assert registry.finish(operation_id, "other", "succeeded") is False
    assert registry.finish(operation_id, "owner", "succeeded") is True
    terminal = registry.request_cancel(operation_id)
    assert terminal is not None
    assert terminal["status"] == "succeeded"
    assert terminal["cancel_requested"] == 0
    assert registry.find_active("request-key") is None
    assert registry.find_reusable("request-key", 1)[0]["operation_id"] == operation_id

    registry.invalidate_reusable(operation_id)
    assert registry.get(operation_id)["error_code"] == "workspace_cached_payload_missing"

    registry.touch(operation_id)
    assert registry.cleanup_candidates(1)[0]["operation_id"] == operation_id
    registry.delete(operation_id)
    assert registry.get(operation_id) is None


def test_registry_cancellation_start_failure_and_stale_replacement(tmp_path: Path) -> None:
    registry = WorkspaceOperationRegistry(tmp_path)
    missing = "0" * 32
    assert registry.request_cancel(missing) is None
    assert registry.cancellation_requested(missing, "owner") is True

    unowned = "1" * 32
    _claim(registry, unowned, "unowned")
    registry.fail_unowned_start(unowned, "start_failed")
    assert registry.get(unowned)["error_code"] == "start_failed"

    operation_id = "2" * 32
    _claim(registry, operation_id, "stale")
    registry.set_spawned_pid(operation_id, 222)
    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET heartbeat_at = 1 WHERE operation_id = ?",
            (operation_id,),
        )

    replacement, created, stale = _claim(
        registry,
        "3" * 32,
        "stale",
        stale_before=time.time(),
    )
    assert created is True
    assert replacement["operation_id"] == "3" * 32
    assert stale[0]["operation_id"] == operation_id
    assert registry.get(operation_id)["error_code"] == "workspace_operation_interrupted"

    assert registry.mark_stale(replacement["operation_id"], 0)["status"] == "running"
    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET heartbeat_at = 1 WHERE operation_id = ?",
            (replacement["operation_id"],),
        )
    assert registry.mark_stale(replacement["operation_id"], time.time())["status"] == "failed"

    cancel_id = "4" * 32
    _claim(registry, cancel_id, "cancel")
    registry.claim_worker(cancel_id, "owner", 444)
    row = registry.request_cancel(cancel_id)
    assert row is not None and row["cancel_requested"] == 1
    assert registry.cancellation_requested(cancel_id, "owner") is True
    assert registry.heartbeat(cancel_id, "owner") is False


def test_registry_rolls_back_failed_claim_and_rejects_non_object_request(
    tmp_path: Path,
) -> None:
    registry = WorkspaceOperationRegistry(tmp_path)
    operation_id = "5" * 32
    _claim(registry, operation_id, "first")

    with pytest.raises(sqlite3.IntegrityError):
        _claim(registry, operation_id, "second")
    assert registry.find_active("second") is None

    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET request_json = ? WHERE operation_id = ?",
            (json.dumps([]), operation_id),
        )
    with pytest.raises(ValueError, match="not an object"):
        registry.request(registry.get(operation_id))

    assert current_process_id() == os.getpid()


def test_registry_initialization_retries_only_locked_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = WorkspaceOperationRegistry(tmp_path)
    original_connect = registry._connect
    calls = 0
    sleeps: list[float] = []

    def locked_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_connect()

    monkeypatch.setattr(registry, "_connect", locked_once)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    registry._initialize()
    assert calls == 2
    assert sleeps == [0.05]

    monkeypatch.setattr(
        registry,
        "_connect",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        registry._initialize()


def test_registry_atomic_claim_joins_concurrent_callers(tmp_path: Path) -> None:
    operation_ids = [f"{index:032x}" for index in range(1, 9)]

    def claim(operation_id: str):
        registry = WorkspaceOperationRegistry(tmp_path)
        return _claim(registry, operation_id, "shared-request")

    with ThreadPoolExecutor(max_workers=len(operation_ids)) as executor:
        results = list(executor.map(claim, operation_ids))

    created = [row for row, was_created, _ in results if was_created]
    assert len(created) == 1
    assert {row["operation_id"] for row, _, _ in results} == {created[0]["operation_id"]}


def test_registry_atomic_claim_joins_separate_processes(tmp_path: Path) -> None:
    gate = tmp_path / "start"
    script = """
import json
import sys
import time
from pathlib import Path
from jenkins_mcp_server.workspace_registry import WorkspaceOperationRegistry

root = Path(sys.argv[1])
operation_id = sys.argv[2]
gate = Path(sys.argv[3])
registry = WorkspaceOperationRegistry(root)
while not gate.exists():
    time.sleep(0.01)
operation_dir = registry.directory / operation_id
row, created, _ = registry.claim_or_join(
    operation_id=operation_id,
    request_key="shared-process-request",
    request={"operation": "workspace_bundle", "job": "demo"},
    progress_path=operation_dir / "progress.json",
    cancel_path=operation_dir / "cancel",
    stale_before=0,
)
print(json.dumps({"operation_id": row["operation_id"], "created": created}))
"""
    operation_ids = [f"{index:032x}" for index in range(10, 14)]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), operation_id, str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for operation_id in operation_ids
    ]
    gate.touch()
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))

    created = [result for result in results if result["created"]]
    assert len(created) == 1
    assert {result["operation_id"] for result in results} == {created[0]["operation_id"]}
