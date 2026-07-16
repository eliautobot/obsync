from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import platform
import socket
import sqlite3
import tempfile
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from platformdirs import user_config_path, user_data_path
from watchfiles import Change, awatch

from . import __version__
from .desktop import choose_directory
from .markdown import (
    adopt_preserving_original,
    is_managed_note,
    likely_same_note_title,
    managed_note_metadata,
    merge_preserving_manual,
    note_tags,
    note_title,
    note_title_from_path,
    set_source_status,
)
from .security import new_token, safe_vault_path
from .vault_intelligence import content_hash, parse_note


def default_config_path() -> Path:
    override = os.getenv("OBSYNC_AGENT_CONFIG")
    return Path(override).expanduser() if override else user_config_path("Obsync") / "agent.yml"


@dataclass(slots=True)
class RootConfig:
    root_key: str
    name: str
    path: str
    destination: str = "Obsync"
    include_patterns: list[str] = field(default_factory=lambda: ["**/*"])
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            "**/.git/**",
            "**/.obsidian/**",
            "**/node_modules/**",
            "**/~$*",
            "**/*.tmp",
        ]
    )
    enabled: bool = True
    sync_state: str = "running"

    def __post_init__(self) -> None:
        if self.sync_state not in {"running", "paused", "stopped"}:
            self.sync_state = "running"


@dataclass(slots=True)
class AgentConfig:
    server_url: str = ""
    agent_id: str = ""
    agent_token: str = ""
    name: str = ""
    verify_tls: bool = True
    scan_interval_seconds: int = 300
    settle_seconds: float = 1.5
    vault_path: str = ""
    roots: list[RootConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> AgentConfig:
        path = path or default_config_path()
        if not path.exists():
            return cls(name=socket.gethostname())
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roots = [RootConfig(**item) for item in raw.pop("roots", [])]
        return cls(**raw, roots=roots)

    def save(self, path: Path | None = None) -> Path:
        path = path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = yaml.safe_dump(asdict(self), sort_keys=False, allow_unicode=True)
        path.write_text(content, encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def add_root(
        self,
        path: Path,
        *,
        name: str = "",
        destination: str = "Obsync",
    ) -> RootConfig:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Folder does not exist: {resolved}")
        for root in self.roots:
            if Path(root.path) == resolved:
                raise ValueError("That folder is already watched")
        root = RootConfig(
            root_key=str(uuid.uuid4()),
            name=name.strip() or resolved.name or "Folder",
            path=str(resolved),
            destination=destination,
        )
        self.roots.append(root)
        return root

    def remove_root(self, root_key: str) -> RootConfig | None:
        for index, root in enumerate(self.roots):
            if root.root_key == root_key:
                return self.roots.pop(index)
        return None

    def set_vault(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Vault folder does not exist: {resolved}")
        if resolved.name.casefold() == ".obsidian":
            raise ValueError(
                "Choose the Obsidian vault folder itself, not its hidden .obsidian settings folder"
            )
        if not (resolved / ".obsidian").is_dir():
            raise ValueError(
                "That folder does not look like an Obsidian vault. Choose the folder that "
                "contains .obsidian"
            )
        if not os.access(resolved, os.W_OK):
            raise ValueError(f"Vault folder is not writable: {resolved}")
        self.vault_path = str(resolved)
        return resolved


class AgentState:
    def __init__(self, path: Path | None = None):
        self.path = path or user_data_path("Obsync") / "agent-state.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    root_key TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    missing INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(root_key, relative_path)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_files_hash
                    ON files(root_key, sha256, missing);
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, root_key: str, relative_path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM files WHERE root_key = ? AND relative_path = ?",
                (root_key, relative_path),
            ).fetchone()
            return dict(row) if row else None

    def all_for_root(self, root_key: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM files WHERE root_key = ?", (root_key,)
                ).fetchall()
            ]

    def find_missing_by_hash(self, root_key: str, sha256: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM files WHERE root_key = ? AND sha256 = ? AND missing = 1 LIMIT 1
                """,
                (root_key, sha256),
            ).fetchone()
            return dict(row) if row else None

    def mark_synced(
        self, root_key: str, relative_path: str, mtime_ns: int, size: int, sha256: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO files(root_key, relative_path, mtime_ns, size, sha256, missing)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(root_key, relative_path) DO UPDATE SET
                    mtime_ns = excluded.mtime_ns,
                    size = excluded.size,
                    sha256 = excluded.sha256,
                    missing = 0,
                    synced_at = CURRENT_TIMESTAMP
                """,
                (root_key, relative_path, mtime_ns, size, sha256),
            )
            connection.commit()

    def rename(self, root_key: str, previous_path: str, new_path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM files WHERE root_key = ? AND relative_path = ?",
                (root_key, new_path),
            )
            connection.execute(
                """
                UPDATE files SET relative_path = ?, missing = 0, synced_at = CURRENT_TIMESTAMP
                WHERE root_key = ? AND relative_path = ?
                """,
                (new_path, root_key, previous_path),
            )
            connection.commit()

    def mark_missing(self, root_key: str, relative_path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE files SET missing = 1 WHERE root_key = ? AND relative_path = ?",
                (root_key, relative_path),
            )
            connection.commit()

    def remove_root(self, root_key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM files WHERE root_key = ?", (root_key,))
            connection.commit()


class SyncPausedError(RuntimeError):
    pass


class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        *,
        state: AgentState | None = None,
        client: httpx.AsyncClient | None = None,
        config_path: Path | None = None,
    ):
        if not config.server_url or not config.agent_token:
            raise ValueError("Agent is not paired. Run 'obsync agent pair' first.")
        self.config = config
        self.state = state or AgentState()
        self._external_client = client
        self.config_path = config_path or default_config_path()
        self._root_ids: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        self._server_sync_enabled = True

    def _client(self) -> httpx.AsyncClient:
        return self._external_client or httpx.AsyncClient(
            base_url=self.config.server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.config.agent_token}"},
            verify=self.config.verify_tls,
            timeout=httpx.Timeout(180, connect=20),
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        finally:
            if not self._external_client:
                await client.aclose()

    async def register_roots(self) -> None:
        for root in list(self.config.roots):
            if not root.enabled:
                continue
            response = await self._request("POST", "/api/v1/agent/roots", json=asdict(root))
            payload = response.json()
            if payload.get("removal_requested"):
                self._remove_local_root(root.root_key)
                continue
            self._root_ids[root.root_key] = payload["id"]

    @staticmethod
    def _root_is_running(root: RootConfig) -> bool:
        return root.enabled and root.sync_state == "running"

    def _remove_local_root(self, root_key: str) -> RootConfig | None:
        task = self._watch_tasks.pop(root_key, None)
        if task:
            task.cancel()
        self._root_ids.pop(root_key, None)
        removed = self.config.remove_root(root_key)
        self.state.remove_root(root_key)
        if removed:
            self.config.save(self.config_path)
        return removed

    def _require_sync_enabled(self) -> None:
        if not self._server_sync_enabled:
            raise SyncPausedError("Syncing was stopped by the user")

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _matches(root: RootConfig, relative: str) -> bool:
        normalized = relative.replace("\\", "/")

        def match(pattern: str) -> bool:
            return fnmatch.fnmatch(normalized, pattern) or (
                pattern.startswith("**/") and fnmatch.fnmatch(normalized, pattern[3:])
            )

        included = not root.include_patterns or any(
            match(pattern) for pattern in root.include_patterns
        )
        excluded = any(match(pattern) for pattern in root.exclude_patterns)
        return included and not excluded

    async def _stable_stat(self, path: Path) -> os.stat_result | None:
        try:
            first = path.stat()
            await asyncio.sleep(max(0.05, self.config.settle_seconds))
            second = path.stat()
        except (FileNotFoundError, PermissionError, OSError):
            return None
        if first.st_size != second.st_size or first.st_mtime_ns != second.st_mtime_ns:
            return None
        return second

    def _vault_duplicate_hint(self, root: RootConfig, source_path: str) -> tuple[str, str]:
        """Find a strong title match before a new watcher event can write remotely."""
        if not self.config.vault_path:
            return "", ""
        vault = Path(self.config.vault_path).expanduser().resolve()
        if not vault.is_dir():
            return "", ""
        source_title = note_title_from_path(Path(source_path))
        for candidate in vault.rglob("*.md"):
            if candidate.is_symlink() or not candidate.is_file() or ".obsidian" in candidate.parts:
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            metadata = managed_note_metadata(content)
            if metadata and (
                metadata.get("obsync_machine", "").casefold() == self.config.name.casefold()
                and metadata.get("obsync_root", "").casefold() == root.name.casefold()
                and metadata.get("obsync_source", "") == source_path
            ):
                continue
            title = note_title(content, candidate)
            if likely_same_note_title(source_title, title):
                return candidate.relative_to(vault).as_posix(), title
        return "", ""

    async def sync_file(
        self,
        root: RootConfig,
        path: Path,
        *,
        force: bool = False,
        review_feedback: str = "",
    ) -> dict[str, Any] | None:
        self._require_sync_enabled()
        if not self._root_is_running(root):
            raise SyncPausedError(f"Syncing is {root.sync_state} for {root.name}")
        root_path = Path(root.path).resolve()
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(root_path).as_posix()
        except (OSError, ValueError):
            return None
        if not resolved.is_file() or resolved.is_symlink() or not self._matches(root, relative):
            return None
        stat = await self._stable_stat(resolved)
        self._require_sync_enabled()
        if stat is None:
            return None
        previous = self.state.get(root.root_key, relative)
        unchanged_stat = (
            previous
            and not previous["missing"]
            and previous["mtime_ns"] == stat.st_mtime_ns
            and previous["size"] == stat.st_size
        )
        if unchanged_stat and not force:
            return {"result": "unchanged", "source_path": relative}

        sha256 = self._hash(resolved)
        if previous and not previous["missing"] and previous["sha256"] == sha256 and not force:
            self.state.mark_synced(root.root_key, relative, stat.st_mtime_ns, stat.st_size, sha256)
            return {"result": "metadata-only", "source_path": relative}
        renamed = self.state.find_missing_by_hash(root.root_key, sha256)
        previous_path = renamed["relative_path"] if renamed else ""
        duplicate_path = duplicate_title = ""
        if not previous and not renamed:
            duplicate_path, duplicate_title = self._vault_duplicate_hint(root, relative)
        root_id = self._root_ids[root.root_key]
        with resolved.open("rb") as handle:
            response = await self._request(
                "POST",
                "/api/v1/agent/documents/sync",
                data={
                    "root_id": root_id,
                    "source_path": relative,
                    "source_mtime_ns": str(stat.st_mtime_ns),
                    "source_size": str(stat.st_size),
                    "sha256": sha256,
                    "previous_path": previous_path,
                    "duplicate_path": duplicate_path,
                    "duplicate_title": duplicate_title,
                    "review_feedback": review_feedback[:4000],
                    "force_review": "true" if force else "false",
                },
                files={"file": (resolved.name, handle, "application/octet-stream")},
            )
        if renamed:
            self.state.rename(root.root_key, previous_path, relative)
        self.state.mark_synced(root.root_key, relative, stat.st_mtime_ns, stat.st_size, sha256)
        return response.json()

    async def mark_missing(self, root: RootConfig, relative: str) -> dict[str, Any]:
        self._require_sync_enabled()
        response = await self._request(
            "POST",
            "/api/v1/agent/documents/missing",
            json={"root_id": self._root_ids[root.root_key], "source_path": relative},
        )
        self.state.mark_missing(root.root_key, relative)
        return response.json()

    async def scan_root(self, root: RootConfig) -> dict[str, int]:
        self._require_sync_enabled()
        root_path = Path(root.path).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"Watched folder is unavailable: {root_path}")
        current: set[str] = set()
        synced = unchanged = errors = 0
        for path in root_path.rglob("*"):
            self._require_sync_enabled()
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root_path).as_posix()
            if not self._matches(root, relative):
                continue
            current.add(relative)
            try:
                result = await self.sync_file(root, path)
                if result and result.get("result") in {"synced", "marked-missing"}:
                    synced += 1
                else:
                    unchanged += 1
            except (httpx.HTTPError, OSError, ValueError):
                errors += 1
        for row in self.state.all_for_root(root.root_key):
            if not row["missing"] and row["relative_path"] not in current:
                try:
                    await self.mark_missing(root, row["relative_path"])
                except (httpx.HTTPError, ValueError):
                    errors += 1
        return {"synced": synced, "unchanged": unchanged, "errors": errors, "files": len(current)}

    async def inventory_root(self, root: RootConfig) -> dict[str, Any]:
        self._require_sync_enabled()
        if not self._root_is_running(root):
            raise SyncPausedError(f"Syncing is {root.sync_state} for {root.name}")
        root_path = Path(root.path).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"Watched folder is unavailable: {root_path}")
        if root.root_key not in self._root_ids:
            response = await self._request("POST", "/api/v1/agent/roots", json=asdict(root))
            self._root_ids[root.root_key] = response.json()["id"]
        scan_id = str(uuid.uuid4())
        batch: list[dict[str, Any]] = []
        files = errors = 0

        async def send_batch(*, complete: bool = False) -> dict[str, Any]:
            nonlocal batch
            response = await self._request(
                "POST",
                "/api/v1/agent/inventory",
                json={
                    "root_id": self._root_ids[root.root_key],
                    "scan_id": scan_id,
                    "items": batch,
                    "complete": complete,
                },
            )
            batch = []
            return response.json()

        for path in root_path.rglob("*"):
            self._require_sync_enabled()
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root_path).as_posix()
            if not self._matches(root, relative):
                continue
            try:
                before = path.stat()
                sha256 = self._hash(path)
                after = path.stat()
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    errors += 1
                    continue
                batch.append(
                    {
                        "source_path": relative,
                        "source_mtime_ns": after.st_mtime_ns,
                        "source_size": after.st_size,
                        "sha256": sha256,
                    }
                )
                files += 1
                if len(batch) >= 200:
                    await send_batch()
            except (OSError, ValueError):
                errors += 1
        result = await send_batch(complete=True)
        result.update({"files": files, "errors": errors})
        return result

    async def inventory_all(self) -> dict[str, dict[str, Any]]:
        await self.register_roots()
        results = {}
        for root in self.config.roots:
            if self._root_is_running(root):
                results[root.name] = await self.inventory_root(root)
        return results

    async def sync_pending_root(self, root: RootConfig) -> dict[str, int]:
        self._require_sync_enabled()
        if not self._root_is_running(root):
            raise SyncPausedError(f"Syncing is {root.sync_state} for {root.name}")
        if root.root_key not in self._root_ids:
            await self.register_roots()
        root_id = self._root_ids[root.root_key]
        response = await self._request("GET", f"/api/v1/agent/roots/{root_id}/pending")
        items = response.json()["items"]
        synced = missing = errors = 0
        for item in items:
            self._require_sync_enabled()
            if item["comparison_status"] == "source-missing":
                missing += 1
                continue
            try:
                result = await self.sync_file(
                    root, Path(root.path) / item["source_path"], force=True
                )
                if result:
                    synced += 1
            except (httpx.HTTPError, OSError, ValueError):
                errors += 1
        return {"synced": synced, "missing": missing, "errors": errors, "files": len(items)}

    async def sync_pending_all(self) -> dict[str, dict[str, int]]:
        await self.register_roots()
        results = {}
        for root in self.config.roots:
            if self._root_is_running(root):
                results[root.name] = await self.sync_pending_root(root)
        return results

    async def scan_all(self) -> dict[str, dict[str, int]]:
        await self.register_roots()
        results = {}
        for root in self.config.roots:
            if self._root_is_running(root):
                results[root.name] = await self.scan_root(root)
        return results

    async def heartbeat_once(self) -> dict[str, Any]:
        vault = Path(self.config.vault_path).expanduser() if self.config.vault_path else None
        vault_ready = bool(vault and vault.is_dir() and os.access(vault, os.W_OK))
        response = await self._request(
            "POST",
            "/api/v1/agent/heartbeat",
            json={
                "agent_version": __version__,
                "vault_path": str(vault) if vault else "",
                "vault_ready": vault_ready,
                "vault_error": "" if vault_ready or not vault else "Vault folder is unavailable",
            },
        )
        payload = response.json()
        self._server_sync_enabled = bool(payload.get("sync_enabled", True))
        return payload

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

    def _write_vault_note(self, payload: dict[str, Any]) -> str:
        if not self.config.vault_path:
            raise ValueError("No Obsidian vault is selected on this computer")
        destination = safe_vault_path(
            Path(self.config.vault_path), str(payload.get("destination_path", ""))
        )
        content = str(payload.get("content", ""))
        if not content:
            raise ValueError("Vault write has no Markdown content")
        if destination.exists():
            current = destination.read_text(encoding="utf-8")
            if not is_managed_note(current):
                if payload.get("allow_adopt"):
                    content = adopt_preserving_original(current, content)
                else:
                    raise ValueError(
                        "Destination already exists and is not managed: "
                        f"{payload['destination_path']}"
                    )
            else:
                content = merge_preserving_manual(current, content)
        self._atomic_write(destination, content)
        return str(destination)

    async def _index_vault(self, payload: dict[str, Any], command_id: str) -> str:
        if not self.config.vault_path:
            raise ValueError("No Obsidian vault is selected on this computer")
        vault = Path(self.config.vault_path).expanduser().resolve()
        sweep_id = str(payload.get("sweep_id", ""))
        paths = sorted(
            path
            for path in vault.rglob("*.md")
            if path.is_file() and not path.is_symlink() and ".obsidian" not in path.parts
        )
        batch: list[dict[str, Any]] = []
        indexed = 0
        for index, path in enumerate(paths, start=1):
            try:
                batch.append(parse_note(path, vault=vault).as_dict())
            except (OSError, UnicodeError, ValueError):
                continue
            if sweep_id and (len(batch) >= 25 or index == len(paths)):
                response = await self._request(
                    "POST",
                    f"/api/v1/agent/sweeps/{sweep_id}/notes",
                    json={"notes": batch},
                )
                result = response.json()
                indexed += int(result.get("accepted", 0))
                batch = []
                if result.get("stop_requested"):
                    raise SyncPausedError("Vault sweep stopped by user")
            if sweep_id and (index == 1 or index % 20 == 0 or index == len(paths)):
                response = await self._request(
                    "POST",
                    f"/api/v1/agent/sweeps/{sweep_id}/progress",
                    json={
                        "command_id": command_id,
                        "processed": index,
                        "total": len(paths),
                        "current_note": path.relative_to(vault).as_posix(),
                    },
                )
                if response.json().get("stop_requested"):
                    raise SyncPausedError("Vault sweep stopped by user")
            await asyncio.sleep(0)
        return json.dumps(
            {
                "sweep_id": sweep_id,
                "full_rebuild": bool(payload.get("full_rebuild")),
                "indexed": indexed,
            },
            ensure_ascii=False,
        )

    def _apply_vault_change(self, payload: dict[str, Any]) -> str:
        if not self.config.vault_path:
            raise ValueError("No Obsidian vault is selected on this computer")
        destination = safe_vault_path(Path(self.config.vault_path), str(payload.get("path", "")))
        if not destination.is_file():
            raise ValueError(f"Vault note is missing: {payload.get('path', '')}")
        current = destination.read_text(encoding="utf-8")
        if content_hash(current) != str(payload.get("expected_hash", "")):
            raise ValueError("The note changed after this recommendation was created")
        replacement = str(payload.get("content", ""))
        if not replacement:
            raise ValueError("Vault change contains no Markdown content")
        self._atomic_write(destination, replacement)
        return str(destination)

    def _set_remote_source_status(self, payload: dict[str, Any]) -> str:
        if not self.config.vault_path:
            raise ValueError("No Obsidian vault is selected on this computer")
        destination = safe_vault_path(
            Path(self.config.vault_path), str(payload.get("destination_path", ""))
        )
        if not destination.exists():
            raise ValueError(f"Managed note is missing: {payload['destination_path']}")
        current = destination.read_text(encoding="utf-8")
        if not is_managed_note(current):
            raise ValueError(
                f"Destination exists and is not managed: {payload['destination_path']}"
            )
        updated = set_source_status(
            current,
            str(payload.get("source_status", "source-missing")),
        )
        self._atomic_write(destination, updated)
        return str(destination)

    def _audit_vault(self, payload: dict[str, Any]) -> str:
        if not self.config.vault_path:
            raise ValueError("No Obsidian vault is selected on this computer")
        vault = Path(self.config.vault_path).expanduser().resolve()
        source_agent = str(payload.get("source_agent", "")).casefold()
        root_name = str(payload.get("root_name", "")).casefold()
        index: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
        catalog: list[dict[str, str]] = []
        for note in vault.rglob("*.md"):
            if note.is_symlink() or not note.is_file() or ".obsidian" in note.parts:
                continue
            try:
                content = note.read_text(encoding="utf-8")
                metadata = managed_note_metadata(content)
            except (OSError, UnicodeError):
                continue
            relative = note.relative_to(vault).as_posix()
            catalog.append(
                {
                    "path": relative,
                    "title": note_title(content, note),
                    "tags": note_tags(content),
                }
            )
            if not metadata:
                continue
            key = (
                metadata["obsync_machine"].casefold(),
                metadata["obsync_root"].casefold(),
                metadata["obsync_source"],
            )
            index.setdefault(key, (relative, metadata))

        results: list[dict[str, str]] = []
        for item in payload.get("documents", []):
            destination = str(item.get("destination_path", ""))
            metadata: dict[str, str] | None = None
            note_exists = False
            if destination:
                try:
                    note = safe_vault_path(vault, destination)
                    if note.is_file():
                        note_exists = True
                        metadata = managed_note_metadata(note.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    metadata = None
            else:
                match = index.get((source_agent, root_name, str(item.get("source_path", ""))))
                if match:
                    destination, metadata = match
            if not metadata:
                duplicate = None
                if payload.get("duplicate_policy", "review") == "review" and not item.get(
                    "duplicate_dismissed"
                ):
                    source_title = note_title_from_path(Path(str(item.get("source_path", ""))))
                    duplicate = next(
                        (
                            candidate
                            for candidate in catalog
                            if candidate["path"] != destination
                            and likely_same_note_title(source_title, candidate["title"])
                        ),
                        None,
                    )
                state = (
                    "modified"
                    if note_exists
                    else "vault-missing"
                    if destination
                    else "possible-duplicate"
                    if duplicate
                    else "new"
                )
                note_hash = ""
            else:
                duplicate = None
                note_hash = metadata.get("obsync_hash", "")
                state = (
                    "in-sync"
                    if note_hash and note_hash == str(item.get("observed_hash", ""))
                    else "modified"
                )
            results.append(
                {
                    "document_id": str(item.get("id", "")),
                    "comparison_status": state,
                    "destination_path": destination,
                    "note_hash": note_hash,
                    "duplicate_path": duplicate["path"] if duplicate else "",
                    "duplicate_title": duplicate["title"] if duplicate else "",
                }
            )
        return json.dumps({"documents": results, "notes": catalog[:5000]}, ensure_ascii=False)

    def _ensure_watch_task(self, root: RootConfig) -> None:
        existing = self._watch_tasks.get(root.root_key)
        if existing and existing.done():
            self._watch_tasks.pop(root.root_key, None)
            existing = None
        if not self._running or not self._root_is_running(root) or existing or self._stop.is_set():
            return
        self._watch_tasks[root.root_key] = asyncio.create_task(self._watch_root(root))

    async def process_commands_once(self) -> None:
        response = await self._request("GET", "/api/v1/agent/commands")
        payload = response.json()
        self._server_sync_enabled = bool(payload.get("sync_enabled", self._server_sync_enabled))
        for command in payload["items"]:
            ok = True
            result = ""
            try:
                if command["command"] in {
                    "scan",
                    "sync",
                    "reconcile",
                    "scan_root",
                    "sync_root",
                    "resync",
                    "write_note",
                    "set_source_status",
                    "audit_vault",
                    "select_source",
                }:
                    self._require_sync_enabled()
                if command["command"] == "scan":
                    result = json.dumps(await self.inventory_all())
                elif command["command"] == "sync":
                    result = json.dumps(await self.sync_pending_all())
                elif command["command"] == "reconcile":
                    result = json.dumps(
                        {
                            "inventory": await self.inventory_all(),
                            "sync": await self.sync_pending_all(),
                        }
                    )
                elif command["command"] == "scan_root":
                    payload = command.get("payload", {})
                    root = next(r for r in self.config.roots if r.root_key == payload["root_key"])
                    result = json.dumps(await self.inventory_root(root))
                elif command["command"] == "sync_root":
                    payload = command.get("payload", {})
                    root = next(r for r in self.config.roots if r.root_key == payload["root_key"])
                    result = json.dumps(await self.sync_pending_root(root))
                elif command["command"] == "resync":
                    payload = command.get("payload", {})
                    root = next(r for r in self.config.roots if r.root_key == payload["root_key"])
                    path = Path(root.path) / payload["source_path"]
                    result = json.dumps(
                        await self.sync_file(
                            root,
                            path,
                            force=True,
                            review_feedback=str(payload.get("review_feedback", "")),
                        )
                    )
                elif command["command"] == "write_note":
                    result = self._write_vault_note(command.get("payload", {}))
                elif command["command"] == "set_source_status":
                    result = self._set_remote_source_status(command.get("payload", {}))
                elif command["command"] == "audit_vault":
                    result = self._audit_vault(command.get("payload", {}))
                elif command["command"] == "index_vault":
                    result = await self._index_vault(
                        command.get("payload", {}), str(command.get("id", ""))
                    )
                elif command["command"] == "apply_vault_change":
                    result = self._apply_vault_change(command.get("payload", {}))
                elif command["command"] == "select_vault":
                    selected = choose_directory(
                        "Choose your Obsidian vault", self.config.vault_path
                    )
                    result = str(self.config.set_vault(selected))
                    self.config.save(self.config_path)
                    await self.heartbeat_once()
                elif command["command"] == "select_source":
                    payload = command.get("payload", {})
                    selected = choose_directory("Choose a folder for Obsync to sync")
                    root = self.config.add_root(
                        selected,
                        name=str(payload.get("name", "")),
                        destination=str(payload.get("destination", "Obsync")),
                    )
                    self.config.save(self.config_path)
                    response = await self._request("POST", "/api/v1/agent/roots", json=asdict(root))
                    self._root_ids[root.root_key] = response.json()["id"]
                    inventory = await self.inventory_root(root)
                    self._ensure_watch_task(root)
                    result = json.dumps({"root": asdict(root), "inventory": inventory})
                elif command["command"] == "remove_root":
                    payload = command.get("payload", {})
                    removed = self._remove_local_root(str(payload.get("root_key", "")))
                    result = json.dumps(
                        {
                            "removed": bool(removed),
                            "root_key": str(payload.get("root_key", "")),
                        }
                    )
                elif command["command"] == "set_root_state":
                    payload = command.get("payload", {})
                    root = next(r for r in self.config.roots if r.root_key == payload["root_key"])
                    requested = str(payload.get("sync_state", ""))
                    if requested not in {"running", "paused", "stopped"}:
                        raise ValueError("Folder state must be running, paused, or stopped")
                    root.sync_state = requested
                    root.enabled = True
                    if requested == "stopped":
                        task = self._watch_tasks.pop(root.root_key, None)
                        if task:
                            task.cancel()
                    elif requested == "running":
                        self._ensure_watch_task(root)
                    self.config.save(self.config_path)
                    response = await self._request("POST", "/api/v1/agent/roots", json=asdict(root))
                    self._root_ids[root.root_key] = response.json()["id"]
                    reconciliation: dict[str, Any] = {}
                    if requested == "running" and self._server_sync_enabled:
                        reconciliation["inventory"] = await self.inventory_root(root)
                        reconciliation["sync"] = await self.sync_pending_root(root)
                    result = json.dumps(
                        {
                            "root_key": root.root_key,
                            "sync_state": root.sync_state,
                            **reconciliation,
                        }
                    )
                else:
                    raise ValueError(f"Unknown command: {command['command']}")
            except Exception as exc:
                ok = False
                result = str(exc)
            await self._request(
                "POST",
                f"/api/v1/agent/commands/{command['id']}/complete",
                json={"ok": ok, "result": result},
            )

    async def _watch_root(self, root: RootConfig) -> None:
        root_path = Path(root.path).expanduser().resolve()
        while not self._stop.is_set():
            try:
                async for changes in awatch(root_path, stop_event=self._stop):
                    for change, changed_path in changes:
                        if not self._server_sync_enabled or not self._root_is_running(root):
                            continue
                        path = Path(changed_path)
                        if change == Change.deleted:
                            try:
                                relative = path.relative_to(root_path).as_posix()
                            except ValueError:
                                continue
                            if self.state.get(root.root_key, relative):
                                await self.mark_missing(root, relative)
                        else:
                            await self.sync_file(root, path)
            except (FileNotFoundError, OSError, httpx.HTTPError, SyncPausedError):
                await asyncio.sleep(10)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.heartbeat_once()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    self._stop.set()
                    return
            except httpx.HTTPError:
                pass
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    async def _command_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_commands_once()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    self._stop.set()
                    return
            except httpx.HTTPError:
                pass
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1)

    async def _reconcile_loop(self) -> None:
        first_pass = True
        while not self._stop.is_set():
            if self._server_sync_enabled:
                with suppress(httpx.HTTPError, OSError, ValueError, SyncPausedError):
                    await self.inventory_all()
                    if not first_pass:
                        await self.sync_pending_all()
                    first_pass = False
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(30, self.config.scan_interval_seconds)
                )

    async def run_forever(self) -> None:
        self._running = True
        await self.heartbeat_once()
        await self.register_roots()
        tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._command_loop()),
            asyncio.create_task(self._reconcile_loop()),
        ]
        for root in self.config.roots:
            if self._root_is_running(root):
                self._ensure_watch_task(root)
        try:
            await asyncio.gather(*tasks)
        finally:
            self._running = False
            self._stop.set()
            for task in [*tasks, *self._watch_tasks.values()]:
                task.cancel()


async def pair_agent(
    *,
    server_url: str,
    code: str,
    name: str = "",
    verify_tls: bool = True,
) -> AgentConfig:
    proposed_token = new_token("agent")
    payload = {
        "code": code.upper(),
        "name": name or socket.gethostname(),
        "hostname": socket.gethostname(),
        "os_name": platform.system(),
        "os_version": platform.platform(),
        "agent_version": __version__,
        # Client-generated credentials make a registration retry idempotent. The
        # server stores only the hash and returns this secret only to this client.
        "agent_token": proposed_token,
    }
    async with httpx.AsyncClient(
        base_url=server_url.rstrip("/"), verify=verify_tls, timeout=30
    ) as client:
        response = None
        for attempt in range(2):
            try:
                response = await client.post("/api/v1/agents/register", json=payload)
                break
            except httpx.TransportError:
                if attempt:
                    raise
                await asyncio.sleep(0.35)
        assert response is not None
        if not response.is_success:
            try:
                detail = str(response.json().get("detail", ""))
            except (ValueError, AttributeError):
                detail = ""
            raise ValueError(
                detail or f"The Obsync server rejected pairing ({response.status_code})"
            )
        result = response.json()
    return AgentConfig(
        server_url=server_url.rstrip("/"),
        agent_id=result["agent_id"],
        agent_token=result["agent_token"],
        name=result["name"],
        verify_tls=verify_tls,
    )
