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
from obsync.vault_intelligence import MAINTENANCE_END, MAINTENANCE_START, content_hash

UNSAFE_ANCHORS = {
    "owner",
    "status",
    "updated",
    "current",
    "date",
    "number",
    "who want a more",
    "define exact free vs pro",
    "around virtual office",
    "draft for eli review",
    "expose host port",
    "goal is to keep cody",
    "archo must perform",
}
LOW_VALUE_TAGS = {"ai", "business", "phase", "project", "estimate-list"}
EXPECTED_ANCHORS = {"Workspace Backup Plan", "Ent Forge Outreach Scripts"}


def build_fixture_vault(vault: Path, *, note_count: int = 511) -> None:
    """Create a disposable release corpus with explicit positive and negative outcomes."""

    fixtures = {
        "Operations/Setup Checklist.md": (
            "# Setup Checklist\n\n"
            "- [x] Review Workspace Backup Plan before launch.\n"
            "- [x] Update Ent Forge Outreach Scripts for the campaign.\n"
            "- [ ] Create a backup plan for temporary exports.\n"
            "- [ ] Help people who want a more flexible office.\n"
            "- [ ] Define exact Free vs Pro positioning.\n"
        ),
        "Operations/Workspace Backup Plan.md": (
            "# Workspace Backup Plan\n\nEncrypted recovery plan for the Ent Forge workspace.\n"
        ),
        "Sales/Ent Forge Outreach Scripts.md": (
            "# Ent Forge Outreach Scripts\n\nApproved named scripts for the Ent Forge campaign.\n"
        ),
        "Guides/Ollama Guide.md": (
            "---\ntags: [guide, human-tag]\n---\n# Ollama Guide\n\nLocal model setup.\n\n"
            f"{MAINTENANCE_START}\nRelated tags: #ollama #local-ai #windows #obsync\n"
            f"{MAINTENANCE_END}\n"
        ),
        "Projects/Atlas Overview.md": (
            "# Atlas Overview\n\nReview Atlas Record before closing the project.\n"
        ),
        "Projects/Atlas Record.md": (
            "# Atlas Record\n\nThe canonical record links back to [[Projects/Atlas Overview]].\n"
        ),
        "Product/Positioning Notes.md": (
            "# Positioning Notes\n\nThe sentence says define exact Free vs Pro positioning.\n"
        ),
    }
    if note_count < len(fixtures):
        raise ValueError(f"note_count must be at least {len(fixtures)}")
    for relative, content in fixtures.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for number in range(1, note_count - len(fixtures) + 1):
        path = vault / f"Archive/Record {number:04d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Record {number:04d}\n\nArchive identifier ARC-{number:04d}. "
            "Routine status text with no cross-document claim.\n",
            encoding="utf-8",
        )


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
            supports = [
                item
                for item in candidate.get("knowledge_graph", {}).get("supported_edges", [])
                if isinstance(item, dict)
                and str(item.get("anchor", "")).casefold() == str(option.get("text", "")).casefold()
            ]
            if not supports:
                continue
            support = supports[0]
            target = str(candidate.get("link_target", ""))
            anchor = str(option.get("text", ""))
            if not target or not anchor:
                continue
            target_evidence = candidate.get("content_excerpt", candidate.get("title", target))
            relationships.append(
                {
                    "target": target,
                    "anchor": anchor,
                    "anchor_occurrence": int(option.get("occurrence", 0) or 0),
                    "anchor_context": str(option.get("context", "")),
                    "relationship_type": "specific-record",
                    "graph_edge_id": str(support.get("id", "")),
                    "source_entity": str(support.get("source_entity", "")),
                    "target_entity": str(support.get("target_entity", "")),
                    "predicate": str(support.get("predicate", "")),
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

    async def extract_note_graph(
        _self,
        note: dict[str, Any],
        *,
        vault_model: dict[str, Any],
        allowed_predicates: set[str],
    ) -> dict[str, Any]:
        del note, vault_model, allowed_predicates
        return {"chunks": [], "entities": [], "mentions": [], "claims": []}

    LLMAnalyzer.learn_vault_model = learn_vault_model
    LLMAnalyzer.adjudicate_relationships = adjudicate_relationships
    LLMAnalyzer.extract_note_graph = extract_note_graph


async def run(args: argparse.Namespace) -> None:
    if args.generate_fixture:
        build_fixture_vault(args.vault, note_count=args.fixture_notes)
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
            "vault_link_limit": ("3", False),
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
    observed_anchors: set[str] = set()
    legacy_migration_tags: set[str] = set()
    graph_contract_failures: list[dict[str, str]] = []
    operation_counts: dict[str, int] = {}
    link_counts: dict[str, int] = {}
    for change in pending:
        operations = decoded_operations(change)
        operation_counts[str(change["id"])] = len(operations)
        link_counts[str(change["id"])] = sum(
            item.get("kind") == "inline-link" and item.get("action") == "add" for item in operations
        )
        for operation in operations:
            kind = str(operation.get("kind", ""))
            if operation.get("migration_source") == "legacy-maintenance-block":
                legacy_migration_tags.add(str(operation.get("tag", "")))
            if kind == "inline-link" and operation.get("action") == "add":
                anchor = str(operation.get("anchor", "")).strip()
                observed_anchors.add(anchor)
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
                if not all(
                    str(operation.get(field, "")).strip()
                    for field in (
                        "graph_edge_id",
                        "source_entity",
                        "target_entity",
                        "predicate",
                    )
                ):
                    graph_contract_failures.append({"path": str(change["path"]), "anchor": anchor})
            if kind == "frontmatter-tag" and operation.get("action") == "add":
                tag = str(operation.get("tag", "")).casefold()
                if tag in LOW_VALUE_TAGS:
                    unsafe.append({"path": str(change["path"]), "tag": tag})
    if unsafe:
        raise RuntimeError(f"Unsafe generated operations: {json.dumps(unsafe[:20])}")
    if graph_contract_failures:
        raise RuntimeError(
            "Inline links missing graph contract: " + json.dumps(graph_contract_failures[:20])
        )
    if args.generate_fixture and not observed_anchors >= EXPECTED_ANCHORS:
        raise RuntimeError(
            f"Expected anchors missing: {sorted(EXPECTED_ANCHORS - observed_anchors)}"
        )
    if args.generate_fixture and "Atlas Record" in observed_anchors:
        raise RuntimeError("Reciprocal Atlas Record navigation was not suppressed")
    if args.generate_fixture and legacy_migration_tags != {"ollama", "local-ai", "windows"}:
        raise RuntimeError(f"Legacy tag migration mismatch: {sorted(legacy_migration_tags)}")
    if any(count > 3 for count in link_counts.values()):
        raise RuntimeError("A recommendation exceeded the conservative per-note link bound")

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
        "graph_contract_failures": graph_contract_failures,
        "expected_anchors": sorted(EXPECTED_ANCHORS & observed_anchors),
        "legacy_migration_tags": sorted(legacy_migration_tags),
        "reciprocal_anchor_suppressed": "Atlas Record" not in observed_anchors,
        "graph_entities": service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_entities"
        )["count"],
        "graph_edges": service.db.query_one("SELECT count(*) AS count FROM vault_graph_edges")[
            "count"
        ],
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
    parser.add_argument("--generate-fixture", action="store_true")
    parser.add_argument("--fixture-notes", type=int, default=511)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
