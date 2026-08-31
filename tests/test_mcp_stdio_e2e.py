from __future__ import annotations

import base64
import gzip
import io
import json
import sys
import threading
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TEST_USER = "mcp-test-user"
TEST_TOKEN = "mcp-test-token"
TEST_CRUMB = "mcp-test-crumb"
JOB_PATH = "/job/folder/job/demo"
CONSOLE_LOG = b"Started by MCP test\nINFO running\nERROR example failure\nFinished\n"
WORKSPACE_FILE = b"workspace readme\n"
ARTIFACT_FILE = b"artifact report\n"

EXPECTED_TOOLS = frozenset(
    {
        "jenkins_whoami",
        "jenkins_version",
        "jenkins_health",
        "jenkins_get_json",
        "jenkins_list_jobs",
        "jenkins_get_job",
        "jenkins_get_job_config",
        "jenkins_list_builds",
        "jenkins_get_build",
        "jenkins_get_build_log",
        "jenkins_get_build_log_chunk",
        "jenkins_search_build_log",
        "jenkins_get_build_artifacts",
        "jenkins_start_artifact_download",
        "jenkins_get_artifact_download_status",
        "jenkins_cancel_artifact_download",
        "jenkins_get_test_report",
        "jenkins_list_queue",
        "jenkins_get_queue_item",
        "jenkins_list_views",
        "jenkins_get_view",
        "jenkins_list_nodes",
        "jenkins_get_node",
        "jenkins_list_plugins",
        "jenkins_trigger_build",
        "jenkins_trigger_build_with_parameters",
        "jenkins_stop_build",
        "jenkins_cancel_queue_item",
        "jenkins_disable_job",
        "jenkins_enable_job",
        "jenkins_create_job",
        "jenkins_copy_job",
        "jenkins_update_job_config",
        "jenkins_delete_job",
        "jenkins_start_workspace_bundle_download",
        "jenkins_start_workspace_path_download",
        "jenkins_get_workspace_bundle_status",
        "jenkins_cancel_workspace_bundle_download",
        "jenkins_cleanup_workspace_bundle_operations",
    }
)

GATED_TOOL_CALLS: dict[str, dict[str, Any]] = {
    "jenkins_trigger_build": {"job": ["folder", "demo"]},
    "jenkins_trigger_build_with_parameters": {
        "job": ["folder", "demo"],
        "parameters": {"BRANCH": "main"},
    },
    "jenkins_stop_build": {"job": ["folder", "demo"], "build": 123},
    "jenkins_cancel_queue_item": {"item_id": 7},
    "jenkins_disable_job": {"job": ["folder", "demo"]},
    "jenkins_enable_job": {"job": ["folder", "demo"]},
    "jenkins_create_job": {"name": "created", "config_xml": "<project/>"},
    "jenkins_copy_job": {"from_job": "demo", "new_name": "copied"},
    "jenkins_update_job_config": {
        "job": ["folder", "demo"],
        "config_xml": "<project/>",
    },
    "jenkins_delete_job": {"job": ["folder", "demo"]},
    "jenkins_start_workspace_bundle_download": {"job": ["folder", "demo"]},
    "jenkins_start_workspace_path_download": {
        "job": ["folder", "demo"],
        "workspace_path": "README.txt",
        "kind": "file",
    },
    "jenkins_get_workspace_bundle_status": {"operation_id": "0" * 32},
    "jenkins_cancel_workspace_bundle_download": {"operation_id": "0" * 32},
    "jenkins_cleanup_workspace_bundle_operations": {
        "older_than_days": 30,
        "max_operations": 100,
    },
    "jenkins_start_artifact_download": {
        "job": ["folder", "demo"],
        "artifact_path": "reports/report.txt",
    },
    "jenkins_get_artifact_download_status": {"operation_id": "0" * 32},
    "jenkins_cancel_artifact_download": {"operation_id": "0" * 32},
}


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeJenkinsState:
    base_url: str = ""
    requests: list[RecordedRequest] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, request: RecordedRequest) -> None:
        with self.lock:
            self.requests.append(request)

    def fail(self, message: str) -> None:
        with self.lock:
            self.failures.append(message)

    def snapshot(self) -> list[RecordedRequest]:
        with self.lock:
            return list(self.requests)


class FakeJenkinsServer(ThreadingHTTPServer):
    daemon_threads = True
    state: FakeJenkinsState


class FakeJenkinsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> FakeJenkinsState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def log_message(self, _format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        request = self._request("GET")
        if not self._authenticated(request):
            return
        self._handle_get(request)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        request = self._request("POST")
        if not self._authenticated(request):
            return
        if request.headers.get("jenkins-crumb") != TEST_CRUMB:
            self.state.fail(f"POST {request.path} omitted the Jenkins crumb")
            self._send_text("No valid crumb", status=403)
            return
        self._handle_post(request)

    def _request(self, method: str) -> RecordedRequest:
        split = urlsplit(self.path)
        raw_length = self.headers.get("Content-Length", "0")
        body = self.rfile.read(int(raw_length)) if raw_length.isdigit() else b""
        request = RecordedRequest(
            method=method,
            path=split.path,
            query=parse_qs(split.query, keep_blank_values=True),
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )
        self.state.record(request)
        return request

    def _authenticated(self, request: RecordedRequest) -> bool:
        expected = "Basic " + base64.b64encode(f"{TEST_USER}:{TEST_TOKEN}".encode()).decode()
        if request.headers.get("authorization") == expected:
            return True
        self._send_text(
            "Invalid username or API token",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Jenkins"'},
        )
        return False

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/octet-stream",
        gzip_layers: int = 0,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = body
        for _ in range(gzip_layers):
            encoded = gzip.compress(encoded)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Jenkins", "2.579")
        self.send_header("X-Jenkins-Session", "mcp-e2e-session")
        if gzip_layers:
            self.send_header("Content-Encoding", ", ".join(["gzip"] * gzip_layers))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        gzip_layers: int = 1,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send_bytes(
            json.dumps(payload).encode(),
            status=status,
            content_type="application/json",
            gzip_layers=gzip_layers,
            headers=headers,
        )

    def _send_text(
        self,
        text: str,
        *,
        status: int = 200,
        gzip_layers: int = 0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send_bytes(
            text.encode(),
            status=status,
            content_type="text/plain; charset=utf-8",
            gzip_layers=gzip_layers,
            headers=headers,
        )

    def _job_payload(self) -> dict[str, Any]:
        job_url = f"{self.state.base_url}job/folder/job/demo/"
        build = {
            "number": 123,
            "url": f"{job_url}123/",
            "queueId": 7,
            "building": False,
            "inProgress": False,
            "result": "SUCCESS",
            "timestamp": 1_800_000_000_000,
            "duration": 12_345,
        }
        return {
            "name": "demo",
            "fullName": "folder/demo",
            "url": job_url,
            "color": "blue",
            "inQueue": False,
            "queueItem": None,
            "lastBuild": build,
            "lastCompletedBuild": {"number": 123},
            "builds": [build],
        }

    def _build_payload(self) -> dict[str, Any]:
        return {
            "number": 123,
            "url": f"{self.state.base_url}job/folder/job/demo/123/",
            "fullDisplayName": "folder/demo #123",
            "result": "SUCCESS",
            "building": False,
            "artifacts": [
                {
                    "displayPath": "report.txt",
                    "fileName": "report.txt",
                    "relativePath": "reports/report.txt",
                }
            ],
        }

    def _require_identity(self, request: RecordedRequest) -> bool:
        if request.headers.get("accept-encoding") == "identity":
            return True
        self.state.fail(f"GET {request.path} did not request identity encoding")
        self._send_text("Downloads require identity encoding", status=400)
        return False

    def _handle_get(self, request: RecordedRequest) -> None:
        path = request.path
        if path == "/whoAmI/api/json":
            self._send_json(
                {"authenticated": True, "anonymous": False, "name": TEST_USER},
                gzip_layers=2,
            )
        elif path == "/crumbIssuer/api/json":
            self._send_json(
                {"crumbRequestField": "Jenkins-Crumb", "crumb": TEST_CRUMB}
            )
        elif path == "/api/json":
            self._send_json(
                {
                    "mode": "NORMAL",
                    "nodeDescription": "the Jenkins controller",
                    "nodeName": "built-in",
                    "numExecutors": 2,
                    "quietingDown": False,
                    "useCrumbs": True,
                    "jobs": [self._job_payload()],
                    "views": [
                        {
                            "name": "All jobs",
                            "url": f"{self.state.base_url}view/All%20jobs/",
                            "_class": "hudson.model.AllView",
                        }
                    ],
                }
            )
        elif path == f"{JOB_PATH}/api/json":
            self._send_json(self._job_payload())
        elif path == f"{JOB_PATH}/config.xml":
            self._send_text("<project><disabled>false</disabled></project>", gzip_layers=1)
        elif path in {f"{JOB_PATH}/123/api/json", f"{JOB_PATH}/lastBuild/api/json"}:
            self._send_json(self._build_payload())
        elif path == f"{JOB_PATH}/123/consoleText":
            if request.headers.get("accept-encoding") == "identity":
                self._send_bytes(CONSOLE_LOG, content_type="text/plain; charset=utf-8")
            else:
                self._send_bytes(
                    CONSOLE_LOG,
                    content_type="text/plain; charset=utf-8",
                    gzip_layers=1,
                )
        elif path == f"{JOB_PATH}/123/logText/progressiveText":
            start = int(request.query.get("start", ["0"])[0])
            chunk = CONSOLE_LOG[start:]
            self._send_bytes(
                chunk,
                content_type="text/plain; charset=utf-8",
                gzip_layers=1,
                headers={
                    "X-Text-Size": str(start + len(chunk)),
                    "X-More-Data": "false",
                },
            )
        elif path == f"{JOB_PATH}/123/testReport/api/json":
            self._send_json({"failCount": 0, "skipCount": 0, "totalCount": 12})
        elif path == "/queue/api/json":
            self._send_json({"items": []})
        elif path == "/queue/item/7/api/json":
            self._send_json({"id": 7, "why": None, "cancelled": False})
        elif path == "/view/All%20jobs/api/json":
            self._send_json({"name": "All jobs", "jobs": [self._job_payload()]})
        elif path == "/computer/api/json":
            self._send_json(
                {
                    "computer": [
                        {
                            "displayName": "Built-In Node",
                            "offline": False,
                            "temporarilyOffline": False,
                            "numExecutors": 2,
                            "assignedLabels": [{"name": "built-in"}],
                        }
                    ]
                }
            )
        elif path == "/computer/%28built-in%29/api/json":
            self._send_json({"displayName": "Built-In Node", "offline": False})
        elif path == "/pluginManager/api/json":
            self._send_json(
                {
                    "plugins": [
                        {
                            "shortName": "junit",
                            "longName": "JUnit",
                            "version": "test-version",
                            "active": True,
                            "enabled": True,
                        }
                    ]
                }
            )
        elif path == "/unauthorized/api/json":
            self._send_text("Unauthorized", status=401, gzip_layers=1)
        elif path == "/forbidden/api/json":
            self._send_text("Missing Overall/Read", status=403, gzip_layers=1)
        elif path == "/missing/api/json":
            self._send_text("Not Found", status=404, gzip_layers=1)
        elif path == f"{JOB_PATH}/123/artifact/reports/report.txt":
            if self._require_identity(request):
                self._send_bytes(ARTIFACT_FILE)
        elif path == f"{JOB_PATH}/ws/README.txt":
            if self._require_identity(request):
                self._send_bytes(WORKSPACE_FILE)
        elif path.startswith(f"{JOB_PATH}/ws/") and path.endswith(".zip"):
            if not self._require_identity(request):
                return
            files = (
                {"result.txt": b"folder result\n"}
                if path.startswith(f"{JOB_PATH}/ws/reports/")
                else {"README.txt": WORKSPACE_FILE, "reports/result.txt": b"workspace result\n"}
            )
            self._send_bytes(_zip_bytes(files), content_type="application/zip")
        else:
            self._send_text("Not Found", status=404)

    def _handle_post(self, request: RecordedRequest) -> None:
        known_paths = {
            f"{JOB_PATH}/build",
            f"{JOB_PATH}/buildWithParameters",
            f"{JOB_PATH}/123/stop",
            "/queue/cancelItem",
            f"{JOB_PATH}/disable",
            f"{JOB_PATH}/enable",
            "/createItem",
            f"{JOB_PATH}/config.xml",
            f"{JOB_PATH}/doDelete",
        }
        if request.path not in known_paths:
            self._send_text("Not Found", status=404)
            return
        status = 201 if request.path in {f"{JOB_PATH}/build", "/createItem"} else 200
        location = (
            f"{self.state.base_url}queue/item/7/"
            if request.path.endswith("/build")
            else None
        )
        self._send_bytes(
            b"",
            status=status,
            headers={"Location": location} if location else None,
        )


@contextmanager
def fake_jenkins() -> Iterator[FakeJenkinsState]:
    server = FakeJenkinsServer(("127.0.0.1", 0), FakeJenkinsHandler)
    state = FakeJenkinsState()
    state.base_url = f"http://127.0.0.1:{server.server_port}/"
    server.state = state
    thread = threading.Thread(target=server.serve_forever, name="fake-jenkins", daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _server_env(state: FakeJenkinsState, tmp_path: Path, *, enabled: bool) -> dict[str, str]:
    return {
        "JENKINS_URL": state.base_url,
        "JENKINS_USER": TEST_USER,
        "JENKINS_API_TOKEN": TEST_TOKEN if enabled else "incorrect-test-token",
        "JENKINS_VERIFY_SSL": "true",
        "JENKINS_TIMEOUT_SECONDS": "5",
        "JENKINS_MCP_MAX_RESPONSE_BYTES": "1000000",
        "JENKINS_MCP_MAX_LOG_BYTES": "1000000",
        "JENKINS_MCP_MAX_LOG_SCAN_BYTES": "1000000",
        "JENKINS_MCP_ENABLE_WRITES": "1" if enabled else "0",
        "JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE": "1" if enabled else "0",
        "JENKINS_MCP_ENABLE_DELETE": "1" if enabled else "0",
        "JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD": "1" if enabled else "0",
        "JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR": str(tmp_path / "workspace-downloads"),
        "JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES": "1000000",
        "JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES": "1000000",
        "JENKINS_MCP_MAX_WORKSPACE_FILES": "100",
        "JENKINS_MCP_MAX_BUNDLE_LOG_BYTES": "1000000",
        "JENKINS_MCP_WORKSPACE_PROGRESS_INTERVAL_SECONDS": "0.1",
        "JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD": "1" if enabled else "0",
        "JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR": str(tmp_path / "artifact-downloads"),
        "JENKINS_MCP_MAX_ARTIFACT_BYTES": "1000000",
        "JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS": "0.1",
        "PYTHONUNBUFFERED": "1",
    }


@asynccontextmanager
async def mcp_session(environment: dict[str, str]) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jenkins_mcp_server"],
        env=environment,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


async def _call_tool(
    session: ClientSession,
    called: set[str],
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await session.call_tool(name, arguments or {})
    assert not result.is_error, (name, result)
    assert isinstance(result.structured_content, dict), (name, result)
    called.add(name)
    return result.structured_content


async def _call_success(
    session: ClientSession,
    called: set[str],
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    payload = await _call_tool(session, called, name, arguments)
    assert payload.get("ok") is True, (name, payload)
    return payload["data"]


async def _wait_for_operation(
    session: ClientSession,
    called: set[str],
    status_tool: str,
    operation_id: str,
) -> dict[str, Any]:
    with anyio.fail_after(30):
        while True:
            status = await _call_success(
                session,
                called,
                status_tool,
                {"operation_id": operation_id},
            )
            if status["status"] in {"succeeded", "failed", "cancelled"}:
                return status
            await anyio.sleep(0.05)


async def _exercise_all_tools(state: FakeJenkinsState, tmp_path: Path) -> None:
    called: set[str] = set()
    returned: list[Any] = []
    async with mcp_session(_server_env(state, tmp_path, enabled=True)) as session:
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
        assert all(tool.input_schema.get("type") == "object" for tool in listed.tools)

        safety = await session.read_resource("jenkins-mcp://safety")
        assert len(safety.contents) == 1
        assert "read-only by default" in safety.contents[0].text

        whoami = await _call_success(session, called, "jenkins_whoami")
        assert whoami == {"authenticated": True, "anonymous": False, "name": TEST_USER}
        returned.append(whoami)

        version = await _call_success(session, called, "jenkins_version")
        assert version == {"version": "2.579", "session": "mcp-e2e-session"}
        returned.append(version)

        health = await _call_success(session, called, "jenkins_health")
        assert health["version"] == "2.579"
        returned.append(health)

        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_get_json",
                {"path": "queue", "query": {"depth": 1}},
            )
        )
        returned.append(await _call_success(session, called, "jenkins_list_jobs", {"depth": 1}))
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_get_job",
                {"job": ["folder", "demo"], "tree": "name,fullName,url"},
            )
        )
        config_xml = await _call_success(
            session, called, "jenkins_get_job_config", {"job": ["folder", "demo"]}
        )
        assert "<project>" in config_xml
        returned.append(config_xml)
        returned.append(
            await _call_success(
                session, called, "jenkins_list_builds", {"job": ["folder", "demo"]}
            )
        )
        build = await _call_success(
            session,
            called,
            "jenkins_get_build",
            {"job": ["folder", "demo"], "build": 123},
        )
        assert build["number"] == 123
        returned.append(build)

        log = await _call_success(
            session,
            called,
            "jenkins_get_build_log",
            {"job": ["folder", "demo"], "build": 123},
        )
        assert log["text"].encode() == CONSOLE_LOG
        returned.append(log)

        chunk = await _call_success(
            session,
            called,
            "jenkins_get_build_log_chunk",
            {"job": ["folder", "demo"], "build": 123, "start": 0},
        )
        assert chunk["next_start"] == len(CONSOLE_LOG)
        returned.append(chunk)

        search = await _call_success(
            session,
            called,
            "jenkins_search_build_log",
            {"job": ["folder", "demo"], "build": 123, "pattern": "ERROR"},
        )
        assert search["match_count"] == 1
        returned.append(search)

        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_get_build_artifacts",
                {"job": ["folder", "demo"], "build": 123},
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_get_test_report",
                {"job": ["folder", "demo"], "build": 123},
            )
        )
        returned.append(await _call_success(session, called, "jenkins_list_queue"))
        returned.append(
            await _call_success(session, called, "jenkins_get_queue_item", {"item_id": 7})
        )
        returned.append(await _call_success(session, called, "jenkins_list_views"))
        returned.append(
            await _call_success(session, called, "jenkins_get_view", {"view": "All jobs"})
        )
        returned.append(await _call_success(session, called, "jenkins_list_nodes"))
        returned.append(
            await _call_success(session, called, "jenkins_get_node", {"node": "(built-in)"})
        )
        returned.append(await _call_success(session, called, "jenkins_list_plugins"))

        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_trigger_build",
                {"job": ["folder", "demo"], "delay": "0sec"},
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_trigger_build_with_parameters",
                {
                    "job": ["folder", "demo"],
                    "parameters": {"BRANCH": "main", "RETRIES": 2, "DRY_RUN": True},
                    "delay": "0sec",
                },
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_stop_build",
                {"job": ["folder", "demo"], "build": 123},
            )
        )
        returned.append(
            await _call_success(session, called, "jenkins_cancel_queue_item", {"item_id": 7})
        )
        returned.append(
            await _call_success(
                session, called, "jenkins_disable_job", {"job": ["folder", "demo"]}
            )
        )
        returned.append(
            await _call_success(
                session, called, "jenkins_enable_job", {"job": ["folder", "demo"]}
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_create_job",
                {"name": "created", "config_xml": "<project/>"},
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_copy_job",
                {"from_job": "demo", "new_name": "copied"},
            )
        )
        returned.append(
            await _call_success(
                session,
                called,
                "jenkins_update_job_config",
                {"job": ["folder", "demo"], "config_xml": "<project/>"},
            )
        )
        returned.append(
            await _call_success(
                session, called, "jenkins_delete_job", {"job": ["folder", "demo"]}
            )
        )

        bundle_start = await _call_success(
            session,
            called,
            "jenkins_start_workspace_bundle_download",
            {"job": ["folder", "demo"], "force_refresh": True},
        )
        bundle = await _wait_for_operation(
            session,
            called,
            "jenkins_get_workspace_bundle_status",
            bundle_start["operation_id"],
        )
        assert bundle["status"] == "succeeded", bundle
        bundle_dir = Path(bundle["output_dir"])
        assert bundle_dir.parent == tmp_path / "workspace-downloads" / "folder" / "demo"
        assert bundle_dir.name.startswith("123")
        assert (bundle_dir / "workspace" / "README.txt").read_bytes() == WORKSPACE_FILE
        assert Path(bundle["console_log_path"]).read_bytes() == CONSOLE_LOG
        assert bundle["archive_deleted"] is True
        assert not list(bundle_dir.glob("*.zip"))
        returned.append(bundle)

        cancel_bundle = await _call_success(
            session,
            called,
            "jenkins_cancel_workspace_bundle_download",
            {"operation_id": bundle_start["operation_id"]},
        )
        assert cancel_bundle["cancel_requested"] is False
        returned.append(cancel_bundle)

        for workspace_path, kind, expected in (
            ("README.txt", "file", WORKSPACE_FILE),
            ("reports", "folder", b"folder result\n"),
        ):
            path_start = await _call_success(
                session,
                called,
                "jenkins_start_workspace_path_download",
                {
                    "job": ["folder", "demo"],
                    "workspace_path": workspace_path,
                    "kind": kind,
                    "force_refresh": True,
                },
            )
            path_status = await _wait_for_operation(
                session,
                called,
                "jenkins_get_workspace_bundle_status",
                path_start["operation_id"],
            )
            assert path_status["status"] == "succeeded", path_status
            target = Path(path_status["target_path"])
            downloaded = (
                target.read_bytes()
                if kind == "file"
                else (target / "result.txt").read_bytes()
            )
            assert downloaded == expected
            assert Path(path_status["console_log_path"]).read_bytes() == CONSOLE_LOG
            if kind == "folder":
                assert path_status["archive_deleted"] is True
                assert not Path(path_status["archive_path"]).exists()
            returned.append(path_status)

        artifact_start = await _call_success(
            session,
            called,
            "jenkins_start_artifact_download",
            {
                "job": ["folder", "demo"],
                "build": "lastBuild",
                "artifact_path": "reports/report.txt",
            },
        )
        artifact = await _wait_for_operation(
            session,
            called,
            "jenkins_get_artifact_download_status",
            artifact_start["operation_id"],
        )
        assert artifact["status"] == "succeeded", artifact
        assert Path(artifact["destination_path"]).read_bytes() == ARTIFACT_FILE
        returned.append(artifact)

        cancel_artifact = await _call_success(
            session,
            called,
            "jenkins_cancel_artifact_download",
            {"operation_id": artifact_start["operation_id"]},
        )
        assert cancel_artifact["cancel_requested"] is False
        returned.append(cancel_artifact)

        cleanup = await _call_success(
            session,
            called,
            "jenkins_cleanup_workspace_bundle_operations",
            {"older_than_days": 1, "max_operations": 100},
        )
        assert cleanup["deleted_count"] == 0
        returned.append(cleanup)

        for path, status_code, code in (
            ("unauthorized", 401, "jenkins_unauthorized"),
            ("forbidden", 403, "jenkins_forbidden"),
            ("missing", 404, "jenkins_not_found"),
        ):
            error = await _call_tool(session, called, "jenkins_get_json", {"path": path})
            assert error["ok"] is False
            assert error["error"]["status_code"] == status_code
            assert error["error"]["code"] == code
            returned.append(error)

    assert called == EXPECTED_TOOLS
    assert TEST_TOKEN not in json.dumps(returned)


def test_real_stdio_mcp_calls_every_tool_and_resource(tmp_path: Path) -> None:
    with fake_jenkins() as state:
        anyio.run(_exercise_all_tools, state, tmp_path)

    requests = state.snapshot()
    expected_auth = "Basic " + base64.b64encode(f"{TEST_USER}:{TEST_TOKEN}".encode()).decode()
    assert state.failures == []
    assert requests
    assert all(request.headers.get("authorization") == expected_auth for request in requests)

    posts = [request for request in requests if request.method == "POST"]
    assert posts
    assert all(request.headers.get("jenkins-crumb") == TEST_CRUMB for request in posts)
    assert {request.path for request in posts} == {
        f"{JOB_PATH}/build",
        f"{JOB_PATH}/buildWithParameters",
        f"{JOB_PATH}/123/stop",
        "/queue/cancelItem",
        f"{JOB_PATH}/disable",
        f"{JOB_PATH}/enable",
        "/createItem",
        f"{JOB_PATH}/config.xml",
        f"{JOB_PATH}/doDelete",
    }

    trigger = next(request for request in posts if request.path == f"{JOB_PATH}/build")
    assert trigger.query == {"delay": ["0sec"]}

    parameterized = next(
        request for request in posts if request.path == f"{JOB_PATH}/buildWithParameters"
    )
    assert parse_qs(parameterized.body.decode()) == {
        "BRANCH": ["main"],
        "RETRIES": ["2"],
        "DRY_RUN": ["True"],
        "delay": ["0sec"],
    }

    cancel_queue = next(request for request in posts if request.path == "/queue/cancelItem")
    assert cancel_queue.query == {"id": ["7"]}

    create_requests = [request for request in posts if request.path == "/createItem"]
    assert len(create_requests) == 2
    create = next(request for request in create_requests if request.query == {"name": ["created"]})
    assert create.body == b"<project/>"
    assert create.headers["content-type"] == "application/xml"
    copy = next(request for request in create_requests if request.query.get("mode") == ["copy"])
    assert copy.query == {"mode": ["copy"], "from": ["demo"], "name": ["copied"]}

    config_update = next(
        request for request in posts if request.path == f"{JOB_PATH}/config.xml"
    )
    assert config_update.body == b"<project/>"
    assert config_update.headers["content-type"] == "application/xml"

    downloads = [
        request
        for request in requests
        if "/ws/" in request.path or "/artifact/" in request.path
    ]
    workspace_console_downloads = [
        request
        for request in requests
        if request.path.endswith("/consoleText")
        and request.headers.get("accept-encoding") == "identity"
    ]
    assert downloads
    assert workspace_console_downloads
    assert all(request.headers.get("accept-encoding") == "identity" for request in downloads)


async def _exercise_default_gates_and_bad_auth(state: FakeJenkinsState, tmp_path: Path) -> None:
    called: set[str] = set()
    async with mcp_session(_server_env(state, tmp_path, enabled=False)) as session:
        for name, arguments in GATED_TOOL_CALLS.items():
            payload = await _call_tool(session, called, name, arguments)
            assert payload["ok"] is False
            assert payload["error"]["code"] == "permission_gate"

        unauthorized = await _call_tool(session, called, "jenkins_whoami")
        assert unauthorized["ok"] is False
        assert unauthorized["error"]["code"] == "jenkins_unauthorized"
        assert unauthorized["error"]["status_code"] == 401
        assert "incorrect-test-token" not in json.dumps(unauthorized)


def test_real_stdio_mcp_enforces_default_gates_and_reports_bad_auth(tmp_path: Path) -> None:
    with fake_jenkins() as state:
        anyio.run(_exercise_default_gates_and_bad_auth, state, tmp_path)

    requests = state.snapshot()
    assert state.failures == []
    assert len(requests) == 1
    assert requests[0].path == "/whoAmI/api/json"
