from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database, utc_now
from .extractors import extract_document
from .llm import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    MAX_LLM_TIMEOUT_SECONDS,
    MIN_LLM_TIMEOUT_SECONDS,
    Analysis,
    LLMAnalyzer,
    LLMConfig,
)
from .markdown import (
    adopt_preserving_original,
    is_managed_note,
    likely_same_note_title,
    managed_note_metadata,
    merge_preserving_manual,
    note_title_from_path,
    render_markdown,
    set_source_status,
)
from .profiles import (
    BUILTIN_PROFILE_MAP,
    BUILTIN_PROFILES,
    FULL_TRANSFER_PROFILE,
    PROMPT_PLACEHOLDERS,
    PROTECTED_SYSTEM_PROMPT,
    AIProfile,
    validate_profile,
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
from .vault_intelligence import (
    AdaptiveVaultIndex,
    add_backlinks,
    add_index_membership,
    apply_native_operations,
    change_native_tag,
    content_hash,
    decode_db_note,
    exact_duplicate_groups,
    existing_note_match,
    explicit_category_hub_relationships,
    explicit_reciprocal_relationships,
    explicit_reference_relationships,
    link_target,
    native_maintenance_content,
    normalize_obsidian_tag,
    note_links_to,
    parse_note,
    reapply_owned_operations,
    serialize_note_for_db,
    strip_maintenance_block,
)

DEFAULT_SETTINGS: dict[str, tuple[str, bool]] = {
    "sync_enabled": ("false", False),
    "vault_confirmed": ("false", False),
    "vault_mode": ("local", False),
    "vault_agent_id": ("", False),
    "llm_enabled": ("false", False),
    "llm_provider": ("ollama", False),
    "llm_base_url": ("", False),
    "llm_model": ("", False),
    "llm_api_key": ("", True),
    "llm_timeout_seconds": (str(DEFAULT_LLM_TIMEOUT_SECONDS), False),
    "llm_instructions": ("", False),
    "llm_vault_context": ("true", False),
    "ai_active_profile_id": ("", False),
    "duplicate_policy": ("review", False),
    "review_threshold": ("0.65", False),
    "existing_note_policy": ("review", False),
    "vault_index_schedule_enabled": ("false", False),
    "vault_index_schedule_frequency": ("weekly", False),
    "vault_index_schedule_time": ("02:00", False),
    "vault_index_schedule_weekday": ("6", False),
    "vault_index_schedule_month_day": ("1", False),
    "vault_index_schedule_interval_hours": ("24", False),
    "vault_index_change_mode": ("index-only", False),
    "vault_maintenance_schedule_enabled": ("false", False),
    "vault_maintenance_schedule_frequency": ("weekly", False),
    "vault_maintenance_schedule_time": ("03:00", False),
    "vault_maintenance_schedule_weekday": ("6", False),
    "vault_maintenance_schedule_month_day": ("1", False),
    "vault_maintenance_schedule_interval_hours": ("168", False),
    "vault_maintenance_change_mode": ("review", False),
    "vault_schedule_timezone": ("America/New_York", False),
    "vault_maintenance_categories": ('["links", "tags", "organization"]', False),
    "vault_relationship_candidate_limit": ("20", False),
    "vault_relationship_min_confidence": ("0.72", False),
    "vault_link_limit": ("8", False),
    "vault_metadata_version": ("2", False),
}

PIPELINE_COMMANDS = frozenset(
    {
        "scan",
        "sync",
        "scan_root",
        "sync_root",
        "resync",
        "write_note",
        "set_source_status",
        "audit_vault",
        "select_source",
        "reconcile",
    }
)


class LoginRateLimitedError(ValueError):
    pass


class PipelinePausedError(RuntimeError):
    pass


class ObsyncService:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.settings.prepare()
        self.db = db or Database(settings.database_path)
        self.db.initialize()
        self._ensure_defaults()
        self._recover_interrupted_sweeps()
        self._repair_stored_vault_metadata()
        self._ensure_ai_profiles()
        self._inventory_vault_indexes: dict[
            str,
            tuple[
                dict[tuple[str, str, str], dict[str, str]],
                list[dict[str, str]],
            ],
        ] = {}
        self._active_processing: dict[str, tuple[str, asyncio.Task[Any]]] = {}
        self._processing_activity: dict[str, dict[str, Any]] = {}
        self._last_inference: dict[str, Any] | None = None
        self._sweep_inference_activity: dict[str, dict[str, Any]] = {}
        self._last_sweep_inference: dict[str, dict[str, Any]] = {}
        self._ai_activity_revision = 0
        self._ai_activity_subscribers: set[asyncio.Queue[int]] = set()
        self._cancel_reasons: dict[str, str] = {}
        self._sweep_task: asyncio.Task[Any] | None = None
        self._sweep_ai_task: asyncio.Task[Any] | None = None
        self._sweep_stop = asyncio.Event()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._dummy_password_hash = hash_password("obsync-invalid-password")
        self._bootstrap_admin_from_env()

    def _ensure_defaults(self) -> None:
        existing = self.db.get_settings()
        missing = {key: value for key, value in DEFAULT_SETTINGS.items() if key not in existing}
        # Upgrades must stop before the first write until the user explicitly
        # confirms the destination vault in the new Vault screen.
        if "vault_confirmed" not in existing:
            missing["sync_enabled"] = ("false", False)
        if missing:
            self.db.set_settings(missing)

    def _recover_interrupted_sweeps(self) -> None:
        """Close persisted work that cannot still have a task after process startup."""
        now = utc_now()
        self.db.execute(
            "UPDATE vault_sweeps SET status = 'stopped', current_note = '', finished_at = ?, "
            "updated_at = ?, error = CASE WHEN error = '' THEN "
            "'Interrupted by a server restart before completion.' ELSE error END "
            "WHERE status IN ('queued', 'running', 'stopping')",
            (now, now),
        )
        self.db.execute(
            "UPDATE vault_models SET status = 'not-learned', error = "
            "'Interrupted by a server restart before completion.', updated_at = ? "
            "WHERE status = 'learning'",
            (now,),
        )

    def _ensure_ai_profiles(self) -> None:
        active_id = self.db.get_setting("ai_active_profile_id", "")
        if active_id and self._profile_by_id(active_id):
            return
        legacy_instructions = self.db.get_setting("llm_instructions", "").strip()
        legacy_vault_context = self.db.get_setting("llm_vault_context", "true") == "true"
        if legacy_instructions or not legacy_vault_context:
            profile = FULL_TRANSFER_PROFILE.custom_copy(
                profile_id=str(uuid.uuid4()), name="Imported AI settings"
            )
            if legacy_instructions:
                profile.role_prompt = (
                    f"{profile.role_prompt}\n\nImported organization preferences:\n"
                    f"{legacy_instructions[:8000]}"
                )
            profile.use_vault_context = legacy_vault_context
            profile.use_wikilinks = legacy_vault_context
            self._insert_custom_profile(profile)
        # Upgrades preserve legacy preferences as a custom profile, but the
        # complete-transfer behavior becomes active so existing installations
        # do not keep producing the brief notes this release is correcting.
        active_id = FULL_TRANSFER_PROFILE.id
        self.db.set_settings({"ai_active_profile_id": (active_id, False)})

    def _insert_custom_profile(self, profile: AIProfile) -> None:
        now = utc_now()
        stored = profile.as_dict()
        stored.update({"builtin": False, "created_at": now, "updated_at": now})
        self.db.execute(
            """
            INSERT INTO ai_profiles(id, name, description, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                profile.id,
                profile.name,
                profile.description,
                json.dumps(stored, ensure_ascii=False),
                now,
                now,
            ),
        )

    @staticmethod
    def _profile_from_row(row: dict[str, Any]) -> AIProfile:
        try:
            payload = json.loads(row.get("config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload.update({"name": row["name"], "description": row["description"]})
        return validate_profile(
            payload,
            profile_id=str(row["id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _profile_by_id(self, profile_id: str) -> AIProfile | None:
        builtin = BUILTIN_PROFILE_MAP.get(profile_id)
        if builtin:
            return builtin
        row = self.db.query_one("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,))
        return self._profile_from_row(row) if row else None

    def active_ai_profile(self) -> AIProfile:
        profile_id = self.db.get_setting("ai_active_profile_id", FULL_TRANSFER_PROFILE.id)
        return self._profile_by_id(profile_id) or FULL_TRANSFER_PROFILE

    def ai_profiles_for_ui(self) -> dict[str, Any]:
        custom = [
            self._profile_from_row(row)
            for row in self.db.query_all("SELECT * FROM ai_profiles ORDER BY name COLLATE NOCASE")
        ]
        active = self.active_ai_profile()
        return {
            "active_profile_id": active.id,
            "items": [profile.as_dict() for profile in (*BUILTIN_PROFILES, *custom)],
            "protected_system_prompt": PROTECTED_SYSTEM_PROMPT,
            "prompt_placeholders": list(PROMPT_PLACEHOLDERS),
            "implementation": (
                "Obsync maintains a whole-vault index of Markdown content, headings, aliases, "
                "properties, tags, links, backlinks, folders, and record entities through the "
                "server mount or Obsync Desktop. The model proposes organization; Obsync "
                "validates paths, links, hashes, and writes. No Obsidian plugin is required."
            ),
        }

    def _unique_profile_name(self, preferred: str, *, exclude_id: str = "") -> str:
        base = preferred.strip()[:80] or "Custom AI profile"
        names = {profile.name.casefold() for profile in BUILTIN_PROFILES}
        names.update(
            str(row["name"]).casefold()
            for row in self.db.query_all(
                "SELECT name FROM ai_profiles WHERE id != ?", (exclude_id,)
            )
        )
        if base.casefold() not in names:
            return base
        suffix = 2
        while True:
            addition = f" {suffix}"
            candidate = f"{base[: 80 - len(addition)]}{addition}"
            if candidate.casefold() not in names:
                return candidate
            suffix += 1

    def create_ai_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_id = str(payload.get("source_profile_id", "")).strip()
        source = self._profile_by_id(source_id) if source_id else self.active_ai_profile()
        if not source:
            raise ValueError("The source AI profile does not exist")
        requested_name = str(payload.get("name", "")).strip() or f"{source.name} copy"
        name = self._unique_profile_name(requested_name)
        profile = source.custom_copy(profile_id=str(uuid.uuid4()), name=name)
        self._insert_custom_profile(profile)
        self.db.add_event("ai.profile_created", f"Created AI profile {name}")
        return profile.as_dict()

    def update_ai_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if profile_id in BUILTIN_PROFILE_MAP:
            raise ValueError("Built-in AI profiles cannot be edited; copy it to a custom profile")
        row = self.db.query_one("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,))
        if not row:
            raise ValueError("AI profile not found")
        existing = self._profile_from_row(row).as_dict()
        existing.update(payload)
        requested_name = str(existing.get("name", "")).strip()
        existing["name"] = self._unique_profile_name(requested_name, exclude_id=profile_id)
        now = utc_now()
        profile = validate_profile(
            existing,
            profile_id=profile_id,
            created_at=str(row["created_at"]),
            updated_at=now,
        )
        self.db.execute(
            """
            UPDATE ai_profiles SET name = ?, description = ?, config_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                profile.name,
                profile.description,
                json.dumps(profile.as_dict(), ensure_ascii=False),
                now,
                profile_id,
            ),
        )
        self.db.add_event("ai.profile_updated", f"Updated AI profile {profile.name}")
        return profile.as_dict()

    def activate_ai_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self._profile_by_id(profile_id)
        if not profile:
            raise ValueError("AI profile not found")
        self.db.set_settings({"ai_active_profile_id": (profile.id, False)})
        self.db.add_event("ai.profile_activated", f"Activated AI profile {profile.name}")
        return self.ai_profiles_for_ui()

    def delete_ai_profile(self, profile_id: str) -> dict[str, Any]:
        if profile_id in BUILTIN_PROFILE_MAP:
            raise ValueError("Built-in AI profiles cannot be deleted")
        row = self.db.query_one("SELECT name FROM ai_profiles WHERE id = ?", (profile_id,))
        if not row:
            raise ValueError("AI profile not found")
        self.db.execute("DELETE FROM ai_profiles WHERE id = ?", (profile_id,))
        if self.db.get_setting("ai_active_profile_id", "") == profile_id:
            self.db.set_settings({"ai_active_profile_id": (FULL_TRANSFER_PROFILE.id, False)})
        self.db.add_event("ai.profile_deleted", f"Deleted AI profile {row['name']}")
        return self.ai_profiles_for_ui()

    def _vault_key(self) -> str:
        if self.db.get_setting("vault_mode", "local") == "agent":
            return self.db.get_setting("vault_agent_id", "")
        return "local"

    def _indexed_vault_notes(self, vault_key: str | None = None) -> list[dict[str, Any]]:
        key = vault_key if vault_key is not None else self._vault_key()
        return [
            decode_db_note(row)
            for row in self.db.query_all(
                "SELECT * FROM vault_notes WHERE vault_key = ? ORDER BY path COLLATE NOCASE",
                (key,),
            )
        ]

    def _reparse_indexed_notes(self, vault_key: str) -> list[dict[str, Any]]:
        """Derive metadata from current stored Markdown instead of trusting legacy JSON columns."""
        rows = self._indexed_vault_notes(vault_key)
        reparsed: list[dict[str, Any]] = []
        for row in rows:
            note = parse_note(
                Path(str(row.get("path", ""))),
                content=str(row.get("content", "")),
                modified_ns=int(row.get("modified_ns", 0)),
            ).as_dict()
            note["size"] = int(row.get("size", note["size"]))
            note["content_hash"] = content_hash(str(row.get("content", "")))
            reparsed.append(note)
        add_backlinks(reparsed)
        owned_tags: dict[str, set[str]] = {}
        for owned in self.db.query_all(
            "SELECT path, target FROM vault_edit_ownership WHERE vault_key = ? "
            "AND kind = 'frontmatter-tag' AND status = 'active'",
            (vault_key,),
        ):
            owned_tags.setdefault(str(owned["path"]), set()).add(str(owned["target"]).casefold())
        for note in reparsed:
            generated = owned_tags.get(str(note.get("path", "")), set())
            note["human_tags"] = [
                tag for tag in note.get("tags", []) if str(tag).casefold() not in generated
            ]
            note["learning_properties"] = {
                **dict(note.get("properties", {})),
                "tags": list(note["human_tags"]),
            }
            learning_content = str(note.get("content", ""))
            for tag in generated:
                learning_content, _removed = change_native_tag(
                    learning_content, tag=tag, remove=True
                )
            note["learning_content"] = learning_content
        return reparsed

    def _repair_stored_vault_metadata(self) -> None:
        if self.db.get_setting("vault_metadata_version", "2") == "2":
            return
        keys = [
            str(row["vault_key"])
            for row in self.db.query_all("SELECT DISTINCT vault_key FROM vault_notes")
        ]
        for vault_key in keys:
            notes = self._reparse_indexed_notes(vault_key)
            self._store_vault_index(vault_key, notes, full_rebuild=True)
        self.db.set_settings({"vault_metadata_version": ("2", False)})

    def _store_vault_index(
        self,
        vault_key: str,
        notes: list[dict[str, Any]],
        *,
        full_rebuild: bool,
        delete_missing: bool = True,
    ) -> dict[str, int]:
        add_backlinks(notes)
        now = utc_now()
        before_rows = {
            str(row["path"]): str(row["content_hash"])
            for row in self.db.query_all(
                "SELECT path, content_hash FROM vault_notes WHERE vault_key = ?", (vault_key,)
            )
        }
        with self.db.transaction() as connection:
            if full_rebuild:
                connection.execute("DELETE FROM vault_notes WHERE vault_key = ?", (vault_key,))
            elif notes and delete_missing:
                current_paths = {str(note.get("path", "")) for note in notes if note.get("path")}
                missing_paths = [path for path in before_rows if path not in current_paths]
                connection.executemany(
                    "DELETE FROM vault_notes WHERE vault_key = ? AND path = ?",
                    [(vault_key, path) for path in missing_paths],
                )
            elif not full_rebuild and delete_missing:
                connection.execute("DELETE FROM vault_notes WHERE vault_key = ?", (vault_key,))
            connection.executemany(
                """
                INSERT INTO vault_notes(
                    vault_key, path, title, tags_json, aliases_json, headings_json, links_json,
                    backlinks_json, properties_json, entities_json, content, content_hash,
                    modified_ns, size, managed, indexed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vault_key, path) DO UPDATE SET
                    title = excluded.title, tags_json = excluded.tags_json,
                    aliases_json = excluded.aliases_json, headings_json = excluded.headings_json,
                    links_json = excluded.links_json, backlinks_json = excluded.backlinks_json,
                    properties_json = excluded.properties_json,
                    entities_json = excluded.entities_json, content = excluded.content,
                    content_hash = excluded.content_hash, modified_ns = excluded.modified_ns,
                    size = excluded.size, managed = excluded.managed,
                    indexed_at = excluded.indexed_at, updated_at = excluded.updated_at
                """,
                [
                    (vault_key, *serialize_note_for_db(note), now, now)
                    for note in notes
                    if str(note.get("path", "")).strip()
                ],
            )
        previous = len(before_rows)
        after_hashes = {
            str(note.get("path", "")): str(note.get("content_hash", "")) for note in notes
        }
        changed = sum(1 for path, sha256 in after_hashes.items() if before_rows.get(path) != sha256)
        if delete_missing:
            changed += sum(1 for path in before_rows if path not in after_hashes)
        return {"notes": len(notes), "previous": previous, "changed": changed}

    def _upsert_indexed_note(self, vault_key: str, note: dict[str, Any]) -> None:
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO vault_notes(
                vault_key, path, title, tags_json, aliases_json, headings_json, links_json,
                backlinks_json, properties_json, entities_json, content, content_hash,
                modified_ns, size, managed, indexed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vault_key, path) DO UPDATE SET
                title = excluded.title, tags_json = excluded.tags_json,
                aliases_json = excluded.aliases_json, headings_json = excluded.headings_json,
                links_json = excluded.links_json, properties_json = excluded.properties_json,
                entities_json = excluded.entities_json, content = excluded.content,
                content_hash = excluded.content_hash, modified_ns = excluded.modified_ns,
                size = excluded.size, managed = excluded.managed,
                indexed_at = excluded.indexed_at, updated_at = excluded.updated_at
            """,
            (vault_key, *serialize_note_for_db(note), now, now),
        )
        notes = self._indexed_vault_notes(vault_key)
        add_backlinks(notes)
        with self.db.transaction() as connection:
            connection.executemany(
                "UPDATE vault_notes SET backlinks_json = ? WHERE vault_key = ? AND path = ?",
                [
                    (
                        json.dumps(item.get("backlinks", []), ensure_ascii=False),
                        vault_key,
                        item["path"],
                    )
                    for item in notes
                ],
            )

    async def _scan_local_vault(self, sweep_id: str, *, full_rebuild: bool) -> bool:
        vault = self.settings.vault_path
        paths = sorted(
            path
            for path in vault.rglob("*.md")
            if path.is_file() and not path.is_symlink() and ".obsidian" not in path.parts
        )
        self.db.execute(
            "UPDATE vault_sweeps SET total_notes = ?, updated_at = ? WHERE id = ?",
            (len(paths), utc_now(), sweep_id),
        )
        notes: list[dict[str, Any]] = []
        for index, path in enumerate(paths, start=1):
            if self._sweep_stop.is_set():
                break
            try:
                note = await asyncio.to_thread(parse_note, path, vault=vault)
            except (OSError, UnicodeError, ValueError) as exc:
                self.db.add_event(
                    "vault.index_note_error",
                    f"Could not index {path.name}: {exc}",
                    level="warning",
                )
                continue
            notes.append(note.as_dict())
            if index == 1 or index % 10 == 0 or index == len(paths):
                self.db.execute(
                    """
                    UPDATE vault_sweeps SET processed_notes = ?, current_note = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (index, note.path, utc_now(), sweep_id),
                )
            await asyncio.sleep(0)
        stopped = self._sweep_stop.is_set()
        if not stopped or (notes and not full_rebuild):
            result = self._store_vault_index(
                "local",
                notes,
                full_rebuild=full_rebuild and not stopped,
                delete_missing=not stopped,
            )
            self.db.execute(
                "UPDATE vault_sweeps SET changed_notes = ?, updated_at = ? WHERE id = ?",
                (result["changed"], utc_now(), sweep_id),
            )
        return not stopped

    async def _wait_for_command(self, command_id: str, sweep_id: str) -> bool:
        while True:
            command = self.db.query_one(
                "SELECT status, result FROM commands WHERE id = ?", (command_id,)
            )
            if not command:
                raise ValueError("Vault sweep command disappeared")
            if command["status"] == "completed":
                return True
            if command["status"] in {"failed", "cancelled"}:
                if self._sweep_stop.is_set() or command["status"] == "cancelled":
                    return False
                raise ValueError(str(command.get("result") or "Desktop vault sweep failed"))
            if self._sweep_stop.is_set():
                self.db.execute(
                    "UPDATE commands SET status = 'cancelled', result = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    ("Vault sweep stopped by user", utc_now(), command_id),
                )
            await asyncio.sleep(0.25)
            sweep = self.db.query_one("SELECT status FROM vault_sweeps WHERE id = ?", (sweep_id,))
            if not sweep or sweep["status"] == "stopped":
                return False

    async def _scan_remote_vault(self, sweep_id: str, *, full_rebuild: bool) -> bool:
        agent = self._remote_vault_agent()
        if not agent:
            raise ValueError("The selected desktop vault is unavailable")
        command = self.queue_command(
            agent["id"],
            "index_vault",
            {"sweep_id": sweep_id, "full_rebuild": full_rebuild},
        )
        return await self._wait_for_command(command["id"], sweep_id)

    def _create_vault_change(
        self,
        *,
        sweep_id: str,
        note: dict[str, Any],
        after_content: str,
        reason: str,
        evidence: list[str],
        confidence: float,
        decision: dict[str, Any] | None = None,
        change_type: str = "native-maintenance",
    ) -> str:
        change_id = str(uuid.uuid4())
        before_content = str(note.get("content", ""))
        self.db.execute(
            """
            INSERT INTO vault_changes(
                id, sweep_id, vault_key, path, change_type, status, before_hash, after_hash,
                before_content, after_content, reason, evidence_json, confidence, created_at
                , decision_json
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_id,
                sweep_id,
                self._vault_key(),
                note["path"],
                change_type,
                str(note.get("content_hash") or content_hash(before_content)),
                content_hash(after_content),
                before_content,
                after_content,
                reason[:1000],
                json.dumps(evidence[:30], ensure_ascii=False),
                max(0.0, min(float(confidence), 1.0)),
                utc_now(),
                json.dumps(decision or {}, ensure_ascii=False),
            ),
        )
        return change_id

    def _relationship_feedback(self, *, maximum: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT path, status, decision_json FROM vault_changes "
            "WHERE status IN ('applied', 'rejected') AND decision_json != '{}' "
            "ORDER BY reviewed_at DESC LIMIT ?",
            (max(1, min(maximum, 500)),),
        )
        feedback: list[dict[str, Any]] = []
        for row in rows:
            try:
                decision = json.loads(row.get("decision_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(decision, dict):
                continue
            feedback.append(
                {
                    "source_path": str(row.get("path", "")),
                    "outcome": str(row.get("status", "")),
                    "relationships": list(decision.get("relationships", []))[:20],
                    "suggested_tags": list(decision.get("suggested_tags", []))[:20],
                    "operations": list(decision.get("operations", []))[:30],
                    "review": decision.get("review", {}),
                }
            )
        return feedback

    def _vault_model_fingerprint(
        self,
        notes: list[dict[str, Any]],
        feedback: list[dict[str, Any]],
        config: LLMConfig,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(config.provider.encode())
        digest.update(b"\0")
        digest.update(config.model.encode())
        digest.update(b"\0")
        digest.update(config.active_profile.id.encode())
        for note in sorted(notes, key=lambda item: str(item.get("path", "")).casefold()):
            digest.update(str(note.get("path", "")).encode())
            digest.update(b"\0")
            knowledge_hash = content_hash(
                strip_maintenance_block(str(note.get("learning_content", note.get("content", ""))))
            )
            digest.update(knowledge_hash.encode())
        digest.update(json.dumps(feedback, sort_keys=True, ensure_ascii=False).encode())
        return digest.hexdigest()

    def vault_model_status(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM vault_models WHERE vault_key = ?", (self._vault_key(),)
        )
        if not row:
            return {
                "status": "not-learned",
                "model": {},
                "note_count": 0,
                "learned_at": "",
                "provider": "",
                "model_name": "",
                "error": "",
                "corpus": {},
                "profiled_at": "",
            }
        try:
            row["model"] = json.loads(row.pop("model_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            row["model"] = {}
        try:
            row["corpus"] = json.loads(row.pop("corpus_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            row["corpus"] = {}
        return row

    def _refresh_vault_corpus_profile(
        self,
        notes: list[dict[str, Any]],
        *,
        noncanonical_paths: set[str] | None = None,
    ) -> tuple[AdaptiveVaultIndex, dict[str, Any]]:
        adaptive = AdaptiveVaultIndex(notes, noncanonical_paths=noncanonical_paths)
        corpus = adaptive.corpus_profile()
        serialized = json.dumps(corpus, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO vault_models(
                vault_key, status, model_json, corpus_json, corpus_fingerprint,
                note_count, profiled_at, updated_at
            ) VALUES (?, 'not-learned', '{}', ?, ?, ?, ?, ?)
            ON CONFLICT(vault_key) DO UPDATE SET corpus_json = excluded.corpus_json,
                corpus_fingerprint = excluded.corpus_fingerprint,
                note_count = excluded.note_count, profiled_at = excluded.profiled_at,
                updated_at = excluded.updated_at
            """,
            (self._vault_key(), serialized, fingerprint, len(notes), now, now),
        )
        return adaptive, corpus

    def _owned_operations(
        self,
        path: str,
        *,
        content: str | None = None,
        vault_key: str | None = None,
    ) -> list[dict[str, Any]]:
        key = vault_key or self._vault_key()
        rows = self.db.query_all(
            "SELECT * FROM vault_edit_ownership WHERE vault_key = ? AND path = ? "
            "AND status = 'active' ORDER BY created_at, id",
            (key, path),
        )
        active: list[dict[str, Any]] = []
        tags = (
            {value.casefold() for value in parse_note(Path(path), content=content).tags}
            if content is not None
            else set()
        )
        for row in rows:
            row["key"] = str(row.get("operation_key", ""))
            row["tag"] = str(row.get("target", "")) if row.get("kind") == "frontmatter-tag" else ""
            present = True
            if content is not None and row.get("kind") == "inline-link":
                present = bool(row.get("rendered") and str(row["rendered"]) in content)
            elif content is not None and row.get("kind") == "frontmatter-tag":
                present = str(row.get("target", "")).casefold() in tags
            if not present:
                self.db.execute(
                    "UPDATE vault_edit_ownership SET status = 'modified', updated_at = ? "
                    "WHERE id = ? AND status = 'active'",
                    (utc_now(), row["id"]),
                )
                continue
            active.append(row)
        return active

    @staticmethod
    def _owned_removals(
        decision: dict[str, Any], owned_operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        link_targets = {
            str(item.get("target", "")).casefold()
            for item in decision.get("obsolete_owned_links", [])
        }
        tags = {
            str(item.get("tag", "")).casefold() for item in decision.get("obsolete_owned_tags", [])
        }
        return [
            item
            for item in owned_operations
            if (
                item.get("kind") == "inline-link"
                and str(item.get("target", "")).casefold() in link_targets
            )
            or (
                item.get("kind") == "frontmatter-tag"
                and str(item.get("tag", "")).casefold() in tags
            )
        ]

    def _update_edit_ownership(self, change: dict[str, Any], *, reverse: bool = False) -> None:
        try:
            decision = json.loads(change.get("decision_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return
        operations = decision.get("operations", []) if isinstance(decision, dict) else []
        if not isinstance(operations, list):
            return
        now = utc_now()
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("kind") not in {
                "inline-link",
                "frontmatter-tag",
            }:
                continue
            action = str(operation.get("action", ""))
            if reverse:
                action = "remove" if action == "add" else "add" if action == "remove" else action
            kind = str(operation["kind"])
            operation_key = str(operation.get("key", "")).casefold()
            target = str(
                operation.get("target", "")
                if kind == "inline-link"
                else operation.get("tag") or operation.get("key", "")
            )
            if not operation_key or not target:
                continue
            if action == "add":
                self.db.execute(
                    """
                    INSERT INTO vault_edit_ownership(
                        id, vault_key, path, kind, operation_key, target, anchor, rendered,
                        status, source_change_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    ON CONFLICT(vault_key, path, kind, operation_key) DO UPDATE SET
                        target = excluded.target, anchor = excluded.anchor,
                        rendered = excluded.rendered, status = 'active',
                        source_change_id = excluded.source_change_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        change["vault_key"],
                        change["path"],
                        kind,
                        operation_key,
                        target,
                        str(operation.get("anchor", "")),
                        str(operation.get("rendered", target)),
                        change["id"],
                        now,
                        now,
                    ),
                )
            elif action == "remove":
                self.db.execute(
                    "UPDATE vault_edit_ownership SET status = 'removed', "
                    "source_change_id = ?, updated_at = ? WHERE vault_key = ? AND path = ? "
                    "AND kind = ? AND operation_key = ? AND status IN ('active', 'modified')",
                    (
                        change["id"],
                        now,
                        change["vault_key"],
                        change["path"],
                        kind,
                        operation_key,
                    ),
                )

    async def _learn_vault_model(
        self,
        notes: list[dict[str, Any]],
        sweep_id: str,
        *,
        corpus_profile: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        config = self._llm_config()
        if not config.active:
            raise ValueError(
                "Maintenance Sweep requires Local AI. Enable and test a model in Local AI settings."
            )
        feedback = self._relationship_feedback()
        fingerprint = self._vault_model_fingerprint(notes, feedback, config)
        current = self.db.query_one(
            "SELECT * FROM vault_models WHERE vault_key = ?", (self._vault_key(),)
        )
        if (
            current
            and current.get("status") == "ready"
            and current.get("fingerprint") == fingerprint
            and current.get("provider") == config.provider
            and current.get("model_name") == config.model
        ):
            try:
                cached = json.loads(current.get("model_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                cached = {}
            if cached:
                self._update_sweep_inference(
                    sweep_id,
                    "stage",
                    "Using the current adaptive vault model because the indexed knowledge and "
                    "review feedback have not changed.",
                    phase="learning",
                    phase_label="Loading adaptive vault model",
                )
                return cached, feedback
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO vault_models(
                vault_key, status, model_json, fingerprint, provider, model_name,
                note_count, learned_at, error, updated_at
            ) VALUES (?, 'learning', '{}', ?, ?, ?, ?, NULL, '', ?)
            ON CONFLICT(vault_key) DO UPDATE SET status = 'learning',
                fingerprint = excluded.fingerprint,
                provider = excluded.provider, model_name = excluded.model_name,
                note_count = excluded.note_count, error = '', updated_at = excluded.updated_at
            """,
            (self._vault_key(), fingerprint, config.provider, config.model, len(notes), now),
        )
        self.db.execute(
            "UPDATE vault_sweeps SET current_note = 'Learning adaptive vault model', "
            "updated_at = ? "
            "WHERE id = ?",
            (now, sweep_id),
        )
        try:
            analyzer = LLMAnalyzer(
                config,
                progress=lambda kind, message: self._update_sweep_inference(
                    sweep_id,
                    kind,
                    message,
                    phase="learning",
                    phase_label="Learning adaptive vault model",
                ),
            )
            ai_task = asyncio.create_task(
                analyzer.learn_vault_model(notes, feedback=feedback, corpus_profile=corpus_profile)
            )
            self._sweep_ai_task = ai_task
            try:
                model = await ai_task
            finally:
                if self._sweep_ai_task is ai_task:
                    self._sweep_ai_task = None
            self._update_sweep_inference(
                sweep_id,
                "decision",
                f"Learned the vault model from {len(notes)} indexed notes.",
                phase="learning",
                phase_label="Adaptive vault model ready",
            )
        except asyncio.CancelledError:
            self.db.execute(
                "UPDATE vault_models SET status = 'not-learned', error = ?, updated_at = ? "
                "WHERE vault_key = ?",
                ("Learning stopped by user", utc_now(), self._vault_key()),
            )
            raise
        except Exception as exc:
            self._update_sweep_inference(
                sweep_id,
                "error",
                str(exc).strip() or type(exc).__name__,
                phase="failed",
                phase_label="Vault model learning failed",
            )
            self.db.execute(
                "UPDATE vault_models SET status = 'failed', error = ?, updated_at = ? "
                "WHERE vault_key = ?",
                (str(exc)[:2000], utc_now(), self._vault_key()),
            )
            raise
        learned_at = utc_now()
        self.db.execute(
            "UPDATE vault_models SET status = 'ready', model_json = ?, learned_at = ?, "
            "error = '', updated_at = ? WHERE vault_key = ?",
            (json.dumps(model, ensure_ascii=False), learned_at, learned_at, self._vault_key()),
        )
        return model, feedback

    def _resolved_duplicate_paths(self) -> set[str]:
        return {
            str(row["duplicate_path"])
            for row in self.db.query_all(
                "SELECT duplicate_path FROM vault_duplicate_resolutions "
                "WHERE vault_key = ? AND status = 'active'",
                (self._vault_key(),),
            )
        }

    def _create_duplicate_findings(self, sweep_id: str, notes: list[dict[str, Any]]) -> list[str]:
        """Create review-only canonical selections before relationship inference."""
        resolved = {path.casefold() for path in self._resolved_duplicate_paths()}
        change_ids: list[str] = []
        for group in exact_duplicate_groups(notes):
            canonical = group[0]
            canonical_path = str(canonical.get("path", ""))
            for duplicate in group[1:]:
                duplicate_path = str(duplicate.get("path", ""))
                if not duplicate_path or duplicate_path.casefold() in resolved:
                    continue
                reason = (
                    f"Exact duplicate content detected. Use {canonical_path} as the canonical "
                    f"knowledge record and exclude {duplicate_path} from relationship retrieval."
                )
                operation = {
                    "operation_id": str(uuid.uuid4()),
                    "action": "select",
                    "kind": "canonical-selection",
                    "key": duplicate_path.casefold(),
                    "canonical_path": canonical_path,
                    "duplicate_path": duplicate_path,
                    "reason": reason,
                    "confidence": 1.0,
                }
                decision = {
                    "summary": reason,
                    "relationships": [],
                    "suggested_tags": [],
                    "operations": [operation],
                }
                change_ids.append(
                    self._create_vault_change(
                        sweep_id=sweep_id,
                        note=duplicate,
                        after_content=str(duplicate.get("content", "")),
                        reason=reason,
                        evidence=[
                            f"Both notes have the same SHA-256 after legacy maintenance cleanup: "
                            f"{canonical_path} and {duplicate_path}"
                        ],
                        confidence=1.0,
                        decision=decision,
                        change_type="duplicate-finding",
                    )
                )
        return change_ids

    async def _generate_maintenance_changes(self, sweep_id: str) -> list[str]:
        notes = self._reparse_indexed_notes(self._vault_key())
        if notes:
            self._store_vault_index(
                self._vault_key(), notes, full_rebuild=False, delete_missing=False
            )
        if not notes:
            self.db.execute(
                "UPDATE vault_sweeps SET recommendations = 0, updated_at = ? WHERE id = ?",
                (utc_now(), sweep_id),
            )
            return []
        sweep = self.db.query_one("SELECT sweep_type FROM vault_sweeps WHERE id = ?", (sweep_id,))
        sweep_type = str(sweep.get("sweep_type") if sweep else "maintenance")
        self._start_sweep_inference(sweep_id, sweep_type)
        try:
            maximum = max(1, min(int(self.db.get_setting("vault_link_limit", "8")), 20))
            candidate_limit = max(
                5,
                min(
                    int(self.db.get_setting("vault_relationship_candidate_limit", "20")),
                    50,
                ),
            )
            minimum_confidence = max(
                0.5,
                min(
                    float(self.db.get_setting("vault_relationship_min_confidence", "0.72")),
                    1.0,
                ),
            )
        except ValueError:
            maximum, candidate_limit, minimum_confidence = 8, 20, 0.72
        categories_raw = self.db.get_setting("vault_maintenance_categories", "[]")
        try:
            categories = set(json.loads(categories_raw))
        except (TypeError, json.JSONDecodeError):
            categories = {"links", "tags", "organization"}
        change_ids = self._create_duplicate_findings(sweep_id, notes)
        total = len(notes)
        noncanonical_paths = self._resolved_duplicate_paths() | {
            str(group_item.get("path", ""))
            for group in exact_duplicate_groups(notes)
            for group_item in group[1:]
        }
        search_index, corpus_profile = self._refresh_vault_corpus_profile(
            notes, noncanonical_paths=noncanonical_paths
        )
        vault_model, feedback = await self._learn_vault_model(
            notes, sweep_id, corpus_profile=corpus_profile
        )
        analyzer = LLMAnalyzer(
            self._llm_config(),
            progress=lambda kind, message: self._update_sweep_inference(
                sweep_id,
                kind,
                message,
                phase="relationships",
                phase_label="Analyzing note relationships",
            ),
        )
        failures = 0
        failure_messages: list[str] = []
        attempted = 0
        for index, note in enumerate(notes, start=1):
            if self._sweep_stop.is_set():
                break
            self.db.execute(
                "UPDATE vault_sweeps SET total_notes = ?, processed_notes = ?, current_note = ?, "
                "updated_at = ? WHERE id = ?",
                (total, index, note["path"], utc_now(), sweep_id),
            )
            self._update_sweep_inference(
                sweep_id,
                "stage",
                f"Analyzing note {index} of {total}: {note['path']}",
                phase="relationships",
                phase_label=f"Analyzing note {index} of {total}",
                current_note=str(note["path"]),
            )
            if str(note.get("path", "")) in noncanonical_paths:
                continue
            if not ({"links", "tags", "organization"} & categories):
                continue
            candidates = await asyncio.to_thread(
                search_index.candidates,
                str(note.get("path", "")),
                strip_maintenance_block(str(note.get("content", ""))),
                exclude_path=str(note.get("path", "")),
                maximum=min(50, candidate_limit * 3),
                source_note=note,
            )
            candidates = [
                candidate
                for candidate in candidates
                if candidate.get("anchor_options")
                or candidate.get("already_linked")
                or candidate.get("structural_role") == "category-hub"
            ][:candidate_limit]
            owned_operations = self._owned_operations(
                str(note.get("path", "")), content=str(note.get("content", ""))
            )
            attempted += 1
            try:
                ai_task = asyncio.create_task(
                    analyzer.adjudicate_relationships(
                        note,
                        candidates,
                        vault_model=vault_model,
                        minimum_confidence=minimum_confidence,
                        maximum_links=maximum,
                        feedback=feedback,
                        owned_operations=owned_operations,
                        tag_vocabulary=search_index.tag_vocabulary_for(note),
                        allowed_folders=[
                            path for path, _count in search_index.folder_counts.most_common(150)
                        ],
                    )
                )
                self._sweep_ai_task = ai_task
                try:
                    decision = await ai_task
                finally:
                    if self._sweep_ai_task is ai_task:
                        self._sweep_ai_task = None
            except Exception as exc:
                failures += 1
                failure_message = str(exc).strip() or type(exc).__name__
                failure_messages.append(failure_message)
                self._update_sweep_inference(
                    sweep_id,
                    "error",
                    f"Skipped {note['path']}: {failure_message}",
                    phase="relationships",
                    phase_label=f"Analyzing note {index} of {total}",
                    current_note=str(note["path"]),
                )
                self.db.add_event(
                    "vault.relationship_decision_failed",
                    "Skipped "
                    f"{note['path']}; Local AI relationship decision failed: {failure_message}",
                    level="error",
                    details={"sweep_id": sweep_id, "path": note["path"]},
                )
                continue
            relationships = decision.get("relationships", []) if "links" in categories else []
            if "links" in categories:
                existing_targets = {
                    str(relationship.get("target", "")).casefold() for relationship in relationships
                }
                for explicit in [
                    *explicit_reciprocal_relationships(note, candidates),
                    *explicit_category_hub_relationships(note, candidates),
                    *explicit_reference_relationships(note, candidates),
                ]:
                    key = str(explicit.get("target", "")).casefold()
                    if key and key not in existing_targets:
                        relationships.append(explicit)
                        existing_targets.add(key)
            tag_decisions = decision.get("suggested_tags", []) if "tags" in categories else []
            allowed_tag_keys = {
                normalize_obsidian_tag(tag).casefold()
                for tag in search_index.tag_counts
                if normalize_obsidian_tag(tag)
            }
            tag_decisions = [
                {**item, "tag": normalize_obsidian_tag(item.get("tag", ""))}
                for item in tag_decisions
                if isinstance(item, dict)
                and normalize_obsidian_tag(item.get("tag", "")).casefold() in allowed_tag_keys
                and normalize_obsidian_tag(item.get("tag", "")).casefold() != "obsync"
            ]
            tags = [item["tag"] for item in tag_decisions]
            decision["suggested_tags"] = tag_decisions
            organization = (
                decision.get("organization_operations", []) if "organization" in categories else []
            )
            if note.get("managed") or note.get("backlinks"):
                # Raw filesystem moves do not receive Obsidian's interactive link rewrite.
                # Keep managed notes and backlink targets stable instead of proposing a move
                # that could disconnect sources or path-qualified wikilinks.
                organization = []
            memberships = (
                decision.get("index_memberships", []) if "organization" in categories else []
            )
            decision["organization_operations"] = organization
            decision["index_memberships"] = memberships
            if "links" not in categories:
                decision["obsolete_owned_links"] = []
            if "tags" not in categories:
                decision["obsolete_owned_tags"] = []
            owned_removals = self._owned_removals(decision, owned_operations)
            self._update_sweep_inference(
                sweep_id,
                "decision",
                f"{note['path']}: {len(relationships)} evidence-backed relationship(s), "
                f"{len(tags)} suggested tag(s), "
                f"{len(organization) + len(memberships)} organization proposal(s). "
                f"{str(decision.get('summary') or '').strip()}",
                phase="relationships",
                phase_label=f"Analyzing note {index} of {total}",
                current_note=str(note["path"]),
            )
            after, operations = native_maintenance_content(
                str(note.get("content", "")),
                relationships,
                suggested_tags=tags,
                owned_removals=owned_removals,
            )
            if operations:
                # A separate move card would retain the pre-edit hash and become stale as soon
                # as the native-maintenance card is applied (or vice versa). A later sweep can
                # reconsider placement after content maintenance has settled.
                organization = []
                decision["organization_operations"] = []
            relationship_by_target = {
                str(item.get("target", "")).split("|", 1)[0].removesuffix(".md").casefold(): item
                for item in relationships
            }
            tag_by_key = {item["tag"].casefold(): item for item in tag_decisions}
            cleanup_items = [
                item
                for key in ("obsolete_owned_links", "obsolete_owned_tags")
                for item in decision.get(key, [])
            ]
            for operation in operations:
                operation["operation_id"] = str(uuid.uuid4())
                metadata: dict[str, Any] = {}
                if operation.get("kind") == "inline-link" and operation.get("action") == "add":
                    metadata = relationship_by_target.get(
                        str(operation.get("target", "")).casefold(), {}
                    )
                elif (
                    operation.get("kind") == "frontmatter-tag" and operation.get("action") == "add"
                ):
                    metadata = tag_by_key.get(str(operation.get("tag", "")).casefold(), {})
                elif operation.get("action") == "remove":
                    key = str(operation.get("target") or operation.get("tag") or "").casefold()
                    metadata = next(
                        (
                            item
                            for item in cleanup_items
                            if str(item.get("target") or item.get("tag") or "").casefold() == key
                        ),
                        {},
                    )
                operation["reason"] = str(
                    metadata.get("relationship") or metadata.get("reason") or ""
                )
                operation["evidence"] = list(metadata.get("evidence", []))
                operation["confidence"] = float(metadata.get("confidence", 1.0))
                for graph_field in ("source_entity", "target_entity", "predicate"):
                    if metadata.get(graph_field):
                        operation[graph_field] = str(metadata[graph_field])
            actual_link_keys = {
                str(operation.get("target", "")).casefold()
                for operation in operations
                if operation.get("kind") == "inline-link" and operation.get("action") == "add"
            }
            relationships = [
                item
                for item in relationships
                if str(item.get("target", "")).split("|", 1)[0].removesuffix(".md").casefold()
                in actual_link_keys
            ]
            actual_tag_keys = {
                str(operation.get("tag", "")).casefold()
                for operation in operations
                if operation.get("kind") == "frontmatter-tag" and operation.get("action") == "add"
            }
            tag_decisions = [
                item for item in tag_decisions if item["tag"].casefold() in actual_tag_keys
            ]
            decision["relationships"] = relationships
            decision["suggested_tags"] = tag_decisions
            decision["operations"] = operations
            if after != note.get("content"):
                evidence = [
                    *(
                        f"{relationship['target']}: {item}"
                        for relationship in relationships
                        for item in relationship.get("evidence", [])
                    ),
                    *(
                        f"#{item['tag']}: {fact}"
                        for item in tag_decisions
                        for fact in item.get("evidence", [])
                    ),
                ][:30]
                confidence = min(
                    (float(operation.get("confidence", 1.0)) for operation in operations),
                    default=1.0,
                )
                added_links = sum(
                    operation.get("kind") == "inline-link" and operation.get("action") == "add"
                    for operation in operations
                )
                added_tags = sum(
                    operation.get("kind") == "frontmatter-tag" and operation.get("action") == "add"
                    for operation in operations
                )
                removed = sum(operation.get("action") == "remove" for operation in operations)
                reason = (
                    f"Native Obsidian maintenance proposes {added_links} inline link(s), "
                    f"{added_tags} frontmatter tag(s), and {removed} cleanup operation(s)."
                )
                change_ids.append(
                    self._create_vault_change(
                        sweep_id=sweep_id,
                        note=note,
                        after_content=after,
                        reason=reason,
                        evidence=evidence,
                        confidence=confidence,
                        decision=decision,
                    )
                )

            existing_paths = {str(item.get("path", "")).casefold() for item in notes}
            for move in organization[:1]:
                destination = (
                    Path(str(move.get("destination_folder", ""))) / Path(str(note["path"])).name
                ).as_posix()
                if not destination or destination.casefold() in existing_paths:
                    continue
                operation = {
                    **move,
                    "operation_id": str(uuid.uuid4()),
                    "action": "move",
                    "kind": "move-note",
                    "key": str(note["path"]).casefold(),
                    "from_path": str(note["path"]),
                    "to_path": destination,
                }
                move_decision = {
                    "summary": str(move.get("reason", "")),
                    "relationships": [],
                    "suggested_tags": [],
                    "operations": [operation],
                }
                change_ids.append(
                    self._create_vault_change(
                        sweep_id=sweep_id,
                        note=note,
                        after_content=str(note.get("content", "")),
                        reason=(
                            f"Review-only organization proposes moving this note to {destination}."
                        ),
                        evidence=list(move.get("evidence", [])),
                        confidence=float(move.get("confidence", 0.0)),
                        decision=move_decision,
                        change_type="move-note",
                    )
                )

            candidates_by_target = {
                str(candidate.get("link_target", "")).casefold(): candidate
                for candidate in candidates
            }
            for membership in memberships:
                hub = candidates_by_target.get(str(membership.get("target", "")).casefold())
                if not hub or note_links_to(hub, note):
                    continue
                hub_after, operation = add_index_membership(
                    str(hub.get("content", "")), source_target=link_target(note)
                )
                if not operation:
                    continue
                operation.update(
                    {
                        "operation_id": str(uuid.uuid4()),
                        "source_target": link_target(note),
                        "reason": str(membership.get("reason", "")),
                        "evidence": list(membership.get("evidence", [])),
                        "confidence": float(membership.get("confidence", 0.0)),
                    }
                )
                membership_decision = {
                    "summary": str(membership.get("reason", "")),
                    "relationships": [],
                    "suggested_tags": [],
                    "operations": [operation],
                }
                change_ids.append(
                    self._create_vault_change(
                        sweep_id=sweep_id,
                        note=hub,
                        after_content=hub_after,
                        reason=(
                            "Review-only organization proposes adding "
                            f"{note['path']} to this index."
                        ),
                        evidence=list(membership.get("evidence", [])),
                        confidence=float(membership.get("confidence", 0.0)),
                        decision=membership_decision,
                        change_type="index-membership",
                    )
                )
            await asyncio.sleep(0)
        if attempted and failures == attempted:
            detail = failure_messages[-1] if failure_messages else "Unknown Local AI error"
            raise ValueError(
                "Local AI failed every relationship decision; no vault changes were generated. "
                f"Last error: {detail}"
            )
        self.db.execute(
            "UPDATE vault_sweeps SET recommendations = ?, updated_at = ? WHERE id = ?",
            (len(change_ids), utc_now(), sweep_id),
        )
        self._update_sweep_inference(
            sweep_id,
            "decision",
            f"AI analysis finished with {len(change_ids)} recommendation(s) for Review or "
            "automatic handling, according to the selected sweep mode.",
            phase="complete",
            phase_label="AI analysis complete",
        )
        return change_ids

    async def _apply_change_content(
        self,
        change: dict[str, Any],
        *,
        reverse: bool = False,
    ) -> None:
        if str(change.get("change_type", "")) == "duplicate-finding":
            return
        expected_hash = str(change["after_hash"] if reverse else change["before_hash"])
        new_content = str(change["before_content"] if reverse else change["after_content"])
        vault_key = str(change["vault_key"])
        decision = json.loads(change.get("decision_json") or "{}")
        operations = decision.get("operations", []) if isinstance(decision, dict) else []
        move = next(
            (
                operation
                for operation in operations
                if isinstance(operation, dict) and operation.get("kind") == "move-note"
            ),
            None,
        )
        source_path = str(change["path"])
        move_to = ""
        if move:
            source_path = str(move.get("to_path" if reverse else "from_path", ""))
            move_to = str(move.get("from_path" if reverse else "to_path", ""))
        if vault_key == "local":
            path = safe_vault_path(self.settings.vault_path, source_path)
            if not path.is_file():
                raise ValueError(f"Vault note is missing: {source_path}")
            current = path.read_text(encoding="utf-8")
            if content_hash(current) != expected_hash:
                raise ValueError("The note changed after this recommendation was created")
            if move_to:
                destination = safe_vault_path(self.settings.vault_path, move_to)
                if not destination.parent.is_dir():
                    raise ValueError("The proposed destination folder no longer exists")
                if destination.exists():
                    raise ValueError("The proposed destination note already exists")
                path.replace(destination)
                now = utc_now()
                self.db.execute(
                    "UPDATE vault_edit_ownership SET path = ?, updated_at = ? "
                    "WHERE vault_key = ? AND path = ?",
                    (move_to, now, vault_key, source_path),
                )
                self.db.execute(
                    "UPDATE vault_duplicate_resolutions SET canonical_path = ?, updated_at = ? "
                    "WHERE vault_key = ? AND canonical_path = ?",
                    (move_to, now, vault_key, source_path),
                )
                self.db.execute(
                    "DELETE FROM vault_notes WHERE vault_key = 'local' AND path = ?",
                    (source_path,),
                )
                indexed = parse_note(destination, vault=self.settings.vault_path).as_dict()
                self._upsert_indexed_note("local", indexed)
                return
            self._atomic_write(path, new_content)
            indexed = parse_note(path, vault=self.settings.vault_path).as_dict()
            self._upsert_indexed_note("local", indexed)
            return
        command = self.queue_command(
            vault_key,
            "apply_vault_change",
            {
                "sweep_id": change["sweep_id"],
                "change_id": change["id"],
                "path": change["path"],
                "expected_hash": expected_hash,
                "content": new_content,
                "move_to": move_to,
                "source_path": source_path,
            },
        )
        if not await self._wait_for_command(command["id"], str(change["sweep_id"])):
            raise ValueError("Vault change was stopped")
        indexed_path = move_to or source_path
        if move_to:
            self.db.execute(
                "DELETE FROM vault_notes WHERE vault_key = ? AND path = ?",
                (vault_key, source_path),
            )
        indexed = parse_note(Path(indexed_path), content=new_content).as_dict()
        self._upsert_indexed_note(vault_key, indexed)

    def _select_change_operations(
        self, change: dict[str, Any], selected_operation_ids: list[str]
    ) -> dict[str, Any]:
        try:
            decision = json.loads(change.get("decision_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            decision = {}
        operations = decision.get("operations", []) if isinstance(decision, dict) else []
        selected_keys = {str(value) for value in selected_operation_ids if str(value)}
        selected = [
            operation
            for operation in operations
            if isinstance(operation, dict)
            and str(operation.get("operation_id", "")) in selected_keys
        ]
        if not selected or len(selected) != len(selected_keys):
            raise ValueError("Choose at least one valid proposed operation")
        if str(change.get("change_type", "")) in {"native-maintenance", "index-membership"}:
            after, applied = apply_native_operations(str(change["before_content"]), selected)
            if len(applied) != len(selected) or after == str(change["before_content"]):
                raise ValueError("The selected operations can no longer be applied safely")
            selected = applied
            change["after_content"] = after
            change["after_hash"] = content_hash(after)
        selected_targets = {
            str(item.get("target", "")).casefold()
            for item in selected
            if item.get("kind") == "inline-link"
        }
        selected_tags = {
            str(item.get("tag", "")).casefold()
            for item in selected
            if item.get("kind") == "frontmatter-tag"
        }
        decision["relationships"] = [
            item
            for item in decision.get("relationships", [])
            if str(item.get("target", "")).split("|", 1)[0].removesuffix(".md").casefold()
            in selected_targets
        ]
        decision["suggested_tags"] = [
            item
            for item in decision.get("suggested_tags", [])
            if isinstance(item, dict) and str(item.get("tag", "")).casefold() in selected_tags
        ]
        decision["review"] = {
            "selected_operation_ids": sorted(selected_keys),
            "rejected_operation_ids": sorted(
                str(item.get("operation_id", ""))
                for item in operations
                if str(item.get("operation_id", "")) not in selected_keys
            ),
        }
        decision["operations"] = selected
        change["decision_json"] = json.dumps(decision, ensure_ascii=False)
        change["confidence"] = min(
            (float(item.get("confidence", 1.0)) for item in selected), default=1.0
        )
        self.db.execute(
            "UPDATE vault_changes SET after_content = ?, after_hash = ?, decision_json = ?, "
            "confidence = ? WHERE id = ? AND status = 'pending'",
            (
                change["after_content"],
                change["after_hash"],
                change["decision_json"],
                change["confidence"],
                change["id"],
            ),
        )
        return change

    async def approve_vault_change(
        self, change_id: str, selected_operation_ids: list[str] | None = None
    ) -> dict[str, Any]:
        change = self.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
        if not change:
            raise ValueError("Vault recommendation not found")
        if change["status"] != "pending":
            raise ValueError("Vault recommendation has already been reviewed")
        if selected_operation_ids is not None:
            change = self._select_change_operations(change, selected_operation_ids)
        if change["change_type"] == "duplicate-finding":
            decision = json.loads(change.get("decision_json") or "{}")
            operation = next(
                (
                    item
                    for item in decision.get("operations", [])
                    if isinstance(item, dict) and item.get("kind") == "canonical-selection"
                ),
                None,
            )
            if not operation:
                raise ValueError("Duplicate recommendation is invalid")
            now = utc_now()
            self.db.execute(
                "INSERT INTO vault_duplicate_resolutions(vault_key, duplicate_path, "
                "canonical_path, status, source_change_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?) ON CONFLICT(vault_key, duplicate_path) "
                "DO UPDATE SET canonical_path = excluded.canonical_path, status = 'active', "
                "source_change_id = excluded.source_change_id, updated_at = excluded.updated_at",
                (
                    change["vault_key"],
                    operation["duplicate_path"],
                    operation["canonical_path"],
                    change_id,
                    now,
                    now,
                ),
            )
        try:
            await self._apply_change_content(change)
        except Exception as exc:
            self.db.execute(
                "UPDATE vault_changes SET status = 'failed', error = ?, reviewed_at = ? "
                "WHERE id = ?",
                (str(exc)[:2000], utc_now(), change_id),
            )
            raise
        self._update_edit_ownership(change)
        now = utc_now()
        self.db.execute(
            "UPDATE vault_changes SET status = 'applied', error = '', applied_at = ?, "
            "reviewed_at = ? WHERE id = ?",
            (now, now, change_id),
        )
        self.db.execute(
            "UPDATE vault_sweeps SET applied_changes = applied_changes + 1, updated_at = ? "
            "WHERE id = ?",
            (now, change["sweep_id"]),
        )
        self.db.add_event(
            "vault.change_applied",
            f"Applied vault recommendation to {change['path']}",
            details={"change_id": change_id, "sweep_id": change["sweep_id"]},
        )
        return {"ok": True, "id": change_id, "status": "applied"}

    def reject_vault_change(self, change_id: str) -> dict[str, Any]:
        changed = self.db.execute(
            "UPDATE vault_changes SET status = 'rejected', reviewed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (utc_now(), change_id),
        )
        if not changed:
            raise ValueError("Pending vault recommendation not found")
        return {"ok": True, "id": change_id, "status": "rejected"}

    async def undo_vault_sweep(self, sweep_id: str) -> dict[str, Any]:
        changes = self.db.query_all(
            "SELECT * FROM vault_changes WHERE sweep_id = ? AND status = 'applied' "
            "ORDER BY applied_at DESC",
            (sweep_id,),
        )
        reverted = 0
        errors: list[str] = []
        for change in changes:
            if change.get("change_type") == "duplicate-finding":
                self.db.execute(
                    "DELETE FROM vault_duplicate_resolutions WHERE vault_key = ? "
                    "AND source_change_id = ?",
                    (change["vault_key"], change["id"]),
                )
                self.db.execute(
                    "UPDATE vault_changes SET status = 'reverted', reviewed_at = ? WHERE id = ?",
                    (utc_now(), change["id"]),
                )
                reverted += 1
                continue
            try:
                await self._apply_change_content(change, reverse=True)
            except Exception as exc:
                errors.append(f"{change['path']}: {exc}")
                continue
            self._update_edit_ownership(change, reverse=True)
            self.db.execute(
                "UPDATE vault_changes SET status = 'reverted', reviewed_at = ? WHERE id = ?",
                (utc_now(), change["id"]),
            )
            reverted += 1
        self.db.add_event(
            "vault.sweep_undone",
            f"Reverted {reverted} change(s) from vault sweep",
            level="warning" if errors else "info",
            details={"sweep_id": sweep_id, "errors": errors[:20]},
        )
        return {"ok": not errors, "reverted": reverted, "errors": errors}

    async def _run_vault_sweep(self, sweep_id: str) -> None:
        sweep = self.db.query_one("SELECT * FROM vault_sweeps WHERE id = ?", (sweep_id,))
        if not sweep:
            return
        self._sweep_stop.clear()
        now = utc_now()
        self.db.execute(
            "UPDATE vault_sweeps SET status = 'running', started_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now, now, sweep_id),
        )
        try:
            if self.db.get_setting("vault_mode", "local") == "local":
                completed = await self._scan_local_vault(
                    sweep_id, full_rebuild=bool(sweep["full_rebuild"])
                )
            else:
                completed = await self._scan_remote_vault(
                    sweep_id, full_rebuild=bool(sweep["full_rebuild"])
                )
            if not completed or self._sweep_stop.is_set():
                self.db.execute(
                    "UPDATE vault_sweeps SET status = 'stopped', current_note = '', "
                    "finished_at = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), utc_now(), sweep_id),
                )
                return
            change_mode = str(sweep["change_mode"])
            if sweep["sweep_type"] == "index":
                notes = self._indexed_vault_notes()
                await asyncio.to_thread(self._refresh_vault_corpus_profile, notes)
            else:
                change_ids = await self._generate_maintenance_changes(sweep_id)
                if change_mode == "auto":
                    for change_id in change_ids:
                        if self._sweep_stop.is_set():
                            break
                        candidate = self.db.query_one(
                            "SELECT change_type FROM vault_changes WHERE id = ?", (change_id,)
                        )
                        if not candidate or candidate["change_type"] != "native-maintenance":
                            continue
                        try:
                            await self.approve_vault_change(change_id)
                        except (OSError, UnicodeError, ValueError) as exc:
                            self.db.add_event(
                                "vault.change_failed",
                                f"Could not apply a sweep recommendation: {exc}",
                                level="error",
                                details={"change_id": change_id, "sweep_id": sweep_id},
                            )
            status = "stopped" if self._sweep_stop.is_set() else "completed"
            self.db.execute(
                "UPDATE vault_sweeps SET status = ?, current_note = '', finished_at = ?, "
                "updated_at = ? WHERE id = ?",
                (status, utc_now(), utc_now(), sweep_id),
            )
            self.db.add_event(
                f"vault.{sweep['sweep_type']}_sweep_{status}",
                f"Vault {sweep['sweep_type']} sweep {status}",
                details={"sweep_id": sweep_id},
            )
        except asyncio.CancelledError:
            if not self._sweep_stop.is_set():
                raise
            now = utc_now()
            self.db.execute(
                "UPDATE vault_sweeps SET status = 'stopped', current_note = '', finished_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, sweep_id),
            )
            self.db.add_event(
                f"vault.{sweep['sweep_type']}_sweep_stopped",
                f"Vault {sweep['sweep_type']} sweep stopped",
                details={"sweep_id": sweep_id},
            )
        except Exception as exc:
            error = str(exc).strip() or f"{type(exc).__name__}: vault sweep stopped unexpectedly"
            self._update_sweep_inference(
                sweep_id,
                "error",
                error,
                phase="failed",
                phase_label="Sweep AI analysis failed",
            )
            self.db.execute(
                "UPDATE vault_sweeps SET status = 'failed', error = ?, current_note = '', "
                "finished_at = ?, updated_at = ? WHERE id = ?",
                (error[:2000], utc_now(), utc_now(), sweep_id),
            )
            self.db.add_event(
                "vault.sweep_failed",
                f"Vault sweep failed: {error}",
                level="error",
                details={"sweep_id": sweep_id},
            )
        finally:
            finished = self.db.query_one(
                "SELECT status FROM vault_sweeps WHERE id = ?", (sweep_id,)
            )
            self._finish_sweep_inference(
                sweep_id,
                outcome=str(finished.get("status") if finished else "unknown"),
            )
            if self._sweep_task is asyncio.current_task():
                self._sweep_task = None

    def start_vault_sweep(
        self,
        sweep_type: str,
        *,
        change_mode: str = "",
        full_rebuild: bool = False,
        scheduled: bool = False,
    ) -> dict[str, Any]:
        if sweep_type not in {"index", "maintenance"}:
            raise ValueError("Sweep type must be index or maintenance")
        active = self.db.query_one(
            "SELECT * FROM vault_sweeps WHERE status IN ('queued', 'running', 'stopping') "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if active:
            raise ValueError("Another vault sweep is already running")
        if not self.vault_status()["configured"] or not self.vault_status()["writable"]:
            raise ValueError("Choose an available Obsidian vault before starting a sweep")
        default_mode = (
            self.db.get_setting("vault_maintenance_change_mode", "review")
            if sweep_type == "maintenance"
            else "index-only"
        )
        mode = change_mode or default_mode
        allowed = {"index-only"} if sweep_type == "index" else {"review", "auto"}
        if mode not in allowed:
            raise ValueError("Sweep change handling is invalid")
        if sweep_type == "maintenance" and not self._llm_config().active:
            raise ValueError(
                "Maintenance recommendations require Local AI. Enable and test a model first."
            )
        sweep_id = str(uuid.uuid4())
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO vault_sweeps(
                id, sweep_type, vault_key, status, change_mode, full_rebuild, scheduled,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                sweep_id,
                sweep_type,
                self._vault_key(),
                mode,
                int(full_rebuild),
                int(scheduled),
                now,
                now,
            ),
        )
        self._sweep_stop.clear()
        self._sweep_task = asyncio.create_task(self._run_vault_sweep(sweep_id))
        return self.vault_sweep(sweep_id)

    def stop_vault_sweep(self, sweep_id: str = "") -> dict[str, Any]:
        sweep = self.db.query_one(
            "SELECT * FROM vault_sweeps WHERE "
            + ("id = ? AND " if sweep_id else "")
            + "status IN ('queued', 'running', 'stopping') ORDER BY created_at DESC LIMIT 1",
            (sweep_id,) if sweep_id else (),
        )
        if not sweep:
            return {"ok": True, "stopped": False}
        self._sweep_stop.set()
        if self._sweep_ai_task and not self._sweep_ai_task.done():
            self._cancel_task(self._sweep_ai_task)
        self._update_sweep_inference(
            str(sweep["id"]),
            "stage",
            "Stop requested. Cancelling the active model request; no additional notes will be "
            "analyzed afterward.",
            phase="stopping",
            phase_label="Stopping sweep safely",
        )
        now = utc_now()
        self.db.execute(
            "UPDATE vault_sweeps SET status = 'stopping', updated_at = ? WHERE id = ?",
            (now, sweep["id"]),
        )
        self.db.execute(
            "UPDATE commands SET status = 'cancelled', result = ?, completed_at = ? "
            "WHERE status = 'pending' AND payload_json LIKE ?",
            ("Vault sweep stopped by user", now, f'%"sweep_id": "{sweep["id"]}"%'),
        )
        return {"ok": True, "stopped": True, "id": sweep["id"]}

    def vault_sweep(self, sweep_id: str) -> dict[str, Any]:
        sweep = self.db.query_one("SELECT * FROM vault_sweeps WHERE id = ?", (sweep_id,))
        if not sweep:
            raise ValueError("Vault sweep not found")
        return sweep

    def vault_sweep_status(self) -> dict[str, Any]:
        vault_key = self._vault_key()
        active = self.db.query_one(
            "SELECT * FROM vault_sweeps WHERE status IN ('queued', 'running', 'stopping') "
            "ORDER BY created_at DESC LIMIT 1"
        )
        recent = self.db.query_all("SELECT * FROM vault_sweeps ORDER BY created_at DESC LIMIT 20")
        indexed = self.db.query_one(
            "SELECT count(*) AS count, max(indexed_at) AS last_indexed_at FROM vault_notes "
            "WHERE vault_key = ?",
            (vault_key,),
        )
        completed_index = self.db.query_one(
            "SELECT finished_at FROM vault_sweeps WHERE vault_key = ? AND sweep_type = 'index' "
            "AND status = 'completed' ORDER BY finished_at DESC LIMIT 1",
            (vault_key,),
        )
        last_indexed_at = (
            str(completed_index.get("finished_at") or "") if completed_index else ""
        ) or (str(indexed.get("last_indexed_at") or "") if indexed else "")
        return {
            "active": active,
            "recent": recent,
            "indexed_notes": int(indexed["count"] if indexed else 0),
            "last_indexed_at": last_indexed_at,
            "model": self.vault_model_status(),
        }

    def list_vault_changes(
        self, *, status: str = "pending", limit: int = 200, offset: int = 0
    ) -> dict[str, Any]:
        where = "WHERE c.status = ?" if status else ""
        params: list[Any] = [status] if status else []
        count = self.db.query_one(f"SELECT count(*) AS count FROM vault_changes c {where}", params)
        rows = self.db.query_all(
            f"""
            SELECT c.*, s.sweep_type, s.change_mode
            FROM vault_changes c JOIN vault_sweeps s ON s.id = c.sweep_id
            {where} ORDER BY c.created_at DESC LIMIT ? OFFSET ?
            """,
            [*params, max(1, min(limit, 500)), max(0, offset)],
        )
        for row in rows:
            try:
                row["evidence"] = json.loads(row.pop("evidence_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                row["evidence"] = []
            try:
                row["decision"] = json.loads(row.pop("decision_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["decision"] = {}
            row.pop("before_content", None)
            row.pop("after_content", None)
        return {"items": rows, "total": int(count["count"] if count else 0)}

    def vault_change_diff(self, change_id: str) -> dict[str, Any]:
        change = self.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
        if not change:
            raise ValueError("Vault recommendation not found")
        try:
            change["evidence"] = json.loads(change.pop("evidence_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            change["evidence"] = []
        try:
            change["decision"] = json.loads(change.pop("decision_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            change["decision"] = {}
        return change

    def agent_sweep_progress(
        self,
        agent_id: str,
        sweep_id: str,
        *,
        processed: int,
        total: int,
        current_note: str,
    ) -> dict[str, Any]:
        sweep = self.db.query_one(
            "SELECT * FROM vault_sweeps WHERE id = ? AND vault_key = ?", (sweep_id, agent_id)
        )
        if not sweep:
            raise ValueError("Vault sweep not found for this computer")
        self.db.execute(
            "UPDATE vault_sweeps SET processed_notes = ?, total_notes = ?, current_note = ?, "
            "updated_at = ? WHERE id = ?",
            (max(0, processed), max(0, total), current_note[:2000], utc_now(), sweep_id),
        )
        return {"ok": True, "stop_requested": sweep["status"] in {"stopping", "stopped"}}

    @staticmethod
    def _sanitize_indexed_note(raw_note: dict[str, Any]) -> dict[str, Any]:
        path = safe_relative_path(str(raw_note.get("path", ""))).as_posix()
        content = str(raw_note.get("content", ""))[:2_000_000]
        parsed = parse_note(
            Path(path), content=content, modified_ns=int(raw_note.get("modified_ns", 0))
        ).as_dict()
        parsed["content_hash"] = str(raw_note.get("content_hash") or content_hash(content))
        parsed["size"] = max(0, int(raw_note.get("size", len(content.encode("utf-8")))))
        return parsed

    def agent_sweep_notes(
        self, agent_id: str, sweep_id: str, notes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        sweep = self.db.query_one(
            "SELECT * FROM vault_sweeps WHERE id = ? AND vault_key = ?", (sweep_id, agent_id)
        )
        if not sweep:
            raise ValueError("Vault sweep not found for this computer")
        if sweep["status"] in {"stopping", "stopped"}:
            return {"ok": True, "accepted": 0, "stop_requested": True}
        if len(notes) > 50:
            raise ValueError("Vault index batches are limited to 50 notes")
        sanitized = [self._sanitize_indexed_note(note) for note in notes if isinstance(note, dict)]
        stored = self._store_vault_index(
            agent_id, sanitized, full_rebuild=False, delete_missing=False
        )
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO vault_sweep_paths(sweep_id, vault_key, path) "
                "VALUES (?, ?, ?)",
                [(sweep_id, agent_id, note["path"]) for note in sanitized],
            )
            connection.execute(
                "UPDATE vault_sweeps SET changed_notes = changed_notes + ?, updated_at = ? "
                "WHERE id = ?",
                (stored["changed"], utc_now(), sweep_id),
            )
        return {"ok": True, "accepted": len(sanitized), "stop_requested": False}

    def _finalize_remote_index(self, agent_id: str, sweep_id: str) -> dict[str, int]:
        before = self.db.query_one(
            "SELECT count(*) AS count FROM vault_notes WHERE vault_key = ?", (agent_id,)
        )
        self.db.execute(
            """
            DELETE FROM vault_notes
            WHERE vault_key = ? AND NOT EXISTS (
                SELECT 1 FROM vault_sweep_paths p
                WHERE p.sweep_id = ? AND p.vault_key = vault_notes.vault_key
                  AND p.path = vault_notes.path
            )
            """,
            (agent_id, sweep_id),
        )
        notes = self._indexed_vault_notes(agent_id)
        add_backlinks(notes)
        with self.db.transaction() as connection:
            connection.executemany(
                "UPDATE vault_notes SET backlinks_json = ? WHERE vault_key = ? AND path = ?",
                [
                    (
                        json.dumps(note.get("backlinks", []), ensure_ascii=False),
                        agent_id,
                        note["path"],
                    )
                    for note in notes
                ],
            )
        previous = int(before["count"] if before else 0)
        return {"notes": len(notes), "removed": max(0, previous - len(notes))}

    @staticmethod
    def _parse_schedule_time(value: str) -> tuple[int, int]:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
        if not match:
            raise ValueError("Sweep schedule time must use HH:MM")
        return int(match.group(1)), int(match.group(2))

    def _schedule_due(self, sweep_type: str, now_utc: datetime) -> bool:
        prefix = "vault_index" if sweep_type == "index" else "vault_maintenance"
        if self.db.get_setting(f"{prefix}_schedule_enabled", "false") != "true":
            return False
        timezone_name = self.db.get_setting("vault_schedule_timezone", "America/New_York")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone = UTC
        local_now = now_utc.astimezone(timezone)
        frequency = self.db.get_setting(f"{prefix}_schedule_frequency", "weekly")
        last = self.db.query_one(
            "SELECT created_at FROM vault_sweeps WHERE sweep_type = ? AND scheduled = 1 "
            "ORDER BY created_at DESC LIMIT 1",
            (sweep_type,),
        )
        last_at = datetime.fromisoformat(last["created_at"]) if last else None
        if frequency == "custom":
            try:
                hours = max(
                    1,
                    min(int(self.db.get_setting(f"{prefix}_schedule_interval_hours", "24")), 8760),
                )
            except ValueError:
                hours = 24
            return last_at is None or now_utc - last_at >= timedelta(hours=hours)
        hour, minute = self._parse_schedule_time(
            self.db.get_setting(f"{prefix}_schedule_time", "02:00")
        )
        due = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if frequency == "weekly":
            weekday = max(
                0,
                min(int(self.db.get_setting(f"{prefix}_schedule_weekday", "6")), 6),
            )
            due -= timedelta(days=(due.weekday() - weekday) % 7)
        elif frequency == "monthly":
            day = max(
                1,
                min(int(self.db.get_setting(f"{prefix}_schedule_month_day", "1")), 28),
            )
            due = due.replace(day=day)
            if due > local_now:
                previous_month = due.replace(day=1) - timedelta(days=1)
                due = due.replace(year=previous_month.year, month=previous_month.month, day=day)
        elif frequency != "daily":
            raise ValueError("Sweep schedule frequency is invalid")
        if due > local_now:
            if frequency == "daily":
                due -= timedelta(days=1)
            elif frequency == "weekly":
                due -= timedelta(days=7)
        due_utc = due.astimezone(UTC)
        return due_utc <= now_utc and (last_at is None or last_at < due_utc)

    async def scheduler_tick(self, now: datetime | None = None) -> None:
        if self._sweep_task and not self._sweep_task.done():
            return
        active = self.db.query_one(
            "SELECT id FROM vault_sweeps WHERE status IN ('queued', 'running', 'stopping') LIMIT 1"
        )
        if active:
            return
        vault = self.vault_status()
        if not vault["configured"] or not vault["writable"]:
            # A scheduled run is postponed while a desktop or mounted vault is
            # unavailable. Nothing is queued, so missed runs cannot stack up.
            return
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        for sweep_type in ("index", "maintenance"):
            if self._schedule_due(sweep_type, now_utc):
                self.start_vault_sweep(sweep_type, scheduled=True)
                return

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.add_event(
                    "vault.scheduler_error",
                    f"Vault sweep scheduler failed: {exc}",
                    level="error",
                )
            await asyncio.sleep(30)

    def start_background_tasks(self) -> None:
        if not self._scheduler_task or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop_background_tasks(self) -> None:
        self._sweep_stop.set()
        tasks = [task for task in (self._scheduler_task, self._sweep_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler_task = None
        self._sweep_task = None

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

    def pipeline_enabled(self) -> bool:
        return self.db.get_setting("sync_enabled", "true").lower() == "true"

    def require_pipeline_enabled(self) -> None:
        if not self.pipeline_enabled():
            raise PipelinePausedError(
                "Global syncing is stopped. Choose Start Global Sync before processing files."
            )
        if self.db.get_setting("vault_confirmed", "false").lower() != "true":
            raise ValueError(
                "Choose and save an Obsidian Vault before adding or syncing source folders"
            )

    def pipeline_status(self) -> dict[str, Any]:
        enabled = self.pipeline_enabled()
        return {
            "enabled": enabled,
            "state": "running" if enabled else "stopped",
            "active_jobs": len(self._active_processing),
            "active_files": self.active_work(),
        }

    @staticmethod
    def _elapsed_seconds(started_at: str, finished_at: str = "") -> int:
        try:
            ending = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
            elapsed = ending - datetime.fromisoformat(started_at)
            return max(0, int(elapsed.total_seconds()))
        except (TypeError, ValueError):
            return 0

    def _public_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value
            for key, value in activity.items()
            if key not in {"task", "used_ai", "ai_active"}
        }
        result["elapsed_seconds"] = self._elapsed_seconds(
            str(activity.get("started_at", "")), str(activity.get("finished_at", ""))
        )
        result["cancellable"] = bool(activity.get("cancellable", activity.get("ai_active")))
        return result

    def active_work(self) -> list[dict[str, Any]]:
        rows = [self._public_activity(item) for item in self._processing_activity.values()]
        return sorted(rows, key=lambda item: str(item.get("started_at", "")))

    def ai_activity(self) -> dict[str, Any]:
        profile = self.active_ai_profile()
        active = [
            self._public_activity(item)
            for item in self._processing_activity.values()
            if item.get("used_ai")
        ]
        active.sort(key=lambda item: str(item.get("started_at", "")))
        sweeps: dict[str, dict[str, Any]] = {}
        for sweep_type in ("index", "maintenance"):
            sweep_active = [
                self._public_activity(item)
                for item in self._sweep_inference_activity.values()
                if item.get("sweep_type") == sweep_type
            ]
            sweep_active.sort(key=lambda item: str(item.get("started_at", "")))
            sweeps[sweep_type] = {
                "active": sweep_active,
                "last": dict(self._last_sweep_inference[sweep_type])
                if sweep_type in self._last_sweep_inference
                else None,
            }
        return {
            "active": active,
            "last": dict(self._last_inference) if self._last_inference else None,
            "sweeps": sweeps,
            "provider": self.db.get_setting("llm_provider", "ollama"),
            "model": self.db.get_setting("llm_model", ""),
            "profile_id": profile.id,
            "profile_name": profile.name,
            "enabled": self._llm_config().active,
            "revision": self._ai_activity_revision,
        }

    @property
    def ai_activity_subscriber_count(self) -> int:
        return len(self._ai_activity_subscribers)

    def _notify_ai_activity(self) -> None:
        self._ai_activity_revision += 1
        for queue in tuple(self._ai_activity_subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(self._ai_activity_revision)

    async def stream_ai_activity(
        self, *, keepalive_seconds: float = 15
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Yield current AI activity immediately and whenever model activity changes."""
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._ai_activity_subscribers.add(queue)
        try:
            yield self.ai_activity()
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
                except TimeoutError:
                    yield None
                    continue
                while not queue.empty():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                yield self.ai_activity()
        finally:
            self._ai_activity_subscribers.discard(queue)

    def _start_activity(
        self,
        document_id: str,
        *,
        root_id: str,
        source_path: str,
        agent_name: str,
        root_name: str,
        task: asyncio.Task[Any],
    ) -> None:
        now = utc_now()
        self._processing_activity[document_id] = {
            "document_id": document_id,
            "source_path": source_path,
            "source_name": Path(source_path).name,
            "agent_name": agent_name,
            "root_name": root_name,
            "root_id": root_id,
            "phase": "extracting",
            "phase_label": "Extracting readable content",
            "started_at": now,
            "updated_at": now,
            "provider": "",
            "model": "",
            "events": [
                {"kind": "stage", "message": "Extracting readable content.", "created_at": now}
            ],
            "task": task,
            "used_ai": False,
            "ai_active": False,
        }

    def _update_activity(
        self,
        document_id: str,
        kind: str,
        message: str,
        *,
        phase: str = "",
        phase_label: str = "",
    ) -> None:
        activity = self._processing_activity.get(document_id)
        if not activity:
            return
        now = utc_now()
        raw = str(message)
        clean = raw if kind in {"reasoning", "output"} else raw.strip()
        if clean:
            events = activity["events"]
            if kind in {"reasoning", "output"} and events and events[-1]["kind"] == kind:
                events[-1]["message"] = (events[-1]["message"] + clean)[-20000:]
                events[-1]["created_at"] = now
            else:
                events.append({"kind": kind, "message": clean[:20000], "created_at": now})
            activity["events"] = events[-80:]
        if phase:
            activity["phase"] = phase
        if phase_label:
            activity["phase_label"] = phase_label
        activity["updated_at"] = now
        if activity.get("used_ai"):
            self._notify_ai_activity()

    def _finish_activity(self, document_id: str, *, outcome: str) -> None:
        activity = self._processing_activity.get(document_id)
        if not activity:
            return
        activity["ai_active"] = False
        activity["outcome"] = outcome
        activity["finished_at"] = utc_now()
        if activity.get("used_ai"):
            self._last_inference = self._public_activity(activity)
            self._notify_ai_activity()

    def _start_sweep_inference(self, sweep_id: str, sweep_type: str) -> None:
        config = self._llm_config()
        now = utc_now()
        label = "Index Sweep" if sweep_type == "index" else "Maintenance Sweep"
        self._sweep_inference_activity[sweep_id] = {
            "document_id": f"sweep:{sweep_id}",
            "activity_kind": "sweep",
            "sweep_id": sweep_id,
            "sweep_type": sweep_type,
            "source_name": label,
            "source_path": "Preparing AI-assisted vault analysis",
            "agent_name": "Whole vault",
            "root_name": "Obsidian intelligence",
            "phase": "preparing",
            "phase_label": "Preparing AI analysis",
            "started_at": now,
            "updated_at": now,
            "provider": config.provider,
            "model": config.model,
            "profile_id": config.active_profile.id,
            "profile_name": config.active_profile.name,
            "events": [
                {
                    "kind": "stage",
                    "message": f"Starting AI-assisted {label} analysis.",
                    "created_at": now,
                }
            ],
            "used_ai": True,
            "ai_active": True,
            "cancellable": False,
        }
        self._notify_ai_activity()

    def _update_sweep_inference(
        self,
        sweep_id: str,
        kind: str,
        message: str,
        *,
        phase: str = "",
        phase_label: str = "",
        current_note: str = "",
    ) -> None:
        activity = self._sweep_inference_activity.get(sweep_id)
        if not activity:
            return
        now = utc_now()
        raw = str(message)
        clean = raw if kind in {"reasoning", "output"} else raw.strip()
        if clean:
            events = activity["events"]
            if kind in {"reasoning", "output"} and events and events[-1]["kind"] == kind:
                events[-1]["message"] = (events[-1]["message"] + clean)[-20000:]
                events[-1]["created_at"] = now
            else:
                events.append({"kind": kind, "message": clean[:20000], "created_at": now})
            activity["events"] = events[-80:]
        if phase:
            activity["phase"] = phase
        if phase_label:
            activity["phase_label"] = phase_label
        if current_note:
            activity["source_path"] = current_note
            activity["current_note"] = current_note
        activity["updated_at"] = now
        self._notify_ai_activity()

    def _finish_sweep_inference(self, sweep_id: str, *, outcome: str) -> None:
        activity = self._sweep_inference_activity.get(sweep_id)
        if not activity:
            return
        activity["ai_active"] = False
        activity["outcome"] = outcome
        activity["finished_at"] = utc_now()
        activity["updated_at"] = activity["finished_at"]
        sweep_type = str(activity.get("sweep_type", "maintenance"))
        self._last_sweep_inference[sweep_type] = self._public_activity(activity)
        self._sweep_inference_activity.pop(sweep_id, None)
        self._notify_ai_activity()

    def stop_inference(self, document_id: str = "") -> dict[str, Any]:
        stopped: list[str] = []
        for active_document_id, (_root_id, task) in list(self._active_processing.items()):
            activity = self._processing_activity.get(active_document_id, {})
            if document_id and active_document_id != document_id:
                continue
            if not activity.get("ai_active"):
                continue
            self._cancel_reasons[active_document_id] = "AI inference stopped by user"
            self._update_activity(
                active_document_id,
                "stage",
                "Stop requested. Cancelling the active model request.",
                phase="stopping",
                phase_label="Stopping AI inference",
            )
            self._cancel_task(task)
            stopped.append(active_document_id)
        if stopped:
            self.db.add_event(
                "ai.inference_stop_requested",
                f"Stop requested for {len(stopped)} active AI inference job(s)",
                level="warning",
                document_id=stopped[0] if len(stopped) == 1 else None,
            )
        return {"ok": True, "stopped": len(stopped), "document_ids": stopped}

    @staticmethod
    def _cancel_task(task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is task.get_loop():
            task.cancel()
        else:
            task.get_loop().call_soon_threadsafe(task.cancel)

    def _cancel_processing(self, *, root_id: str = "", reason: str = "Stopped by user") -> int:
        tasks = [
            (document_id, task)
            for document_id, (active_root_id, task) in self._active_processing.items()
            if not root_id or active_root_id == root_id
        ]
        for document_id, task in tasks:
            self._cancel_reasons[document_id] = reason
            self._cancel_task(task)
        return len(tasks)

    def pause_pipeline(self) -> dict[str, Any]:
        self.db.set_settings({"sync_enabled": ("false", False)})
        now = utc_now()
        pending = self.db.query_all(
            "SELECT id, command, payload_json FROM commands WHERE status = 'pending'"
        )
        cancelled = [row for row in pending if row["command"] in PIPELINE_COMMANDS]
        with self.db.transaction() as connection:
            for row in cancelled:
                connection.execute(
                    "UPDATE commands SET status = 'cancelled', result = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    ("Stopped by user", now, row["id"]),
                )
                if row["command"] == "write_note":
                    payload = json.loads(row.get("payload_json") or "{}")
                    document_id = str(payload.get("document_id", ""))
                    if document_id:
                        connection.execute(
                            "UPDATE documents SET status = 'paused', error = ?, updated_at = ? "
                            "WHERE id = ?",
                            ("Stopped by user", now, document_id),
                        )
        active = self._cancel_processing()
        self.db.add_event(
            "pipeline.stopped",
            "Syncing was stopped; active sync and AI work was cancelled",
            level="warning",
            details={"active_jobs": active, "cancelled_commands": len(cancelled)},
        )
        return {**self.pipeline_status(), "cancelled_commands": len(cancelled)}

    def resume_pipeline(self) -> dict[str, Any]:
        if self.db.get_setting("vault_confirmed", "false").lower() != "true":
            raise ValueError("Choose and save an Obsidian Vault before starting Global Sync")
        self.db.set_settings({"sync_enabled": ("true", False)})
        reconciliations = 0
        for agent in self.db.query_all("SELECT id FROM agents WHERE enabled = 1"):
            self.queue_command(agent["id"], "reconcile")
            reconciliations += 1
        self.db.add_event(
            "pipeline.started",
            "Syncing was started; connected computers will reconcile missed changes",
        )
        return {**self.pipeline_status(), "reconciliations": reconciliations}

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

    def create_reconnect_enrollment(self, agent_id: str, *, minutes: int = 20) -> dict[str, Any]:
        """Create a one-time credential that repairs an existing computer in place."""
        agent = self.db.query_one("SELECT id, name, enabled FROM agents WHERE id = ?", (agent_id,))
        if not agent or not agent["enabled"]:
            raise ValueError("Computer not found")
        code = new_enrollment_code()
        enrollment_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=max(1, min(minutes, 1440)))
        with self.db.transaction() as connection:
            # Only the newest unused repair credential should remain valid for a computer.
            connection.execute(
                "DELETE FROM enrollments WHERE agent_id = ? AND used_at IS NULL", (agent_id,)
            )
            connection.execute(
                """
                INSERT INTO enrollments(
                    id, code_hash, label, expires_at, agent_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    enrollment_id,
                    hash_token(code),
                    str(agent["name"])[:120],
                    expires.isoformat(),
                    agent_id,
                    now.isoformat(),
                ),
            )
        self.db.add_event(
            "enrollment.reconnect_created",
            f"Reconnect code created for {agent['name']}",
            agent_id=agent_id,
        )
        return {
            "id": enrollment_id,
            "code": code,
            "expires_at": expires.isoformat(),
            "agent_id": agent_id,
            "name": str(agent["name"]),
        }

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
            reconnect_agent_id = str(enrollment["agent_id"] or "")
            if reconnect_agent_id:
                existing = connection.execute(
                    "SELECT id, name, enabled FROM agents WHERE id = ?", (reconnect_agent_id,)
                ).fetchone()
                if not existing or not existing["enabled"]:
                    raise ValueError("The computer being reconnected no longer exists")
                agent_id = reconnect_agent_id
                connection.execute(
                    """
                    UPDATE agents
                    SET name = ?, hostname = ?, os_name = ?, os_version = ?, agent_version = ?,
                        token_hash = ?, status = 'pending', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        hostname,
                        (payload.get("os_name") or "unknown")[:100],
                        (payload.get("os_version") or "")[:200],
                        (payload.get("agent_version") or "")[:50],
                        hash_token(token),
                        now.isoformat(),
                        agent_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO agents(id, name, hostname, os_name, os_version, agent_version,
                                       token_hash, status, last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
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
        event_type = "agent.reconnected" if reconnect_agent_id else "agent.registered"
        event_message = (
            f"Computer {name} reconnected without removing its records"
            if reconnect_agent_id
            else f"Device {name} joined Obsync"
        )
        self.db.add_event(event_type, event_message, agent_id=agent_id)
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
            connection.execute("DELETE FROM vault_notes WHERE vault_key = ?", (agent_id,))
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
                """
                SELECT id, name, hostname, os_name, status, enabled, last_seen_at
                FROM agents WHERE id = ?
                """,
                (enrollment["agent_id"],),
            )
        cutoff = datetime.now(UTC) - timedelta(seconds=90)
        try:
            connected = bool(
                agent
                and agent["enabled"]
                and agent["status"] == "online"
                and datetime.fromisoformat(agent["last_seen_at"]) >= cutoff
            )
        except (TypeError, ValueError):
            connected = False
        enrollment["connected"] = connected
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
            stored_status = str(row.get("status", ""))
            try:
                online = bool(
                    row["enabled"]
                    and stored_status == "online"
                    and datetime.fromisoformat(row["last_seen_at"]) >= cutoff
                )
            except (TypeError, ValueError):
                online = False
            effective_status = "online" if online else "offline"
            if stored_status != effective_status:
                self.db.execute("UPDATE agents SET status = 'offline' WHERE id = ?", (row["id"],))
            row["status"] = effective_status
        return rows

    def upsert_root(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        root_key = str(payload["root_key"])[:200]
        pending_removals = self.db.query_all(
            "SELECT payload_json FROM commands "
            "WHERE agent_id = ? AND command = 'remove_root' AND status = 'pending'",
            (agent_id,),
        )
        if any(
            str(json.loads(row.get("payload_json") or "{}").get("root_key", "")) == root_key
            for row in pending_removals
        ):
            return {"id": "", "root_key": root_key, "removal_requested": True}
        now = utc_now()
        existing = self.db.query_one(
            "SELECT * FROM roots WHERE agent_id = ? AND root_key = ?", (agent_id, root_key)
        )
        root_id = existing["id"] if existing else str(uuid.uuid4())
        name = str(payload.get("name") or Path(str(payload.get("path", "Folder"))).name or "Folder")
        destination = str(payload.get("destination") or "Obsync")
        safe_relative_path(destination)
        sync_state = str(payload.get("sync_state") or "running")
        if sync_state not in {"running", "paused", "stopped"}:
            raise ValueError("Folder state must be running, paused, or stopped")
        include = json.dumps(list(payload.get("include_patterns") or ["**/*"]))
        exclude = json.dumps(list(payload.get("exclude_patterns") or []))
        self.db.execute(
            """
            INSERT INTO roots(id, agent_id, root_key, name, path, destination, include_patterns,
                              exclude_patterns, enabled, sync_state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, root_key) DO UPDATE SET
                name = excluded.name,
                path = excluded.path,
                destination = excluded.destination,
                include_patterns = excluded.include_patterns,
                exclude_patterns = excluded.exclude_patterns,
                enabled = excluded.enabled,
                sync_state = excluded.sync_state,
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
                sync_state,
                now,
                now,
            ),
        )
        root = self.db.query_one("SELECT * FROM roots WHERE id = ?", (root_id,))
        assert root is not None
        return self._decode_root(root)

    def set_root_state(self, root_id: str, sync_state: str) -> dict[str, Any]:
        if sync_state not in {"running", "paused", "stopped"}:
            raise ValueError("Folder state must be running, paused, or stopped")
        root = self.db.query_one(
            "SELECT r.*, a.name AS agent_name FROM roots r "
            "JOIN agents a ON a.id = r.agent_id WHERE r.id = ?",
            (root_id,),
        )
        if not root:
            raise ValueError("Watched folder not found")

        cancelled = 0
        active = 0
        if sync_state != "running":
            active = self._cancel_processing(root_id=root_id)
            now = utc_now()
            pending = self.db.query_all(
                "SELECT id, command, payload_json FROM commands "
                "WHERE agent_id = ? AND status = 'pending'",
                (root["agent_id"],),
            )
            with self.db.transaction() as connection:
                for row in pending:
                    payload = json.loads(row.get("payload_json") or "{}")
                    matches_root = str(payload.get("root_key", "")) == root["root_key"]
                    document_id = str(payload.get("document_id", ""))
                    if document_id:
                        document = connection.execute(
                            "SELECT root_id FROM documents WHERE id = ?", (document_id,)
                        ).fetchone()
                        matches_root = bool(document and document["root_id"] == root_id)
                    if row["command"] in PIPELINE_COMMANDS and matches_root:
                        connection.execute(
                            "UPDATE commands SET status = 'cancelled', result = ?, "
                            "completed_at = ? "
                            "WHERE id = ? AND status = 'pending'",
                            (f"Folder {sync_state} by user", now, row["id"]),
                        )
                        cancelled += 1

        self.db.execute(
            "UPDATE roots SET sync_state = ?, enabled = 1, updated_at = ? WHERE id = ?",
            (sync_state, utc_now(), root_id),
        )
        command = self.queue_command(
            root["agent_id"],
            "set_root_state",
            {"root_key": root["root_key"], "sync_state": sync_state},
        )
        label = {"running": "started", "paused": "paused", "stopped": "stopped"}[sync_state]
        self.db.add_event(
            f"root.{label}",
            f"{root['name']} on {root['agent_name']} was {label}",
            level="warning" if sync_state != "running" else "info",
            agent_id=root["agent_id"],
            root_id=root_id,
            details={"active_jobs": active, "cancelled_commands": cancelled},
        )
        updated = self.db.query_one("SELECT * FROM roots WHERE id = ?", (root_id,))
        assert updated is not None
        return {
            **self._decode_root(updated),
            "command_id": command["id"],
            "cancelled_commands": cancelled,
            "active_jobs": active,
        }

    def queue_root_command(self, root_id: str, command: str) -> dict[str, Any]:
        root = self.db.query_one(
            "SELECT agent_id, root_key, sync_state, enabled FROM roots WHERE id = ?",
            (root_id,),
        )
        if not root:
            raise ValueError("Watched folder not found")
        if not root["enabled"] or root["sync_state"] != "running":
            raise PipelinePausedError(f"Folder syncing is {root['sync_state']}")
        return self.queue_command(root["agent_id"], command, {"root_key": root["root_key"]})

    def remove_root(self, root_id: str) -> dict[str, Any]:
        root = self.db.query_one(
            "SELECT r.*, a.name AS agent_name FROM roots r "
            "JOIN agents a ON a.id = r.agent_id WHERE r.id = ?",
            (root_id,),
        )
        if not root:
            raise ValueError("Watched folder not found")
        document_count = int(
            (
                self.db.query_one(
                    "SELECT count(*) AS count FROM documents WHERE root_id = ?", (root_id,)
                )
                or {}
            ).get("count", 0)
        )
        self._cancel_processing(root_id=root_id)
        command = self.queue_command(
            root["agent_id"],
            "remove_root",
            {"root_key": root["root_key"], "name": root["name"]},
        )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM events WHERE root_id = ?", (root_id,))
            connection.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        self.db.add_event(
            "root.removed",
            (
                f"Stopped watching {root['name']} on {root['agent_name']}; "
                "source files and Obsidian notes were kept"
            ),
            agent_id=root["agent_id"],
        )
        return {
            "ok": True,
            "root_id": root_id,
            "name": root["name"],
            "agent_id": root["agent_id"],
            "removed_documents": document_count,
            "command_id": command["id"],
        }

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
                       AS checking_count,
                   sum(CASE WHEN d.comparison_status = 'possible-duplicate' THEN 1 ELSE 0 END)
                       AS duplicate_count
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

    def _local_vault_snapshot(
        self,
    ) -> tuple[dict[tuple[str, str, str], dict[str, str]], list[dict[str, Any]]]:
        index: dict[tuple[str, str, str], dict[str, str]] = {}
        catalog = self._indexed_vault_notes("local")
        if catalog:
            for note in catalog:
                properties = note.get("properties", {})
                if not isinstance(properties, dict) or not properties.get("obsync_id"):
                    continue
                metadata = {
                    key: str(properties.get(key, ""))
                    for key in (
                        "obsync_id",
                        "obsync_status",
                        "obsync_source",
                        "obsync_machine",
                        "obsync_root",
                        "obsync_hash",
                    )
                }
                metadata["destination_path"] = str(note["path"])
                key = (
                    metadata["obsync_machine"].casefold(),
                    metadata["obsync_root"].casefold(),
                    metadata["obsync_source"],
                )
                index.setdefault(key, metadata)
            return index, catalog
        catalog = []
        if not self.settings.vault_path.is_dir():
            return index, catalog
        for path in self.settings.vault_path.rglob("*.md"):
            if path.is_symlink() or not path.is_file() or ".obsidian" in path.parts:
                continue
            try:
                parsed = parse_note(path, vault=self.settings.vault_path).as_dict()
                content = str(parsed["content"])
                metadata = managed_note_metadata(content)
            except (OSError, UnicodeError, ValueError):
                continue
            relative = str(parsed["path"])
            catalog.append(parsed)
            if not metadata:
                continue
            key = (
                metadata["obsync_machine"].casefold(),
                metadata["obsync_root"].casefold(),
                metadata["obsync_source"],
            )
            metadata["destination_path"] = relative
            index.setdefault(key, metadata)
        add_backlinks(catalog)
        self._store_vault_index("local", catalog, full_rebuild=True)
        return index, catalog

    @staticmethod
    def _possible_duplicate(
        source_title: str,
        catalog: list[dict[str, Any]],
        *,
        current_destination: str = "",
    ) -> dict[str, str] | None:
        for note in catalog:
            if current_destination and note["path"] == current_destination:
                continue
            if likely_same_note_title(source_title, note["title"]):
                return note
        return None

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
        vault_catalog: list[dict[str, Any]],
    ) -> tuple[str, str, str, str, str]:
        destination = str(document.get("destination_path") or "")
        if document.get("status") == "ignored":
            return "ignored", destination, "", "", ""
        if (
            document.get("review_resolution") == "approved"
            and document.get("comparison_status") == "possible-duplicate"
        ):
            return (
                "possible-duplicate",
                destination,
                "",
                str(document.get("duplicate_path") or ""),
                str(document.get("duplicate_title") or ""),
            )
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
                return (
                    "modified" if note_exists else "vault-missing",
                    destination,
                    "",
                    "",
                    "",
                )
        else:
            metadata = vault_index.get(
                self._identity_key(agent_name, root_name, str(document["source_path"]))
            )
            if not metadata:
                duplicate = None
                if self.db.get_setting(
                    "duplicate_policy", "review"
                ) == "review" and not document.get("duplicate_dismissed"):
                    duplicate = self._possible_duplicate(
                        note_title_from_path(Path(str(document["source_path"]))), vault_catalog
                    )
                if duplicate:
                    return (
                        "possible-duplicate",
                        "",
                        "",
                        duplicate["path"],
                        duplicate["title"],
                    )
                return "new", "", "", "", ""
            destination = metadata["destination_path"]

        note_hash = str(metadata.get("obsync_hash", ""))
        observed_hash = str(document.get("observed_hash") or document.get("source_hash") or "")
        state = "in-sync" if note_hash and note_hash == observed_hash else "modified"
        return state, destination, note_hash, "", ""

    def _compare_indexed_document(
        self,
        document: dict[str, Any],
        *,
        agent_name: str,
        root_name: str,
        vault_index: dict[tuple[str, str, str], dict[str, str]],
        vault_catalog: list[dict[str, Any]],
    ) -> tuple[str, str, str, str, str]:
        destination = str(document.get("destination_path") or "")
        if document.get("status") == "ignored":
            return "ignored", destination, "", "", ""
        by_path = {str(note.get("path", "")): note for note in vault_catalog}
        metadata: dict[str, str] | None = None
        if destination:
            note = by_path.get(destination)
            if not note:
                return "vault-missing", destination, "", "", ""
            properties = note.get("properties", {})
            if isinstance(properties, dict) and properties.get("obsync_id"):
                metadata = {key: str(value) for key, value in properties.items()}
            else:
                return "modified", destination, "", "", ""
        else:
            metadata = vault_index.get(
                self._identity_key(agent_name, root_name, str(document["source_path"]))
            )
            if not metadata:
                duplicate = None
                if self.db.get_setting(
                    "duplicate_policy", "review"
                ) == "review" and not document.get("duplicate_dismissed"):
                    duplicate = self._possible_duplicate(
                        note_title_from_path(Path(str(document["source_path"]))), vault_catalog
                    )
                if duplicate:
                    return (
                        "possible-duplicate",
                        "",
                        "",
                        str(duplicate["path"]),
                        str(duplicate["title"]),
                    )
                return "new", "", "", "", ""
            destination = str(metadata["destination_path"])
        note_hash = str(metadata.get("obsync_hash", ""))
        observed_hash = str(document.get("observed_hash") or document.get("source_hash") or "")
        state = "in-sync" if note_hash and note_hash == observed_hash else "modified"
        return state, destination, note_hash, "", ""

    def inventory_files(
        self,
        *,
        agent: dict[str, Any],
        root_id: str,
        scan_id: str,
        items: list[dict[str, Any]],
        complete: bool = False,
    ) -> dict[str, Any]:
        self.require_pipeline_enabled()
        root = self.db.query_one(
            "SELECT * FROM roots WHERE id = ? AND agent_id = ?", (root_id, agent["id"])
        )
        if not root or not root["enabled"]:
            raise ValueError("Watched folder is unknown or disabled")
        if root.get("sync_state", "running") != "running":
            raise PipelinePausedError(f"Folder syncing is {root['sync_state']}")
        if not scan_id or len(scan_id) > 100:
            raise ValueError("Inventory scan id is invalid")
        now = utc_now()
        local_mode = self.db.get_setting("vault_mode", "local") == "local"
        if local_mode:
            if scan_id not in self._inventory_vault_indexes:
                if len(self._inventory_vault_indexes) >= 8:
                    self._inventory_vault_indexes.pop(next(iter(self._inventory_vault_indexes)))
                self._inventory_vault_indexes[scan_id] = self._local_vault_snapshot()
            vault_index, vault_catalog = self._inventory_vault_indexes[scan_id]
        else:
            vault_catalog = self._indexed_vault_notes(self.db.get_setting("vault_agent_id", ""))
            vault_index = {}
            for note in vault_catalog:
                properties = note.get("properties", {})
                if not isinstance(properties, dict) or not properties.get("obsync_id"):
                    continue
                metadata = {key: str(value) for key, value in properties.items()}
                metadata["destination_path"] = str(note["path"])
                key = self._identity_key(
                    metadata.get("obsync_machine", ""),
                    metadata.get("obsync_root", ""),
                    metadata.get("obsync_source", ""),
                )
                vault_index.setdefault(key, metadata)

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

            if local_mode or vault_catalog:
                document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
                assert document is not None
                compare = (
                    self._compare_local_document if local_mode else self._compare_indexed_document
                )
                comparison, destination, note_hash, duplicate_path, duplicate_title = compare(
                    document,
                    agent_name=str(agent["name"]),
                    root_name=str(root["name"]),
                    vault_index=vault_index,
                    vault_catalog=vault_catalog,
                )
                values: list[Any] = [
                    comparison,
                    destination,
                    duplicate_path,
                    duplicate_title,
                    now,
                ]
                source_hash_sql = ""
                if note_hash and not document["source_hash"]:
                    source_hash_sql = ", source_hash = ?, status = 'synced', processed_at = ?"
                    values.extend([note_hash, now])
                values.append(document_id)
                self.db.execute(
                    f"""
                    UPDATE documents SET comparison_status = ?, destination_path = ?,
                        duplicate_path = ?, duplicate_title = ?,
                        updated_at = ? {source_hash_sql} WHERE id = ?
                    """,
                    values,
                )
                if (
                    comparison == "possible-duplicate"
                    and document.get("review_resolution") != "approved"
                ):
                    self.db.execute(
                        "UPDATE documents SET status = 'duplicate-review', needs_review = 1 "
                        "WHERE id = ?",
                        (document_id,),
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
            if not local_mode and not vault_catalog:
                vault_agent = self._remote_vault_agent()
                documents = self.db.query_all(
                    """
                            SELECT id, source_path, observed_hash, destination_path,
                                   duplicate_dismissed
                            FROM documents WHERE root_id = ? AND inventory_scan_id = ?
                              AND status != 'ignored'
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
                            "duplicate_policy": self.db.get_setting("duplicate_policy", "review"),
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
        confirmed = self.db.get_setting("vault_confirmed", "false").lower() == "true"
        if mode == "agent":
            agent_id = self.db.get_setting("vault_agent_id", "")
            agent = self.db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
            return {
                "mode": "agent",
                "configured": confirmed,
                "agent_id": agent_id,
                "agent_name": agent["name"] if agent else "",
                "path": agent["vault_path"] if agent else "",
                "exists": bool(agent and agent["vault_ready"]),
                "writable": bool(agent and agent["vault_ready"] and agent["enabled"]),
            }
        return {
            "mode": "local",
            "configured": confirmed,
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
            timeout = int(
                self.db.get_setting("llm_timeout_seconds", str(DEFAULT_LLM_TIMEOUT_SECONDS))
            )
        except ValueError:
            timeout = DEFAULT_LLM_TIMEOUT_SECONDS
        return LLMConfig(
            enabled=enabled,
            provider=self.db.get_setting("llm_provider", "ollama"),
            base_url=self.db.get_setting("llm_base_url", ""),
            model=self.db.get_setting("llm_model", ""),
            api_key=self.db.get_setting("llm_api_key", ""),
            timeout_seconds=max(MIN_LLM_TIMEOUT_SECONDS, min(timeout, MAX_LLM_TIMEOUT_SECONDS)),
            profile=self.active_ai_profile(),
        )

    @staticmethod
    def _candidate_terms(source_path: str, text: str) -> set[str]:
        stop = {
            "about",
            "after",
            "before",
            "could",
            "document",
            "from",
            "have",
            "into",
            "other",
            "should",
            "that",
            "their",
            "there",
            "these",
            "this",
            "with",
        }
        return {
            word
            for word in re.findall(r"[a-z0-9][a-z0-9-]{3,}", f"{source_path} {text[:8000]}".lower())
            if word not in stop
        }

    def _candidate_notes(
        self,
        source_path: str,
        text: str,
        profile: AIProfile,
        *,
        exclude_path: str = "",
    ) -> list[dict[str, Any]]:
        if not profile.use_vault_context or not profile.candidate_limit:
            return []
        catalog = self._indexed_vault_notes()
        if not catalog and self.db.get_setting("vault_mode", "local") == "local":
            _index, catalog = self._local_vault_snapshot()
        if not catalog:
            catalog = [
                {
                    "path": row["destination_path"],
                    "title": row["title"],
                    "tags": [],
                    "aliases": [],
                    "headings": [],
                    "entities": [],
                    "content": row["summary"],
                    "properties": {},
                }
                for row in self.db.query_all(
                    "SELECT DISTINCT destination_path, title, summary FROM documents "
                    "WHERE status = 'synced' AND title != '' ORDER BY updated_at DESC LIMIT 1000"
                )
            ]
        adaptive = AdaptiveVaultIndex(catalog)
        return adaptive.candidates(
            source_path,
            text,
            exclude_path=exclude_path,
            maximum=min(profile.candidate_limit, 50),
        )

    def _new_destination(
        self,
        *,
        document_id: str,
        agent_name: str,
        root: dict[str, Any],
        analysis: Analysis,
        profile: AIProfile,
        candidates: list[dict[str, Any]] | None = None,
    ) -> str:
        segments: list[str]
        learned_folder = (
            Path(analysis.destination_folder) if analysis.destination_folder else Path()
        )
        if profile.organize_folders and str(learned_folder) not in {"", "."}:
            segments = list(learned_folder.parts)
        else:
            segments = [
                str(root["destination"]),
                slugify(agent_name, "device"),
                slugify(str(root["name"]), "folder"),
            ]
            if profile.organize_folders:
                segments.append(slugify(analysis.category, "documents"))
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
        duplicate_path: str = "",
        duplicate_title: str = "",
        review_feedback: str = "",
        force_review: bool = False,
    ) -> dict[str, Any]:
        self.require_pipeline_enabled()
        source_rel = safe_relative_path(source_path).as_posix()
        root = self.db.query_one(
            "SELECT * FROM roots WHERE id = ? AND agent_id = ?", (root_id, agent["id"])
        )
        if not root or not root["enabled"]:
            raise ValueError("Watched folder is unknown or disabled")
        if root.get("sync_state", "running") != "running":
            raise PipelinePausedError(f"Folder syncing is {root['sync_state']}")
        if staged_file.stat().st_size > self.settings.max_upload_bytes:
            raise ValueError(f"File exceeds the {self.settings.max_upload_mb} MB upload limit")

        actual_hash = self._sha256(staged_file)
        if claimed_hash and claimed_hash.lower() != actual_hash:
            raise ValueError("Uploaded file hash does not match the agent manifest")

        duplicate_hint_path = ""
        duplicate_hint_title = str(duplicate_title).strip()[:200]
        if duplicate_path:
            duplicate_hint_path = safe_relative_path(duplicate_path).as_posix()
        if duplicate_hint_path and not likely_same_note_title(
            note_title_from_path(Path(source_rel)), duplicate_hint_title
        ):
            duplicate_hint_path = duplicate_hint_title = ""

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

        review_feedback = str(review_feedback).strip()[:4000]
        if existing and existing.get("status") == "ignored" and not force_review:
            self.db.execute(
                """
                UPDATE documents SET observed_hash = ?, observed_mtime_ns = ?,
                    observed_size = ?, updated_at = ? WHERE id = ?
                """,
                (actual_hash, source_mtime_ns, source_size, utc_now(), existing["id"]),
            )
            row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (existing["id"],))
            assert row is not None
            return {**self._decode_document(row), "result": "ignored"}

        if (
            existing
            and existing["source_hash"] == actual_hash
            and not existing["missing"]
            and existing["comparison_status"] == "in-sync"
            and not force_review
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
        if (
            existing
            and existing.get("comparison_status") == "possible-duplicate"
            and not existing.get("duplicate_dismissed")
            and self.db.get_setting("duplicate_policy", "review") == "review"
            and not force_review
        ):
            return {**self._decode_document(existing), "result": "possible-duplicate"}

        document_id = existing["id"] if existing else str(uuid.uuid4())
        now = utc_now()
        if existing:
            self.db.execute(
                """
                UPDATE documents SET source_mtime_ns = ?, source_size = ?, source_hash = ?,
                    observed_hash = ?, observed_mtime_ns = ?, observed_size = ?,
                    status = 'processing', missing = 0, error = '', needs_review = 0,
                    review_feedback = ?, review_resolution = '', updated_at = ? WHERE id = ?
                """,
                (
                    source_mtime_ns,
                    source_size,
                    actual_hash,
                    actual_hash,
                    source_mtime_ns,
                    source_size,
                    review_feedback,
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
                    status, comparison_status, review_feedback, first_seen_at, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', 'new', ?, ?, ?, ?)
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
                    review_feedback,
                    now,
                    now,
                    now,
                ),
            )

        if (
            duplicate_hint_path
            and self.db.get_setting("duplicate_policy", "review") == "review"
            and not (existing and existing.get("destination_path"))
            and not (existing and existing.get("duplicate_dismissed"))
            and not force_review
        ):
            self.db.execute(
                """
                UPDATE documents SET status = 'duplicate-review',
                    comparison_status = 'possible-duplicate', needs_review = 1,
                    duplicate_path = ?, duplicate_title = ?, error = '', updated_at = ?
                WHERE id = ?
                """,
                (duplicate_hint_path, duplicate_hint_title, utc_now(), document_id),
            )
            self.db.add_event(
                "document.duplicate_review",
                f"Held {source_rel}; it may match {duplicate_hint_title}",
                level="warning",
                agent_id=agent["id"],
                root_id=root_id,
                document_id=document_id,
                details={"duplicate_path": duplicate_hint_path},
            )
            row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
            assert row is not None
            return {**self._decode_document(row), "result": "possible-duplicate"}

        task = asyncio.current_task()
        if task is not None:
            self._active_processing[document_id] = (root_id, task)
            self._start_activity(
                document_id,
                root_id=root_id,
                source_path=source_rel,
                agent_name=str(agent["name"]),
                root_name=str(root["name"]),
                task=task,
            )
        try:
            llm_config = self._llm_config()
            active_profile = llm_config.active_profile
            extracted = await asyncio.to_thread(
                extract_document, staged_file, active_profile.input_char_limit
            )
            vault_candidates = self._candidate_notes(
                source_rel,
                extracted.text,
                active_profile,
                exclude_path=str(existing.get("destination_path", "")) if existing else "",
            )
            match = None
            if not (existing and existing.get("destination_path")) and not (
                existing and existing.get("duplicate_dismissed")
            ):
                match = existing_note_match(
                    source_rel,
                    extracted.text,
                    actual_hash,
                    self._indexed_vault_notes(),
                )
            if match:
                match_path = safe_relative_path(str(match.get("path", ""))).as_posix()
                evidence = [str(item)[:500] for item in match.get("evidence", [])[:20]]
                auto_adopt = match.get("strength") == "exact" or (
                    match.get("strength") == "strong"
                    and self.db.get_setting("existing_note_policy", "review") == "auto"
                )
                if auto_adopt:
                    self.db.execute(
                        """
                        UPDATE documents SET destination_path = ?, vault_adopted = ?,
                            match_evidence_json = ?, duplicate_path = '', duplicate_title = '',
                            updated_at = ? WHERE id = ?
                        """,
                        (
                            match_path,
                            int(not bool(match.get("managed"))),
                            json.dumps(evidence, ensure_ascii=False),
                            utc_now(),
                            document_id,
                        ),
                    )
                    existing = self.db.query_one(
                        "SELECT * FROM documents WHERE id = ?", (document_id,)
                    )
                else:
                    self.db.execute(
                        """
                        UPDATE documents SET status = 'duplicate-review',
                            comparison_status = 'possible-duplicate', needs_review = 1,
                            duplicate_path = ?, duplicate_title = ?, match_evidence_json = ?,
                            error = '', updated_at = ? WHERE id = ?
                        """,
                        (
                            match_path,
                            str(match.get("title", ""))[:200],
                            json.dumps(evidence, ensure_ascii=False),
                            utc_now(),
                            document_id,
                        ),
                    )
                    self.db.add_event(
                        "document.existing_note_review",
                        f"Held {source_rel}; it may update {match.get('title', match_path)}",
                        level="warning",
                        agent_id=agent["id"],
                        root_id=root_id,
                        document_id=document_id,
                        details={
                            "duplicate_path": match_path,
                            "strength": match.get("strength"),
                            "evidence": evidence,
                        },
                    )
                    row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
                    assert row is not None
                    return {**self._decode_document(row), "result": "possible-duplicate"}
            self._update_activity(
                document_id,
                "stage",
                f"Extracted content with {extracted.extractor}.",
                phase="preparing-ai",
                phase_label="Preparing Local AI request",
            )
            activity = self._processing_activity.get(document_id)
            if activity:
                activity["used_ai"] = llm_config.active
                activity["ai_active"] = llm_config.active
                activity["provider"] = llm_config.provider if llm_config.active else "rules"
                activity["model"] = llm_config.model if llm_config.active else ""
                activity["profile_id"] = active_profile.id
                activity["profile_name"] = active_profile.name
                if llm_config.active:
                    activity["phase"] = "inference"
                    activity["phase_label"] = "Local AI is analyzing the file"
                    self._notify_ai_activity()

            def report_progress(kind: str, message: str) -> None:
                phase = "inference"
                phase_label = "Local AI is analyzing the file"
                if kind == "decision":
                    phase = "decision"
                    phase_label = "AI decision received"
                elif kind == "error":
                    phase = "fallback"
                    phase_label = "AI failed; applying safe fallback"
                elif kind == "stage" and "Validating" in message:
                    phase = "validating"
                    phase_label = "Validating AI decision"
                self._update_activity(
                    document_id,
                    kind,
                    message,
                    phase=phase,
                    phase_label=phase_label,
                )

            analyzer = LLMAnalyzer(llm_config, progress=report_progress)
            analysis = await analyzer.analyze(
                source_path=source_rel,
                text=extracted.text,
                mime_type=extracted.mime_type,
                candidates=vault_candidates,
                review_feedback=review_feedback,
                vault_model=self.vault_model_status().get("model", {}),
            )
            try:
                minimum_relationship_confidence = float(
                    self.db.get_setting("vault_relationship_min_confidence", "0.72")
                )
                maximum_links = min(
                    active_profile.related_notes_limit,
                    int(self.db.get_setting("vault_link_limit", "8")),
                    50,
                )
            except ValueError:
                minimum_relationship_confidence, maximum_links = (
                    0.72,
                    min(active_profile.related_notes_limit, 8),
                )
            analysis.relationships = [
                relationship
                for relationship in analysis.relationships
                if relationship.get("evidence")
                and float(relationship.get("confidence", 0.0)) >= minimum_relationship_confidence
            ][:maximum_links]
            analysis.related_notes = [
                str(relationship["target"]) for relationship in analysis.relationships
            ]
            if activity:
                activity["ai_active"] = False
            self._update_activity(
                document_id,
                "stage",
                "Checking the decision against the selected Obsidian vault.",
                phase="checking-vault",
                phase_label="Checking for duplicates and destination conflicts",
            )
            self.require_pipeline_enabled()
            await asyncio.sleep(0)
            try:
                threshold = float(self.db.get_setting("review_threshold", "0.65"))
            except ValueError:
                threshold = 0.65
            needs_review = analysis.confidence < max(0.0, min(1.0, threshold))
            duplicate = None
            if (
                self.db.get_setting("duplicate_policy", "review") == "review"
                and not (existing and existing.get("duplicate_dismissed"))
                and self.db.get_setting("vault_mode", "local") == "local"
                and not match
            ):
                _managed_index, vault_catalog = self._local_vault_snapshot()
                duplicate = self._possible_duplicate(
                    analysis.title,
                    vault_catalog,
                    current_destination=str(existing.get("destination_path", ""))
                    if existing
                    else "",
                )
            if duplicate:
                updated_at = utc_now()
                self.db.execute(
                    """
                    UPDATE documents SET mime_type = ?, title = ?, category = ?, tags_json = ?,
                        summary = ?, analysis_json = ?, status = 'duplicate-review',
                        comparison_status = 'possible-duplicate', llm_status = ?, confidence = ?,
                        needs_review = 1, duplicate_path = ?, duplicate_title = ?, error = '',
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        extracted.mime_type,
                        analysis.title,
                        analysis.category,
                        json.dumps(analysis.tags, ensure_ascii=False),
                        analysis.summary,
                        json.dumps(analysis.as_dict(), ensure_ascii=False),
                        "analyzed" if analysis.provider != "rules" else "rules",
                        analysis.confidence,
                        duplicate["path"],
                        duplicate["title"],
                        updated_at,
                        document_id,
                    ),
                )
                self.db.add_event(
                    "document.duplicate_review",
                    f"Held {source_rel}; it may match {duplicate['title']}",
                    level="warning",
                    agent_id=agent["id"],
                    root_id=root_id,
                    document_id=document_id,
                    details={"duplicate_path": duplicate["path"]},
                )
                row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
                assert row is not None
                return {**self._decode_document(row), "result": "possible-duplicate"}
            destination = (
                existing["destination_path"]
                if existing and existing["destination_path"]
                else self._new_destination(
                    document_id=document_id,
                    agent_name=agent["name"],
                    root=root,
                    analysis=analysis,
                    profile=active_profile,
                    candidates=vault_candidates,
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
                profile=active_profile,
            )
            remote_vault = self._remote_vault_agent()
            self._update_activity(
                document_id,
                "stage",
                "Writing the managed note to the selected Obsidian vault."
                if not remote_vault
                else f"Queueing the managed note for {remote_vault['name']}.",
                phase="writing",
                phase_label="Writing to Obsidian",
            )
            status = "pending-write" if remote_vault else "synced"
            processed_at = None if remote_vault else utc_now()
            if not remote_vault:
                destination_path = safe_vault_path(self.settings.vault_path, destination)
                current = ""
                owned_operations: list[dict[str, Any]] = []
                if destination_path.exists():
                    current = destination_path.read_text(encoding="utf-8")
                    owned_operations = self._owned_operations(
                        destination, content=current, vault_key="local"
                    )
                    if not is_managed_note(current):
                        if existing and existing.get("vault_adopted"):
                            generated = adopt_preserving_original(current, generated)
                        else:
                            raise ValueError(
                                f"Destination already exists and is not managed: {destination}"
                            )
                    else:
                        generated = merge_preserving_manual(current, generated)
                    generated = reapply_owned_operations(current, generated, owned_operations)
                self._atomic_write(destination_path, generated)
                self._upsert_indexed_note(
                    "local",
                    parse_note(destination_path, vault=self.settings.vault_path).as_dict(),
                )

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
                owned_operations = self._owned_operations(
                    destination, vault_key=str(remote_vault["id"])
                )
                self.queue_command(
                    remote_vault["id"],
                    "write_note",
                    {
                        "document_id": document_id,
                        "destination_path": destination,
                        "content": generated,
                        "allow_adopt": bool(existing and existing.get("vault_adopted")),
                        "owned_operations": owned_operations,
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
        except asyncio.CancelledError as exc:
            reason = self._cancel_reasons.pop(document_id, "Stopped by user")
            ai_stopped = reason == "AI inference stopped by user"
            self.db.execute(
                "UPDATE documents SET status = ?, error = ?, needs_review = ?, "
                "review_resolution = '', updated_at = ? WHERE id = ?",
                (
                    "ai-stopped" if ai_stopped else "paused",
                    reason,
                    int(ai_stopped),
                    utc_now(),
                    document_id,
                ),
            )
            self.db.add_event(
                "document.stopped",
                f"Stopped AI review for {source_rel}"
                if ai_stopped
                else f"Stopped processing {source_rel}",
                level="warning",
                agent_id=agent["id"],
                root_id=root_id,
                document_id=document_id,
            )
            raise PipelinePausedError(
                "AI inference was stopped by the user"
                if ai_stopped
                else "Syncing was stopped by the user"
            ) from exc
        except PipelinePausedError:
            self.db.execute(
                "UPDATE documents SET status = 'paused', error = ?, updated_at = ? WHERE id = ?",
                ("Stopped by user", utc_now(), document_id),
            )
            raise
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
        finally:
            active = self._active_processing.get(document_id)
            if active and active[1] is task:
                self._active_processing.pop(document_id, None)
            self._cancel_reasons.pop(document_id, None)
            final = self.db.query_one("SELECT status FROM documents WHERE id = ?", (document_id,))
            used_ai = bool(self._processing_activity.get(document_id, {}).get("used_ai"))
            self._finish_activity(document_id, outcome=str(final["status"]) if final else "unknown")
            self._processing_activity.pop(document_id, None)
            if used_ai:
                self._notify_ai_activity()

    def mark_missing(self, agent_id: str, root_id: str, source_path: str) -> dict[str, Any]:
        self.require_pipeline_enabled()
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
            clauses.append(
                "(d.title LIKE ? OR d.source_path LIKE ? OR d.summary LIKE ? OR d.tags_json LIKE ?)"
            )
            term = f"%{search}%"
            params.extend([term, term, term, term])
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
        self.require_pipeline_enabled()
        root = self.db.query_one(
            "SELECT id, sync_state FROM roots WHERE id = ? AND agent_id = ? AND enabled = 1",
            (root_id, agent_id),
        )
        if not root:
            raise ValueError("Watched folder is unknown or disabled")
        if root["sync_state"] != "running":
            raise PipelinePausedError(f"Folder syncing is {root['sync_state']}")
        return self.db.query_all(
            """
            SELECT id, source_path, comparison_status FROM documents
            WHERE root_id = ? AND status NOT IN ('ai-stopped', 'ignored')
              AND comparison_status NOT IN ('in-sync', 'possible-duplicate', 'ignored')
            ORDER BY source_path
            """,
            (root_id,),
        )

    @staticmethod
    def _decode_document(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json", "[]") or "[]")
        result["analysis"] = json.loads(result.pop("analysis_json", "{}") or "{}")
        result["match_evidence"] = json.loads(result.pop("match_evidence_json", "[]") or "[]")
        return result

    def approve_document(self, document_id: str) -> dict[str, Any]:
        document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise ValueError("Document not found")
        adopt_path = str(document.get("duplicate_path") or "")
        adopted = 0
        if adopt_path:
            indexed = self.db.query_one(
                "SELECT managed FROM vault_notes WHERE vault_key = ? AND path = ?",
                (self._vault_key(), adopt_path),
            )
            adopted = int(not bool(indexed and indexed["managed"]))
        self.db.execute(
            """
            UPDATE documents SET needs_review = 0, review_resolution = 'approved',
                status = CASE WHEN status = 'duplicate-review' THEN 'duplicate-approved'
                              ELSE status END,
                destination_path = CASE WHEN duplicate_path != '' THEN duplicate_path
                                        ELSE destination_path END,
                vault_adopted = CASE WHEN duplicate_path != '' THEN ? ELSE vault_adopted END,
                error = '', updated_at = ? WHERE id = ?
            """,
            (adopted, utc_now(), document_id),
        )
        self.db.add_event(
            "document.review_approved",
            f"Approved the review result for {document['source_path']}",
            agent_id=document["agent_id"],
            root_id=document["root_id"],
            document_id=document_id,
        )
        command = None
        root = self.db.query_one(
            "SELECT root_key, enabled, sync_state FROM roots WHERE id = ?", (document["root_id"],)
        )
        if (
            adopt_path
            and root
            and root["enabled"]
            and root["sync_state"] == "running"
            and self.pipeline_enabled()
        ):
            command = self.queue_command(
                document["agent_id"],
                "resync",
                {"root_key": root["root_key"], "source_path": document["source_path"]},
            )
        return {"ok": True, "command": command, "adopted_path": adopt_path}

    def disregard_document(self, document_id: str) -> dict[str, Any]:
        document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise ValueError("Document not found")
        self.db.execute(
            """
            UPDATE documents SET status = 'ignored', comparison_status = 'ignored',
                needs_review = 0, review_resolution = 'ignored', error = '', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), document_id),
        )
        self.db.add_event(
            "document.disregarded",
            f"Disregarded {document['source_path']}; no note will be created",
            level="warning",
            agent_id=document["agent_id"],
            root_id=document["root_id"],
            document_id=document_id,
        )
        return {"ok": True}

    def redo_ai_review(self, document_id: str, feedback: str = "") -> dict[str, Any]:
        document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise ValueError("Document not found")
        if not self._llm_config().active:
            raise ValueError("Enable and configure Local AI before requesting an AI re-review")
        root = self.db.query_one(
            "SELECT root_key, sync_state, enabled FROM roots WHERE id = ?",
            (document["root_id"],),
        )
        if not root:
            raise ValueError("Watched folder not found")
        if not root["enabled"] or root["sync_state"] != "running":
            raise PipelinePausedError("Start this source folder before requesting an AI re-review")
        clean_feedback = str(feedback).strip()[:4000]
        self.db.execute(
            """
            UPDATE documents SET status = 'review-queued', comparison_status = 'new',
                duplicate_dismissed = 0, needs_review = 0, review_feedback = ?,
                review_resolution = '', error = '', updated_at = ? WHERE id = ?
            """,
            (clean_feedback, utc_now(), document_id),
        )
        command = self.queue_command(
            document["agent_id"],
            "resync",
            {
                "root_key": root["root_key"],
                "source_path": document["source_path"],
                "force_review": True,
                "review_feedback": clean_feedback,
            },
        )
        self.db.add_event(
            "document.review_redo_queued",
            f"Queued a new AI review for {document['source_path']}",
            agent_id=document["agent_id"],
            root_id=document["root_id"],
            document_id=document_id,
            details={"has_feedback": bool(clean_feedback)},
        )
        return {"ok": True, "command": command}

    def allow_duplicate(self, document_id: str) -> dict[str, Any]:
        document = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise ValueError("Document not found")
        root = self.db.query_one(
            "SELECT root_key, sync_state, enabled FROM roots WHERE id = ?",
            (document["root_id"],),
        )
        if not root:
            raise ValueError("Watched folder not found")
        self.db.execute(
            """
            UPDATE documents SET duplicate_dismissed = 1, comparison_status = 'new',
                status = 'discovered', needs_review = 0, error = '', updated_at = ? WHERE id = ?
            """,
            (utc_now(), document_id),
        )
        command = None
        if self.pipeline_enabled() and root["enabled"] and root["sync_state"] == "running":
            command = self.queue_command(
                document["agent_id"],
                "resync",
                {"root_key": root["root_key"], "source_path": document["source_path"]},
            )
        self.db.add_event(
            "document.duplicate_allowed",
            f"Allowed a separate Obsync note for {document['source_path']}",
            agent_id=document["agent_id"],
            root_id=document["root_id"],
            document_id=document_id,
        )
        return {"ok": True, "command": command}

    def queue_command(
        self, agent_id: str, command: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if command in PIPELINE_COMMANDS:
            self.require_pipeline_enabled()
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
        if command["status"] == "cancelled":
            return
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
            if ok:
                content = str(payload.get("content", ""))
                destination = str(payload.get("destination_path", ""))
                if content and destination:
                    cached = self.db.query_one(
                        "SELECT content FROM vault_notes WHERE vault_key = ? AND path = ?",
                        (agent_id, destination),
                    )
                    if cached and str(cached.get("content", "")).strip():
                        current = str(cached["content"])
                        if is_managed_note(current):
                            content = merge_preserving_manual(current, content)
                        elif payload.get("allow_adopt"):
                            content = adopt_preserving_original(current, content)
                        content = reapply_owned_operations(
                            current,
                            content,
                            list(payload.get("owned_operations", [])),
                        )
                    parsed = parse_note(Path(destination), content=content, modified_ns=0).as_dict()
                    self._upsert_indexed_note(agent_id, parsed)
        elif command["command"] == "index_vault" and ok:
            try:
                index_payload = json.loads(result)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Desktop vault index returned invalid data") from exc
            if not isinstance(index_payload, dict):
                raise ValueError("Desktop vault index returned invalid data")
            sweep_id = str(index_payload.get("sweep_id", ""))
            indexed = max(0, int(index_payload.get("indexed", 0)))
            if not sweep_id:
                raise ValueError("Desktop vault index did not identify its sweep")
            stored = self._finalize_remote_index(agent_id, sweep_id)
            self.db.execute(
                "UPDATE vault_sweeps SET total_notes = ?, processed_notes = ?, "
                "changed_notes = changed_notes + ?, current_note = '', updated_at = ? "
                "WHERE id = ?",
                (indexed, indexed, stored["removed"], utc_now(), sweep_id),
            )
            self.db.execute("DELETE FROM vault_sweep_paths WHERE sweep_id = ?", (sweep_id,))
            self.db.add_event(
                "vault.indexed",
                f"Indexed {stored['notes']} notes from the desktop Obsidian vault",
                agent_id=agent_id,
                details={"sweep_id": sweep_id},
            )
        elif command["command"] == "apply_vault_change" and ok:
            content = str(payload.get("content", ""))
            path = safe_relative_path(str(payload.get("path", ""))).as_posix()
            if content and path:
                parsed = parse_note(Path(path), content=content, modified_ns=0).as_dict()
                self._upsert_indexed_note(agent_id, parsed)
        elif command["command"] == "audit_vault" and ok:
            try:
                audit_payload = json.loads(result)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Vault audit returned invalid data") from exc
            vault_notes: list[Any] = []
            if isinstance(audit_payload, dict):
                audit_rows = audit_payload.get("documents", [])
                vault_notes = audit_payload.get("notes", [])
            else:
                audit_rows = audit_payload
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
                if comparison not in {
                    "in-sync",
                    "modified",
                    "new",
                    "vault-missing",
                    "possible-duplicate",
                }:
                    continue
                destination = str(row.get("destination_path", ""))
                note_hash = str(row.get("note_hash", ""))
                duplicate_path = str(row.get("duplicate_path", ""))[:2000]
                duplicate_title = str(row.get("duplicate_title", ""))[:200]
                if document.get("duplicate_dismissed"):
                    duplicate_path = ""
                    duplicate_title = ""
                    if comparison == "possible-duplicate":
                        comparison = "new"
                values: list[Any] = [
                    comparison,
                    destination[:2000],
                    duplicate_path,
                    duplicate_title,
                    now,
                ]
                source_hash_sql = ""
                if note_hash and not document["source_hash"]:
                    source_hash_sql = ", source_hash = ?, status = 'synced', processed_at = ?"
                    values.extend([note_hash, now])
                values.append(audit_document_id)
                self.db.execute(
                    f"""
                    UPDATE documents SET comparison_status = ?, destination_path = ?,
                        duplicate_path = ?, duplicate_title = ?,
                        updated_at = ? {source_hash_sql} WHERE id = ?
                    """,
                    values,
                )
                if (
                    comparison == "possible-duplicate"
                    and document.get("review_resolution") != "approved"
                ):
                    self.db.execute(
                        "UPDATE documents SET status = 'duplicate-review', needs_review = 1 "
                        "WHERE id = ?",
                        (audit_document_id,),
                    )
            if isinstance(vault_notes, list):
                sanitized_notes: list[tuple[str, str, str, str, str]] = []
                for note in vault_notes[:5000]:
                    if not isinstance(note, dict):
                        continue
                    path = str(note.get("path", "")).strip()[:2000]
                    title = str(note.get("title", "")).strip()[:200]
                    raw_tags = note.get("tags", [])
                    tags = (
                        [str(tag).strip()[:80] for tag in raw_tags[:30] if str(tag).strip()]
                        if isinstance(raw_tags, list)
                        else []
                    )
                    if path and title:
                        sanitized_notes.append(
                            (agent_id, path, title, json.dumps(tags, ensure_ascii=False), now)
                        )
                with self.db.transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO vault_notes(vault_key, path, title, tags_json, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(vault_key, path) DO UPDATE SET
                            title = excluded.title,
                            tags_json = excluded.tags_json,
                            updated_at = excluded.updated_at
                        """,
                        sanitized_notes,
                    )
            self.db.add_event(
                "vault.audited",
                f"Compared {len(audit_rows)} files with the desktop Obsidian vault",
                agent_id=agent_id,
            )

    def overview(self) -> dict[str, Any]:
        stats = self.db.dashboard_stats()
        pending_vault_changes = self.db.query_one(
            "SELECT count(*) AS count FROM vault_changes WHERE status = 'pending'"
        )
        stats["document_review"] = stats["review"]
        stats["vault_review"] = int(pending_vault_changes["count"] if pending_vault_changes else 0)
        stats["review"] += stats["vault_review"]
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
            "pipeline": self.pipeline_status(),
            "active_work": self.active_work(),
            "vault_sweep": self.vault_sweep_status()["active"],
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
            "sync_enabled": "true" if self.pipeline_enabled() else "false",
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
            "llm_instructions": False,
            "llm_vault_context": False,
            "duplicate_policy": False,
            "review_threshold": False,
            "existing_note_policy": False,
            "vault_index_schedule_enabled": False,
            "vault_index_schedule_frequency": False,
            "vault_index_schedule_time": False,
            "vault_index_schedule_weekday": False,
            "vault_index_schedule_month_day": False,
            "vault_index_schedule_interval_hours": False,
            "vault_index_change_mode": False,
            "vault_maintenance_schedule_enabled": False,
            "vault_maintenance_schedule_frequency": False,
            "vault_maintenance_schedule_time": False,
            "vault_maintenance_schedule_weekday": False,
            "vault_maintenance_schedule_month_day": False,
            "vault_maintenance_schedule_interval_hours": False,
            "vault_maintenance_change_mode": False,
            "vault_schedule_timezone": False,
            "vault_maintenance_categories": False,
            "vault_relationship_candidate_limit": False,
            "vault_relationship_min_confidence": False,
            "vault_link_limit": False,
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
            vault_leaf = str(agent["vault_path"]).replace("\\", "/").rstrip("/").split("/")[-1]
            if vault_leaf.casefold() == ".obsidian":
                raise ValueError(
                    "Choose the vault folder itself on that computer, not its hidden "
                    ".obsidian folder"
                )
        if requested_mode == "local" and not self.settings.vault_path.is_dir():
            raise ValueError("The server-mounted vault folder is unavailable")
        duplicate_policy = str(payload.get("duplicate_policy", "")).strip()
        if duplicate_policy and duplicate_policy not in {"review", "allow"}:
            raise ValueError("Duplicate policy must be review or allow")
        existing_policy = str(payload.get("existing_note_policy", "")).strip()
        if existing_policy and existing_policy not in {"review", "auto"}:
            raise ValueError("Existing-note handling must be review or auto")
        for key in ("vault_index_change_mode", "vault_maintenance_change_mode"):
            if key not in payload:
                continue
            mode = str(payload[key]).strip()
            allowed_modes = {"index-only"} if "index" in key else {"review", "auto"}
            if mode not in allowed_modes:
                raise ValueError("Sweep change handling is invalid")
        for key in ("vault_index_schedule_frequency", "vault_maintenance_schedule_frequency"):
            if key in payload and str(payload[key]).strip() not in {
                "daily",
                "weekly",
                "monthly",
                "custom",
            }:
                raise ValueError("Sweep frequency must be daily, weekly, monthly, or custom")
        for key in ("vault_index_schedule_time", "vault_maintenance_schedule_time"):
            if key in payload:
                self._parse_schedule_time(str(payload[key]).strip())
        if "vault_schedule_timezone" in payload:
            try:
                ZoneInfo(str(payload["vault_schedule_timezone"]).strip())
            except ZoneInfoNotFoundError:
                raise ValueError("Sweep timezone is invalid") from None
        numeric_ranges = {
            "llm_timeout_seconds": (MIN_LLM_TIMEOUT_SECONDS, MAX_LLM_TIMEOUT_SECONDS),
            "vault_index_schedule_weekday": (0, 6),
            "vault_maintenance_schedule_weekday": (0, 6),
            "vault_index_schedule_month_day": (1, 28),
            "vault_maintenance_schedule_month_day": (1, 28),
            "vault_index_schedule_interval_hours": (1, 8760),
            "vault_maintenance_schedule_interval_hours": (1, 8760),
            "vault_relationship_candidate_limit": (5, 50),
            "vault_link_limit": (1, 20),
        }
        for key, (minimum, maximum) in numeric_ranges.items():
            if key in payload:
                try:
                    number = int(payload[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be a whole number") from None
                if not minimum <= number <= maximum:
                    raise ValueError(f"{key} must be between {minimum} and {maximum}")
        if "vault_relationship_min_confidence" in payload:
            try:
                score = float(payload["vault_relationship_min_confidence"])
            except (TypeError, ValueError):
                raise ValueError("AI relationship confidence must be a number") from None
            if not 0.5 <= score <= 1:
                raise ValueError("AI relationship confidence must be between 0.5 and 1")
        if "vault_maintenance_categories" in payload:
            categories = payload["vault_maintenance_categories"]
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except json.JSONDecodeError:
                    raise ValueError("Maintenance categories are invalid") from None
            allowed_categories = {"links", "tags", "organization"}
            if not isinstance(categories, list) or not set(categories) <= allowed_categories:
                raise ValueError("Maintenance categories are invalid")
            payload = {**payload, "vault_maintenance_categories": json.dumps(categories)}
        if "llm_instructions" in payload and len(str(payload["llm_instructions"])) > 8000:
            raise ValueError("AI instructions must be 8,000 characters or fewer")
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
            if requested_mode:
                values["vault_confirmed"] = ("true", False)
            self.db.set_settings(values)
            if duplicate_policy == "allow":
                self.db.execute(
                    "UPDATE documents SET comparison_status = 'new', needs_review = 0, "
                    "updated_at = ? WHERE comparison_status = 'possible-duplicate'",
                    (utc_now(),),
                )
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
                    config.timeout_seconds = max(
                        MIN_LLM_TIMEOUT_SECONDS,
                        min(int(overrides["llm_timeout_seconds"]), MAX_LLM_TIMEOUT_SECONDS),
                    )
        return await LLMAnalyzer(config).test_connection()
