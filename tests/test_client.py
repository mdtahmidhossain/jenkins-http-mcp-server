from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from jenkins_mcp_server.client import (
    JenkinsClient,
    _body_snippet,
    append_api_json,
    job_path,
    normalize_relative_path,
    safe_segment,
)
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import (
    JenkinsHTTPError,
    JenkinsProtocolError,
    OperationCancelledError,
    PathValidationError,
    ResponseTooLargeError,
)


def config(**overrides: Any) -> JenkinsConfig:
    values = {
        "url": "https://jenkins.example.com/",
        "user": "alice",
        "api_token": "token",
        "max_response_bytes": 1000,
        "max_log_bytes": 10,
    }
    values.update(overrides)
    return JenkinsConfig(**values)


def test_normalize_relative_path_rejects_external_urls() -> None:
    with pytest.raises(PathValidationError):
        normalize_relative_path("https://evil.example/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("//evil.example/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("/job/demo/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("\\job\\demo")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job\\demo")


def test_normalize_relative_path_rejects_traversal() -> None:
    with pytest.raises(PathValidationError):
        normalize_relative_path("../api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job/%2e%2e/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job/%252e%252e/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job/%2e%2e%2fscript")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job/%2e/api/json")
    with pytest.raises(PathValidationError):
        normalize_relative_path("job/demo#fragment")
    assert normalize_relative_path("job/%zz") == "job/%zz"


def test_append_api_json_preserves_query() -> None:
    assert append_api_json("job/example?tree=name") == "job/example/api/json?tree=name"
    assert append_api_json("job/example/api/json?depth=1") == "job/example/api/json?depth=1"


def test_path_requires_a_path_component() -> None:
    with pytest.raises(PathValidationError, match="path component"):
        normalize_relative_path("?depth=1")


def test_request_merges_path_and_explicit_query_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params == httpx.QueryParams("tree=name&depth=2")
        return httpx.Response(200, json={"ok": True})

    with JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client:
        assert client.get_json("job/demo?tree=name", params={"depth": 2}) == {"ok": True}


def test_nested_job_path_encoding() -> None:
    assert job_path("folder/job name") == "job/folder/job/job%20name"


def test_nested_job_path_rejects_bad_list_segment() -> None:
    with pytest.raises(PathValidationError):
        job_path(["folder", "job/name"])


def test_get_json_adds_api_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/job/demo/api/json"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"name": "demo"}, headers={"X-Jenkins": "test-version"})

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))
    assert client.get_json("job/demo") == {"name": "demo"}
    assert len(seen) == 1


def test_response_size_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1001)

    client = JenkinsClient(config(max_response_bytes=1000), transport=httpx.MockTransport(handler))

    with pytest.raises(ResponseTooLargeError):
        client.get_text("api/json")


def test_log_truncation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdefghijklmnopqrstuvwxyz")

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))
    result = client.get_text_limited("job/demo/1/consoleText", limit=5)

    assert result == {"text": "abcde", "bytes_returned": 5, "truncated": True, "limit": 5}


def test_stream_to_file_reports_progress(tmp_path) -> None:
    progress: list[tuple[int, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdef", headers={"Content-Length": "6"})

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))
    result = client.stream_to_file(
        "job/demo/ws/**/*zip*/demo1.zip",
        tmp_path / "demo1.zip.partial",
        max_bytes=10,
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert result["bytes_downloaded"] == 6
    assert (tmp_path / "demo1.zip.partial").read_bytes() == b"abcdef"
    assert progress[-1] == (6, 6)


def test_stream_to_file_size_limit_from_content_length(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdef", headers={"Content-Length": "6"})

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ResponseTooLargeError):
        client.stream_to_file("job/demo/ws/**/*zip*/demo1.zip", tmp_path / "x", max_bytes=5)

    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_common_http_errors(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="denied")

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))

    with pytest.raises(JenkinsHTTPError) as exc_info:
        client.get_json("api/json")
    assert exc_info.value.status_code == status
    assert exc_info.value.body == "denied"


def test_crumb_header_added_for_post() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"})
        assert request.headers["Jenkins-Crumb"] == "abc"
        return httpx.Response(201, headers={"Location": "https://jenkins.example.com/queue/item/1/"})

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))
    result = client.post("job/demo/build")

    assert result["status_code"] == 201
    assert [request.url.path for request in seen] == ["/crumbIssuer/api/json", "/job/demo/build"]


def test_crumb_retry_after_403() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        first_crumb_request = calls.count("/crumbIssuer/api/json") == 1
        if request.url.path == "/crumbIssuer/api/json" and first_crumb_request:
            return httpx.Response(404)
        if request.url.path == "/job/demo/build" and "Jenkins-Crumb" not in request.headers:
            return httpx.Response(403, text="No valid crumb was included")
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"})
        assert request.headers["Jenkins-Crumb"] == "abc"
        return httpx.Response(201)

    client = JenkinsClient(config(), transport=httpx.MockTransport(handler))
    assert client.post("job/demo/build")["status_code"] == 201
    assert calls == [
        "/crumbIssuer/api/json",
        "/job/demo/build",
        "/crumbIssuer/api/json",
        "/job/demo/build",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"crumb": "abc"},
        {"crumbRequestField": "Jenkins-Crumb"},
        {"crumbRequestField": 1, "crumb": "abc"},
        {"crumbRequestField": "Jenkins-Crumb", "crumb": 1},
    ],
)
def test_malformed_crumb_response_fails_when_jenkins_requires_crumb(payload) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(200, json=payload)
        return httpx.Response(403, text="No valid crumb was included")

    with (
        JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(JenkinsProtocolError, match="crumb"),
    ):
        client.post("job/demo/build")


@pytest.mark.parametrize("path", ["", "///path", "/", "./"])
def test_normalize_relative_path_rejects_empty_forms(path: str) -> None:
    with pytest.raises(PathValidationError):
        normalize_relative_path(path)


def test_path_segment_helpers_reject_empty_values() -> None:
    with pytest.raises(PathValidationError):
        job_path("")
    with pytest.raises(PathValidationError):
        safe_segment("", "value")


def test_client_from_env_context_manager_closes_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.delenv("JENKINS_USER", raising=False)
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)

    with JenkinsClient.from_env() as client:
        assert not client.http.is_closed

    assert client.http.is_closed


def test_empty_body_snippet_and_invalid_internal_method() -> None:
    assert _body_snippet(httpx.Response(204)) is None
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    client = JenkinsClient(config(), transport=transport)
    try:
        with pytest.raises(PathValidationError, match="Only GET and POST"):
            client.request("DELETE", "api/json")
    finally:
        client.close()


def test_post_continues_when_crumb_issuer_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(500, text="crumb issuer unavailable")
        return httpx.Response(201)

    with JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client:
        assert client.post("job/demo/build")["status_code"] == 201


def test_invalid_json_and_successful_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/json"):
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, text="plain text")

    with JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JenkinsHTTPError, match="Response was not JSON"):
            client.get_json("job/demo")
        assert client.get_text("job/demo/config.xml") == "plain text"


def test_streamed_text_reads_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    with (
        JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(JenkinsHTTPError) as exc_info,
    ):
        client.get_text_limited("job/demo/1/consoleText", limit=10)

    assert exc_info.value.body == "missing"


def test_streamed_text_handles_multiple_chunks_at_exact_limit() -> None:
    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"abc"
            yield b"de"
            yield b"f"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkStream())

    with JenkinsClient(config(), transport=httpx.MockTransport(handler)) as client:
        result = client.get_text_limited("job/demo/1/consoleText", limit=5)

    assert result == {"text": "abcde", "bytes_returned": 5, "truncated": True, "limit": 5}


def test_stream_to_file_cancellation_empty_chunk_and_incremental_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class FakeStreamResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        def iter_bytes(self) -> Iterator[bytes]:
            return iter(self.chunks)

    client = JenkinsClient(config())

    @contextmanager
    def cancelled_stream(*args: Any, **kwargs: Any) -> Iterator[FakeStreamResponse]:
        yield FakeStreamResponse([b"x"])

    monkeypatch.setattr(client.http, "stream", cancelled_stream)
    with pytest.raises(OperationCancelledError):
        client.stream_to_file(
            "job/demo/ws/file",
            tmp_path / "cancelled.partial",
            max_bytes=10,
            cancel_check=lambda: True,
        )

    @contextmanager
    def oversized_stream(*args: Any, **kwargs: Any) -> Iterator[FakeStreamResponse]:
        yield FakeStreamResponse([b"", b"abcd"])

    monkeypatch.setattr(client.http, "stream", oversized_stream)
    with pytest.raises(ResponseTooLargeError):
        client.stream_to_file(
            "job/demo/ws/file",
            tmp_path / "oversized.partial",
            max_bytes=3,
        )
    client.close()
