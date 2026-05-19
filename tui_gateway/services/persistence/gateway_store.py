"""SQLite persistence for gateway-owned workspace and artifact state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_session_workspaces (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES gateway_workspaces(id)
);

CREATE TABLE IF NOT EXISTS gateway_artifacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    origin_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (workspace_id, path),
    FOREIGN KEY (workspace_id) REFERENCES gateway_workspaces(id)
);

CREATE TABLE IF NOT EXISTS gateway_session_artifacts (
    session_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (session_id, artifact_id),
    FOREIGN KEY (artifact_id) REFERENCES gateway_artifacts(id)
);

CREATE INDEX IF NOT EXISTS idx_gateway_artifacts_workspace
    ON gateway_artifacts(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gateway_session_artifacts_session
    ON gateway_session_artifacts(session_id, last_seen_at DESC);
"""


class GatewayStateStore:
    """Small gateway-owned store separate from Hermes transcript state."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_hermes_home() / "tui-gateway" / "state.db"
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=1.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    def upsert_workspace(self, workspace: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_workspaces
                        (id, name, path, kind, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        path = excluded.path,
                        kind = excluded.kind,
                        updated_at = excluded.updated_at
                    """,
                    (
                        workspace["id"],
                        workspace["name"],
                        workspace["path"],
                        workspace.get("kind", ""),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE gateway_workspaces
                    SET name = ?, kind = ?, updated_at = ?
                    WHERE path = ?
                    """,
                    (
                        workspace["name"],
                        workspace.get("kind", ""),
                        now,
                        workspace["path"],
                    ),
                )
            row = conn.execute(
                "SELECT * FROM gateway_workspaces WHERE path = ?",
                (workspace["path"],),
            ).fetchone()
        return self._row_to_dict(row) or dict(workspace)

    def bind_session_workspace(
        self,
        *,
        session_id: str,
        workspace_id: str,
        cwd: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_session_workspaces
                    (session_id, workspace_id, cwd, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace_id = excluded.workspace_id,
                    cwd = excluded.cwd,
                    updated_at = excluded.updated_at
                """,
                (session_id, workspace_id, cwd, now, now),
            )
            row = conn.execute(
                """
                SELECT sw.*, w.name, w.path, w.kind
                FROM gateway_session_workspaces sw
                JOIN gateway_workspaces w ON w.id = sw.workspace_id
                WHERE sw.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_dict(row) or {}

    def get_session_workspace(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sw.session_id, sw.cwd, sw.workspace_id AS id,
                       w.name, w.path, w.kind, w.created_at, w.updated_at
                FROM gateway_session_workspaces sw
                JOIN gateway_workspaces w ON w.id = sw.workspace_id
                WHERE sw.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def list_workspaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_workspaces
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_artifact(
        self,
        *,
        artifact: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        origin_json = json.dumps(artifact.get("origin") or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_artifacts
                    (id, workspace_id, path, relative_path, title, mime_type,
                     size_bytes, origin_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, path) DO UPDATE SET
                    title = excluded.title,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    origin_json = excluded.origin_json,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact["id"],
                    artifact["workspace_id"],
                    artifact["path"],
                    artifact["relative_path"],
                    artifact["title"],
                    artifact["mime_type"],
                    int(artifact.get("size_bytes") or 0),
                    origin_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE a.workspace_id = ? AND a.path = ?
                """,
                (artifact["workspace_id"], artifact["path"]),
            ).fetchone()
            artifact_id = row["id"] if row else artifact["id"]
            conn.execute(
                """
                INSERT INTO gateway_session_artifacts
                    (session_id, artifact_id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, artifact_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (session_id, artifact_id, now, now),
            )
        return self._artifact_row_to_payload(row)

    def list_artifacts(
        self,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if session_id:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_session_artifacts sa
                JOIN gateway_artifacts a ON a.id = sa.artifact_id
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE sa.session_id = ?
                ORDER BY sa.last_seen_at DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (session_id, limit)
        elif workspace_id:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE a.workspace_id = ?
                ORDER BY a.updated_at DESC
                LIMIT ?
            """
            params = (workspace_id, limit)
        else:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                ORDER BY a.updated_at DESC
                LIMIT ?
            """
            params = (limit,)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._artifact_row_to_payload(row) for row in rows]

    @staticmethod
    def _artifact_row_to_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        try:
            payload["origin"] = json.loads(payload.pop("origin_json") or "{}")
        except json.JSONDecodeError:
            payload["origin"] = {}
        workspace_name = payload.pop("workspace_name", "")
        workspace_path = payload.pop("workspace_path", "")
        workspace_kind = payload.pop("workspace_kind", "")
        payload["workspace"] = {
            "id": payload.get("workspace_id") or "",
            "name": workspace_name,
            "path": workspace_path,
            "kind": workspace_kind,
        }
        return payload


_DEFAULT_STORES: dict[str, GatewayStateStore] = {}
_DEFAULT_STORE_LOCK = threading.Lock()


def get_gateway_state_store() -> GatewayStateStore:
    home = Path(get_hermes_home()).expanduser()
    key = str(home.resolve())
    with _DEFAULT_STORE_LOCK:
        store = _DEFAULT_STORES.get(key)
        if store is None:
            store = GatewayStateStore(home / "tui-gateway" / "state.db")
            _DEFAULT_STORES[key] = store
        return store
