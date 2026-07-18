from __future__ import annotations

import os
from pathlib import Path

import pytest

from obsync.config import Settings
from obsync.service import ObsyncService
from obsync.vault_intelligence import (
    MAINTENANCE_START,
    add_backlinks,
    parse_note,
)

REAL_AI_URL = os.environ.get("OBSYNC_REAL_AI_URL", "").strip()
REAL_AI_MODEL = os.environ.get("OBSYNC_REAL_AI_MODEL", "").strip()

pytestmark = [
    pytest.mark.real_ai,
    pytest.mark.skipif(
        not (REAL_AI_URL and REAL_AI_MODEL),
        reason="Set OBSYNC_REAL_AI_URL and OBSYNC_REAL_AI_MODEL to run real Local AI E2E tests",
    ),
]


def _write(vault: Path, relative: str, content: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _configure_real_ai(service: ObsyncService) -> None:
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "sync_enabled": ("true", False),
            "llm_enabled": ("true", False),
            "llm_provider": ("ollama", False),
            "llm_base_url": (REAL_AI_URL, False),
            "llm_model": (REAL_AI_MODEL, False),
            "llm_timeout_seconds": ("600", False),
            "vault_relationship_candidate_limit": ("16", False),
            "vault_relationship_min_confidence": ("0.78", False),
            "vault_link_limit": ("8", False),
        }
    )


@pytest.mark.asyncio
async def test_real_ai_global_sync_is_independent_from_vault_sweeps(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "atlas.txt"
    source.write_text(
        "Orion Research commissioned Project Atlas field observations.", encoding="utf-8"
    )
    service = ObsyncService(
        Settings(
            data_dir=tmp_path / "sync-data",
            vault_path=tmp_path / "sync-vault",
            admin_token="",
        )
    )
    _configure_real_ai(service)
    enrollment = service.create_enrollment("E2E computer")
    registration = service.register_agent(
        enrollment["code"],
        {"name": "E2E computer", "hostname": "e2e", "os_name": "test"},
    )
    agent = service.db.query_one("SELECT * FROM agents WHERE id = ?", (registration["agent_id"],))
    assert agent is not None
    root = service.upsert_root(
        registration["agent_id"],
        {
            "root_key": "real-ai-e2e",
            "name": "Research source",
            "path": str(source_root),
            "destination": "Imported Knowledge",
        },
    )

    first = await service.process_file(
        agent=agent,
        root_id=root["id"],
        source_path="atlas.txt",
        source_mtime_ns=source.stat().st_mtime_ns,
        source_size=source.stat().st_size,
        staged_file=source,
    )
    note = service.settings.vault_path / first["destination_path"]
    assert first["result"] == "synced"
    assert note.is_file()
    assert "Orion Research commissioned Project Atlas field observations." in note.read_text(
        encoding="utf-8"
    )
    assert service.list_vault_changes()["total"] == 0
    assert service.db.query_one("SELECT count(*) AS count FROM vault_edit_ownership")["count"] == 0

    source.write_text(
        "Orion Research commissioned the revised Project Atlas field observations.",
        encoding="utf-8",
    )
    second = await service.process_file(
        agent=agent,
        root_id=root["id"],
        source_path="atlas.txt",
        source_mtime_ns=source.stat().st_mtime_ns + 1,
        source_size=source.stat().st_size,
        staged_file=source,
    )
    assert second["id"] == first["id"]
    assert "revised Project Atlas" in note.read_text(encoding="utf-8")
    assert all(
        relationship["target"].split("|", 1)[0] != first["destination_path"].removesuffix(".md")
        for relationship in second["analysis"]["relationships"]
    )
    assert service.list_vault_changes()["total"] == 0


@pytest.mark.asyncio
async def test_real_ai_index_and_native_maintenance_end_to_end(tmp_path: Path) -> None:
    vault = tmp_path / "sweep-vault"
    service = ObsyncService(
        Settings(data_dir=tmp_path / "sweep-data", vault_path=vault, admin_token="")
    )
    _configure_real_ai(service)
    _write(
        vault,
        "Indexes/Field Report Index.md",
        """---
tags: [field-report]
---
# Field Report Index

This is the category home for Project Atlas field reports.
It catalogs [[Reports/Field Report 001]], [[Reports/Field Report 002]], and
[[Reports/Field Report 003]].
""",
    )
    _write(
        vault,
        "Reports/Field Report 001.md",
        """---
tags: [field-report]
---
# Field Report 001

Routine field report for Project Cedar commissioned by Nova Institute.
Includes status, observations, deposit, and follow-up schedule.
""",
    )
    _write(
        vault,
        "Reports/Field Report 002.md",
        """---
tags: [field-report]
---
# Field Report 002

Routine field report for Project Birch commissioned by Solstice Group.
Includes status, observations, deposit, and follow-up schedule.
""",
    )
    source_note = _write(
        vault,
        "Reports/Field Report 003.md",
        """---
tags: [field-report]
---
# Field Report 003

Orion Research commissioned this field report for Project Atlas.
The category home is the Field Report Index.
Includes status, observations, deposit, and follow-up schedule.
""",
    )
    _write(
        vault,
        "Organizations/Orion Research.md",
        """---
tags: [organization]
aliases: [Orion]
---
# Orion Research

Orion Research commissioned Project Atlas field work, including Field Report 003.
""",
    )
    _write(
        vault,
        "Indexes/Purchase Order Index.md",
        "# Purchase Order Index\n\nCategory home for purchase orders.\n",
    )
    _write(
        vault,
        "Orders/Purchase Order 101.md",
        "# Purchase Order 101\n\nNorthstar Labs ordered sensor equipment for Project Delta.\n",
    )
    _write(
        vault,
        "Orders/Purchase Order 102.md",
        "# Purchase Order 102\n\nHelios Works ordered safety equipment for Project Echo.\n",
    )
    _write(
        vault,
        "Organizations/Northstar Labs.md",
        "# Northstar Labs\n\nNorthstar Labs purchased sensor equipment under Purchase Order 101.\n",
    )
    before_index = {
        path.relative_to(vault).as_posix(): path.read_bytes() for path in vault.rglob("*.md")
    }

    index = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep(index["id"])["status"] == "completed"
    after_index = {
        path.relative_to(vault).as_posix(): path.read_bytes() for path in vault.rglob("*.md")
    }
    assert after_index == before_index
    corpus = service.vault_model_status()["corpus"]
    assert corpus["note_count"] == 9
    assert {item["path"] for item in corpus["existing_category_hubs"]} >= {
        "Indexes/Field Report Index.md",
        "Indexes/Purchase Order Index.md",
    }

    before_maintenance = source_note.read_text(encoding="utf-8")
    maintenance = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep(maintenance["id"])["status"] == "completed"
    assert source_note.read_text(encoding="utf-8") == before_maintenance

    changes = service.list_vault_changes(status="pending")["items"]
    source_change = next(
        change for change in changes if change["path"] == "Reports/Field Report 003.md"
    )
    diff = service.vault_change_diff(source_change["id"])
    decision_targets = {
        relationship["target"] for relationship in diff["decision"]["relationships"]
    }
    assert {
        "Organizations/Orion Research",
        "Indexes/Field Report Index",
    } <= decision_targets
    assert not any(target.startswith("Reports/Field Report 00") for target in decision_targets)
    membership_sources = {
        operation.get("source_target", "")
        for change in changes
        if change["change_type"] == "index-membership"
        for operation in change["decision"].get("operations", [])
    }
    assert "Organizations/Orion Research" not in membership_sources
    assert "Organizations/Northstar Labs" not in membership_sources
    assert MAINTENANCE_START not in diff["after_content"]
    assert "Related knowledge" not in diff["after_content"]
    for operation in diff["decision"]["operations"]:
        if operation["kind"] == "inline-link" and operation["action"] == "add":
            assert operation["anchor"] in diff["before_content"]

    await service.approve_vault_change(source_change["id"])
    applied = source_note.read_text(encoding="utf-8")
    assert "[[Organizations/Orion Research|Orion Research]]" in applied
    assert "[[Indexes/Field Report Index|Field Report Index]]" in applied
    notes = [parse_note(path, vault=vault).as_dict() for path in vault.rglob("*.md")]
    add_backlinks(notes)
    by_path = {note["path"]: note for note in notes}
    assert "Reports/Field Report 003.md" in by_path["Organizations/Orion Research.md"]["backlinks"]
    assert "Reports/Field Report 003.md" in by_path["Indexes/Field Report Index.md"]["backlinks"]

    undo = await service.undo_vault_sweep(maintenance["id"])
    assert undo["reverted"] >= 1
    assert source_note.read_text(encoding="utf-8") == before_maintenance
