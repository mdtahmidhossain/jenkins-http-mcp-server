from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jenkins_mcp_server.workspace_bundle as workspace_bundle
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import OperationCancelledError, WorkspaceBundleError
from jenkins_mcp_server.workspace_bundle import ProgressFile
from jenkins_mcp_server.workspace_registry import WorkspaceOperationRegistry


def _set_workspace_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR", str(root))


def _config(root: Path) -> JenkinsConfig:
    return JenkinsConfig(
        url="https://jenkins.example.com/",
        user="alice",
        api_token="secret",
        enable_workspace_download=True,
        workspace_download_dir=root,
    )


def _build(number: int, *, building: bool = False, in_progress: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://jenkins.example.com/job/demo/{number}/",
        "queueId": number + 100,
        "building": building,
        "inProgress": in_progress,
        "result": None if in_progress else "SUCCESS",
    }


def _state(
    number: int,
    *,
    stable: bool = True,
    building: bool = False,
    in_progress: bool = False,
    queued: bool = False,
) -> dict[str, Any]:
    build = {
        "number": number,
        "url": f"https://jenkins.example.com/job/demo/{number}/",
        "queue_id": number + 100,
        "building": building,
        "in_progress": in_progress,
        "result": None if in_progress else "SUCCESS",
    }
    return {
        "checked_at": "2026-08-30T00:00:00Z",
        "job_url": "https://jenkins.example.com/job/demo/",
        "in_queue": queued,
        "queue_unresolved": False,
        "queued_items": [{"id": 1}] if queued else [],
        "active_builds": [build] if in_progress else [],
        "last_build": build,
        "last_completed_build": {"number": number},
        "stable": stable,
    }


def _job_api(number: int = 7) -> dict[str, Any]:
    build = _build(number)
    return {
        "url": "https://jenkins.example.com/job/demo/",
        "inQueue": False,
        "queueItem": None,
        "lastBuild": build,
        "lastCompletedBuild": {"number": number},
        "builds": [build],
    }


def test_stale_worker_threshold_covers_both_retried_state_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workspace_bundle.time, "time", lambda: 1_000.0)
    config = _config(tmp_path)
    assert workspace_bundle._stale_before(config) == 790.0


class _ApiClient:
    def __init__(
        self,
        job_data: Any,
        queue_data: Any,
        *,
        build_data: Any = None,
    ) -> None:
        self.config = JenkinsConfig(
            url="https://jenkins.example.com/",
            user="alice",
            api_token="secret",
        )
        self.job_data = job_data
        self.queue_data = queue_data
        self.build_data = build_data

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "job/demo":
            return self.job_data
        if path == "queue":
            return self.queue_data
        return self.build_data


class _ContextApiClient(_ApiClient):
    job_data: Any = _job_api()
    queue_data: Any = {"items": []}
    build_data: Any = {"number": 7}

    def __init__(self, config: JenkinsConfig) -> None:
        super().__init__(self.job_data, self.queue_data, build_data=self.build_data)
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _register_operation(
    root: Path,
    request: dict[str, Any],
    operation_id: str = "a" * 32,
    owner_id: str = "owner",
) -> tuple[WorkspaceOperationRegistry, dict[str, Any], str]:
    registry = WorkspaceOperationRegistry(root)
    operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(
        root, operation_id
    )
    row, created, _ = registry.claim_or_join(
        operation_id=operation_id,
        request_key=f"key-{operation_id}",
        request=request,
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    assert created
    operation_dir.mkdir()
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": request.get("operation"),
            "job": request.get("job"),
            "workspace_path": request.get("workspace_path"),
            "kind": request.get("kind"),
            "status": "running",
            "phase": "checking_workspace_state",
            "output_dir": None,
            "workspace_guard": {},
        },
    )
    claimed = registry.claim_worker(operation_id, owner_id, 123)
    assert claimed is not None
    return registry, claimed, owner_id


def test_workspace_state_probe_reports_queue_build_and_post_processing() -> None:
    job_data = _job_api()
    job_data["inQueue"] = True
    job_data["builds"] = [
        _build(7),
        _build(6, building=True, in_progress=True),
        _build(5, in_progress=True),
    ]
    queue_data = {
        "items": [
            None,
            {"cancelled": True, "task": {"url": "/job/demo/"}},
            {"cancelled": False, "task": {"url": "/job/other/"}},
            {
                "id": 9,
                "why": "waiting",
                "blocked": False,
                "buildable": True,
                "stuck": False,
                "cancelled": False,
                "task": {"url": "/job/demo/"},
            },
        ]
    }

    state = workspace_bundle._probe_workspace_state(_ApiClient(job_data, queue_data), "demo")

    assert state["stable"] is False
    assert state["queue_unresolved"] is False
    assert state["queued_items"] == [
        {"id": 9, "why": "waiting", "blocked": False, "buildable": True, "stuck": False}
    ]
    assert [build["number"] for build in state["active_builds"]] == [5, 6]
    assert workspace_bundle._workspace_wait_phase(state) == "waiting_for_build"

    state["active_builds"] = [_state(5, stable=False, in_progress=True)["last_build"]]
    assert workspace_bundle._workspace_wait_phase(state) == "waiting_for_post_processing"
    state["active_builds"] = []
    assert workspace_bundle._workspace_wait_phase(state) == "waiting_for_queue"


@pytest.mark.parametrize(
    ("job_data", "queue_data", "message"),
    [
        ([], {}, "not an object"),
        ({"url": None}, {"items": []}, "omitted its URL"),
        ({"url": "/job/demo/", "inQueue": None}, {"items": []}, "omitted inQueue"),
        (
            {"url": "/job/demo/", "inQueue": False, "builds": None},
            {"items": []},
            "omitted its recent builds",
        ),
        (
            {
                "url": "/job/demo/",
                "inQueue": False,
                "builds": [],
                "lastBuild": None,
            },
            {},
            "omitted its items",
        ),
    ],
)
def test_workspace_state_probe_rejects_incomplete_responses(
    job_data: Any,
    queue_data: Any,
    message: str,
) -> None:
    with pytest.raises(WorkspaceBundleError, match=message):
        workspace_bundle._probe_workspace_state(_ApiClient(job_data, queue_data), "demo")


def test_workspace_state_validation_and_build_resolution_errors() -> None:
    assert workspace_bundle._normalize_object_url("https://jenkins.example.com/", "") is None
    with pytest.raises(WorkspaceBundleError, match="not an object"):
        workspace_bundle._build_state(None)
    with pytest.raises(WorkspaceBundleError, match="numeric build number"):
        workspace_bundle._build_state({})
    with pytest.raises(WorkspaceBundleError, match="building or inProgress"):
        workspace_bundle._build_state({"number": 1, "building": True})

    with pytest.raises(WorkspaceBundleError, match="no build"):
        workspace_bundle._state_anchor({})
    with pytest.raises(WorkspaceBundleError, match="numeric build number"):
        workspace_bundle._state_anchor({"last_build": {}})
    assert (
        workspace_bundle._resolve_requested_build(_ApiClient({}, {}), "demo", "lastBuild") is None
    )
    assert (
        workspace_bundle._resolve_requested_build(
            _ApiClient({}, {}, build_data={"number": "8"}), "demo", 8
        )
        == 8
    )
    with pytest.raises(WorkspaceBundleError, match="not an object"):
        workspace_bundle._resolve_requested_build(_ApiClient({}, {}, build_data=[]), "demo", 8)
    with pytest.raises(WorkspaceBundleError, match="numeric build number"):
        workspace_bundle._resolve_requested_build(_ApiClient({}, {}, build_data={}), "demo", 8)

    workspace_bundle._require_current_build(None, 8)
    with pytest.raises(WorkspaceBundleError) as error:
        workspace_bundle._require_current_build(7, 8)
    assert error.value.code == "workspace_build_not_current"


def test_workspace_request_key_normalizes_job_and_build_forms(tmp_path: Path) -> None:
    config = _config(tmp_path)
    string_request = {
        "operation": "workspace_bundle",
        "job": "folder/demo",
        "build": 7,
    }
    list_request = {
        "operation": "workspace_bundle",
        "job": ["folder", "demo"],
        "build": "7",
    }

    assert workspace_bundle._request_key(config, string_request) == workspace_bundle._request_key(
        config, list_request
    )


def test_wait_for_stable_workspace_tracks_queue_build_and_post_processing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    states = [
        _state(7, stable=False, queued=True),
        _state(7, stable=False, building=True, in_progress=True),
        _state(7, stable=False, in_progress=True),
        _state(7),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(
        workspace_bundle, "_probe_workspace_state", lambda client, job: states.pop(0)
    )
    monkeypatch.setattr(
        workspace_bundle,
        "_sleep_with_cancel",
        lambda seconds, cancel: sleeps.append(seconds),
    )
    progress = ProgressFile(tmp_path / "progress.json", {"status": "running"})

    result = workspace_bundle._wait_for_stable_workspace(
        client=object(),  # type: ignore[arg-type]
        job="demo",
        progress=progress,
        cancel_check=lambda: False,
    )

    assert result["stable"] is True
    assert sleeps == [10.0, 10.0, 10.0]
    assert progress.data["workspace_guard"]["state_checks"] == 4


def test_start_reuses_capture_force_refreshes_and_joins_active_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)
    spawned: list[str] = []
    monkeypatch.setattr(
        workspace_bundle,
        "_spawn_workspace_worker",
        lambda operation_id: spawned.append(operation_id) or SimpleNamespace(pid=777),
    )

    config = JenkinsConfig.from_env()
    request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": "lastBuild",
        "workspace_path": None,
        "kind": None,
    }
    key = workspace_bundle._request_key(config, request)
    registry = WorkspaceOperationRegistry(root)
    operation_id = "b" * 32
    operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(
        root, operation_id
    )
    row, _, _ = registry.claim_or_join(
        operation_id=operation_id,
        request_key=key,
        request=request,
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    operation_dir.mkdir()
    output_dir = root / "demo" / "7"
    workspace_dir = output_dir / "workspace"
    workspace_dir.mkdir(parents=True)
    console_log = output_dir / "demo7-console.log"
    metadata = output_dir / "metadata.json"
    console_log.write_text("log", encoding="utf-8")
    metadata.write_text("{}", encoding="utf-8")
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": "workspace_bundle",
            "job": "demo",
            "status": "succeeded",
            "phase": "completed",
            "output_dir": str(output_dir),
            "workspace_dir": str(workspace_dir),
            "console_log_path": str(console_log),
            "metadata_path": str(metadata),
        },
    )
    registry.claim_worker(operation_id, "owner", 111)
    registry.set_capture(
        operation_id,
        "owner",
        output_dir=output_dir,
        anchor_build_number=7,
    )
    registry.finish(operation_id, "owner", "succeeded")

    reused = workspace_bundle.start_workspace_bundle_download("demo")
    assert reused["disposition"] == "reused"
    assert reused["operation_id"] == operation_id
    assert spawned == []

    refreshed = workspace_bundle.start_workspace_bundle_download("demo", force_refresh=True)
    assert refreshed["disposition"] == "started"
    assert spawned == [refreshed["operation_id"]]

    joined = workspace_bundle.start_workspace_bundle_download("demo")
    assert joined["disposition"] == "joined"
    assert joined["operation_id"] == refreshed["operation_id"]


def test_start_invalidates_missing_cache_and_reports_worker_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)
    config = JenkinsConfig.from_env()
    request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": "lastBuild",
        "workspace_path": None,
        "kind": None,
    }
    registry = WorkspaceOperationRegistry(root)
    operation_id = "c" * 32
    operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(
        root, operation_id
    )
    registry.claim_or_join(
        operation_id=operation_id,
        request_key=workspace_bundle._request_key(config, request),
        request=request,
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    operation_dir.mkdir()
    output_dir = root / "demo" / "7"
    output_dir.mkdir(parents=True)
    ProgressFile(
        progress_path,
        {
            "operation_id": operation_id,
            "operation": "workspace_bundle",
            "job": "demo",
            "status": "succeeded",
            "phase": "completed",
            "output_dir": str(output_dir),
            "workspace_dir": str(output_dir / "missing-workspace"),
            "console_log_path": str(output_dir / "missing.log"),
            "metadata_path": str(output_dir / "missing.json"),
        },
    )
    registry.claim_worker(operation_id, "owner", 111)
    registry.set_capture(
        operation_id,
        "owner",
        output_dir=output_dir,
        anchor_build_number=7,
    )
    registry.finish(operation_id, "owner", "succeeded")

    monkeypatch.setattr(
        workspace_bundle,
        "_spawn_workspace_worker",
        lambda operation_id: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )
    with pytest.raises(WorkspaceBundleError) as error:
        workspace_bundle.start_workspace_bundle_download("demo")

    assert error.value.code == "workspace_worker_start_failed"
    assert registry.get(operation_id)["error_code"] == "workspace_cached_payload_missing"
    failed = [
        row
        for row in registry.cleanup_candidates(10)
        if row["error_code"] == "workspace_worker_start_failed"
    ]
    assert len(failed) == 1
    progress = json.loads(Path(failed[0]["progress_path"]).read_text())
    assert progress["error"]["type"] == "RuntimeError"


def test_registered_worker_retries_once_when_workspace_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": "lastBuild",
        "desired_build_number": None,
        "workspace_path": None,
        "kind": None,
    }
    registry, row, owner = _register_operation(root, request)
    states = [_state(1), _state(1), _state(2), _state(2), _state(2), _state(2)]
    monkeypatch.setattr(
        workspace_bundle, "_probe_workspace_state", lambda client, job: states.pop(0)
    )
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)

    def capture(**kwargs: Any) -> dict[str, Any]:
        kwargs["state_check"]()
        kwargs["metadata_path"].write_text("{}", encoding="utf-8")
        return {"completed_at": "2026-08-30T00:00:00Z"}

    monkeypatch.setattr(workspace_bundle, "_capture_workspace_bundle", capture)

    workspace_bundle.run_registered_workspace_operation(_config(root), registry, row, owner)

    status = registry.get(row["operation_id"])
    assert status["status"] == "succeeded"
    assert status["anchor_build_number"] == 2
    assert not (root / "demo" / "1").exists()
    assert (root / "demo" / "2").exists()
    progress = json.loads(Path(status["progress_path"]).read_text())
    assert progress["workspace_guard"]["capture_attempt"] == 2
    assert progress["workspace_guard"]["retry_count"] == 1


def test_registered_worker_fails_after_second_workspace_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": "lastBuild",
        "desired_build_number": None,
        "workspace_path": None,
        "kind": None,
    }
    registry, row, owner = _register_operation(root, request)
    states = [_state(1), _state(1), _state(2), _state(2), _state(2), _state(3)]
    monkeypatch.setattr(
        workspace_bundle, "_probe_workspace_state", lambda client, job: states.pop(0)
    )
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)

    def capture(**kwargs: Any) -> dict[str, Any]:
        kwargs["state_check"]()
        return {"completed_at": "unused"}

    monkeypatch.setattr(workspace_bundle, "_capture_workspace_bundle", capture)

    workspace_bundle.run_registered_workspace_operation(_config(root), registry, row, owner)

    status = registry.get(row["operation_id"])
    assert status["status"] == "failed"
    progress = json.loads(Path(status["progress_path"]).read_text())
    assert progress["error"]["code"] == "workspace_changed_during_download"
    assert not (root / "demo" / "1").exists()
    assert not (root / "demo" / "2").exists()


def test_registered_worker_rejects_historical_build_and_persists_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)
    monkeypatch.setattr(workspace_bundle, "_probe_workspace_state", lambda client, job: _state(7))

    historical_root = tmp_path / "historical"
    historical_request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": 6,
        "desired_build_number": 6,
        "workspace_path": None,
        "kind": None,
    }
    registry, row, owner = _register_operation(historical_root, historical_request)
    workspace_bundle.run_registered_workspace_operation(
        _config(historical_root), registry, row, owner
    )
    progress = json.loads(Path(row["progress_path"]).read_text())
    assert progress["error"]["code"] == "workspace_build_not_current"

    cancelled_root = tmp_path / "cancelled"
    cancelled_request = {**historical_request, "build": "lastBuild", "desired_build_number": None}
    registry, row, owner = _register_operation(
        cancelled_root,
        cancelled_request,
        operation_id="d" * 32,
    )
    Path(row["cancel_path"]).write_text("cancel\n", encoding="utf-8")
    workspace_bundle.run_registered_workspace_operation(
        _config(cancelled_root), registry, row, owner
    )
    progress = json.loads(Path(row["progress_path"]).read_text())
    assert progress["status"] == "cancelled"
    assert progress["error"]["code"] == "operation_cancelled"


def test_worker_cancel_check_and_sleep_honor_ownership_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry, row, _ = _register_operation(
        tmp_path,
        {"operation": "workspace_bundle", "job": "demo"},
    )
    monkeypatch.setattr(workspace_bundle, "WORKSPACE_HEARTBEAT_SECONDS", 0.0)
    check = workspace_bundle._worker_cancel_check(
        registry,
        row["operation_id"],
        "wrong-owner",
        Path(row["cancel_path"]),
    )
    assert check() is True
    assert check() is True

    workspace_bundle._sleep_with_cancel(0.001, lambda: False)
    with pytest.raises(OperationCancelledError):
        workspace_bundle._sleep_with_cancel(1.0, lambda: True)


def test_download_progress_checks_jenkins_state_during_and_after_stream(
    tmp_path: Path,
) -> None:
    class Client:
        def stream_to_file(
            self,
            path: str,
            destination: Path,
            *,
            max_bytes: int,
            progress_callback,
            cancel_check,
        ) -> None:
            destination.write_bytes(b"abc")
            progress_callback(1, 3)
            progress_callback(3, 3)

    checks: list[int] = []
    progress = ProgressFile(tmp_path / "progress.json", {"status": "running"})
    partial = tmp_path / "file.partial"
    final = tmp_path / "file"

    workspace_bundle._download_with_progress(
        client=Client(),  # type: ignore[arg-type]
        source_path="job/demo/ws/file",
        partial_path=partial,
        final_path=final,
        max_bytes=10,
        progress=progress,
        progress_key="workspace_file",
        phase="downloading_workspace_file",
        cancel_check=lambda: False,
        interval_seconds=0,
        state_check=lambda: checks.append(1) or _state(7),
        state_check_interval_seconds=0,
    )

    assert final.read_bytes() == b"abc"
    assert checks == [1, 1, 1]
    assert progress.data["workspace_file"]["complete"] is True


def test_registry_status_recovers_stale_worker_and_synthesizes_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)

    stale_id = "1" * 32
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id=stale_id,
    )
    output_dir = root / "stale-output"
    output_dir.mkdir()
    assert registry.set_capture(
        stale_id,
        owner,
        output_dir=output_dir,
        anchor_build_number=7,
    )
    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET heartbeat_at = 1 WHERE operation_id = ?",
            (stale_id,),
        )

    status = workspace_bundle.read_workspace_bundle_status(stale_id)
    assert status["status"] == "failed"
    assert status["error"]["code"] == "workspace_operation_interrupted"
    assert not output_dir.exists()

    failed_id = "2" * 32
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id=failed_id,
    )
    assert registry.finish(failed_id, owner, "failed", error_code="worker_failed")
    status = workspace_bundle.read_workspace_bundle_status(failed_id)
    assert status["phase"] == "failed"
    assert status["error"]["code"] == "worker_failed"


def test_registry_status_falls_back_without_progress_and_rejects_invalid_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    registry = WorkspaceOperationRegistry(root)

    fallback_id = "3" * 32
    operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(root, fallback_id)
    registry.claim_or_join(
        operation_id=fallback_id,
        request_key="fallback",
        request={"operation": "workspace_path_download", "job": "demo", "kind": "file"},
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    status = workspace_bundle.read_workspace_bundle_status(fallback_id)
    assert status["phase"] == "starting_worker"
    assert status["operation"] == "workspace_path_download"

    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET request_json = 'not-json' WHERE operation_id = ?",
            (fallback_id,),
        )
    progress_path.unlink(missing_ok=True)
    _, fallback = workspace_bundle._registry_progress(root, registry.get(fallback_id))
    assert fallback["operation"] is None
    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET request_json = '[]' WHERE operation_id = ?",
            (fallback_id,),
        )
    _, fallback = workspace_bundle._registry_progress(root, registry.get(fallback_id))
    assert fallback["operation"] is None

    invalid_id = "4" * 32
    registry, row, _ = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id=invalid_id,
    )
    invalid_path = Path(row["progress_path"])
    invalid_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(WorkspaceBundleError) as invalid_error:
        workspace_bundle._registry_progress(root, registry.get(invalid_id))
    assert invalid_error.value.code == "workspace_progress_invalid"

    invalid_path.write_text("[]", encoding="utf-8")
    with pytest.raises(WorkspaceBundleError, match="not an object"):
        workspace_bundle._registry_progress(root, registry.get(invalid_id))


def test_registry_cancel_is_durable_when_marker_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    registry, row, _ = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id="5" * 32,
    )
    cancel_path = Path(row["cancel_path"])
    original_write_text = Path.write_text

    def write_text(path: Path, *args: Any, **kwargs: Any) -> int:
        if path == cancel_path:
            raise OSError("read only")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    result = workspace_bundle.cancel_workspace_bundle(row["operation_id"])

    assert result["cancel_requested"] is True
    assert result["cancel_marker_written"] is False
    assert registry.get(row["operation_id"])["cancel_requested"] == 1


def test_registry_cancel_writes_marker_for_running_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    _, row, _ = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id="1" * 32,
    )

    result = workspace_bundle.cancel_workspace_bundle(row["operation_id"])

    assert result["cancel_requested"] is True
    assert result["cancel_marker_written"] is True
    assert Path(row["cancel_path"]).exists()


def test_registry_cancel_does_not_change_completed_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
        operation_id="0" * 32,
    )
    assert registry.finish(row["operation_id"], owner, "succeeded") is True

    result = workspace_bundle.cancel_workspace_bundle(row["operation_id"])

    assert result["cancel_requested"] is False
    assert result["cancel_marker_written"] is False
    assert result["status"] == "succeeded"
    assert not Path(row["cancel_path"]).exists()


def test_registry_cleanup_handles_terminal_active_stale_and_invalid_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    registry = WorkspaceOperationRegistry(root)

    def add(
        operation_id: str,
        status: str,
        *,
        old: bool,
        stale: bool = False,
        unsafe_progress: bool = False,
    ) -> Path:
        operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(
            root, operation_id
        )
        if unsafe_progress:
            progress_path = tmp_path / f"outside-{operation_id}.json"
        registry.claim_or_join(
            operation_id=operation_id,
            request_key=operation_id,
            request={"operation": "workspace_bundle", "job": "demo"},
            progress_path=progress_path,
            cancel_path=cancel_path,
            stale_before=0,
        )
        if not unsafe_progress:
            operation_dir.mkdir()
        output_dir = root / f"output-{operation_id[0]}"
        output_dir.mkdir()
        ProgressFile(
            progress_path,
            {
                "operation_id": operation_id,
                "operation": "workspace_bundle",
                "status": "running" if status == "running" else status,
                "phase": "running" if status == "running" else status,
                "output_dir": str(output_dir),
            },
        )
        registry.claim_worker(operation_id, f"owner-{operation_id}", 123)
        registry.set_capture(
            operation_id,
            f"owner-{operation_id}",
            output_dir=output_dir,
            anchor_build_number=7,
        )
        if status != "running":
            registry.finish(operation_id, f"owner-{operation_id}", status)
        with registry._connection() as connection:
            values = [1 if old else time.time(), operation_id]
            connection.execute(
                "UPDATE workspace_operations SET last_accessed_at = ? WHERE operation_id = ?",
                values,
            )
            if stale:
                connection.execute(
                    "UPDATE workspace_operations SET heartbeat_at = 1 WHERE operation_id = ?",
                    (operation_id,),
                )
        return output_dir

    old_output = add("6" * 32, "succeeded", old=True)
    recent_output = add("7" * 32, "failed", old=False)
    active_output = add("8" * 32, "running", old=True)
    stale_output = add("9" * 32, "running", old=True, stale=True)
    invalid_output = add("a" * 32, "succeeded", old=True, unsafe_progress=True)

    result = workspace_bundle.cleanup_workspace_bundle_operations(older_than_days=30)

    assert result["deleted_operation_ids"] == ["6" * 32]
    assert result["skipped_running"] == 1
    assert result["skipped_recent"] == 2
    assert result["skipped_invalid"] == 1
    assert not old_output.exists()
    assert recent_output.exists()
    assert active_output.exists()
    assert not stale_output.exists()
    assert invalid_output.exists()


def test_registry_cleanup_handles_concurrent_stale_row_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    progress_path = root / ".operations" / ("b" * 32) / "progress.json"
    progress_path.parent.mkdir(parents=True)
    ProgressFile(progress_path, {"status": "running", "operation": "workspace_bundle"})
    row = {
        "operation_id": "b" * 32,
        "status": "running",
        "heartbeat_at": 1,
        "last_accessed_at": 1,
        "progress_path": str(progress_path),
        "output_dir": None,
    }

    class ConcurrentDeleteRegistry:
        def __init__(self, path: Path) -> None:
            self.path = path

        def cleanup_candidates(self, limit: int) -> list[dict[str, Any]]:
            return [row]

        def mark_stale(self, operation_id: str, stale_before: float) -> None:
            return None

    monkeypatch.setattr(workspace_bundle, "WorkspaceOperationRegistry", ConcurrentDeleteRegistry)
    result = workspace_bundle.cleanup_workspace_bundle_operations(30)
    assert result["invalid_operation_ids"] == ["b" * 32]


def test_cached_payload_validation_supports_paths_and_rejects_unsafe_or_missing_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    root.mkdir()
    progress_path = root / ".operations" / ("c" * 32) / "progress.json"
    target = root / "output" / "workspace" / "report.txt"
    target.parent.mkdir(parents=True)
    target.write_text("report", encoding="utf-8")
    console = root / "output" / "console.log"
    metadata = root / "output" / "metadata.json"
    console.write_text("log", encoding="utf-8")
    metadata.write_text("{}", encoding="utf-8")
    row = {"operation_id": "c" * 32, "progress_path": str(progress_path)}

    ProgressFile(progress_path, {"status": "running"})
    assert workspace_bundle._cached_payload_exists(root, row) is False

    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "file",
            "target_path": str(target),
            "console_log_path": str(console),
            "metadata_path": str(metadata),
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is True

    target.unlink()
    target.mkdir()
    assert workspace_bundle._cached_payload_exists(root, row) is False
    target.rmdir()
    target.write_text("report", encoding="utf-8")

    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "folder",
            "target_path": str(target),
            "console_log_path": str(console),
            "metadata_path": str(metadata),
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is False
    target.unlink()
    target.mkdir()
    assert workspace_bundle._cached_payload_exists(root, row) is True
    target.rmdir()
    target.write_text("report", encoding="utf-8")

    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "unsupported",
            "target_path": str(target),
            "console_log_path": str(console),
            "metadata_path": str(metadata),
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is False

    target.unlink()
    target.symlink_to(console)
    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "file",
            "target_path": str(target),
            "console_log_path": str(console),
            "metadata_path": str(metadata),
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is False
    target.unlink()
    target.write_text("report", encoding="utf-8")

    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "file",
            "target_path": str(target),
            "console_log_path": str(console),
            "metadata_path": None,
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is False

    ProgressFile(
        progress_path,
        {
            "status": "succeeded",
            "operation": "workspace_path_download",
            "kind": "file",
            "target_path": str(tmp_path / "outside"),
            "console_log_path": str(console),
            "metadata_path": str(metadata),
        },
    )
    assert workspace_bundle._cached_payload_exists(root, row) is False


def test_detached_worker_spawn_uses_current_python_and_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(workspace_bundle.subprocess, "Popen", popen)
    worker = workspace_bundle._spawn_workspace_worker("d" * 32)

    assert worker.pid == 123
    command, kwargs = calls[0]
    assert command[0] == workspace_bundle.sys.executable
    assert command[-2:] == ["jenkins_mcp_server.workspace_worker", "d" * 32]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_start_recovers_stale_operation_and_joins_claim_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stale_root = tmp_path / "stale"
    _set_workspace_env(monkeypatch, stale_root)
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)
    monkeypatch.setattr(
        workspace_bundle,
        "_spawn_workspace_worker",
        lambda operation_id: SimpleNamespace(pid=123),
    )
    config = JenkinsConfig.from_env()
    request = {
        "operation": "workspace_bundle",
        "job": "demo",
        "build": "lastBuild",
        "workspace_path": None,
        "kind": None,
    }
    registry = WorkspaceOperationRegistry(stale_root)
    stale_id = "e" * 32
    operation_dir, progress_path, cancel_path = workspace_bundle._operation_paths(
        stale_root, stale_id
    )
    registry.claim_or_join(
        operation_id=stale_id,
        request_key=workspace_bundle._request_key(config, request),
        request=request,
        progress_path=progress_path,
        cancel_path=cancel_path,
        stale_before=0,
    )
    operation_dir.mkdir()
    ProgressFile(progress_path, {"operation_id": stale_id, "status": "running"})
    with registry._connection() as connection:
        connection.execute(
            "UPDATE workspace_operations SET heartbeat_at = 1 WHERE operation_id = ?",
            (stale_id,),
        )

    started = workspace_bundle.start_workspace_bundle_download("demo")
    assert started["disposition"] == "started"
    assert json.loads(progress_path.read_text())["error"]["code"] == (
        "workspace_operation_interrupted"
    )

    race_root = tmp_path / "race"
    _set_workspace_env(monkeypatch, race_root)
    race_id = "f" * 32

    class RacingClient(_ContextApiClient):
        inserted = False

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
            if path == "queue" and not self.inserted:
                self.__class__.inserted = True
                race_registry = WorkspaceOperationRegistry(race_root)
                race_config = JenkinsConfig.from_env()
                operation_dir, race_progress, race_cancel = workspace_bundle._operation_paths(
                    race_root, race_id
                )
                race_registry.claim_or_join(
                    operation_id=race_id,
                    request_key=workspace_bundle._request_key(race_config, request),
                    request=request,
                    progress_path=race_progress,
                    cancel_path=race_cancel,
                    stale_before=0,
                )
                operation_dir.mkdir()
                ProgressFile(
                    race_progress,
                    {"operation_id": race_id, "job": "demo", "status": "running"},
                )
            return super().get_json(path, params)

    monkeypatch.setattr(workspace_bundle, "JenkinsClient", RacingClient)
    joined = workspace_bundle.start_workspace_bundle_download("demo")
    assert joined["disposition"] == "joined"
    assert joined["operation_id"] == race_id


def test_start_reports_local_operation_directory_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    _set_workspace_env(monkeypatch, root)
    monkeypatch.setattr(workspace_bundle, "JenkinsClient", _ContextApiClient)
    existing = root / ".operations" / "already-exists"
    existing.mkdir(parents=True)
    monkeypatch.setattr(
        workspace_bundle,
        "_operation_paths",
        lambda root, operation_id: (existing, existing / "progress.json", existing / "cancel"),
    )

    with pytest.raises(WorkspaceBundleError) as error:
        workspace_bundle.start_workspace_bundle_download("demo")
    assert error.value.code == "workspace_operation_setup_failed"


def test_output_reservation_discard_and_capture_ownership_failures(tmp_path: Path) -> None:
    root = tmp_path / "bundles"
    root.mkdir()
    job_dir = root / "demo"
    (job_dir / "7").mkdir(parents=True)
    reserved = workspace_bundle._reserve_output_dir(job_dir, "7", "1" * 32)
    assert reserved == job_dir / "7-11111111"
    assert reserved.stat().st_mode & 0o777 == 0o700

    for index in range(2, 100):
        (job_dir / f"7-11111111-{index}").mkdir()
    with pytest.raises(WorkspaceBundleError) as error:
        workspace_bundle._reserve_output_dir(job_dir, "7", "1" * 32)
    assert error.value.code == "workspace_output_reservation_failed"

    target = root / "target"
    target.mkdir()
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    workspace_bundle._discard_output_dir(root, link)
    assert not link.exists()
    assert target.exists()

    class LostRegistry:
        def set_capture(self, *args: Any, **kwargs: Any) -> bool:
            return False

    progress = ProgressFile(root / "progress.json", {"status": "running"})
    with pytest.raises(OperationCancelledError):
        workspace_bundle._configure_capture_paths(
            root=root,
            registry=LostRegistry(),  # type: ignore[arg-type]
            operation_id="2" * 32,
            owner_id="lost",
            request={"operation": "workspace_bundle", "job": "demo", "kind": None},
            anchor_build=8,
            progress=progress,
        )
    assert not (root / "demo" / "8").exists()


def test_capture_path_setup_rejects_symlinked_job_parent_before_creating_children(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundles"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "folder").symlink_to(outside, target_is_directory=True)
    progress = ProgressFile(root / "progress.json", {"status": "running"})

    with pytest.raises(WorkspaceBundleError, match="unsafe job output directory"):
        workspace_bundle._configure_capture_paths(
            root=root,
            registry=SimpleNamespace(set_capture=lambda *args, **kwargs: True),
            operation_id="2" * 32,
            owner_id="owner",
            request={"operation": "workspace_bundle", "job": "folder/demo", "kind": None},
            anchor_build=8,
            progress=progress,
        )

    assert not (outside / "demo").exists()


def test_registered_worker_rejects_invalid_progress_and_request_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invalid-progress"
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo"},
    )
    Path(row["progress_path"]).write_text("[]", encoding="utf-8")
    with pytest.raises(WorkspaceBundleError, match="not an object"):
        workspace_bundle.run_registered_workspace_operation(_config(root), registry, row, owner)

    root = tmp_path / "invalid-job"
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": None},
        operation_id="3" * 32,
    )
    with pytest.raises(WorkspaceBundleError, match="omitted job"):
        workspace_bundle.run_registered_workspace_operation(_config(root), registry, row, owner)

    root = tmp_path / "invalid-build"
    registry, row, owner = _register_operation(
        root,
        {"operation": "workspace_bundle", "job": "demo", "desired_build_number": "bad"},
        operation_id="4" * 32,
    )
    with pytest.raises(WorkspaceBundleError, match="invalid desired build"):
        workspace_bundle.run_registered_workspace_operation(_config(root), registry, row, owner)


def test_finish_ignores_lost_ownership_and_legacy_runners_record_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry, row, _ = _register_operation(
        tmp_path / "ownership",
        {"operation": "workspace_bundle", "job": "demo"},
    )
    progress = ProgressFile(Path(row["progress_path"]), {"status": "running"})
    workspace_bundle._finish_registered_operation(
        registry=registry,
        operation_id=row["operation_id"],
        owner_id="wrong-owner",
        progress=progress,
        status="failed",
        phase="failed",
    )
    assert progress.data["status"] == "running"

    monkeypatch.setattr(
        workspace_bundle,
        "_capture_workspace_bundle",
        lambda **kwargs: {"completed_at": "bundle-complete"},
    )
    bundle_progress = ProgressFile(
        tmp_path / "bundle-progress.json",
        {"operation_id": "legacy-bundle", "status": "running"},
    )
    workspace_bundle._run_workspace_bundle(
        config=_config(tmp_path),
        job="demo",
        build_number=7,
        name_prefix="demo7",
        archive_path=tmp_path / "demo7.zip",
        workspace_dir=tmp_path / "workspace",
        console_log_path=tmp_path / "console.log",
        metadata_path=tmp_path / "metadata.json",
        progress=bundle_progress,
        cancel_path=tmp_path / "cancel",
    )
    assert bundle_progress.data["completed_at"] == "bundle-complete"

    monkeypatch.setattr(
        workspace_bundle,
        "_capture_workspace_path_download",
        lambda **kwargs: {"completed_at": "path-complete"},
    )
    path_progress = ProgressFile(
        tmp_path / "path-progress.json",
        {"operation_id": "legacy-path", "status": "running"},
    )
    workspace_bundle._run_workspace_path_download(
        config=_config(tmp_path),
        job="demo",
        build_number=7,
        workspace_path="report.txt",
        kind="file",
        archive_path=tmp_path / "unused.zip",
        target_path=tmp_path / "workspace" / "report.txt",
        console_log_path=tmp_path / "path-console.log",
        metadata_path=tmp_path / "path-metadata.json",
        progress=path_progress,
        cancel_path=tmp_path / "path-cancel",
    )
    assert path_progress.data["completed_at"] == "path-complete"


def test_path_confinement_and_registry_output_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(WorkspaceBundleError, match="unsafe output"):
        workspace_bundle._path_under_root(root, root, "output")

    target = root / "target"
    target.mkdir()
    link = root / "linked-output"
    link.symlink_to(target, target_is_directory=True)
    row = {"output_dir": str(link)}
    workspace_bundle._remove_registry_output(root, row, {})
    assert not link.exists()
    assert target.exists()
    workspace_bundle._remove_registry_output(root, {}, {})
