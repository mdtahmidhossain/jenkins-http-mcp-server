from __future__ import annotations

import httpx
import pytest

from jenkins_mcp_server.client import JenkinsClient
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import (
    JenkinsHTTPError,
    JenkinsProtocolError,
    PathValidationError,
    ResponseTooLargeError,
    ToolInputError,
    WorkspaceListingError,
)
from jenkins_mcp_server.workspace_tree import get_workspace_tree


def _config(max_response_bytes: int = 10_000) -> JenkinsConfig:
    return JenkinsConfig(
        url="https://jenkins.example.com/",
        user="alice",
        api_token="token",
        max_response_bytes=max_response_bytes,
    )


def _client(
    routes: dict[str, tuple[bytes, str] | int],
    *,
    max_response_bytes: int = 10_000,
    seen: list[str] | None = None,
) -> JenkinsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        request_path = request.url.raw_path.decode().split("?", 1)[0]
        if seen is not None:
            seen.append(request_path)
        route = routes.get(request_path)
        if isinstance(route, int):
            return httpx.Response(route, text="denied")
        if route is None:
            return httpx.Response(404, text="missing")
        body, content_type = route
        return httpx.Response(200, content=body, headers={"Content-Type": content_type})

    return JenkinsClient(
        _config(max_response_bytes),
        transport=httpx.MockTransport(handler),
    )


def test_workspace_tree_resolves_subdirectory_and_recurses_nested_job() -> None:
    seen: list[str] = []
    routes = {
        "/job/folder/job/demo/ws/*plain*": (
            b"README.md\nreports/\n",
            "text/plain;charset=UTF-8",
        ),
        "/job/folder/job/demo/ws/reports/*plain*": (
            b"result.xml\nnested/\n",
            "text/plain;charset=UTF-8",
        ),
        "/job/folder/job/demo/ws/reports/nested/*plain*": (
            b"deep.log\n",
            "text/plain;charset=UTF-8",
        ),
    }

    with _client(routes, seen=seen) as client:
        result = get_workspace_tree(
            client,
            ["folder", "demo"],
            workspace_path="reports",
            max_depth=2,
        )

    assert result["job"] == "folder/demo"
    assert result["workspace_path"] == "reports"
    assert result["entries"] == [
        {"path": "reports/nested", "type": "directory", "depth": 1},
        {"path": "reports/result.xml", "type": "file", "depth": 1},
        {"path": "reports/nested/deep.log", "type": "file", "depth": 2},
    ]
    assert result["directory_count"] == 1
    assert result["file_count"] == 2
    assert result["directories_scanned"] == 2
    assert result["listing_requests"] == 3
    assert result["truncated"] is False
    assert result["workspace_freshness"] == "best_effort"
    assert result["data_trust"] == "untrusted"
    assert "not bound to a build number" in result["warning"]
    assert seen == list(routes)


def test_workspace_tree_encodes_directory_segments_and_accepts_empty_directory() -> None:
    seen: list[str] = []
    routes = {
        "/job/demo/ws/*plain*": (b"release notes/\n", "text/plain"),
        "/job/demo/ws/release%20notes/*plain*": (b"", "text/plain; charset=UTF-8"),
    }

    with _client(routes, seen=seen) as client:
        result = get_workspace_tree(client, "demo", "release notes")

    assert result["job"] == "demo"
    assert result["entries"] == []
    assert result["entry_count"] == 0
    assert result["listing_bytes_read"] == len(b"release notes/\n")
    assert result["limits"] == {
        "max_depth": 4,
        "max_entries": 1_000,
        "max_response_bytes": 10_000,
    }
    assert seen[-1] == "/job/demo/ws/release%20notes/*plain*"


@pytest.mark.parametrize(
    ("body", "content_type", "error_type", "message"),
    [
        (b"<html>no workspace</html>", "text/html", WorkspaceListingError, "plain-text"),
        (b"file.txt", "text/plain", JenkinsProtocolError, "newline"),
        (b"\xff\n", "text/plain", JenkinsProtocolError, "UTF-8"),
        (b"../\n", "text/plain", JenkinsProtocolError, "unsafe entry"),
        (b"same\nsame/\n", "text/plain", JenkinsProtocolError, "duplicate entry"),
    ],
)
def test_workspace_tree_rejects_non_directory_and_malformed_listings(
    body: bytes,
    content_type: str,
    error_type: type[Exception],
    message: str,
) -> None:
    routes = {"/job/demo/ws/*plain*": (body, content_type)}

    with _client(routes) as client, pytest.raises(error_type, match=message):
        get_workspace_tree(client, "demo")


@pytest.mark.parametrize("body", [b"\n", b" name\n", b"bad\\name\n", b"bad?name\n", b"bad\tname\n"])
def test_workspace_tree_rejects_ambiguous_remote_names(body: bytes) -> None:
    routes = {"/job/demo/ws/*plain*": (body, "text/plain")}

    with _client(routes) as client, pytest.raises(JenkinsProtocolError, match="unsafe entry"):
        get_workspace_tree(client, "demo")


@pytest.mark.parametrize(
    ("workspace_path", "message"),
    [
        ("missing", "was not present"),
        ("README.md", "is a file"),
    ],
)
def test_workspace_tree_rejects_missing_or_file_target(
    workspace_path: str,
    message: str,
) -> None:
    routes = {"/job/demo/ws/*plain*": (b"README.md\nreports/\n", "text/plain")}

    with _client(routes) as client, pytest.raises(WorkspaceListingError, match=message):
        get_workspace_tree(client, "demo", workspace_path)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_workspace_tree_preserves_jenkins_http_errors(status: int) -> None:
    routes: dict[str, tuple[bytes, str] | int] = {"/job/demo/ws/*plain*": status}

    with _client(routes) as client, pytest.raises(JenkinsHTTPError) as raised:
        get_workspace_tree(client, "demo")

    assert raised.value.status_code == status


@pytest.mark.parametrize("workspace_path", ["../secret", "https://evil.example/x", "*plain*"])
def test_workspace_tree_rejects_unsafe_requested_paths(workspace_path: str) -> None:
    with _client({}) as client, pytest.raises(PathValidationError):
        get_workspace_tree(client, "demo", workspace_path)


def test_workspace_tree_rejects_oversized_requested_path() -> None:
    with _client({}) as client, pytest.raises(ToolInputError, match="4096"):
        get_workspace_tree(client, "demo", "x" * 4_097)


@pytest.mark.parametrize(
    ("max_depth", "max_entries", "message"),
    [(0, 10, "max_depth"), (11, 10, "max_depth"), (1, 0, "max_entries"), (1, 2_001, "max_entries")],
)
def test_workspace_tree_rejects_invalid_limits(
    max_depth: int,
    max_entries: int,
    message: str,
) -> None:
    with _client({}) as client, pytest.raises(ToolInputError, match=message):
        get_workspace_tree(
            client,
            "demo",
            max_depth=max_depth,
            max_entries=max_entries,
        )


def test_workspace_tree_reports_depth_truncation() -> None:
    routes = {"/job/demo/ws/*plain*": (b"dir/\nfile.txt\n", "text/plain")}

    with _client(routes) as client:
        result = get_workspace_tree(client, "demo", max_depth=1)

    assert result["entry_count"] == 2
    assert result["truncated"] is True
    assert result["truncation_reasons"] == ["max_depth"]


@pytest.mark.parametrize(
    "root_listing",
    [b"a.txt\nb.txt\n", b"dir/\n"],
)
def test_workspace_tree_reports_entry_truncation(root_listing: bytes) -> None:
    routes = {"/job/demo/ws/*plain*": (root_listing, "text/plain")}

    with _client(routes) as client:
        result = get_workspace_tree(client, "demo", max_entries=1)

    assert result["entry_count"] == 1
    assert result["truncation_reasons"] == ["max_entries"]


def test_workspace_tree_reports_cumulative_response_limit_after_complete_entries() -> None:
    routes = {
        "/job/demo/ws/*plain*": (b"dir/\n", "text/plain"),
        "/job/demo/ws/dir/*plain*": (b"a.txt\nb.txt\n", "text/plain"),
    }

    with _client(routes, max_response_bytes=10) as client:
        result = get_workspace_tree(client, "demo")

    assert result["entries"] == [{"path": "dir", "type": "directory", "depth": 1}]
    assert result["listing_bytes_read"] == len(b"dir/\n")
    assert result["truncation_reasons"] == ["max_response_bytes"]


def test_workspace_tree_fails_if_first_listing_exceeds_response_limit() -> None:
    routes = {"/job/demo/ws/*plain*": (b"file.txt\n", "text/plain")}

    with _client(routes, max_response_bytes=4) as client, pytest.raises(ResponseTooLargeError):
        get_workspace_tree(client, "demo")


def test_workspace_tree_fails_if_path_resolution_exhausts_response_limit() -> None:
    routes = {
        "/job/demo/ws/*plain*": (b"a/\n", "text/plain"),
        "/job/demo/ws/a/*plain*": (b"b/\n", "text/plain"),
    }

    with _client(routes, max_response_bytes=3) as client, pytest.raises(ResponseTooLargeError):
        get_workspace_tree(client, "demo", "a/b")
