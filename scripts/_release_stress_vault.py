from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from obsync.config import Settings
from obsync.llm import LLMAnalyzer
from obsync.service import ObsyncService
from obsync.vault_intelligence import content_hash

UNSAFE_ANCHORS = {"owner", "status", "updated", "current", "date", "number"}
LOW_VALUE_TAGS = {"ai", "business", "phase", "project", "estimate-list"}


def vault_hashes(vault: Path) -> dict[str, str]:
    return {
        path.relative_to(vault).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(vault.rglob("*.md"))
        if path.is_file()
    }


def decoded_operations(change: dict[str, Any]) -> list[dict[str, Any]]:
    decision = change.get("decision", {})
    operations = decision.get("operations", []) if isinstance(decision, dict) else []
    return [item for item in operations if isinstance(item, dict)]


def install_deterministic_scale_harness() -> None:
    """Exercise all sweep mechanics without making 512 sequential model calls."""

    async def learn_vault_model(
        _self,
        notes: list[dict[str, Any]],
        *,
        feedback: list[dict[str, Any]] | None = None,
        corpus_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del feedback, corpus_profile
        return {
            "vault_summary": "Deterministic full-vault release scale harness.",
            "organization_principles": [],
            "note_patterns": [],
            "folder_hierarchy": [],
            "entity_types": [],
            "relationship_types": [],
            "canonicalization_rules": [],
            "low_value_relationship_patterns": [],
            "tag_guidance": [],
            "relationship_guidance": [],
            "confidence": 1.0,
            "provider": "release-scale-harness",
            "model": "deterministic",
            "note_count": len(notes),
        }

    async def adjudicate_relationships(
        _self,
        source_note: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        vault_model: dict[str, Any],
        minimum_confidence: float,
        maximum_links: int,
        feedback: list[dict[str, Any]] | None = None,
        owned_operations: list[dict[str, Any]] | None = None,
        tag_vocabulary: list[str] | None = None,
        allowed_folders: list[str] | None = None,
    ) -> dict[str, Any]:
        del (
            vault_model,
            minimum_confidence,
            feedback,
            owned_operations,
            tag_vocabulary,
            allowed_folders,
        )
        relationships: list[dict[str, Any]] = []
        for candidate in candidates:
            if (
                candidate.get("already_linked")
                or candidate.get("structural_role") == "category-hub"
            ):
                continue
            option = next(
                (
                    item
                    for item in candidate.get("anchor_options", [])
                    if item.get("reason") == "exact target title, alias, or identifier"
                    and not any(marker in str(item.get("text", "")) for marker in (",", ";", ":"))
                ),
                None,
            )
            if not option:
                continue
            target = str(candidate.get("link_target", ""))
            anchor = str(option.get("text", ""))
            if not target or not anchor:
                continue
            target_evidence = candidate.get("content_excerpt", candidate.get("title", target))
            source_nodes = source_note.get("knowledge_graph", {}).get("entity_nodes", [])
            target_nodes = candidate.get("knowledge_graph", {}).get("entity_nodes", [])
            source_document = next(
                (item for item in source_nodes if item.get("type") == "document"), {}
            )
            target_document = next(
                (item for item in target_nodes if item.get("type") == "document"), {}
            )
            relationships.append(
                {
                    "target": target,
                    "anchor": anchor,
                    "anchor_occurrence": int(option.get("occurrence", 0) or 0),
                    "anchor_context": str(option.get("context", "")),
                    "relationship_type": "specific-record",
                    "source_entity": str(source_document.get("name", "")),
                    "target_entity": str(target_document.get("name", "")),
                    "predicate": "references_named_document",
                    "relationship": (
                        f"{source_note.get('title', source_note.get('path', 'Source'))} "
                        f"explicitly names {candidate.get('title', target)}"
                    ),
                    "evidence": [
                        f"SOURCE: {option.get('context', anchor)}",
                        f"TARGET: {target_evidence}",
                    ],
                    "confidence": 1.0,
                }
            )
            if len(relationships) >= max(0, min(maximum_links, 2)):
                break
        return {
            "source_category": "release-scale-harness",
            "source_role": "indexed-note",
            "summary": "Deterministic scale decision using exact target names only.",
            "suggested_tags": [],
            "relationships": relationships,
            "organization_operations": [],
            "index_memberships": [],
            "obsolete_owned_links": [],
            "obsolete_owned_tags": [],
        }

    LLMAnalyzer.learn_vault_model = learn_vault_model
    LLMAnalyzer.adjudicate_relationships = adjudicate_relationships


async def run(args: argparse.Namespace) -> None:
    if args.deterministic_scale:
        install_deterministic_scale_harness()
    before = vault_hashes(args.vault)
    service = ObsyncService(
        Settings(data_dir=args.data, vault_path=args.vault, admin_token="release-audit")
    )
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "vault_mode": ("local", False),
            "llm_enabled": ("true", False),
            "llm_provider": ("ollama", False),
            "llm_base_url": (args.url, False),
            "llm_model": (args.model, False),
            "llm_timeout_seconds": (str(args.timeout), False),
            "vault_relationship_candidate_limit": (str(args.candidate_limit), False),
            "vault_relationship_min_confidence": ("0.72", False),
            "vault_link_limit": ("8", False),
            "vault_maintenance_categories": ('["links", "tags", "organization"]', False),
        }
    )

    index = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    index = service.vault_sweep(index["id"])
    if index["status"] != "completed":
        raise RuntimeError(index["error"])

    maintenance = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    maintenance = service.vault_sweep(maintenance["id"])
    if maintenance["status"] != "completed":
        raise RuntimeError(maintenance["error"])
    if vault_hashes(args.vault) != before:
        raise RuntimeError("Review-mode maintenance changed the safe-copy vault")

    pending = service.list_vault_changes(status="pending", limit=500)["items"]
    unsafe: list[dict[str, str]] = []
    operation_counts: dict[str, int] = {}
    for change in pending:
        operations = decoded_operations(change)
        operation_counts[str(change["id"])] = len(operations)
        for operation in operations:
            kind = str(operation.get("kind", ""))
            if kind == "inline-link" and operation.get("action") == "add":
                anchor = str(operation.get("anchor", "")).strip()
                folded = anchor.casefold().strip(" .,:;!?()[]{}\"'`")
                words = folded.split()
                if (
                    folded in UNSAFE_ANCHORS
                    or re.fullmatch(r"\d+(?:[-/:.]\d+)*", folded)
                    or any(marker in anchor for marker in (",", ";", ":"))
                    or anchor.startswith((".", ",", ":", ";"))
                    or anchor.endswith((".", ",", ":", ";"))
                    or (words and words[0] in {"create", "watch"})
                    or (words and words[-1] == "where")
                ):
                    unsafe.append({"path": str(change["path"]), "anchor": anchor})
            if kind == "frontmatter-tag" and operation.get("action") == "add":
                tag = str(operation.get("tag", "")).casefold()
                if tag in LOW_VALUE_TAGS:
                    unsafe.append({"path": str(change["path"]), "tag": tag})
    if unsafe:
        raise RuntimeError(f"Unsafe generated operations: {json.dumps(unsafe[:20])}")

    selective: dict[str, Any] = {"tested": False}
    native = next(
        (
            change
            for change in pending
            if change.get("change_type") == "native-maintenance"
            and len(decoded_operations(change)) >= 2
            and any(operation.get("action") == "add" for operation in decoded_operations(change))
        ),
        None,
    )
    if native:
        original_path = args.vault / str(native["path"])
        original = original_path.read_text(encoding="utf-8")
        chosen = next(
            operation
            for operation in decoded_operations(native)
            if operation.get("action") == "add"
        )
        await service.approve_vault_change(native["id"], [str(chosen["operation_id"])])
        applied_path = original_path
        if chosen.get("kind") == "move-note":
            applied_path = args.vault / str(chosen["destination_path"])
        applied = applied_path.read_text(encoding="utf-8")
        if content_hash(applied) == content_hash(original):
            raise RuntimeError("Selective apply did not change the selected safe-copy note")
        undo = await service.undo_vault_sweep(maintenance["id"])
        if not undo["ok"] or vault_hashes(args.vault) != before:
            raise RuntimeError(f"Undo did not restore the safe-copy vault: {undo}")
        selective = {
            "tested": True,
            "path": native["path"],
            "selected_kind": chosen.get("kind"),
            "selected_operation_count": 1,
            "card_operation_count": len(decoded_operations(native)),
            "undo_reverted": undo["reverted"],
        }

    summary = {
        "schema": service.db.query_one("SELECT version FROM schema_meta LIMIT 1")["version"],
        "indexed_notes": service.vault_sweep_status()["indexed_notes"],
        "index_status": index["status"],
        "maintenance_status": maintenance["status"],
        "processed_notes": maintenance["processed_notes"],
        "recommendations": maintenance["recommendations"],
        "pending_cards": len(pending),
        "operation_count": sum(operation_counts.values()),
        "event_failures": service.db.query_one(
            "SELECT count(*) AS count FROM events "
            "WHERE event_type = 'vault.relationship_decision_failed'"
        )["count"],
        "review_mode_vault_unchanged": True,
        "unsafe_operations": unsafe,
        "selective_apply_and_undo": selective,
        "decision_mode": "deterministic-scale" if args.deterministic_scale else "real-model",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("STRESS_SUMMARY=" + json.dumps(summary), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--candidate-limit", type=int, default=12)
    parser.add_argument("--deterministic-scale", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
