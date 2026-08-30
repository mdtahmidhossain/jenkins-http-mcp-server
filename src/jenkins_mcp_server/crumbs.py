from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from .errors import JenkinsProtocolError, ResponseTooLargeError


@dataclass
class Crumb:
    request_field: str
    crumb: str


class CrumbManager:
    def __init__(self, max_bytes: int = 64_000) -> None:
        self._crumb: Crumb | None = None
        self.max_bytes = max_bytes

    def clear(self) -> None:
        self._crumb = None

    def get(self, client: httpx.Client, base_url: str) -> Crumb | None:
        if self._crumb is not None:
            return self._crumb

        with client.stream("GET", base_url + "crumbIssuer/api/json") as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            raw_length = response.headers.get("Content-Length")
            if raw_length and raw_length.isdigit() and int(raw_length) > self.max_bytes:
                raise ResponseTooLargeError(self.max_bytes)
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > self.max_bytes:
                    raise ResponseTooLargeError(self.max_bytes)
                content.extend(chunk)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise JenkinsProtocolError("Jenkins crumb issuer response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise JenkinsProtocolError("Jenkins crumb issuer response must be a JSON object")
        request_field = payload.get("crumbRequestField")
        crumb = payload.get("crumb")
        if not isinstance(request_field, str) or not request_field:
            raise JenkinsProtocolError(
                "Jenkins crumb issuer response omitted a valid crumbRequestField"
            )
        if not isinstance(crumb, str) or not crumb:
            raise JenkinsProtocolError("Jenkins crumb issuer response omitted a valid crumb")
        self._crumb = Crumb(request_field=request_field, crumb=crumb)
        return self._crumb
