from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class Crumb:
    request_field: str
    crumb: str


class CrumbManager:
    def __init__(self) -> None:
        self._crumb: Crumb | None = None

    def clear(self) -> None:
        self._crumb = None

    def get(self, client: httpx.Client, base_url: str) -> Crumb | None:
        if self._crumb is not None:
            return self._crumb

        response = client.get(base_url + "crumbIssuer/api/json")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        request_field = payload.get("crumbRequestField")
        crumb = payload.get("crumb")
        if not request_field or not crumb:
            return None
        self._crumb = Crumb(request_field=request_field, crumb=crumb)
        return self._crumb
