from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from pathlib import Path

from obsync.config import Settings
from obsync.llm import LLMAnalyzer
from obsync.service import ObsyncService
from obsync.vault_intelligence import (
    exact_duplicate_groups,
    explicit_category_hub_relationships,
    explicit_reciprocal_relationships,
    explicit_reference_relationships,
    native_maintenance_content,
    normalize_obsidian_tag,
    strip_maintenance_block,
)


async def run(args: argparse.Namespace) -> None:
    service = ObsyncService(Settings(data_dir=args.data, vault_path=args.vault, admin_token=""))
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "vault_mode": ("local", False),
            "llm_enabled": ("true", False),
            "llm_provider": ("ollama", False),
            "llm_base_url": (args.url, False),
            "llm_model": (args.model, False),
            "llm_timeout_seconds": (str(args.timeout), False),
            "vault_relationship_candidate_limit": ("20", False),
            "vault_relationship_min_confidence": ("0.72", False),
            "vault_link_limit": ("8", False),
            "vault_maintenance_categories": ('["links", "tags", "organization"]', False),
        }
    )
    index_sweep = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    index_result = service.vault_sweep(index_sweep["id"])
    if index_result["status"] != "completed":
        raise RuntimeError(index_result["error"])

    notes = service._reparse_indexed_notes("local")
    duplicate_groups = exact_duplicate_groups(notes)
    duplicate_canonicals = {
        str(item.get("path", "")): str(group[0].get("path", ""))
        for group in duplicate_groups
        for item in group[1:]
    }
    noncanonical = set(duplicate_canonicals)
    search_index, corpus = service._refresh_vault_corpus_profile(
        notes, noncanonical_paths=noncanonical
    )
    model_output: list[str] = []

    def capture_model_activity(kind: str, message: str) -> None:
        if kind == "output":
            model_output.append(message)

    analyzer = LLMAnalyzer(service._llm_config(), progress=capture_model_activity)
    cached_model = service.db.get_setting("release_audit_model_json", "")
    if cached_model:
        model = json.loads(cached_model)
    else:
        model = await analyzer.learn_vault_model(notes, corpus_profile=corpus)
        service.db.set_settings(
            {"release_audit_model_json": (json.dumps(model, ensure_ascii=False), False)}
        )

    source_connection = sqlite3.connect(args.source_database)
    source_rows = source_connection.execute(
        "SELECT DISTINCT path FROM vault_changes "
        "WHERE status = 'pending' AND change_type = 'native-maintenance' ORDER BY path"
    ).fetchall()
    source_paths = [str(row[0]) for row in source_rows]
    by_path = {str(note["path"]): note for note in notes}
    reports = []

    def save_reports() -> None:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    for position, source_path in enumerate(source_paths, start=1):
        if args.positions and position not in args.positions:
            continue
        if position < args.start:
            continue
        if args.end and position > args.end:
            break
        note = by_path.get(source_path)
        if not note:
            reports.append({"path": source_path, "error": "source not indexed"})
            save_reports()
            continue
        if source_path in duplicate_canonicals:
            report = {
                "position": position,
                "path": source_path,
                "duplicate_of": duplicate_canonicals[source_path],
                "relationships": [],
                "tags": [],
                "proposed_moves": [],
                "proposed_memberships": [],
                "operations": [],
                "operation_count": 0,
                "content_changed": False,
                "unsafe_anchors": [],
                "low_value_tags": [],
            }
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            save_reports()
            continue
        candidates = search_index.candidates(
            source_path,
            strip_maintenance_block(str(note.get("content", ""))),
            exclude_path=source_path,
            maximum=60,
            source_note=note,
        )
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("anchor_options")
            or candidate.get("already_linked")
            or candidate.get("structural_role") == "category-hub"
        ][:20]
        tag_vocabulary = search_index.tag_vocabulary_for(note)
        model_output.clear()
        try:
            decision = await analyzer.adjudicate_relationships(
                note,
                candidates,
                vault_model=model,
                minimum_confidence=0.72,
                maximum_links=8,
                feedback=[],
                owned_operations=[],
                tag_vocabulary=tag_vocabulary,
                allowed_folders=[
                    path for path, _count in search_index.folder_counts.most_common(150)
                ],
            )
        except Exception as exc:
            report = {
                "position": position,
                "path": source_path,
                "error": f"{type(exc).__name__}: {exc}",
                "model_output": "".join(model_output)[-20_000:],
            }
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            save_reports()
            continue
        relationships = list(decision["relationships"])
        existing_targets = {str(item["target"]).casefold() for item in relationships}
        for explicit in [
            *explicit_reciprocal_relationships(note, candidates),
            *explicit_category_hub_relationships(note, candidates),
            *explicit_reference_relationships(note, candidates),
        ]:
            key = str(explicit["target"]).casefold()
            if key not in existing_targets:
                relationships.append(explicit)
                existing_targets.add(key)
        tag_decisions = [
            {**item, "tag": normalize_obsidian_tag(item.get("tag", ""))}
            for item in decision["suggested_tags"]
            if normalize_obsidian_tag(item.get("tag", "")).casefold()
            in {tag.casefold() for tag in tag_vocabulary}
        ]
        after, operations = native_maintenance_content(
            str(note.get("content", "")),
            relationships,
            suggested_tags=[item["tag"] for item in tag_decisions],
        )
        actual_targets = {
            str(item.get("target", "")).casefold()
            for item in operations
            if item.get("kind") == "inline-link"
        }
        relationships = [
            item
            for item in relationships
            if str(item.get("target", "")).split("|", 1)[0].removesuffix(".md").casefold()
            in actual_targets
        ]
        actual_tag_keys = {
            str(item.get("tag", "")).casefold()
            for item in operations
            if item.get("kind") == "frontmatter-tag" and item.get("action") == "add"
        }
        tag_decisions = [
            item for item in tag_decisions if item["tag"].casefold() in actual_tag_keys
        ]
        unsafe_anchors = []
        for item in relationships:
            anchor = str(item["anchor"])
            words = anchor.casefold().split()
            if (
                anchor.casefold() in {"owner", "status", "updated", "current"}
                or re.fullmatch(r"\d+(?:[-/:.]\d+)*", anchor)
                or any(marker in anchor for marker in (",", ";", ":"))
                or anchor.startswith((".", ",", ":", ";"))
                or anchor.endswith((".", ",", ":", ";"))
                or (words and words[0] in {"create", "watch"})
                or (words and words[-1] == "where")
            ):
                unsafe_anchors.append(anchor)
        low_tags = [
            item["tag"]
            for item in tag_decisions
            if item["tag"] in {"ai", "business", "phase", "project"}
        ]
        reports.append(
            {
                "position": position,
                "path": source_path,
                "summary": decision["summary"],
                "relationships": [
                    {
                        "anchor": item["anchor"],
                        "target": item["target"],
                        "confidence": item["confidence"],
                    }
                    for item in relationships
                ],
                "tags": [item["tag"] for item in tag_decisions],
                "proposed_moves": decision["organization_operations"],
                "proposed_memberships": decision["index_memberships"],
                "operations": [
                    {
                        key: item.get(key)
                        for key in ("action", "kind", "anchor", "target", "tag")
                        if item.get(key) not in {None, ""}
                    }
                    for item in operations
                ],
                "operation_count": len(operations),
                "content_changed": after != str(note.get("content", "")),
                "unsafe_anchors": unsafe_anchors,
                "low_value_tags": low_tags,
            }
        )
        print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
        save_reports()

    failures = [
        item
        for item in reports
        if item.get("error") or item.get("unsafe_anchors") or item.get("low_value_tags")
    ]
    summary = {
        "indexed_notes": len(notes),
        "audited_sources": len(reports),
        "relationships": sum(len(item.get("relationships", [])) for item in reports),
        "tags": sum(len(item.get("tags", [])) for item in reports),
        "proposed_moves": sum(len(item.get("proposed_moves", [])) for item in reports),
        "proposed_memberships": sum(len(item.get("proposed_memberships", [])) for item in reports),
        "failures": failures,
    }
    print("AUDIT_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    save_reports()
    if failures:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--positions", type=lambda value: {int(item) for item in value.split(",")})
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
