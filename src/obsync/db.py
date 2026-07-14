from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry
ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username_key TEXT NOT NULL,
    client_key TEXT NOT NULL,
    succeeded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup
ON auth_attempts(username_key, client_key, created_at DESC);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL,
    os_name TEXT NOT NULL,
    os_version TEXT NOT NULL DEFAULT '',
    agent_version TEXT NOT NULL DEFAULT '',
    vault_path TEXT NOT NULL DEFAULT '',
    vault_ready INTEGER NOT NULL DEFAULT 0,
    vault_error TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'online',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    used_at TEXT,
    agent_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roots (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    root_key TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    destination TEXT NOT NULL DEFAULT 'Obsync',
    include_patterns TEXT NOT NULL DEFAULT '[]',
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    file_count INTEGER NOT NULL DEFAULT 0,
    last_scan_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, root_key)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_mtime_ns INTEGER NOT NULL DEFAULT 0,
    source_size INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    destination_path TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    llm_status TEXT NOT NULL DEFAULT 'pending',
    confidence REAL NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    processed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, root_id, source_path)
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_review ON documents(needs_review, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(source_hash);
CREATE INDEX IF NOT EXISTS idx_documents_agent_root ON documents(agent_id, root_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    agent_id TEXT,
    root_id TEXT,
    document_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_commands_agent_status ON commands(agent_id, status, created_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            agent_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(agents)").fetchall()
            }
            for name, declaration in {
                "vault_path": "TEXT NOT NULL DEFAULT ''",
                "vault_ready": "INTEGER NOT NULL DEFAULT 0",
                "vault_error": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in agent_columns:
                    connection.execute(f"ALTER TABLE agents ADD COLUMN {name} {declaration}")
            enrollment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(enrollments)").fetchall()
            }
            if "agent_id" not in enrollment_columns:
                connection.execute("ALTER TABLE enrollments ADD COLUMN agent_id TEXT")
            row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (3)")
            elif int(row["version"]) < 3:
                connection.execute("UPDATE schema_meta SET version = 3")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(parameters))
            connection.commit()
            return cursor.rowcount

    def query_one(self, sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, tuple(parameters)).fetchone()
            return dict(row) if row else None

    def query_all(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]

    def add_event(
        self,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        agent_id: str | None = None,
        root_id: str | None = None,
        document_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO events(level, event_type, message, agent_id, root_id, document_id,
                               details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                level,
                event_type,
                message,
                agent_id,
                root_id,
                document_id,
                json.dumps(details or {}, ensure_ascii=False),
                utc_now(),
            ),
        )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def get_settings(self) -> dict[str, dict[str, Any]]:
        rows = self.query_all("SELECT key, value, is_secret, updated_at FROM settings ORDER BY key")
        return {row["key"]: row for row in rows}

    def set_settings(self, values: dict[str, tuple[str, bool]]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO settings(key, value, is_secret, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = excluded.updated_at
                """,
                [(key, value, int(secret), now) for key, (value, secret) in values.items()],
            )

    def dashboard_stats(self) -> dict[str, int]:
        with self.connect() as connection:

            def scalar(sql: str) -> int:
                return int(connection.execute(sql).fetchone()[0])

            return {
                "agents": scalar("SELECT count(*) FROM agents WHERE enabled = 1"),
                "online_agents": scalar(
                    "SELECT count(*) FROM agents WHERE enabled = 1 AND status = 'online'"
                ),
                "roots": scalar("SELECT count(*) FROM roots WHERE enabled = 1"),
                "documents": scalar("SELECT count(*) FROM documents"),
                "synced": scalar("SELECT count(*) FROM documents WHERE status = 'synced'"),
                "review": scalar("SELECT count(*) FROM documents WHERE needs_review = 1"),
                "errors": scalar("SELECT count(*) FROM documents WHERE status = 'error'"),
                "missing": scalar("SELECT count(*) FROM documents WHERE missing = 1"),
            }
