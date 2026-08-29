from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import jenkins_mcp_server.tools as tool_module
from jenkins_mcp_server.__main__ import build_server
from jenkins_mcp_server.errors import PathValidationError
from jenkins_mcp_server.tools import (
    OPTIONAL_JOB_CONFIG_TOOLS,
    READ_ONLY_TOOLS,
    WORKSPACE_BUNDLE_TOOLS,
    WRITE_TOOLS,
)


def test_tool_schemas_registered() -> None:
    mcp = build_server()
    registered = set(mcp._tool_manager._tools.keys())  # noqa: SLF001

    assert set(READ_ONLY_TOOLS).issubset(registered)
    assert set(WRITE_TOOLS).issubset(registered)
    assert set(OPTIONAL_JOB_CONFIG_TOOLS).issubset(registered)
    assert set(WORKSPACE_BUNDLE_TOOLS).issubset(registered)


def test_tool_schema_has_parameters() -> None:
    mcp = build_server()
    tool = mcp._tool_manager._tools["jenkins_get_json"]  # noqa: SLF001

    assert "path" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["path"]


def _tool_fn(server, name: str):
    return server._tool_manager._tools[name].fn  # noqa: SLF001


def test_tool_helpers_build_paths_queries_and_client(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tool_module._build_path("folder/demo", 12) == "job/folder/job/demo/12"
    with pytest.raises(PathValidationError, match="build must"):
        tool_module._build_path("demo", "bad/build")

    assert tool_module._query() == {}
    assert tool_module._query("jobs[name]", 0) == {"tree": "jobs[name]", "depth": 0}

    sentinel = object()
    monkeypatch.setattr(
        tool_module,
        "JenkinsClient",
        SimpleNamespace(from_env=lambda: sentinel),
    )
    assert tool_module._client() is sentinel


def test_read_only_tools_execute_expected_client_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeReadClient:
        def __init__(self) -> None:
            self.config = SimpleNamespace(max_log_bytes=64)
            self.calls: list[tuple[str, str, Any]] = []

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append(("json", path, params))
            return {"path": path, "params": params}

        def get_text(self, path: str) -> str:
            self.calls.append(("text", path, None))
            return f"text:{path}"

        def get_text_limited(self, path: str, *, limit: int) -> dict[str, Any]:
            self.calls.append(("limited", path, limit))
            return {"text": "log", "limit": limit}

        def request(
            self,
            method: str,
            path: str,
            *,
            params: dict[str, Any] | None = None,
        ) -> httpx.Response:
            self.calls.append((method, path, params))
            return httpx.Response(
                200,
                json={"mode": "NORMAL"},
                headers={"X-Jenkins": "test-version", "X-Jenkins-Session": "session"},
            )

    client = FakeReadClient()
    monkeypatch.setattr(tool_module, "_client", lambda: client)
    server = build_server()

    assert _tool_fn(server, "jenkins_whoami")()["ok"] is True
    assert _tool_fn(server, "jenkins_version")()["data"] == {
        "version": "test-version",
        "session": "session",
    }
    assert _tool_fn(server, "jenkins_health")()["data"] == {
        "mode": "NORMAL",
        "version": "test-version",
    }
    assert _tool_fn(server, "jenkins_get_json")("queue?depth=1", {"tree": "items[id]"})[
        "ok"
    ]
    assert _tool_fn(server, "jenkins_list_jobs")("jobs[name]", 2)["ok"]
    assert _tool_fn(server, "jenkins_get_job")("folder/demo", "name,url")["ok"]
    assert _tool_fn(server, "jenkins_get_job_config")("folder/demo")["ok"]
    assert _tool_fn(server, "jenkins_list_builds")("folder/demo", "builds[number]")["ok"]
    assert _tool_fn(server, "jenkins_get_build")("folder/demo", "lastBuild", "number")[
        "ok"
    ]
    assert _tool_fn(server, "jenkins_get_build_log")("folder/demo", 12)["data"] == {
        "text": "log",
        "limit": 64,
    }
    assert _tool_fn(server, "jenkins_get_build_artifacts")("folder/demo", 12)["ok"]
    assert _tool_fn(server, "jenkins_get_test_report")("folder/demo", 12)["ok"]
    assert _tool_fn(server, "jenkins_list_queue")("items[id]")["ok"]
    assert _tool_fn(server, "jenkins_get_queue_item")(7)["ok"]
    assert _tool_fn(server, "jenkins_list_views")("views[name]")["ok"]
    assert _tool_fn(server, "jenkins_get_view")("All jobs", "jobs[name]")["ok"]
    assert _tool_fn(server, "jenkins_list_nodes")("computer[displayName]")["ok"]
    assert _tool_fn(server, "jenkins_get_node")("")["ok"]
    assert _tool_fn(server, "jenkins_list_plugins")("plugins[shortName]")["ok"]

    paths = [call[1] for call in client.calls]
    assert "queue/api/json?depth=1" in paths
    assert "job/folder/job/demo/config.xml" in paths
    assert "job/folder/job/demo/12/consoleText" in paths
    assert "computer/%28built-in%29" in paths
    assert "pluginManager" in paths


def test_tool_errors_are_returned_as_structured_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WRITES", "0")
    server = build_server()

    invalid_path = _tool_fn(server, "jenkins_get_json")("https://evil.example/api/json")
    blocked_write = _tool_fn(server, "jenkins_trigger_build")("demo")

    assert invalid_path["error"]["code"] == "invalid_jenkins_path"
    assert blocked_write["error"]["code"] == "permission_gate"


def test_write_tools_execute_gated_client_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    gate_calls: list[str] = []
    post_calls: list[tuple[str, dict[str, Any]]] = []

    class FakeConfig:
        def require_writes(self) -> None:
            gate_calls.append("writes")

        def require_job_config_write(self) -> None:
            gate_calls.append("job_config")

        def require_delete(self) -> None:
            gate_calls.append("delete")

    config = FakeConfig()

    class FakeConfigFactory:
        @staticmethod
        def from_env() -> FakeConfig:
            return config

    class FakeWriteClient:
        def __init__(self, received_config: FakeConfig) -> None:
            assert received_config is config

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
            post_calls.append((path, kwargs))
            return {"path": path, **kwargs}

    monkeypatch.setattr(tool_module, "JenkinsConfig", FakeConfigFactory)
    monkeypatch.setattr(tool_module, "JenkinsClient", FakeWriteClient)
    server = build_server()

    assert _tool_fn(server, "jenkins_trigger_build")("folder/demo", "5sec")["ok"]
    assert _tool_fn(server, "jenkins_trigger_build_with_parameters")(
        "folder/demo",
        {"COUNT": 2, "DEPLOY": True},
        "1sec",
    )["ok"]
    assert _tool_fn(server, "jenkins_stop_build")("folder/demo", 12)["ok"]
    assert _tool_fn(server, "jenkins_cancel_queue_item")(7)["ok"]
    assert _tool_fn(server, "jenkins_disable_job")("folder/demo")["ok"]
    assert _tool_fn(server, "jenkins_enable_job")("folder/demo")["ok"]
    assert _tool_fn(server, "jenkins_create_job")("new-job", "<project />")["ok"]
    assert _tool_fn(server, "jenkins_copy_job")("source", "copy")["ok"]
    assert _tool_fn(server, "jenkins_update_job_config")("folder/demo", "<project />")[
        "ok"
    ]
    assert _tool_fn(server, "jenkins_delete_job")("folder/demo")["ok"]

    assert gate_calls == [
        "writes",
        "writes",
        "writes",
        "writes",
        "writes",
        "writes",
        "job_config",
        "job_config",
        "job_config",
        "delete",
    ]
    assert post_calls[0] == ("job/folder/job/demo/build", {"params": {"delay": "5sec"}})
    assert post_calls[1][1]["data"] == {"COUNT": "2", "DEPLOY": "True", "delay": "1sec"}
    assert post_calls[-1][0] == "job/folder/job/demo/doDelete"


def test_workspace_tools_delegate_to_bundle_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_module,
        "start_workspace_bundle_download",
        lambda job, build: {"operation": "bundle", "job": job, "build": build},
    )
    monkeypatch.setattr(
        tool_module,
        "start_workspace_path_download",
        lambda job, path, kind, build: {
            "operation": "path",
            "job": job,
            "path": path,
            "kind": kind,
            "build": build,
        },
    )
    monkeypatch.setattr(
        tool_module,
        "read_workspace_bundle_status",
        lambda operation_id: {"operation_id": operation_id, "status": "running"},
    )
    monkeypatch.setattr(
        tool_module,
        "cancel_workspace_bundle",
        lambda operation_id: {"operation_id": operation_id, "cancel_requested": True},
    )
    server = build_server()

    assert _tool_fn(server, "jenkins_start_workspace_bundle_download")("demo", 12)["data"][
        "operation"
    ] == "bundle"
    assert _tool_fn(server, "jenkins_start_workspace_path_download")(
        "demo", "reports/results.xml", "file", 12
    )["data"]["operation"] == "path"
    assert _tool_fn(server, "jenkins_get_workspace_bundle_status")("a" * 32)["data"][
        "status"
    ] == "running"
    assert _tool_fn(server, "jenkins_cancel_workspace_bundle_download")("a" * 32)["data"][
        "cancel_requested"
    ] is True
