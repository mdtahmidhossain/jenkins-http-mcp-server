from __future__ import annotations

import re
import sys
import uuid

from .config import JenkinsConfig
from .workspace_bundle import run_registered_workspace_operation
from .workspace_registry import WorkspaceOperationRegistry, current_process_id


def run(operation_id: str) -> int:
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        return 2
    config = JenkinsConfig.from_env()
    root = config.require_workspace_download()
    registry = WorkspaceOperationRegistry(root)
    owner_id = uuid.uuid4().hex
    row = registry.claim_worker(operation_id, owner_id, current_process_id())
    if row is None:
        return 3
    try:
        run_registered_workspace_operation(config, registry, row, owner_id)
    except Exception:  # noqa: BLE001 - worker failures are persisted without printing secrets.
        registry.finish(
            operation_id,
            owner_id,
            "failed",
            error_code="workspace_worker_failed",
        )
    final = registry.get(operation_id)
    return 0 if final is not None and final["status"] == "succeeded" else 1


def main() -> None:
    operation_id = sys.argv[1] if len(sys.argv) == 2 else ""
    raise SystemExit(run(operation_id))


if __name__ == "__main__":  # pragma: no cover - exercised through the module entry point.
    main()
