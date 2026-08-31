from __future__ import annotations

import errno
import gzip
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import jenkins_mcp_server.client as client_module
from jenkins_mcp_server.client import JenkinsClient, _transport_kind, ensure_free_space
from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import (
    InsufficientDiskSpaceError,
    JenkinsHTTPError,
    JenkinsProtocolError,
    JenkinsTransportError,
    ResponseTooLargeError,
    ToolInputError,
)


def _config(**overrides: Any) -> JenkinsConfig:
    values = {
        "url": "https://jenkins.example.com/",
        "user": "alice",
        "api_token": "token",
        "max_response_bytes": 1_000,
    }
    values.update(overrides)
    return JenkinsConfig(**values)


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class _NoCrumbs:
    def get(self, client: httpx.Client, base_url: str):
        return None

    def clear(self) -> None:
        return None


def test_bounded_response_stops_before_reading_the_full_body() -> None:
    stream = _Chunks([b"ab", b"cd", b"never-read"])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))

    with (
        JenkinsClient(_config(max_response_bytes=3), transport=transport) as client,
        pytest.raises(ResponseTooLargeError),
    ):
        client.get_text("api/json")

    assert stream.read_count == 2


@pytest.mark.parametrize(("content_encoding", "layers"), [("gzip", 1), ("gzip, gzip", 2)])
def test_bounded_whoami_response_is_not_decoded_twice(
    content_encoding: str,
    layers: int,
) -> None:
    payload = b'{"name":"alice","authenticated":true}'
    encoded = payload
    for _ in range(layers):
        encoded = gzip.compress(encoded)

    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/whoAmI/api/json"
        headers = {
            "Content-Type": "application/json",
            "Content-Encoding": content_encoding,
            "X-Jenkins": "2.579",
        }
        if layers == 1:
            headers["Content-Length"] = str(len(encoded))
        else:
            headers["Transfer-Encoding"] = "chunked"
        return httpx.Response(
            200,
            headers=headers,
            stream=_Chunks([encoded]),
        )

    with JenkinsClient(_config(), transport=httpx.MockTransport(handler)) as client:
        response = client.request("GET", "whoAmI/api/json")

    assert response.json() == {"name": "alice", "authenticated": True}
    assert response.headers["X-Jenkins"] == "2.579"
    assert response.headers["Content-Length"] == str(len(payload))
    assert "Content-Encoding" not in response.headers
    assert "Transfer-Encoding" not in response.headers
    assert requests == 1


def test_disk_space_preflight_reports_required_and_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        client_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=4),
    )
    with pytest.raises(InsufficientDiskSpaceError) as exc_info:
        ensure_free_space(tmp_path / "new", 5)

    assert exc_info.value.required_bytes == 5
    assert exc_info.value.available_bytes == 4


def test_transport_error_classification() -> None:
    request = httpx.Request("GET", "https://jenkins.example.com/api/json")
    assert _transport_kind(httpx.ReadTimeout("slow", request=request)) == "timeout"
    assert _transport_kind(httpx.ConnectError("refused", request=request)) == "connection"
    assert _transport_kind(httpx.ReadError("broken", request=request)) == "transport"
    assert _transport_kind(
        httpx.ConnectError("certificate verify failed", request=request)
    ) == "tls"

    try:
        try:
            raise ssl.SSLError("handshake")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("connect", request=request) from cause
    except httpx.ConnectError as error:
        assert _transport_kind(error) == "tls"


def test_get_retries_transient_transport_and_status_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("reset", request=request)
        if attempts == 2:
            return httpx.Response(503, stream=_Chunks([b"x" * 1_500]))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        JenkinsClient,
        "_sleep_before_retry",
        staticmethod(lambda attempt: delays.append(attempt)),
    )
    with JenkinsClient(_config(), transport=httpx.MockTransport(handler)) as client:
        assert client.get_json("api/json") == {"ok": True}

    assert attempts == 3
    assert delays == [1, 2]


def test_error_body_reader_stops_at_its_bound() -> None:
    stream = _Chunks([b"x" * 1_000, b"not-read"])
    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, stream=stream)
            ),
        ) as client,
        pytest.raises(JenkinsHTTPError),
    ):
        client.get_text("api/json")
    assert stream.read_count == 2


def test_retry_backoff_uses_bounded_exponential_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda delay: delays.append(delay))
    JenkinsClient._sleep_before_retry(2)
    assert delays == [0.5]


def test_get_retry_exhaustion_and_tls_failure_are_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(JenkinsClient, "_sleep_before_retry", staticmethod(lambda attempt: None))
    with (
        JenkinsClient(_config(), transport=httpx.MockTransport(timeout_handler)) as client,
        pytest.raises(JenkinsTransportError) as timeout_error,
    ):
        client.get_json("api/json")
    assert timeout_error.value.code == "jenkins_timeout"
    assert timeout_error.value.attempts == 3
    assert attempts == 3

    tls_attempts = 0

    def tls_handler(request: httpx.Request) -> httpx.Response:
        nonlocal tls_attempts
        tls_attempts += 1
        raise httpx.ConnectError("SSL certificate verify failed", request=request)

    with (
        JenkinsClient(_config(), transport=httpx.MockTransport(tls_handler)) as client,
        pytest.raises(JenkinsTransportError) as tls_error,
    ):
        client.get_json("api/json")
    assert tls_error.value.code == "jenkins_tls_error"
    assert tls_attempts == 1


def test_get_redirects_fail_instead_of_following_external_locations() -> None:
    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    302,
                    text="redirect",
                    headers={"Location": "https://storage.example/artifact"},
                )
            ),
        ) as client,
        pytest.raises(JenkinsHTTPError, match="external redirects are disabled") as exc_info,
    ):
        client.get_text("job/demo/1/artifact/report.zip")

    assert exc_info.value.status_code == 302


def test_post_transport_failure_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("refused", request=request)

    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(handler),
            crumb_manager=_NoCrumbs(),  # type: ignore[arg-type]
        ) as client,
        pytest.raises(JenkinsTransportError) as exc_info,
    ):
        client.post("job/demo/build")

    assert exc_info.value.code == "jenkins_connection_error"
    assert attempts == 1


def test_required_crumb_transport_failure_is_structured() -> None:
    request = httpx.Request("GET", "https://jenkins.example.com/crumbIssuer/api/json")

    class BrokenCrumbs:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, client: httpx.Client, base_url: str):
            self.calls += 1
            if self.calls == 1:
                return None
            raise httpx.ReadError("crumb connection lost", request=request)

        def clear(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="No valid crumb was included")

    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(handler),
            crumb_manager=BrokenCrumbs(),  # type: ignore[arg-type]
        ) as client,
        pytest.raises(JenkinsTransportError) as exc_info,
    ):
        client.post("job/demo/build")

    assert exc_info.value.path == "crumbIssuer/api/json"


def test_required_crumb_http_failure_is_structured() -> None:
    crumb_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal crumb_calls
        if request.url.path == "/crumbIssuer/api/json":
            crumb_calls += 1
            return httpx.Response(404 if crumb_calls == 1 else 500)
        return httpx.Response(403, text="No valid crumb was included")

    with (
        JenkinsClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(JenkinsHTTPError) as exc_info,
    ):
        client.post("job/demo/build")

    assert exc_info.value.status_code == 500
    assert exc_info.value.path == "crumbIssuer/api/json"


def test_optional_crumb_transport_failure_does_not_block_post() -> None:
    request = httpx.Request("GET", "https://jenkins.example.com/crumbIssuer/api/json")

    class UnavailableCrumbs:
        def get(self, client: httpx.Client, base_url: str):
            raise httpx.ReadError("crumb unavailable", request=request)

        def clear(self) -> None:
            return None

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(201)),
        crumb_manager=UnavailableCrumbs(),  # type: ignore[arg-type]
    ) as client:
        assert client.post("job/demo/build")["status_code"] == 201


def test_crumb_protocol_failures_are_optional_until_jenkins_requires_one() -> None:
    class InvalidCrumbs:
        def get(self, client: httpx.Client, base_url: str):
            raise JenkinsProtocolError("invalid crumb response")

        def clear(self) -> None:
            return None

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(201)),
        crumb_manager=InvalidCrumbs(),  # type: ignore[arg-type]
    ) as client:
        assert client.post("job/demo/build")["status_code"] == 201

    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, text="crumb required")
            ),
            crumb_manager=InvalidCrumbs(),  # type: ignore[arg-type]
        ) as client,
        pytest.raises(JenkinsProtocolError, match="invalid crumb"),
    ):
        client.post("job/demo/build")


def test_progressive_log_cursor_and_protocol_validation() -> None:
    def success(request: httpx.Request) -> httpx.Response:
        assert request.url.params["start"] == "5"
        return httpx.Response(
            200,
            content=b"next\n",
            headers={"X-Text-Size": "10", "X-More-Data": "true"},
        )

    with JenkinsClient(_config(), transport=httpx.MockTransport(success)) as client:
        result = client.get_progressive_text(
            "job/demo/1/logText/progressiveText",
            start=5,
            limit=20,
        )
    assert result["next_start"] == 10
    assert result["more_data"] is True
    assert result["complete"] is False
    assert result["cursor_reset"] is False

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"X-Text-Size": "3"})
        ),
    ) as client:
        reset = client.get_progressive_text(
            "job/demo/1/logText/progressiveText",
            start=20,
            limit=20,
        )
    assert reset["cursor_reset"] is True
    assert reset["complete"] is True

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    ) as client:
        with pytest.raises(JenkinsProtocolError, match="omitted"):
            client.get_progressive_text("job/demo/1/logText/progressiveText", start=0, limit=20)
        with pytest.raises(ToolInputError, match="start"):
            client.get_progressive_text("job/demo/1/logText/progressiveText", start=-1, limit=20)

    for value, message in [("bad", "invalid"), ("-1", "negative")]:
        with (
            JenkinsClient(
                _config(),
                transport=httpx.MockTransport(
                    lambda request, value=value: httpx.Response(
                        200,
                        headers={"X-Text-Size": value},
                    )
                ),
            ) as client,
            pytest.raises(JenkinsProtocolError, match=message),
        ):
            client.get_progressive_text(
                "job/demo/1/logText/progressiveText",
                start=0,
                limit=20,
            )


def test_streaming_log_search_handles_boundaries_limits_and_snippets() -> None:
    stream = _Chunks([b"line one\nER", b"ROR first\nline three\n"])
    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ) as client:
        result = client.search_text(
            "job/demo/1/consoleText",
            pattern="ERROR",
            max_scan_bytes=100,
            max_matches=5,
        )
    assert result["match_count"] == 1
    assert result["matches"][0]["line_number"] == 2
    assert result["matches"][0]["byte_offset"] == 9
    assert result["scan_truncated"] is False
    assert result["match_limit_reached"] is False

    long_line = b"a" * 300 + b"ERROR" + b"b" * 300 + b"\nERROR\n"
    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_Chunks([long_line]))
        ),
    ) as client:
        limited = client.search_text(
            "job/demo/1/consoleText",
            pattern="ERROR",
            max_scan_bytes=len(long_line),
            max_matches=1,
        )
    assert limited["match_limit_reached"] is True
    assert limited["matches"][0]["snippet_truncated"] is True

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_Chunks([b"abc", b"def"]))
        ),
    ) as client:
        exact_limit = client.search_text(
            "job/demo/1/consoleText",
            pattern="z",
            max_scan_bytes=3,
            max_matches=2,
        )
    assert exact_limit["bytes_scanned"] == 3
    assert exact_limit["scan_truncated"] is True

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_Chunks([b"abcdef"]))
        ),
    ) as client:
        partial_chunk = client.search_text(
            "job/demo/1/consoleText",
            pattern="z",
            max_scan_bytes=3,
            max_matches=2,
        )
    assert partial_chunk["scan_truncated"] is True

    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ERROR")),
    ) as client:
        eof_match = client.search_text(
            "job/demo/1/consoleText",
            pattern="ERROR",
            max_scan_bytes=10,
            max_matches=2,
        )
    assert eof_match["matches"][0]["snippet"] == "ERROR"


def test_structured_local_and_transport_error_payloads(tmp_path: Path) -> None:
    for kind, code in [
        ("timeout", "jenkins_timeout"),
        ("tls", "jenkins_tls_error"),
        ("connection", "jenkins_connection_error"),
        ("other", "jenkins_transport_error"),
    ]:
        error = JenkinsTransportError(kind, "GET", "api/json", 2)
        payload = error.to_dict()
        assert payload["error"]["code"] == code
        assert "GET api/json" in str(error)

    disk_error = InsufficientDiskSpaceError(10, 5, str(tmp_path))
    payload = disk_error.to_dict()
    assert payload["error"]["required_bytes"] == 10
    assert "need 10 bytes" in str(disk_error)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pattern": "", "max_scan_bytes": 1, "max_matches": 1}, "must not be empty"),
        ({"pattern": "x" * 1_025, "max_scan_bytes": 1, "max_matches": 1}, "at most"),
        ({"pattern": "x", "max_scan_bytes": 0, "max_matches": 1}, "max_scan_bytes"),
        ({"pattern": "x", "max_scan_bytes": 1, "max_matches": 0}, "max_matches"),
    ],
)
def test_streaming_log_search_validates_bounds(kwargs: dict[str, Any], message: str) -> None:
    with (
        JenkinsClient(
            _config(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        ) as client,
        pytest.raises(ToolInputError, match=message),
    ):
        client.search_text("job/demo/1/consoleText", **kwargs)


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_stream_to_file_cleans_up_disk_write_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_number: int,
) -> None:
    class BrokenFile:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def write(self, chunk: bytes) -> None:
            raise OSError(error_number, "disk write failed")

    destination = tmp_path / "download.partial"
    monkeypatch.setattr(Path, "open", lambda self, mode: BrokenFile())
    monkeypatch.setattr(
        client_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=100),
    )
    with JenkinsClient(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_Chunks([b"x"]))
        ),
    ) as client:
        expected = InsufficientDiskSpaceError if error_number == errno.ENOSPC else OSError
        with pytest.raises(expected):
            client.stream_to_file("job/demo/1/artifact/x", destination, max_bytes=10)

    assert not destination.exists()


def test_stream_to_file_requests_identity_encoding(tmp_path: Path) -> None:
    payload = b"zip-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(payload))},
            content=payload,
        )

    destination = tmp_path / "workspace.zip.partial"
    with JenkinsClient(_config(), transport=httpx.MockTransport(handler)) as client:
        result = client.stream_to_file("job/demo/ws/*zip*/workspace.zip", destination, max_bytes=20)

    assert destination.read_bytes() == payload
    assert result["bytes_downloaded"] == len(payload)
    assert result["total_bytes"] == len(payload)


def test_stream_to_file_rejects_http_content_encoding(tmp_path: Path) -> None:
    destination = tmp_path / "workspace.zip.partial"
    stream = _Chunks([b"not-consumed"])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        )
    )

    with (
        JenkinsClient(_config(), transport=transport) as client,
        pytest.raises(JenkinsProtocolError, match="requesting identity"),
    ):
        client.stream_to_file("job/demo/ws/*zip*/workspace.zip", destination, max_bytes=20)

    assert not destination.exists()
    assert stream.read_count == 0
