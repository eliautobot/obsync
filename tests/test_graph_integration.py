from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsync.config import Settings
from obsync.knowledge_graph import normalize_graph_claims
from obsync.llm import LLMAnalyzer
from obsync.markdown import note_tags
from obsync.service import ObsyncService, utc_now
from obsync.vault_intelligence import (
    MAINTENANCE_END,
    MAINTENANCE_START,
    AdaptiveVaultIndex,
    apply_native_operations,
    graph_navigation_support,
    native_maintenance_content,
)


def write_note(vault: Path, relative: str, content: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_legacy_cleanup_migrates_block_only_tags_and_enforces_dependency() -> None:
    original = (
        "---\ntags: [guide, human-tag]\n---\n"
        "# Local Model Guide\n\nKeep this prose.\n\n"
        f"{MAINTENANCE_START}\n"
        "Related tags: #ollama #local-ai #windows #obsync\n"
        f"{MAINTENANCE_END}\n"
    )

    preview, operations = native_maintenance_content(original, [])

    assert set(note_tags(preview)) == {"guide", "human-tag", "ollama", "local-ai", "windows"}
    assert MAINTENANCE_START not in preview
    cleanup = next(item for item in operations if item["kind"] == "legacy-maintenance-block")
    tag_operations = [item for item in operations if item["kind"] == "frontmatter-tag"]
    assert set(cleanup["requires_tags"]) == {"ollama", "local-ai", "windows"}
    assert all(item["migration_source"] == "legacy-maintenance-block" for item in tag_operations)

    blocked, applied = apply_native_operations(original, [cleanup])
    assert blocked == original
    assert applied == []

    migrated, applied = apply_native_operations(original, [*tag_operations, cleanup])
    assert MAINTENANCE_START not in migrated
    assert set(note_tags(migrated)) == {"guide", "human-tag", "ollama", "local-ai", "windows"}
    assert len(applied) == 4


def test_legacy_cleanup_fails_closed_when_yaml_cannot_preserve_tags() -> None:
    original = (
        "---\ntags:\n  - guide # keep this human comment\n---\n"
        "# Guide\n\n"
        f"{MAINTENANCE_START}\nRelated tags: #ollama\n{MAINTENANCE_END}\n"
    )

    preview, operations = native_maintenance_content(original, [])

    assert preview == original
    assert operations == []


def test_navigation_support_suppresses_reciprocal_and_common_entity_links() -> None:
    source = {
        "path": "Ops/Checklist.md",
        "title": "Checklist",
        "content": "# Checklist\n\nWorkspace Backup Plan and Virtual Office are reviewed.\n",
        "graph_version": 2,
        "graph_edges": [
            {
                "id": "edge:backup",
                "source_id": "document:ops/checklist",
                "source_name": "Checklist",
                "predicate": "references_named_document",
                "target_id": "document:ops/workspace backup plan",
                "target_name": "Workspace Backup Plan",
                "evidence": "Workspace Backup Plan",
                "confidence": 1.0,
                "source_kind": "structural",
                "state": "active",
            },
            {
                "id": "edge:office",
                "source_id": "document:ops/checklist",
                "source_name": "Checklist",
                "predicate": "uses_system",
                "target_id": "entity:virtual-office",
                "target_name": "Virtual Office",
                "evidence": "Checklist uses Virtual Office",
                "confidence": 0.95,
                "source_kind": "semantic",
                "state": "active",
            },
        ],
        "links": [],
    }
    backup = {
        "path": "Ops/Workspace Backup Plan.md",
        "title": "Workspace Backup Plan",
        "content": "# Workspace Backup Plan\n\nRecovery instructions.\n",
        "links": [],
        "graph_mentions": [],
        "persistent_graph_nodes": [],
    }
    reciprocal = {**backup, "links": ["Ops/Checklist"]}
    category_hub = {
        **backup,
        "structural_role": "category-hub",
        "links": ["Ops/Checklist"],
    }
    common_office = {
        "path": "Guides/Office Guide.md",
        "title": "Office Guide",
        "content": "# Office Guide\n\nVirtual Office guide.\n",
        "links": [],
        "graph_mentions": [
            {
                "entity_id": "entity:virtual-office",
                "entity_name": "Virtual Office",
                "quote": "Virtual Office",
                "aliases": [],
            }
        ],
        "persistent_graph_nodes": [
            {
                "id": "entity:virtual-office",
                "name": "Virtual Office",
                "document_frequency": 50,
                "specificity": 0.4,
            }
        ],
    }

    assert graph_navigation_support(source, backup)[0]["anchor"] == "Workspace Backup Plan"
    assert graph_navigation_support(source, reciprocal) == []
    assert graph_navigation_support(source, category_hub)[0]["predicate"] == "cataloged_by"
    assert graph_navigation_support(source, common_office) == []


@pytest.mark.asyncio
async def test_index_builds_persistent_graph_with_exact_reference_provenance(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    source = write_note(
        settings.vault_path,
        "Operations/Setup Checklist.md",
        "# Setup Checklist\n\n- [x] Review Workspace Backup Plan before launch.\n",
    )
    write_note(
        settings.vault_path,
        "Operations/Workspace Backup Plan.md",
        "---\ntags: [operations, backup]\n---\n"
        "# Workspace Backup Plan\n\nEncrypted recovery design.\n",
    )
    write_note(
        settings.vault_path,
        "Legacy/Generated Noise.md",
        f"# Generated Noise\n\n{MAINTENANCE_START}\n\nWorkspace Backup Plan\n\n{MAINTENANCE_END}\n",
    )
    before = source.read_bytes()

    sweep = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task

    edge = service.db.query_one(
        "SELECT * FROM vault_graph_edges WHERE vault_key='local' "
        "AND predicate='references_named_document'"
    )
    assert edge and edge["source_path"] == "Operations/Setup Checklist.md"
    assert edge["evidence"] == "Workspace Backup Plan"
    assert edge["source_chunk_id"]
    chunk = service.db.query_one(
        "SELECT * FROM vault_graph_chunks WHERE vault_key='local' AND id=?",
        (edge["source_chunk_id"],),
    )
    assert chunk
    assert (
        source.read_text(encoding="utf-8")[edge["start_offset"] : edge["end_offset"]]
        == edge["evidence"]
    )
    assert source.read_bytes() == before
    assert (
        service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_edges WHERE vault_key='local' "
            "AND predicate='references_named_document'"
        )["count"]
        == 1
    )
    assert (
        service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_hierarchy WHERE vault_key='local'"
        )["count"]
        >= 2
    )
    assert (
        service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_state WHERE semantic_status='pending'"
        )["count"]
        == 3
    )
    assert service.vault_sweep(sweep["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_maintenance_review_has_expected_links_tags_cleanup_and_live_counters(
    tmp_path: Path, adaptive_ai
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    checklist = write_note(
        settings.vault_path,
        "Operations/Setup Checklist.md",
        "# Setup Checklist\n\n"
        "- [x] Review Workspace Backup Plan.\n"
        "- [ ] Help people who want a more flexible office.\n"
        "- [ ] Define exact Free vs Pro positioning.\n",
    )
    write_note(
        settings.vault_path,
        "Operations/Workspace Backup Plan.md",
        "# Workspace Backup Plan\n\nEncrypted recovery design for the workspace.\n",
    )
    legacy = write_note(
        settings.vault_path,
        "Guides/Ollama Guide.md",
        "---\ntags: [guide]\n---\n# Ollama Guide\n\nLocal setup.\n\n"
        f"{MAINTENANCE_START}\nRelated tags: #ollama #local-ai #windows\n"
        f"{MAINTENANCE_END}\n",
    )
    before = {path: path.read_bytes() for path in (checklist, legacy)}

    service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    maintenance = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    completed = service.vault_sweep(maintenance["id"])
    changes = service.list_vault_changes(status="pending")["items"]
    operations = [
        operation for change in changes for operation in change["decision"].get("operations", [])
    ]
    link_operations = [item for item in operations if item.get("kind") == "inline-link"]
    anchors = {str(item.get("anchor", "")) for item in link_operations}
    assert "Workspace Backup Plan" in anchors
    assert (
        not {
            "who want a more",
            "define exact Free vs Pro",
            "around Virtual Office",
        }
        & anchors
    )
    migrated_tags = {
        item["tag"]
        for item in operations
        if item.get("migration_source") == "legacy-maintenance-block"
    }
    assert migrated_tags == {"ollama", "local-ai", "windows"}
    assert any(item.get("kind") == "legacy-maintenance-block" for item in operations)
    assert completed["processed_notes"] == 3
    assert completed["recommendations"] == len(changes)
    assert completed["changed_notes"] == len(changes)
    assert completed["recommendations"] > 0
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.asyncio
async def test_semantic_prepass_persists_claims_and_enables_graph_first_retrieval(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(
        settings.vault_path,
        "Projects/Deployment Runbook.md",
        "# Deployment Runbook\n\nEnt Forge (EF) depends on Docker Desktop for container builds.\n",
    )
    write_note(
        settings.vault_path,
        "Guides/Docker Desktop Guide.md",
        "# Docker Desktop Guide\n\nDocker Desktop runs container builds for EF.\n",
    )
    extracted_paths: list[str] = []

    async def extract_graph(
        _self,
        note,
        *,
        vault_model,
        allowed_predicates,
    ):
        del vault_model
        extracted_paths.append(note["path"])
        project = (
            {"name": "Ent Forge", "type": "project", "aliases": ["EF"]}
            if note["path"] == "Projects/Deployment Runbook.md"
            else {"name": "EF", "type": "project", "aliases": []}
        )
        entities = [
            project,
            {"name": "Docker Desktop", "type": "system", "aliases": []},
        ]
        claims = []
        if note["path"] == "Projects/Deployment Runbook.md":
            claims.append(
                {
                    "source_entity": "Ent Forge",
                    "source_type": "project",
                    "predicate": "depends_on",
                    "target_entity": "Docker Desktop",
                    "target_type": "system",
                    "description": "Ent Forge depends on Docker Desktop for container builds.",
                    "evidence": "Ent Forge (EF) depends on Docker Desktop for container builds.",
                    "confidence": 0.97,
                    "state": "active",
                }
            )
        return normalize_graph_claims(
            note,
            {"entities": entities, "claims": claims},
            allowed_predicates=allowed_predicates,
        )

    monkeypatch.setattr(LLMAnalyzer, "extract_note_graph", extract_graph)
    service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    maintenance = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    claim = service.db.query_one(
        "SELECT * FROM vault_graph_edges WHERE source_kind='semantic' AND predicate='depends_on'"
    )
    assert claim and claim["evidence"] == (
        "Ent Forge (EF) depends on Docker Desktop for container builds."
    )
    assert claim["source_chunk_id"] and claim["content_hash"]
    assert (
        service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_state WHERE semantic_status='ready'"
        )["count"]
        == 2
    )
    project_entities = service.db.query_all(
        "SELECT * FROM vault_graph_entities WHERE entity_type='project'"
    )
    assert len(project_entities) == 1
    assert project_entities[0]["name"] == "Ent Forge"
    assert "EF" in json.loads(project_entities[0]["aliases_json"])

    notes = service._reparse_indexed_notes("local")
    service._attach_graph_memory("local", notes)
    source = next(item for item in notes if item["path"] == "Projects/Deployment Runbook.md")
    candidates = AdaptiveVaultIndex(notes).candidates(
        source["path"], source["content"], exclude_path=source["path"], source_note=source
    )
    guide = next(item for item in candidates if item["path"] == "Guides/Docker Desktop Guide.md")
    assert guide["anchor_options"][0]["text"] == "Docker Desktop"
    assert guide["knowledge_graph"]["supported_edges"][0]["predicate"] == "depends_on"
    assert service.vault_sweep(maintenance["id"])["status"] == "completed"
    assert sorted(extracted_paths) == [
        "Guides/Docker Desktop Guide.md",
        "Projects/Deployment Runbook.md",
    ]

    service.start_vault_sweep("index", change_mode="index-only")
    assert service._sweep_task is not None
    await service._sweep_task
    assert (
        service.db.query_one(
            "SELECT count(*) AS count FROM vault_graph_state WHERE semantic_status='ready'"
        )["count"]
        == 2
    )
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    assert len(extracted_paths) == 2


def test_graph_feedback_uses_reviewed_operation_features(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    operation = {
        "operation_id": "operation-1",
        "kind": "inline-link",
        "anchor": "Workspace Backup Plan",
        "predicate": "references_named_document",
    }

    service._record_graph_feedback([operation], approved_ids={"operation-1"})
    service._record_graph_feedback([operation], approved_ids=set())

    predicate = service.db.query_one(
        "SELECT * FROM vault_graph_feedback WHERE feature_type='predicate' "
        "AND feature_value='references_named_document'"
    )
    assert predicate
    assert predicate["approvals"] == 1
    assert predicate["rejections"] == 1
    assert predicate["weight"] == pytest.approx(0.5)


def test_schema_14_migration_supersedes_old_graph_decisions_and_resets_model(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id,sweep_type,vault_key,status,change_mode,created_at,"
        "updated_at) "
        "VALUES ('old-sweep','maintenance','local','completed','review',?,?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="old-sweep",
        note={"path": "Old.md", "content": "# Old\n"},
        after_content="# Old\n\n[[Generic Plan|plan]]\n",
        reason="Old graph-shaped decision",
        evidence=["generic phrase"],
        confidence=0.8,
    )
    service.db.execute(
        "INSERT OR REPLACE INTO vault_models(vault_key,status,model_json,fingerprint,provider,"
        "model_name,note_count,error,corpus_json,corpus_fingerprint,updated_at) "
        "VALUES ('local','ready','{}','old','ollama','old-model',1,'','{}','old-corpus',?)",
        (now,),
    )
    service.db.set_settings({"vault_link_limit": ("8", False)})
    service.db.execute("UPDATE schema_meta SET version=13")

    service.db.initialize()

    change = service.db.query_one("SELECT * FROM vault_changes WHERE id=?", (change_id,))
    model = service.db.query_one("SELECT * FROM vault_models WHERE vault_key='local'")
    assert change and change["status"] == "superseded"
    assert "provenance-backed factual maintenance" in change["error"]
    assert model and model["status"] == "not-learned"
    assert model["fingerprint"] == ""
    assert model["corpus_fingerprint"] == ""
    assert service.db.get_setting("vault_link_limit") == "3"
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 14
