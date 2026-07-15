from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .extractors import extract_document
from .llm import Analysis, LLMAnalyzer, LLMConfig
from .markdown import (
    is_managed_note,
    managed_note_metadata,
    merge_preserving_manual,
    render_markdown,
    set_source_status,
)
from .security import (
    hash_password,
    hash_token,
    new_enrollment_code,
    new_token,
    safe_relative_path,
    safe_vault_path,
    slugify,
    validate_password,
    validate_username,
    verify_password,
    verify_token,
)

DEFAULT_SETTINGS: dict[str, tuple[str, bool]] = {
    "vault_mode": ("local", False),
    "vault_agent_id": ("", False),
    "llm_enabled": ("false", False),
    "llm_provider": ("ollama", False),
    "llm_base_url": ("", False),
    "llm_model": ("", False),
    "llm_api_key": ("", True),
    "llm_timeout_seconds": ("120", False),
    "review_threshold": ("0.65", False),
}


class LoginRateLimitedError(ValueError):
    pass


class ObsyncService:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.settings.prepare()
        self.db = db or Database(settings.database_path)
        self.db.initialize()
        self._ensure_defaults()
        self._inventory_vault_indexes: dict[str, dict[tuple[str, str, str], dict[str, str]]] = {}
        self._dummy_password_hash = hash_password("obsync-invalid-password")
        self._bootstrap_admin_from_env()

    def _ensure_defaults(self) -> None:
        existing = self.db.get_settings()
        missing = {key: value for key, value in DEFAULT_SETTINGS.items() if key not in existing}
        if missing:
            self.db.set_settings(missing)

    def _bootstrap_admin_from_env(self) -> None:
        if self.has_admin_account():
            return
        username = self.settings.admin_username
        password = self.settings.admin_password
        if not username and not password:
            return
        if not username or not password:
            raise ValueError("OBSYNC_ADMIN_USERNAME and OBSYNC_ADMIN_PASSWORD must be set together")
        self.create_admin_account(username, password, bootstrap=True)

    def has_admin_account(self) -> bool:
        return self.db.query_one("SELECT id FROM admin_users LIMIT 1") is not None

    def setup_status(self) -> dict[str, bool]:
        setup_required = not self.has_admin_account()
        return {
            "setup_required": setup_required,
            "legacy_migration_required": setup_required and bool(self.settings.admin_token),
        }

    def verify_admin(self, token: str) -> bool:
        """Verify the pre-0.2 admin token only until an admin account is created."""
        if self.has_admin_account():
            return False
        return bool(self.settings.admin_token and token) and verify_token(
            token, hash_token(self.settings.admin_token)
        )

    def create_admin_account(
        self,
        username: str,
        password: str,
        *,
        legacy_token: str = "",
        bootstrap: bool = False,
        trusted_local: bool = False,
    ) -> dict[str, Any]:
        username = validate_username(username)
        validate_password(password)
        if self.has_admin_account():
            raise ValueError("Admin account setup is already complete")
        if (
            self.settings.admin_token
            and not bootstrap
            and not trusted_local
            and not self.verify_admin(legacy_token)
        ):
            raise ValueError("The current admin token is incorrect")
        now = utc_now()
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
            if existing:
                raise ValueError("Admin account setup is already complete")
            cursor = connection.execute(
                """
                INSERT INTO admin_users(username, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, hash_password(password), now, now),
            )
            user_id = int(cursor.lastrowid)
        self.db.add_event("auth.admin_created", f"Admin account {username} was created")
        return {"id": user_id, "username": username}

    def _record_login_attempt(self, username_key: str, client_key: str, *, succeeded: bool) -> None:
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO auth_attempts(username_key, client_key, succeeded, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username_key, client_key, int(succeeded), now),
        )
        cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM auth_attempts WHERE created_at < ?", (cutoff,))

    def _login_is_rate_limited(self, username_key: str, client_key: str) -> bool:
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat(timespec="seconds")
        row = self.db.query_one(
            """
            SELECT count(*) AS failures
            FROM auth_attempts
            WHERE username_key = ? AND client_key = ? AND succeeded = 0 AND created_at >= ?
            """,
            (username_key, client_key, cutoff),
        )
        client_row = self.db.query_one(
            """
            SELECT count(*) AS failures
            FROM auth_attempts
            WHERE client_key = ? AND succeeded = 0 AND created_at >= ?
            """,
            (client_key, cutoff),
        )
        return bool(
            (row and int(row["failures"]) >= 5)
            or (client_row and int(client_row["failures"]) >= 20)
        )

    def login_admin(
        self,
        username: str,
        password: str,
        *,
        remember: bool,
        client_ip: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        username_key = username.strip().casefold()[:64]
        client_key = (client_ip or "unknown")[:128]
        if self._login_is_rate_limited(username_key, client_key):
            raise LoginRateLimitedError("Too many failed attempts. Try again in 15 minutes")
        user = self.db.query_one(
            "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE", (username.strip(),)
        )
        encoded = str(user["password_hash"]) if user else self._dummy_password_hash
        if not verify_password(password, encoded) or not user:
            self._record_login_attempt(username_key, client_key, succeeded=False)
            raise ValueError("Username or password is incorrect")
        self._record_login_attempt(username_key, client_key, succeeded=True)
        self.db.execute(
            "DELETE FROM auth_attempts WHERE username_key = ? AND client_key = ?",
            (username_key, client_key),
        )
        return self.create_admin_session(
            user,
            remember=remember,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    def create_admin_session(
        self,
        user: dict[str, Any],
        *,
        remember: bool,
        client_ip: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        token = new_token("session")
        csrf_token = new_token("csrf")
        now = datetime.now(UTC)
        lifetime = (
            timedelta(days=self.settings.remembered_session_days)
            if remember
            else timedelta(hours=self.settings.session_hours)
        )
        expires = now + lifetime
        self.db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now.isoformat(),))
        self.db.execute(
            """
            INSERT INTO admin_sessions(token_hash, user_id, csrf_hash, expires_at, created_at,
                                       last_seen_at, user_agent, client_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hash_token(token),
                user["id"],
                hash_token(csrf_token),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                user_agent[:500],
                client_ip[:128],
            ),
        )
        return {
            "token": token,
            "csrf_token": csrf_token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "max_age": int(lifetime.total_seconds()),
            "username": user["username"],
            "user_id": user["id"],
        }

    def authenticate_admin_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        session = self.db.query_one(
            """
            SELECT s.*, u.username
            FROM admin_sessions s
            JOIN admin_users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (hash_token(token),),
        )
        if not session:
            return None
        now = datetime.now(UTC)
        if datetime.fromisoformat(session["expires_at"]) <= now:
            self.db.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?", (session["token_hash"],)
            )
            return None
        try:
            last_seen = datetime.fromisoformat(session["last_seen_at"])
        except (TypeError, ValueError):
            last_seen = now - timedelta(hours=1)
        if now - last_seen >= timedelta(minutes=5):
            self.db.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now.isoformat(timespec="seconds"), session["token_hash"]),
            )
        return session

    def verify_admin_csrf(self, session: dict[str, Any], csrf_token: str) -> bool:
        return bool(csrf_token) and verify_token(csrf_token, str(session["csrf_hash"]))

    def logout_admin(self, token: str) -> None:
        if token:
            self.db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (hash_token(token),))

    def reset_admin_account(self, username: str, password: str) -> dict[str, Any]:
        username = validate_username(username)
        validate_password(password)
        now = utc_now()
        password_hash = hash_password(password)
        with self.db.transaction() as connection:
            user = connection.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
            if user:
                user_id = int(user["id"])
                connection.execute(
                    """
                    UPDATE admin_users
                    SET username = ?, password_hash = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (username, password_hash, now, user_id),
                )
                connection.execute("DELETE FROM admin_sessions")
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO admin_users(username, password_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, password_hash, now, now),
                )
                user_id = int(cursor.lastrowid)
        self.db.add_event("auth.admin_reset", f"Admin credentials reset for {username}")
        return {"id": user_id, "username": username}

    def update_admin_account(
        self,
        user_id: int,
        *,
        current_password: str,
        username: str,
        new_password: str = "",
        keep_session_token: str = "",
    ) -> dict[str, Any]:
        username = validate_username(username)
        if new_password:
            validate_password(new_password)
        user = self.db.query_one("SELECT * FROM admin_users WHERE id = ?", (user_id,))
        if not user or not verify_password(current_password, str(user["password_hash"])):
            raise ValueError("Current password is incorrect")
        password_hash = hash_password(new_password) if new_password else user["password_hash"]
        now = utc_now()
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE admin_users SET username = ?, password_hash = ?, updated_at = ? WHERE id = ?
                """,
                (username, password_hash, now, user_id),
            )
            if new_password:
                keep_hash = hash_token(keep_session_token) if keep_session_token else ""
                connection.execute(
                    "DELETE FROM admin_sessions WHERE user_id = ? AND token_hash != ?",
                    (user_id, keep_hash),
                )
        self.db.add_event("auth.admin_updated", f"Admin account updated for {username}")
        return {"id": user_id, "username": username}

    def create_enrollment(self, label: str = "", minutes: int = 20) -> dict[str, Any]:
        code = new_enrollment_code()
        enrollment_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=max(1, min(minutes, 1440)))
        self.db.execute(
            """
            INSERT INTO enrollments(id, code_hash, label, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (enrollment_id, hash_token(code), label[:120], expires.isoformat(), now.isoformat()),
        )
        self.db.add_event(
            "enrollment.created", f"Enrollment code created for {label or 'a device'}"
        )
        return {"id": enrollment_id, "code": code, "expires_at": expires.isoformat()}

    def register_agent(self, code: str, payload: dict[str, str]) -> dict[str, str]:
        now = datetime.now(UTC)
        proposed_token = str(payload.get("agent_token") or "").strip()
        if proposed_token and (
            not proposed_token.startswith("agent_") or not 32 <= len(proposed_token) <= 128
        ):
            raise ValueError("Agent credential is invalid")
        agent_id = str(uuid.uuid4())
        token = proposed_token or new_token("agent")
        name = (payload.get("name") or payload.get("hostname") or "Unnamed device").strip()[:100]
        hostname = (payload.get("hostname") or name).strip()[:255]
        with self.db.transaction() as connection:
            enrollment = connection.execute(
                "SELECT * FROM enrollments WHERE code_hash = ?", (hash_token(code.upper()),)
            ).fetchone()
            if not enrollment:
                raise ValueError("Enrollment code is invalid or already used")
            if enrollment["used_at"]:
                existing = (
                    connection.execute(
                        "SELECT * FROM agents WHERE id = ?", (enrollment["agent_id"],)
                    ).fetchone()
                    if enrollment["agent_id"]
                    else None
                )
                if (
                    proposed_token
                    and existing
                    and existing["enabled"]
                    and verify_token(proposed_token, str(existing["token_hash"]))
                ):
                    return {
                        "agent_id": str(existing["id"]),
                        "agent_token": proposed_token,
                        "name": str(existing["name"]),
                    }
                raise ValueError("Enrollment code is invalid or already used")
            if datetime.fromisoformat(enrollment["expires_at"]) < now:
                raise ValueError("Enrollment code has expired")
            connection.execute(
                """
                INSERT INTO agents(id, name, hostname, os_name, os_version, agent_version,
                                   token_hash, status, last_seen_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)
                """,
                (
                    agent_id,
                    name,
                    hostname,
                    (payload.get("os_name") or "unknown")[:100],
                    (payload.get("os_version") or "")[:200],
                    (payload.get("agent_version") or "")[:50],
                    hash_token(token),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE enrollments SET used_at = ?, agent_id = ? WHERE id = ?",
                (now.isoformat(), agent_id, enrollment["id"]),
            )
        self.db.add_event("agent.registered", f"Device {name} joined Obsync", agent_id=agent_id)
        return {"agent_id": agent_id, "agent_token": token, "name": name}

    def disconnect_agent(self, agent_id: str) -> dict[str, Any]:
        agent = self.db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not agent:
            raise ValueError("Computer not found")
        if (
            self.db.get_setting("vault_mode", "local") == "agent"
            and self.db.get_setting("vault_agent_id", "") == agent_id
        ):
            raise ValueError(
                "This computer is the active vault writer. Choose another vault in Settings "
                "before disconnecting it."
            )
        root_count = int(
            (
                self.db.query_one(
                    "SELECT count(*) AS count FROM roots WHERE agent_id = ?", (agent_id,)
                )
                or {}
            ).get("count", 0)
        )
        document_count = int(
            (
                self.db.query_one(
                    "SELECT count(*) AS count FROM documents WHERE agent_id = ?", (agent_id,)
                )
                or {}
            ).get("count", 0)
        )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM enrollments WHERE agent_id = ?", (agent_id,))
            connection.execute("DELETE FROM events WHERE agent_id = ?", (agent_id,))
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        self.db.add_event(
            "agent.disconnected",
            f"Computer {agent['name']} was disconnected; source files and Obsidian notes were kept",
        )
        return {
            "ok": True,
            "agent_id": agent_id,
            "name": agent["name"],
            "removed_roots": root_count,
            "removed_documents": document_count,
        }

    def authenticate_agent(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.db.query_one("SELECT * FROM agents WHERE token_hash = ?", (hash_token(token),))
        if not row or not row["enabled"]:
            return None
        return row

    def enrollment_status(self, enrollment_id: str) -> dict[str, Any]:
        enrollment = self.db.query_one(
            "SELECT id, label, expires_at, used_at, agent_id FROM enrollments WHERE id = ?",
            (enrollment_id,),
        )
        if not enrollment:
            raise ValueError("Enrollment not found")
        agent = None
        if enrollment.get("agent_id"):
            agent = self.db.query_one(
                "SELECT id, name, hostname, os_name, last_seen_at FROM agents WHERE id = ?",
                (enrollment["agent_id"],),
            )
        enrollment["connected"] = bool(agent)
        enrollment["agent"] = agent
        return enrollment

    def heartbeat(
        self,
        agent_id: str,
        version: str = "",
        *,
        vault_path: str | None = None,
        vault_ready: bool | None = None,
        vault_error: str | None = None,
    ) -> None:
        now = utc_now()
        self.db.execute(
            """
            UPDATE agents SET status = 'online', last_seen_at = ?, updated_at = ?,
                              agent_version = CASE WHEN ? = '' THEN agent_version ELSE ? END,
                              vault_path = CASE WHEN ? IS NULL THEN vault_path ELSE ? END,
                              vault_ready = CASE WHEN ? IS NULL THEN vault_ready ELSE ? END,
                              vault_error = CASE WHEN ? IS NULL THEN vault_error ELSE ? END
            WHERE id = ?
            """,
            (
                now,
                now,
                version,
                version[:50],
                vault_path,
                (vault_path or "")[:2000],
                vault_ready,
                int(bool(vault_ready)),
                vault_error,
                (vault_error or "")[:1000],
                agent_id,
            ),
        )

    def list_agents(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            """
            SELECT a.*,
                   count(DISTINCT r.id) AS root_count,
                   count(DISTINCT d.id) AS document_count,
                   sum(CASE WHEN d.status = 'error' THEN 1 ELSE 0 END) AS error_count
            FROM agents a
            LEFT JOIN roots r ON r.agent_id = a.id
            LEFT JOIN documents d ON d.agent_id = a.id
            GROUP BY a.id
            ORDER BY a.name COLLATE NOCASE
            """
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=90)
        for row in rows:
            try:
                online = row["enabled"] and datetime.fromisoformat(row["last_seen_at"]) >= cutoff
            except (TypeError, ValueError):
                online = False
            row["status"] = "online" if online else "offline"
            if not online and row["status"] != "offline":
                self.db.execute("UPDATE agents SET status = 'offline' WHERE id = ?", (row["id"],))
        return rows

    def upsert_root(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        root_key = str(payload["root_key"])[:200]
        now = utc_now()
        existing = self.db.query_one(
            "SELECT * FROM roots WHERE agent_id = ? AND root_key = ?", (agent_id, root_key)
        )
        root_id = existing["id"] if existing else str(uuid.uuid4())
        name = str(payload.get("name") or Path(str(payload.get("path", "Folder"))).name or "Folder")
        destination = str(payload.get("destination") or "Obsync")
        safe_relative_path(destination)
        include = json.dumps(list(payload.get("include_patterns") or ["**/*"]))
        exclude = json.dumps(list(payload.get("exclude_patterns") or []))
        self.db.execute(
            """
            INSERT INTO roots(id, agent_id, root_key, name, path, destination, include_patterns,
                              exclude_patterns, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, root_key) DO UPDATE SET
                name = excluded.name,
                path = excluded.path,
                destination = excluded.destination,
                include_patterns = excluded.include_patterns,
                exclude_patterns = excluded.exclude_patterns,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                root_id,
                agent_id,
                root_key,
                name[:120],
                str(payload.get("path") or "")[:2000],
                destination[:500],
                include,
                exclude,
                int(bool(payload.get("enabled", True))),
                now,
                now,
            ),
        )
        root = self.db.query_one("SELECT * FROM roots WHERE id = ?", (root_id,))
        assert root is not None
        return self._decode_root(root)

    def list_roots(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT r.*, a.name AS agent_name,
                   count(d.id) AS document_count,
                   sum(CASE WHEN d.status = 'error' THEN 1 ELSE 0 END) AS error_count,
                   sum(CASE WHEN d.comparison_status = 'in-sync' THEN 1 ELSE 0 END)
                       AS in_sync_count,
                   sum(CASE WHEN d.comparison_status = 'modified' THEN 1 ELSE 0 END)
                       AS modified_count,
                   sum(CASE WHEN d.comparison_status = 'new' THEN 1 ELSE 0 END)
                       AS new_count,
                   sum(CASE WHEN d.comparison_status IN ('vault-missing', 'source-missing')
                            THEN 1 ELSE 0 END) AS missing_count,
                   sum(CASE WHEN d.comparison_status = 'checking' THEN 1 ELSE 0 END)
                       AS checking_count
            FROM roots r
            JOIN agents a ON a.id = r.agent_id
            LEFT JOIN documents d ON d.root_id = r.id
        """
        params: tuple[str, ...] = ()
        if agent_id:
            sql += " WHERE r.agent_id = ?"
            params = (agent_id,)
        sql += " GROUP BY r.id ORDER BY a.name COLLATE NOCASE, r.name COLLATE NOCASE"
        return [self._decode_root(row) for row in self.db.query_all(sql, params)]

    def _local_vault_index(self) -> dict[tuple[str, str, str], dict[str, str]]:
        index: dict[tuple[str, str, str], dict[str, str]] = {}
        if not self.settings.vault_path.is_dir():
            return index
        for path in self.settings.vault_path.rglob("*.md"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                metadata = managed_note_metadata(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if not metadata:
                continue
            key = (
                metadata["obsync_machine"].casefold(),
                metadata["obsync_root"].casefold(),
                metadata["obsync_source"],
            )
            metadata["destination_path"] = path.relative_to(self.settings.vault_path).as_posix()
            index.setdefault(key, metadata)
        return index

    @staticmethod
    def _identity_key(agent_name: str, root_name: str, source_path: str) -> tuple[str, str, str]:
        return (agent_name.casefold(), root_name.casefold(), source_path)

    def _compare_local_document(
        self,
        document: dict[str, Any],
        *,
        agent_name: str,
        root_name: str,
        vault_index: dict[tuple[str, str, str], dict[str, str]],
    ) -> tuple[str, str, str]:
        destination = str(document.get("destination_path") or "")
        metadata: dict[str, str] | None = None
        if destination:
            note_exists = False
            try:
                note_path = safe_vault_path(self.settings.vault_path, destination)
                if note_path.is_file():
                    note_exists = True
                    metadata = managed_note_metadata(note_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                metadata = None
            if metadata is None:
                return ("modified" if note_exists else "vault-missing"), destination, ""
        else:
            metadata = vault_index.get(
                self._identity_key(agent_name, root_name, str(document["source_path"]))
            )
            if not metadata:
                return "new", "", ""
            destination = metadata["destination_path"]

        note_hash = str(metadata.get("obsync_hash", ""))
        observed_hash = str(document.get("observed_hash") or document.get("source_hash") or "")
        state = "in-sync" if note_hash and note_hash == observed_hash else "modified"
        return state, destination, note_hash

    def inventory_files(
        self,
        *,
        agent: dict[str, Any],
        root_id: str,
        scan_id: str,
        items: list[dict[str, Any]],
        complete: bool = False,
    ) -> dict[str, Any]:
        root = self.db.query_one(
            "SELECT * FROM roots WHERE id = ? AND agent_id = ?", (root_id, agent["id"])
        )
        if not root or not root["enabled"]:
            raise ValueError("Watched folder is unknown or disabled")
        if not scan_id or len(scan_id) > 100:
            raise ValueError("Inventory scan id is invalid")
        now = utc_now()
        local_mode = self.db.get_setting("vault_mode", "local") == "local"
        if local_mode:
            if scan_id not in self._inventory_vault_indexes:
                if len(self._inventory_vault_indexes) >= 8:
                    self._inventory_vault_indexes.pop(next(iter(self._inventory_vault_indexes)))
                self._inventory_vault_indexes[scan_id] = self._local_vault_index()
            vault_index = self._inventory_vault_indexes[scan_id]
        else:
            vault_index = {}

        for item in items:
            source_path = safe_relative_path(str(item.get("source_path", ""))).as_posix()
            observed_hash = str(item.get("sha256", "")).lower()
            if len(observed_hash) != 64 or any(c not in "0123456789abcdef" for c in observed_hash):
                raise ValueError(f"Inventory hash is invalid for {source_path}")
            observed_mtime = max(0, int(item.get("source_mtime_ns", 0)))
            observed_size = max(0, int(item.get("source_size", 0)))
            existing = self.db.query_one(
                "SELECT * FROM documents WHERE agent_id = ? AND root_id = ? AND source_path = ?",
                (agent["id"], root_id, source_path),
            )
            if existing:
                if existing["missing"]:
                    comparison = (
                        "modified"
                        if existing["source_hash"] and existing["source_hash"] != observed_hash
                        else "checking"
                        if not local_mode
                        else "in-sync"
                    )
                elif not existing["source_hash"]:
                    comparison = "new"
                elif existing["source_hash"] != observed_hash:
                    comparison = "modified"
                else:
                    comparison = "checking" if not local_mode else "in-sync"
                self.db.execute(
                    """
                    UPDATE documents SET observed_hash = ?, observed_mtime_ns = ?,
                        observed_size = ?, inventory_scan_id = ?, inventory_seen_at = ?,
                        comparison_status = ?, missing = 0, updated_at = ? WHERE id = ?
                    """,
                    (
                        observed_hash,
                        observed_mtime,
                        observed_size,
                        scan_id,
                        now,
                        comparison,
                        now,
                        existing["id"],
                    ),
                )
                document_id = existing["id"]
            else:
                document_id = str(uuid.uuid4())
                self.db.execute(
                    """
                    INSERT INTO documents(
                        id, agent_id, root_id, source_path, source_name,
                        observed_hash, observed_mtime_ns, observed_size,
                        inventory_scan_id, inventory_seen_at, status, comparison_status,
                        first_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 'new', ?, ?, ?)
                    """,
                    (
                        document_id,
                        agent["id"],
                        root_id,
                        source_path,
                        Path(source_path).name,
                        observed_hash,
                        observed_mtime,
                        observed_size,
                        scan_id,
                        now,
                        now,
                        now,
                        now,
                    ),
                )

            if local_mode:
                document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
                assert document is not None
                comparison, destination, note_hash = self._compare_local_document(
                    document,
                    agent_name=str(agent["name"]),
                    root_name=str(root["name"]),
                    vault_index=vault_index,
                )
                values: list[Any] = [comparison, destination, now]
                source_hash_sql = ""
                if note_hash and not document["source_hash"]:
                    source_hash_sql = ", source_hash = ?, status = 'synced', processed_at = ?"
                    values.extend([note_hash, now])
                values.append(document_id)
                self.db.execute(
                    f"""
                    UPDATE documents SET comparison_status = ?, destination_path = ?,
                        updated_at = ? {source_hash_sql} WHERE id = ?
                    """,
                    values,
                )

        audit_commands: list[str] = []
        if complete:
            unseen = self.db.query_all(
                """
                SELECT source_path FROM documents
                WHERE root_id = ? AND inventory_scan_id != ? AND missing = 0
                """,
                (root_id, scan_id),
            )
            for row in unseen:
                self.mark_missing(agent["id"], root_id, row["source_path"])
            self.db.execute(
                "UPDATE roots SET file_count = ?, last_scan_at = ?, updated_at = ? WHERE id = ?",
                (
                    self.db.query_one(
                        "SELECT count(*) AS count FROM documents "
                        "WHERE root_id = ? AND inventory_scan_id = ?",
                        (root_id, scan_id),
                    )["count"],
                    now,
                    now,
                    root_id,
                ),
            )
            if not local_mode:
                vault_agent = self._remote_vault_agent()
                documents = self.db.query_all(
                    """
                    SELECT id, source_path, observed_hash, destination_path
                    FROM documents WHERE root_id = ? AND inventory_scan_id = ?
                    ORDER BY source_path
                    """,
                    (root_id, scan_id),
                )
                for start in range(0, len(documents), 100):
                    command = self.queue_command(
                        vault_agent["id"],
                        "audit_vault",
                        {
                            "source_agent": agent["name"],
                            "root_name": root["name"],
                            "documents": documents[start : start + 100],
                        },
                    )
                    audit_commands.append(command["id"])
            self.db.add_event(
                "root.inventoried",
                f"Compared {root['name']} with the Obsidian vault",
                agent_id=agent["id"],
                root_id=root_id,
            )
            self._inventory_vault_indexes.pop(scan_id, None)

        counts = self.db.query_all(
            """
            SELECT comparison_status, count(*) AS count FROM documents
            WHERE root_id = ? GROUP BY comparison_status
            """,
            (root_id,),
        )
        return {
            "root_id": root_id,
            "scan_id": scan_id,
            "complete": complete,
            "counts": {row["comparison_status"]: row["count"] for row in counts},
            "audit_commands": audit_commands,
        }

    def server_info(self) -> dict[str, Any]:
        in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
        return {
            "name": "Obsync server",
            "hostname": socket.gethostname(),
            "os_name": platform.system(),
            "os_version": platform.release(),
            "container": in_container,
            "vault_path": str(self.settings.vault_path),
            "vault_host_path": self.settings.vault_host_path,
            "vault_writable": self.settings.vault_path.exists()
            and os.access(self.settings.vault_path, os.W_OK),
            "public_url": self.settings.public_url,
        }

    def vault_status(self) -> dict[str, Any]:
        mode = self.db.get_setting("vault_mode", "local")
        if mode == "agent":
            agent_id = self.db.get_setting("vault_agent_id", "")
            agent = self.db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
            return {
                "mode": "agent",
                "agent_id": agent_id,
                "agent_name": agent["name"] if agent else "",
                "path": agent["vault_path"] if agent else "",
                "exists": bool(agent and agent["vault_ready"]),
                "writable": bool(agent and agent["vault_ready"] and agent["enabled"]),
            }
        return {
            "mode": "local",
            "agent_id": "",
            "agent_name": "Obsync server",
            "path": str(self.settings.vault_path),
            "host_path": self.settings.vault_host_path,
            "exists": self.settings.vault_path.exists(),
            "writable": self.settings.vault_path.exists()
            and os.access(self.settings.vault_path, os.W_OK),
        }

    def _remote_vault_agent(self) -> dict[str, Any] | None:
        if self.db.get_setting("vault_mode", "local") != "agent":
            return None
        agent_id = self.db.get_setting("vault_agent_id", "")
        agent = self.db.query_one("SELECT * FROM agents WHERE id = ? AND enabled = 1", (agent_id,))
        if not agent or not agent["vault_ready"] or not agent["vault_path"]:
            raise ValueError("The selected desktop vault is unavailable")
        return agent

    @staticmethod
    def _decode_root(row: dict[str, Any]) -> dict[str, Any]:
        row["include_patterns"] = json.loads(row.get("include_patterns") or "[]")
        row["exclude_patterns"] = json.loads(row.get("exclude_patterns") or "[]")
        return row

    def _llm_config(self) -> LLMConfig:
        enabled = self.db.get_setting("llm_enabled", "false").lower() == "true"
        try:
            timeout = int(self.db.get_setting("llm_timeout_seconds", "120"))
        except ValueError:
            timeout = 120
        return LLMConfig(
            enabled=enabled,
            provider=self.db.get_setting("llm_provider", "ollama"),
            base_url=self.db.get_setting("llm_base_url", ""),
            model=self.db.get_setting("llm_model", ""),
            api_key=self.db.get_setting("llm_api_key", ""),
            timeout_seconds=max(5, min(timeout, 600)),
        )

    def _candidate_titles(self, source_path: str, limit: int = 100) -> list[str]:
        words = {
            word.lower()
            for word in Path(source_path).stem.replace("_", " ").replace("-", " ").split()
            if len(word) >= 4
        }
        rows = self.db.query_all(
            "SELECT DISTINCT title FROM documents WHERE status = 'synced' AND title != '' "
            "ORDER BY updated_at DESC LIMIT 500"
        )
        scored = []
        for row in rows:
            title = row["title"]
            title_words = set(title.lower().split())
            score = len(words & title_words)
            scored.append((score, title))
        scored.sort(key=lambda item: (-item[0], item[1].casefold()))
        return [title for _, title in scored[:limit]]

    def _new_destination(
        self,
        *,
        document_id: str,
        agent_name: str,
        root: dict[str, Any],
        analysis: Analysis,
    ) -> str:
        segments = [
            str(root["destination"]),
            slugify(agent_name, "device"),
            slugify(str(root["name"]), "folder"),
            slugify(analysis.category, "documents"),
        ]
        filename = f"{slugify(analysis.title, 'document')}-{document_id[:8]}.md"
        relative = Path(*segments) / filename
        safe_relative_path(str(relative))
        return relative.as_posix()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".obsync-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    async def process_file(
        self,
        *,
        agent: dict[str, Any],
        root_id: str,
        source_path: str,
        source_mtime_ns: int,
        source_size: int,
        staged_file: Path,
        claimed_hash: str = "",
        previous_path: str = "",
    ) -> dict[str, Any]:
        source_rel = safe_relative_path(source_path).as_posix()
        root = self.db.query_one(
            "SELECT * FROM roots WHERE id = ? AND agent_id = ?", (root_id, agent["id"])
        )
        if not root or not root["enabled"]:
            raise ValueError("Watched folder is unknown or disabled")
        if staged_file.stat().st_size > self.settings.max_upload_bytes:
            raise ValueError(f"File exceeds the {self.settings.max_upload_mb} MB upload limit")

        actual_hash = self._sha256(staged_file)
        if claimed_hash and claimed_hash.lower() != actual_hash:
            raise ValueError("Uploaded file hash does not match the agent manifest")

        existing = self.db.query_one(
            "SELECT * FROM documents WHERE agent_id = ? AND root_id = ? AND source_path = ?",
            (agent["id"], root_id, source_rel),
        )
        if not existing and previous_path:
            previous_rel = safe_relative_path(previous_path).as_posix()
            existing = self.db.query_one(
                "SELECT * FROM documents WHERE agent_id = ? AND root_id = ? AND source_path = ?",
                (agent["id"], root_id, previous_rel),
            )
            if existing:
                self.db.execute(
                    """
                    UPDATE documents SET source_path = ?, source_name = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (source_rel, Path(source_rel).name, utc_now(), existing["id"]),
                )
                existing["source_path"] = source_rel
                existing["source_name"] = Path(source_rel).name

        if (
            existing
            and existing["source_hash"] == actual_hash
            and not existing["missing"]
            and existing["comparison_status"] == "in-sync"
        ):
            self.db.execute(
                """
                UPDATE documents SET source_mtime_ns = ?, source_size = ?, observed_hash = ?,
                    observed_mtime_ns = ?, observed_size = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    source_mtime_ns,
                    source_size,
                    actual_hash,
                    source_mtime_ns,
                    source_size,
                    utc_now(),
                    existing["id"],
                ),
            )
            return {**self._decode_document(existing), "result": "unchanged"}

        document_id = existing["id"] if existing else str(uuid.uuid4())
        now = utc_now()
        if existing:
            self.db.execute(
                """
                UPDATE documents SET source_mtime_ns = ?, source_size = ?, source_hash = ?,
                    observed_hash = ?, observed_mtime_ns = ?, observed_size = ?,
                    status = 'processing', missing = 0, error = '', updated_at = ? WHERE id = ?
                """,
                (
                    source_mtime_ns,
                    source_size,
                    actual_hash,
                    actual_hash,
                    source_mtime_ns,
                    source_size,
                    now,
                    document_id,
                ),
            )
        else:
            self.db.execute(
                """
                INSERT INTO documents(
                    id, agent_id, root_id, source_path, source_name, source_mtime_ns,
                    source_size, source_hash, observed_hash, observed_mtime_ns, observed_size,
                    status, comparison_status, first_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', 'new', ?, ?, ?)
                """,
                (
                    document_id,
                    agent["id"],
                    root_id,
                    source_rel,
                    Path(source_rel).name,
                    source_mtime_ns,
                    source_size,
                    actual_hash,
                    actual_hash,
                    source_mtime_ns,
                    source_size,
                    now,
                    now,
                    now,
                ),
            )

        try:
            extracted = extract_document(staged_file, self.settings.max_extract_chars)
            analyzer = LLMAnalyzer(self._llm_config())
            analysis = await analyzer.analyze(
                source_path=source_rel,
                text=extracted.text,
                mime_type=extracted.mime_type,
                candidates=self._candidate_titles(source_rel),
            )
            try:
                threshold = float(self.db.get_setting("review_threshold", "0.65"))
            except ValueError:
                threshold = 0.65
            needs_review = analysis.confidence < max(0.0, min(1.0, threshold))
            destination = (
                existing["destination_path"]
                if existing and existing["destination_path"]
                else self._new_destination(
                    document_id=document_id,
                    agent_name=agent["name"],
                    root=root,
                    analysis=analysis,
                )
            )
            generated = render_markdown(
                document_id=document_id,
                source_path=source_rel,
                source_name=Path(source_rel).name,
                source_hash=actual_hash,
                source_size=source_size,
                source_mtime_ns=source_mtime_ns,
                machine_name=agent["name"],
                root_name=root["name"],
                mime_type=extracted.mime_type,
                extractor=extracted.extractor,
                extracted_text=extracted.text,
                extraction_warning=extracted.warning,
                truncated=extracted.truncated,
                analysis=analysis,
            )
            remote_vault = self._remote_vault_agent()
            status = "pending-write" if remote_vault else "synced"
            processed_at = None if remote_vault else utc_now()
            if not remote_vault:
                destination_path = safe_vault_path(self.settings.vault_path, destination)
                if destination_path.exists():
                    current = destination_path.read_text(encoding="utf-8")
                    if not is_managed_note(current):
                        raise ValueError(
                            f"Destination already exists and is not managed: {destination}"
                        )
                    generated = merge_preserving_manual(current, generated)
                self._atomic_write(destination_path, generated)

            updated_at = utc_now()
            self.db.execute(
                """
                UPDATE documents SET mime_type = ?, destination_path = ?, title = ?, category = ?,
                    tags_json = ?, summary = ?, analysis_json = ?, status = ?,
                    comparison_status = ?, llm_status = ?, confidence = ?, needs_review = ?,
                    missing = 0, error = '',
                    processed_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    extracted.mime_type,
                    destination,
                    analysis.title,
                    analysis.category,
                    json.dumps(analysis.tags, ensure_ascii=False),
                    analysis.summary,
                    json.dumps(analysis.as_dict(), ensure_ascii=False),
                    status,
                    "in-sync"
                    if not remote_vault
                    else existing.get("comparison_status", "new")
                    if existing
                    else "new",
                    "analyzed" if analysis.provider != "rules" else "rules",
                    analysis.confidence,
                    int(needs_review),
                    processed_at,
                    updated_at,
                    document_id,
                ),
            )
            if remote_vault:
                self.queue_command(
                    remote_vault["id"],
                    "write_note",
                    {
                        "document_id": document_id,
                        "destination_path": destination,
                        "content": generated,
                    },
                )
            self.db.add_event(
                "document.write_queued" if remote_vault else "document.synced",
                (
                    f"Queued {source_rel} for {remote_vault['name']}"
                    if remote_vault
                    else f"Synced {source_rel} to {destination}"
                ),
                agent_id=agent["id"],
                root_id=root_id,
                document_id=document_id,
                details={"llm": analysis.provider, "needs_review": needs_review},
            )
            row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
            assert row is not None
            return {
                **self._decode_document(row),
                "result": "queued" if remote_vault else "synced",
            }
        except Exception as exc:
            self.db.execute(
                "UPDATE documents SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
                (str(exc)[:2000], utc_now(), document_id),
            )
            self.db.add_event(
                "document.error",
                f"Could not sync {source_rel}: {exc}",
                level="error",
                agent_id=agent["id"],
                root_id=root_id,
                document_id=document_id,
            )
            raise

    def mark_missing(self, agent_id: str, root_id: str, source_path: str) -> dict[str, Any]:
        source_rel = safe_relative_path(source_path).as_posix()
        document = self.db.query_one(
            "SELECT * FROM documents WHERE agent_id = ? AND root_id = ? AND source_path = ?",
            (agent_id, root_id, source_rel),
        )
        if not document:
            return {"result": "unknown"}
        if document["missing"]:
            return {"result": "unchanged", **self._decode_document(document)}
        if document["destination_path"]:
            remote_vault = self._remote_vault_agent()
            if remote_vault:
                self.queue_command(
                    remote_vault["id"],
                    "set_source_status",
                    {
                        "document_id": document["id"],
                        "destination_path": document["destination_path"],
                        "source_status": "source-missing",
                    },
                )
            else:
                note_path = safe_vault_path(self.settings.vault_path, document["destination_path"])
                if note_path.exists():
                    updated = set_source_status(
                        note_path.read_text(encoding="utf-8"), "source-missing"
                    )
                    self._atomic_write(note_path, updated)
        now = utc_now()
        self.db.execute(
            """
            UPDATE documents SET missing = 1, comparison_status = 'source-missing',
                updated_at = ? WHERE id = ?
            """,
            (now, document["id"]),
        )
        self.db.add_event(
            "document.missing",
            f"Source file is missing: {source_rel}",
            level="warning",
            agent_id=agent_id,
            root_id=root_id,
            document_id=document["id"],
        )
        document["missing"] = 1
        return {"result": "marked-missing", **self._decode_document(document)}

    def list_documents(
        self,
        *,
        status: str = "",
        comparison_status: str = "",
        root_id: str = "",
        search: str = "",
        review: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("d.status = ?")
            params.append(status)
        if comparison_status:
            clauses.append("d.comparison_status = ?")
            params.append(comparison_status)
        if root_id:
            clauses.append("d.root_id = ?")
            params.append(root_id)
        if search:
            clauses.append("(d.title LIKE ? OR d.source_path LIKE ? OR d.summary LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])
        if review is not None:
            clauses.append("d.needs_review = ?")
            params.append(int(review))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count = self.db.query_one(f"SELECT count(*) AS count FROM documents d {where}", params)
        rows = self.db.query_all(
            f"""
            SELECT d.*, a.name AS agent_name, r.name AS root_name
            FROM documents d
            JOIN agents a ON a.id = d.agent_id
            JOIN roots r ON r.id = d.root_id
            {where}
            ORDER BY d.updated_at DESC LIMIT ? OFFSET ?
            """,
            [*params, max(1, min(limit, 500)), max(0, offset)],
        )
        return {"items": [self._decode_document(row) for row in rows], "total": count["count"]}

    def pending_root_documents(self, agent_id: str, root_id: str) -> list[dict[str, Any]]:
        root = self.db.query_one(
            "SELECT id FROM roots WHERE id = ? AND agent_id = ? AND enabled = 1",
            (root_id, agent_id),
        )
        if not root:
            raise ValueError("Watched folder is unknown or disabled")
        return self.db.query_all(
            """
            SELECT id, source_path, comparison_status FROM documents
            WHERE root_id = ? AND comparison_status != 'in-sync'
            ORDER BY source_path
            """,
            (root_id,),
        )

    @staticmethod
    def _decode_document(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json", "[]") or "[]")
        result["analysis"] = json.loads(result.pop("analysis_json", "{}") or "{}")
        return result

    def approve_document(self, document_id: str) -> None:
        self.db.execute(
            "UPDATE documents SET needs_review = 0, updated_at = ? WHERE id = ?",
            (utc_now(), document_id),
        )

    def queue_command(
        self, agent_id: str, command: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.db.query_one("SELECT id FROM agents WHERE id = ? AND enabled = 1", (agent_id,)):
            raise ValueError("Unknown or disabled agent")
        command_id = str(uuid.uuid4())
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO commands(id, agent_id, command, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (command_id, agent_id, command, json.dumps(payload or {}), now),
        )
        return {
            "id": command_id,
            "agent_id": agent_id,
            "command": command,
            "payload": payload or {},
            "status": "pending",
            "created_at": now,
        }

    def pending_commands(self, agent_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM commands WHERE agent_id = ? AND status = 'pending' ORDER BY created_at",
            (agent_id,),
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def complete_command(self, agent_id: str, command_id: str, result: str, ok: bool) -> None:
        command = self.db.query_one(
            "SELECT * FROM commands WHERE id = ? AND agent_id = ?", (command_id, agent_id)
        )
        if not command:
            raise ValueError("Unknown command")
        self.db.execute(
            """
            UPDATE commands SET status = ?, result = ?, completed_at = ?
            WHERE id = ? AND agent_id = ?
            """,
            ("completed" if ok else "failed", result[:4000], utc_now(), command_id, agent_id),
        )
        payload = json.loads(command.get("payload_json") or "{}")
        document_id = str(payload.get("document_id", ""))
        if command["command"] == "write_note" and document_id:
            now = utc_now()
            self.db.execute(
                """
                UPDATE documents SET status = ?, error = ?, processed_at = ?, updated_at = ?,
                    comparison_status = ? WHERE id = ?
                """,
                (
                    "synced" if ok else "error",
                    "" if ok else result[:2000],
                    now if ok else None,
                    now,
                    "in-sync" if ok else "vault-missing",
                    document_id,
                ),
            )
            self.db.add_event(
                "document.synced" if ok else "document.error",
                (
                    f"Desktop vault wrote {payload.get('destination_path', '')}"
                    if ok
                    else f"Desktop vault write failed: {result[:500]}"
                ),
                level="info" if ok else "error",
                agent_id=agent_id,
                document_id=document_id,
            )
        elif command["command"] == "audit_vault" and ok:
            try:
                audit_rows = json.loads(result)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Vault audit returned invalid data") from exc
            if not isinstance(audit_rows, list):
                raise ValueError("Vault audit returned invalid data")
            now = utc_now()
            for row in audit_rows:
                if not isinstance(row, dict):
                    continue
                audit_document_id = str(row.get("document_id", ""))
                document = self.db.query_one(
                    "SELECT * FROM documents WHERE id = ?", (audit_document_id,)
                )
                if not document:
                    continue
                comparison = str(row.get("comparison_status", ""))
                if comparison not in {"in-sync", "modified", "new", "vault-missing"}:
                    continue
                destination = str(row.get("destination_path", ""))
                note_hash = str(row.get("note_hash", ""))
                values: list[Any] = [comparison, destination[:2000], now]
                source_hash_sql = ""
                if note_hash and not document["source_hash"]:
                    source_hash_sql = ", source_hash = ?, status = 'synced', processed_at = ?"
                    values.extend([note_hash, now])
                values.append(audit_document_id)
                self.db.execute(
                    f"""
                    UPDATE documents SET comparison_status = ?, destination_path = ?,
                        updated_at = ? {source_hash_sql} WHERE id = ?
                    """,
                    values,
                )
            self.db.add_event(
                "vault.audited",
                f"Compared {len(audit_rows)} files with the desktop Obsidian vault",
                agent_id=agent_id,
            )

    def overview(self) -> dict[str, Any]:
        stats = self.db.dashboard_stats()
        stats["online_agents"] = sum(1 for row in self.list_agents() if row["status"] == "online")
        stats["computers"] = stats["agents"] + 1
        stats["online_computers"] = stats["online_agents"] + 1
        events = self.db.query_all("SELECT * FROM events ORDER BY created_at DESC LIMIT 20")
        for event in events:
            event["details"] = json.loads(event.pop("details_json") or "{}")
        return {
            "stats": stats,
            "recent_events": events,
            "vault": self.vault_status(),
            "server": self.server_info(),
        }

    def settings_for_ui(self) -> dict[str, Any]:
        values = self.db.get_settings()
        result: dict[str, Any] = {
            "vault_path": str(self.settings.vault_path),
            "vault_host_path": self.settings.vault_host_path,
            "public_url": self.settings.public_url,
            "runtime": "docker"
            if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
            else "native",
            "max_upload_mb": self.settings.max_upload_mb,
        }
        for key, row in values.items():
            if row["is_secret"]:
                result[key] = "" if not row["value"] else "configured"
            else:
                result[key] = row["value"]
        return result

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed: dict[str, bool] = {
            "vault_mode": False,
            "vault_agent_id": False,
            "llm_enabled": False,
            "llm_provider": False,
            "llm_base_url": False,
            "llm_model": False,
            "llm_api_key": True,
            "llm_timeout_seconds": False,
            "review_threshold": False,
        }
        values: dict[str, tuple[str, bool]] = {}
        requested_mode = str(payload.get("vault_mode", "")).strip()
        if requested_mode and requested_mode not in {"local", "agent"}:
            raise ValueError("Vault mode must be local or agent")
        if requested_mode == "agent":
            requested_agent = str(payload.get("vault_agent_id", "")).strip()
            agent = self.db.query_one(
                "SELECT * FROM agents WHERE id = ? AND enabled = 1", (requested_agent,)
            )
            if not agent or not agent["vault_ready"] or not agent["vault_path"]:
                raise ValueError("Select a connected computer with an available Obsidian vault")
        for key, secret in allowed.items():
            if key not in payload:
                continue
            value = payload[key]
            if key == "llm_api_key" and value in {"", "configured"}:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            values[key] = (str(value).strip(), secret)
        if values:
            self.db.set_settings(values)
            self.db.add_event("settings.updated", "Obsync settings were updated")
        return self.settings_for_ui()

    async def test_llm(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self._llm_config()
        if overrides:
            if "llm_enabled" in overrides:
                config.enabled = bool(overrides["llm_enabled"])
            config.provider = str(overrides.get("llm_provider", config.provider))
            config.base_url = str(overrides.get("llm_base_url", config.base_url))
            config.model = str(overrides.get("llm_model", config.model))
            key = overrides.get("llm_api_key")
            if key and key != "configured":
                config.api_key = str(key)
            if "llm_timeout_seconds" in overrides:
                with suppress(TypeError, ValueError):
                    config.timeout_seconds = max(5, min(int(overrides["llm_timeout_seconds"]), 600))
        return await LLMAnalyzer(config).test_connection()
