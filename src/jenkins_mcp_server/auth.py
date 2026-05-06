from __future__ import annotations

from collections.abc import Mapping

REDACTED = "<redacted>"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
            redacted[key] = REDACTED
        else:
            redacted[key] = value
    return redacted
