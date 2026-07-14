from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import platform
import socket
import sqlite3
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


@dataclass(slots=True)
class AgentConfig:
    server_url: str = ""
    agent_id: str = ""
    agent_token: str = ""
    name: str = ""
    verify_tls: bool = True
    scan_interval_seconds: int = 300
    settle_seconds: float = 1.5
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


class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        *,
        state: AgentState | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if not config.server_url or not config.agent_token:
            raise ValueError("Agent is not paired. Run 'obsync agent pair' first.")
        self.config = config
        self.state = state or AgentState()
        self._external_client = client
        self._root_ids: dict[str, str] = {}
        self._stop = asyncio.Event()

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
        for root in self.config.roots:
            if not root.enabled:
                continue
            response = await self._request("POST", "/api/v1/agent/roots", json=asdict(root))
            self._root_ids[root.root_key] = response.json()["id"]

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

    async def sync_file(self, root: RootConfig, path: Path) -> dict[str, Any] | None:
        root_path = Path(root.path).resolve()
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(root_path).as_posix()
        except (OSError, ValueError):
            return None
        if not resolved.is_file() or resolved.is_symlink() or not self._matches(root, relative):
            return None
        stat = await self._stable_stat(resolved)
        if stat is None:
            return None
        previous = self.state.get(root.root_key, relative)
        unchanged_stat = (
            previous
            and not previous["missing"]
            and previous["mtime_ns"] == stat.st_mtime_ns
            and previous["size"] == stat.st_size
        )
        if unchanged_stat:
            return {"result": "unchanged", "source_path": relative}

        sha256 = self._hash(resolved)
        if previous and not previous["missing"] and previous["sha256"] == sha256:
            self.state.mark_synced(root.root_key, relative, stat.st_mtime_ns, stat.st_size, sha256)
            return {"result": "metadata-only", "source_path": relative}
        renamed = self.state.find_missing_by_hash(root.root_key, sha256)
        previous_path = renamed["relative_path"] if renamed else ""
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
                },
                files={"file": (resolved.name, handle, "application/octet-stream")},
            )
        if renamed:
            self.state.rename(root.root_key, previous_path, relative)
        self.state.mark_synced(root.root_key, relative, stat.st_mtime_ns, stat.st_size, sha256)
        return response.json()

    async def mark_missing(self, root: RootConfig, relative: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v1/agent/documents/missing",
            json={"root_id": self._root_ids[root.root_key], "source_path": relative},
        )
        self.state.mark_missing(root.root_key, relative)
        return response.json()

    async def scan_root(self, root: RootConfig) -> dict[str, int]:
        root_path = Path(root.path).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"Watched folder is unavailable: {root_path}")
        current: set[str] = set()
        synced = unchanged = errors = 0
        for path in root_path.rglob("*"):
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

    async def scan_all(self) -> dict[str, dict[str, int]]:
        await self.register_roots()
        results = {}
        for root in self.config.roots:
            if root.enabled:
                results[root.name] = await self.scan_root(root)
        return results

    async def heartbeat_once(self) -> None:
        await self._request("POST", "/api/v1/agent/heartbeat", json={"agent_version": __version__})

    async def process_commands_once(self) -> None:
        response = await self._request("GET", "/api/v1/agent/commands")
        for command in response.json()["items"]:
            ok = True
            result = ""
            try:
                if command["command"] == "scan":
                    result = json.dumps(await self.scan_all())
                elif command["command"] == "resync":
                    payload = command.get("payload", {})
                    root = next(r for r in self.config.roots if r.root_key == payload["root_key"])
                    path = Path(root.path) / payload["source_path"]
                    result = json.dumps(await self.sync_file(root, path))
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
            except (FileNotFoundError, OSError, httpx.HTTPError):
                await asyncio.sleep(10)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.heartbeat_once()
                await self.process_commands_once()
            except httpx.HTTPError:
                pass
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=30)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            with suppress(httpx.HTTPError, OSError, ValueError):
                await self.scan_all()
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(30, self.config.scan_interval_seconds)
                )

    async def run_forever(self) -> None:
        await self.register_roots()
        tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._reconcile_loop()),
            *[
                asyncio.create_task(self._watch_root(root))
                for root in self.config.roots
                if root.enabled
            ],
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()


async def pair_agent(
    *,
    server_url: str,
    code: str,
    name: str = "",
    verify_tls: bool = True,
) -> AgentConfig:
    payload = {
        "code": code.upper(),
        "name": name or socket.gethostname(),
        "hostname": socket.gethostname(),
        "os_name": platform.system(),
        "os_version": platform.platform(),
        "agent_version": __version__,
    }
    async with httpx.AsyncClient(
        base_url=server_url.rstrip("/"), verify=verify_tls, timeout=30
    ) as client:
        response = await client.post("/api/v1/agents/register", json=payload)
        response.raise_for_status()
        result = response.json()
    return AgentConfig(
        server_url=server_url.rstrip("/"),
        agent_id=result["agent_id"],
        agent_token=result["agent_token"],
        name=result["name"],
        verify_tls=verify_tls,
    )
