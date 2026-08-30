from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]


class WorkspaceOperationRegistry:
    """Cross-process coordination for workspace downloads on one local machine."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = self.root / ".operations"
        self.directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.directory.chmod(0o700)
        self.path = self.directory / "workspace-operations.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        deadline = time.monotonic() + 30.0
        while True:
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS workspace_operations (
                            operation_id TEXT PRIMARY KEY,
                            request_key TEXT NOT NULL,
                            request_json TEXT NOT NULL,
                            status TEXT NOT NULL,
                            owner_id TEXT,
                            worker_pid INTEGER,
                            heartbeat_at REAL NOT NULL,
                            progress_path TEXT NOT NULL,
                            cancel_path TEXT NOT NULL,
                            output_dir TEXT,
                            anchor_build_number INTEGER,
                            cancel_requested INTEGER NOT NULL DEFAULT 0,
                            error_code TEXT,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            last_accessed_at REAL NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS workspace_one_active_request
                        ON workspace_operations(request_key)
                        WHERE status = 'running'
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS workspace_reusable_request
                        ON workspace_operations(
                            request_key, status, anchor_build_number, updated_at
                        )
                        """
                    )
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        with suppress(OSError):
            self.path.chmod(0o600)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> JsonDict | None:
        return dict(row) if row is not None else None

    def claim_or_join(
        self,
        *,
        operation_id: str,
        request_key: str,
        request: JsonDict,
        progress_path: Path,
        cancel_path: Path,
        stale_before: float,
    ) -> tuple[JsonDict, bool, list[JsonDict]]:
        now = time.time()
        stale: list[JsonDict] = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM workspace_operations
                WHERE request_key = ? AND status = 'running'
                """,
                (request_key,),
            ).fetchone()
            if existing is not None and float(existing["heartbeat_at"]) >= stale_before:
                connection.execute(
                    """
                    UPDATE workspace_operations
                    SET last_accessed_at = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, now, existing["operation_id"]),
                )
                connection.commit()
                refreshed = self.get(str(existing["operation_id"]))
                assert refreshed is not None
                return refreshed, False, stale

            if existing is not None:
                stale.append(dict(existing))
                connection.execute(
                    """
                    UPDATE workspace_operations
                    SET status = 'failed', error_code = 'workspace_operation_interrupted',
                        updated_at = ?, last_accessed_at = ?
                    WHERE operation_id = ? AND status = 'running'
                    """,
                    (now, now, existing["operation_id"]),
                )

            connection.execute(
                """
                INSERT INTO workspace_operations (
                    operation_id, request_key, request_json, status, heartbeat_at,
                    progress_path, cancel_path, created_at, updated_at, last_accessed_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    request_key,
                    json.dumps(request, sort_keys=True, separators=(",", ":")),
                    now,
                    str(progress_path),
                    str(cancel_path),
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        created = self.get(operation_id)
        assert created is not None
        return created, True, stale

    def get(self, operation_id: str) -> JsonDict | None:
        with self._connection() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM workspace_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )

    def find_reusable(self, request_key: str, anchor_build_number: int) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_operations
                WHERE request_key = ? AND status = 'succeeded' AND anchor_build_number = ?
                ORDER BY updated_at DESC
                """,
                (request_key, anchor_build_number),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_active(self, request_key: str) -> JsonDict | None:
        with self._connection() as connection:
            return self._row(
                connection.execute(
                    """
                    SELECT * FROM workspace_operations
                    WHERE request_key = ? AND status = 'running'
                    """,
                    (request_key,),
                ).fetchone()
            )

    def claim_worker(self, operation_id: str, owner_id: str, worker_pid: int) -> JsonDict | None:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workspace_operations
                SET owner_id = ?, worker_pid = ?, heartbeat_at = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'running' AND owner_id IS NULL
                """,
                (owner_id, worker_pid, now, now, operation_id),
            ).rowcount
        return self.get(operation_id) if changed == 1 else None

    def set_spawned_pid(self, operation_id: str, worker_pid: int) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations
                SET worker_pid = ?, heartbeat_at = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'running'
                """,
                (worker_pid, now, now, operation_id),
            )

    def heartbeat(self, operation_id: str, owner_id: str) -> bool:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workspace_operations
                SET heartbeat_at = ?, updated_at = ?
                WHERE operation_id = ? AND owner_id = ? AND status = 'running'
                    AND cancel_requested = 0
                """,
                (now, now, operation_id, owner_id),
            ).rowcount
        return changed == 1

    def set_capture(
        self,
        operation_id: str,
        owner_id: str,
        *,
        output_dir: Path,
        anchor_build_number: int,
    ) -> bool:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workspace_operations
                SET output_dir = ?, anchor_build_number = ?, heartbeat_at = ?, updated_at = ?
                WHERE operation_id = ? AND owner_id = ? AND status = 'running'
                """,
                (str(output_dir), anchor_build_number, now, now, operation_id, owner_id),
            ).rowcount
        return changed == 1

    def clear_capture(self, operation_id: str, owner_id: str) -> bool:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workspace_operations
                SET output_dir = NULL, anchor_build_number = NULL,
                    heartbeat_at = ?, updated_at = ?
                WHERE operation_id = ? AND owner_id = ? AND status = 'running'
                """,
                (now, now, operation_id, owner_id),
            ).rowcount
        return changed == 1

    def request_cancel(self, operation_id: str) -> JsonDict | None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations
                SET cancel_requested = 1, updated_at = ?, last_accessed_at = ?
                WHERE operation_id = ? AND status = 'running'
                """,
                (now, now, operation_id),
            )
        return self.get(operation_id)

    def cancellation_requested(self, operation_id: str, owner_id: str) -> bool:
        row = self.get(operation_id)
        if row is None:
            return True
        return (
            row["status"] != "running"
            or row["owner_id"] != owner_id
            or bool(row["cancel_requested"])
        )

    def finish(
        self,
        operation_id: str,
        owner_id: str,
        status: str,
        *,
        error_code: str | None = None,
    ) -> bool:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workspace_operations
                SET status = ?, error_code = ?, heartbeat_at = ?, updated_at = ?,
                    last_accessed_at = ?
                WHERE operation_id = ? AND owner_id = ? AND status = 'running'
                """,
                (status, error_code, now, now, now, operation_id, owner_id),
            ).rowcount
        return changed == 1

    def fail_unowned_start(self, operation_id: str, error_code: str) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations
                SET status = 'failed', error_code = ?, updated_at = ?, last_accessed_at = ?
                WHERE operation_id = ? AND status = 'running' AND owner_id IS NULL
                """,
                (error_code, now, now, operation_id),
            )

    def invalidate_reusable(self, operation_id: str) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations
                SET status = 'failed', error_code = 'workspace_cached_payload_missing',
                    updated_at = ?, last_accessed_at = ?
                WHERE operation_id = ? AND status = 'succeeded'
                """,
                (now, now, operation_id),
            )

    def mark_stale(self, operation_id: str, stale_before: float) -> JsonDict | None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations
                SET status = 'failed', error_code = 'workspace_operation_interrupted',
                    updated_at = ?, last_accessed_at = ?
                WHERE operation_id = ? AND status = 'running' AND heartbeat_at < ?
                """,
                (now, now, operation_id, stale_before),
            )
        return self.get(operation_id)

    def touch(self, operation_id: str) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE workspace_operations SET last_accessed_at = ? WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    def cleanup_candidates(self, limit: int) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_operations
                ORDER BY last_accessed_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, operation_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM workspace_operations WHERE operation_id = ?",
                (operation_id,),
            )

    def request(self, row: JsonDict) -> JsonDict:
        value = json.loads(str(row["request_json"]))
        if not isinstance(value, dict):
            raise ValueError("Workspace operation request is not an object")
        return value


def current_process_id() -> int:
    return os.getpid()
