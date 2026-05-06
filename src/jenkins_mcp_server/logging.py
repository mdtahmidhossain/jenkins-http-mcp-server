from __future__ import annotations

import logging

from .auth import redact_headers


def get_logger(name: str = "jenkins_mcp_server") -> logging.Logger:
    return logging.getLogger(name)


def safe_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    return redact_headers(headers)
