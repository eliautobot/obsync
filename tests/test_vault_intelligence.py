from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obsync.config import Settings
from obsync.llm import LLMAnalyzer, LLMRequestTimeoutError
from obsync.service import ObsyncService, utc_now
from obsync.vault_intelligence import (
    MAINTENANCE_END,
    MAINTENANCE_START,
    AdaptiveVaultIndex,
    add_backlinks,
    existing_note_match,
    maintenance_content,
    parse_note,
    rank_notes,
    strip_maintenance_block,
)


def write_note(vault: Path, relative: str, content: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_whole_vault_note_parser_captures_obsidian_knowledge_graph(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    company = write_note(
        vault,
        "Companies/Pro Quality Plumbing.md",
        """---
aliases: [PQP, Pro Quality]
tags: [company, plumbing]
account: PQP-4419
---
# Pro Quality Plumbing
## Accounts
Permit account PQP-4419 belongs to Pro Quality Plumbing Inc.
See [[Clients/Acme Holdings|Acme Holdings]]. #florida
""",
    )
    client = write_note(vault, "Clients/Acme Holdings.md", "# Acme Holdings\n\nClient record.")
    notes = [
        parse_note(company, vault=vault).as_dict(),
        parse_note(client, vault=vault).as_dict(),
    ]
    add_backlinks(notes)

    parsed = notes[0]
    assert parsed["title"] == "Pro Quality Plumbing"
    assert parsed["aliases"] == ["PQP", "Pro Quality"]
    assert set(parsed["tags"]) >= {"company", "plumbing", "florida"}
    assert "Accounts" in parsed["headings"]
    assert parsed["links"] == ["Clients/Acme Holdings"]
    assert "account:pqp-4419" in parsed["entities"]
    assert "Pro Quality Plumbing" in parsed["entities"]
    assert notes[1]["backlinks"] == ["Companies/Pro Quality Plumbing.md"]
    assert parsed["content"] == company.read_text(encoding="utf-8")


def test_whole_vault_frontmatter_is_json_safe(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = write_note(
        vault,
        "Records/Dated record.md",
        """---
created: 2026-07-16
updated: 2026-07-16T12:34:56Z
history:
  - date: 2026-07-15
date-keyed:
  2026-07-14: active
not-a-number: .nan
flags: !!set {beta: null, alpha: null}
binary: !!binary |
  SGVsbG8=
---
# Dated record
""",
    )

    parsed = parse_note(note, vault=vault).as_dict()

    assert parsed["properties"]["created"] == "2026-07-16"
    assert parsed["properties"]["updated"] == "2026-07-16T12:34:56+00:00"
    assert parsed["properties"]["history"][0]["date"] == "2026-07-15"
    assert parsed["properties"]["date-keyed"] == {"2026-07-14": "active"}
    assert parsed["properties"]["flags"] == ["alpha", "beta"]
    assert parsed["properties"]["binary"] == "Hello"
    json.dumps(parsed, allow_nan=False)


def test_entity_ranking_links_company_client_account_and_application(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    paths = [
        write_note(vault, "Companies/Pro Quality Plumbing.md", "# Pro Quality Plumbing\n"),
        write_note(
            vault,
            "Clients/Acme Holdings.md",
            "# Acme Holdings\nClient account ACME-7788.",
        ),
        write_note(
            vault,
            "Accounts/ACME-7788.md",
            "# ACME-7788\nAccount number ACME-7788 for Acme Holdings.",
        ),
        write_note(vault, "Unrelated/Grocery List.md", "# Grocery List\nMilk and bread."),
    ]
    notes = [parse_note(path, vault=vault).as_dict() for path in paths]
    source = (
        "Pro Quality Plumbing submitted an account application for Acme Holdings. "
        "Account number ACME-7788 must be approved by Friday."
    )
    ranked = rank_notes("Applications/Acme account application.pdf", source, notes, limit=20)
    targets = [item["link_target"] for item in ranked if item["score"] >= 24]

    assert "Companies/Pro Quality Plumbing" in targets
    assert "Clients/Acme Holdings" in targets
    assert "Accounts/ACME-7788" in targets
    assert all("Grocery" not in target for target in targets)
    assert any("shared record identifier" in item["reasons"] for item in ranked)


def test_existing_note_match_distinguishes_exact_strong_and_ambiguous(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    exact_path = write_note(vault, "Accounts/Water Account.md", "Account number WTR-9911 active.")
    exact = parse_note(exact_path, vault=vault).as_dict()
    match = existing_note_match(
        "Water Account.txt",
        "Account number WTR-9911 active.",
        "not-managed",
        [exact],
    )
    assert match and match["strength"] == "exact"

    strong_path = write_note(
        vault,
        "Applications/Water Account.md",
        "# Water Account\nAccount number WTR-9911 has an older mailing address.",
    )
    strong = parse_note(strong_path, vault=vault).as_dict()
    match = existing_note_match(
        "Water Account.pdf",
        "Water Account update. Account number WTR-9911 now uses a new mailing address.",
        "new-hash",
        [strong],
    )
    assert match and match["strength"] == "strong"
    assert "same stable record identifier" in match["evidence"]

    duplicate = {**strong, "path": "Archive/Water Account.md"}
    ambiguous = existing_note_match(
        "Water Account.pdf",
        "Water Account update. Account number WTR-9911 now uses a new mailing address.",
        "new-hash",
        [strong, duplicate],
    )
    assert ambiguous and ambiguous["strength"] == "ambiguous"


def test_maintenance_relationship_block_is_bounded_and_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    related = [
        parse_note(
            write_note(
                vault,
                "Companies/Pro Quality Plumbing.md",
                "---\ntags: [company]\n---\n# Pro Quality Plumbing",
            ),
            vault=vault,
        ).as_dict(),
        parse_note(
            write_note(vault, "Clients/Acme.md", "---\ntags: [client]\n---\n# Acme"),
            vault=vault,
        ).as_dict(),
    ]
    original = "# Account Application\n\nOriginal user-authored content.\n"
    first = maintenance_content(original, related)
    second = maintenance_content(first, related)

    assert first == second
    assert first.count(MAINTENANCE_START) == 1
    assert first.count(MAINTENANCE_END) == 1
    assert "[[Companies/Pro Quality Plumbing]]" in first
    assert "[[Clients/Acme]]" in first
    assert "Original user-authored content." in first
    assert "#company" in first and "#client" in first


def test_generated_maintenance_block_never_becomes_index_or_retrieval_evidence(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    content = """# Invoice 5297

Actual fact: Client Alpha owns account A-5297.

<!-- obsync:maintenance:start -->
## Related knowledge
- [[Invoices/Invoice 1001]] — similar invoice
- [[Invoices/Invoice 1002]] — similar invoice
Related tags: #invoice-template #generated-only
<!-- obsync:maintenance:end -->
"""
    parsed = parse_note(
        write_note(vault, "Invoices/Invoice 5297.md", content), vault=vault
    ).as_dict()

    assert parsed["links"] == []
    assert "generated-only" not in parsed["tags"]
    assert "Invoices/Invoice 1001" not in parsed["entities"]
    assert "Actual fact" in parsed["content"]
    assert "Invoice 1001" not in strip_maintenance_block(parsed["content"])


def test_adaptive_retrieval_is_only_a_shortlist_for_same_type_records() -> None:
    notes = [
        {
            "path": f"Invoices/Invoice {number}.md",
            "title": f"Invoice {number}",
            "tags": ["invoice"],
            "aliases": [],
            "headings": ["Invoice"],
            "properties": {"invoice": str(number)},
            "entities": [f"invoice:{number}"],
            "content": f"Invoice {number}. Standard invoice template and payment terms.",
        }
        for number in range(1000, 1200)
    ]
    source = notes[97]
    candidates = AdaptiveVaultIndex(notes).candidates(
        source["path"], source["content"], source_note=source, exclude_path=source["path"]
    )

    assert candidates
    assert all(item["title"].startswith("Invoice ") for item in candidates)
    # Retrieval intentionally has high recall; only the separate AI adjudicator may create links.
    assert all("relationship" not in item for item in candidates)


@pytest.mark.asyncio
async def test_index_and_maintenance_sweeps_apply_review_and_undo(
    tmp_path: Path, adaptive_ai
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    original = "# Pro Quality Plumbing Account\n\nClient Acme Holdings account ACME-7788.\n"
    account = write_note(settings.vault_path, "Accounts/PQP Account.md", original)
    write_note(
        settings.vault_path,
        "Companies/Pro Quality Plumbing.md",
        "# Pro Quality Plumbing\n\nCompany serving Acme Holdings.",
    )
    write_note(
        settings.vault_path,
        "Clients/Acme Holdings.md",
        "# Acme Holdings\n\nClient account ACME-7788.",
    )

    index = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep(index["id"])["status"] == "completed"
    assert service.vault_sweep_status()["indexed_notes"] == 3

    maintenance = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    completed = service.vault_sweep(maintenance["id"])
    assert completed["status"] == "completed"
    changes = service.list_vault_changes(status="pending")["items"]
    assert changes
    account_change = next(
        change for change in changes if change["path"] == "Accounts/PQP Account.md"
    )
    diff = service.vault_change_diff(account_change["id"])
    assert "Pro Quality Plumbing" in diff["after_content"]
    assert diff["before_content"] == original

    await service.approve_vault_change(account_change["id"])
    assert MAINTENANCE_START in account.read_text(encoding="utf-8")
    undo = await service.undo_vault_sweep(maintenance["id"])
    assert undo["reverted"] >= 1
    assert account.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_maintenance_sweep_streams_model_thinking_output_and_decisions(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(settings.vault_path, "Companies/Example.md", "# Example\n\nNamed company record.")

    async def learn(analyzer, notes, *, feedback=None):
        analyzer._emit("reasoning", "I am learning the observed vault structure.")
        analyzer._emit("output", '{"vault_summary":"Example vault"}')
        return {
            "vault_summary": "Example vault",
            "confidence": 0.9,
            "provider": "ollama",
            "model": "test-model",
            "note_count": len(notes),
        }

    async def adjudicate(
        analyzer,
        source_note,
        candidates,
        *,
        vault_model,
        minimum_confidence,
        maximum_links,
        feedback=None,
    ):
        analyzer._emit("reasoning", f"Checking evidence for {source_note['path']}.")
        analyzer._emit("output", '{"relationships":[]}')
        return {
            "source_category": "",
            "source_role": "company record",
            "summary": "No supported cross-note relationships.",
            "suggested_tags": [],
            "relationships": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", learn)
    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)

    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    activity = service.ai_activity()
    maintenance = activity["sweeps"]["maintenance"]
    session = maintenance["last"]
    assert maintenance["active"] == []
    assert session["sweep_id"] == sweep["id"]
    assert session["activity_kind"] == "sweep"
    assert session["outcome"] == "completed"
    assert session["cancellable"] is False
    assert session["current_note"] == "Companies/Example.md"
    event_kinds = [event["kind"] for event in session["events"]]
    event_text = "\n".join(event["message"] for event in session["events"])
    assert {"stage", "reasoning", "output", "decision"} <= set(event_kinds)
    assert "learning the observed vault structure" in event_text
    assert "No supported cross-note relationships" in event_text
    assert activity["sweeps"]["index"] == {"active": [], "last": None}


@pytest.mark.asyncio
async def test_maintenance_does_not_link_unrelated_invoices_with_shared_template_words(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    for number in range(5200, 5300):
        details = "Standard invoice template. Payment due in 30 days."
        if number == 5297:
            details += (
                " Pro Quality Plumbing billed Client Alpha for Project Zephyr. "
                "This record belongs in the Invoice Index."
            )
        write_note(
            settings.vault_path,
            f"Invoices/Invoice {number}.md",
            f"# Invoice {number}\n\n{details}",
        )
    write_note(
        settings.vault_path,
        "Companies/Pro Quality Plumbing.md",
        "# Pro Quality Plumbing\n\nCompany participating in Project Zephyr.",
    )
    write_note(
        settings.vault_path,
        "Clients/Client Alpha.md",
        "# Client Alpha\n\nClient for Project Zephyr.",
    )
    write_note(
        settings.vault_path,
        "Indexes/Invoice Index.md",
        "# Invoice Index\n\nIndex of billing records including Invoice 5297.",
    )

    async def adjudicate(
        _self,
        source_note,
        candidates,
        *,
        vault_model,
        minimum_confidence,
        maximum_links,
        feedback=None,
    ):
        relationships = []
        if source_note["path"] == "Invoices/Invoice 5297.md":
            allowed = {
                "Companies/Pro Quality Plumbing": "issued by the named company",
                "Clients/Client Alpha": "bills the named client",
                "Indexes/Invoice Index": "is cataloged by this index",
            }
            for candidate in candidates:
                target = str(candidate.get("link_target", ""))
                if target in allowed:
                    relationships.append(
                        {
                            "target": target,
                            "relationship": allowed[target],
                            "evidence": [
                                f"SOURCE: Invoice 5297 names {candidate['title']}",
                                f"TARGET: {candidate['title']} records the matching role",
                            ],
                            "confidence": 0.96,
                        }
                    )
        return {
            "source_category": "",
            "source_role": "billing record",
            "summary": "Only concrete parties and the explicit index are related.",
            "suggested_tags": [],
            "relationships": relationships,
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    assert service.vault_sweep(sweep["id"])["status"] == "completed"
    change = next(
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["path"] == "Invoices/Invoice 5297.md"
    )
    diff = service.vault_change_diff(change["id"])
    after = diff["after_content"]
    assert "[[Companies/Pro Quality Plumbing]]" in after
    assert "[[Clients/Client Alpha]]" in after
    assert "[[Indexes/Invoice Index]]" in after
    assert not any(f"[[Invoices/Invoice {number}]]" in after for number in range(5200, 5300))
    assert len(diff["decision"]["relationships"]) == 3


@pytest.mark.asyncio
async def test_completed_empty_index_clears_stale_notes_and_records_completion(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    note = write_note(settings.vault_path, "Old.md", "# Old")

    first = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep(first["id"])["status"] == "completed"
    assert service.vault_sweep_status()["indexed_notes"] == 1

    note.unlink()
    second = service.start_vault_sweep("index", change_mode="index-only")
    assert service._sweep_task is not None
    await service._sweep_task

    status = service.vault_sweep_status()
    assert service.vault_sweep(second["id"])["status"] == "completed"
    assert status["indexed_notes"] == 0
    assert status["last_indexed_at"] == service.vault_sweep(second["id"])["finished_at"]


@pytest.mark.asyncio
async def test_sweep_rejects_concurrent_note_edit_and_preserves_user_change(
    tmp_path: Path, adaptive_ai
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    target = write_note(
        settings.vault_path, "Projects/Project Alpha.md", "# Project Alpha\nClient Acme."
    )
    write_note(settings.vault_path, "Clients/Acme.md", "# Acme\nProject Alpha client.")
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    change = next(
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["path"] == "Projects/Project Alpha.md"
    )
    target.write_text("# Project Alpha\nClient Acme.\nHuman edit after review.", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after"):
        await service.approve_vault_change(change["id"])
    assert "Human edit after review" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_automatic_maintenance_applies_changes_and_retains_rollback(
    tmp_path: Path, adaptive_ai
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    target = write_note(
        settings.vault_path,
        "Accounts/Acme Account.md",
        "# Acme Account\nPro Quality Plumbing account ACME-1000.",
    )
    original = target.read_text(encoding="utf-8")
    write_note(
        settings.vault_path,
        "Companies/Pro Quality Plumbing.md",
        "# Pro Quality Plumbing\nAcme account ACME-1000.",
    )

    sweep = service.start_vault_sweep("maintenance", change_mode="auto")
    assert service._sweep_task is not None
    await service._sweep_task

    finished = service.vault_sweep(sweep["id"])
    assert finished["status"] == "completed"
    assert finished["applied_changes"] >= 1
    assert MAINTENANCE_START in target.read_text(encoding="utf-8")
    assert service.list_vault_changes(status="applied")["total"] >= 1
    result = await service.undo_vault_sweep(sweep["id"])
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_ai_failure_never_removes_an_existing_generated_block(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    original = maintenance_content(
        "# Record\n\nHuman-authored fact.\n",
        [
            {
                "target": "People/Client Alpha",
                "relationship": "Client Alpha owns this record",
            }
        ],
        suggested_tags=[],
    )
    note = write_note(settings.vault_path, "Records/Record.md", original)

    async def fail(*_args, **_kwargs):
        raise ValueError("model unavailable")

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", fail)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    assert service.vault_sweep(sweep["id"])["status"] == "failed"
    assert service.list_vault_changes(status="pending")["total"] == 0
    assert note.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_successful_empty_ai_decision_repairs_an_overlinked_generated_block(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    original_user_content = "# Invoice 5297\n\nHuman-authored invoice facts.\n"
    overlinked = maintenance_content(
        original_user_content,
        [
            {
                "target": f"Invoices/Invoice {number}",
                "relationship": "similar invoice",
            }
            for number in range(1000, 1050)
        ],
        suggested_tags=[],
    )
    note = write_note(settings.vault_path, "Invoices/Invoice 5297.md", overlinked)

    async def empty_decision(*_args, **_kwargs):
        return {
            "source_category": "",
            "source_role": "billing record",
            "summary": "No candidate has a concrete cross-record relationship.",
            "suggested_tags": [],
            "relationships": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", empty_decision)
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    change = service.list_vault_changes(status="pending")["items"][0]

    assert change["path"] == "Invoices/Invoice 5297.md"
    assert "remove the previous generated block" in change["reason"].casefold() or change[
        "reason"
    ].startswith("No candidate")
    await service.approve_vault_change(change["id"])
    assert note.read_text(encoding="utf-8") == original_user_content


def test_maintenance_requires_local_ai_before_it_is_queued(tmp_path: Path) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(service.settings.vault_path, "One.md", "# One")

    with pytest.raises(ValueError, match="require Local AI"):
        service.start_vault_sweep("maintenance", change_mode="review")
    assert service.vault_sweep_status()["active"] is None


def test_v014_migration_supersedes_static_pending_changes_and_clamps_old_limit(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('old-sweep', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="old-sweep",
        note={"path": "Invoice.md", "content": "# Invoice\n"},
        after_content="# Invoice\n\n[[Other Invoice]]\n",
        reason="Old static similarity",
        evidence=["shared word"],
        confidence=0.8,
    )
    service.db.set_settings({"vault_link_limit": ("100", False)})
    service.db.execute("UPDATE schema_meta SET version = 8")

    service.db.initialize()

    change = service.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
    assert change and change["status"] == "superseded"
    assert "adaptive relationship engine" in change["error"]
    assert service.db.get_setting("vault_link_limit") == "20"


def test_v0151_migration_raises_only_the_legacy_default_ai_timeout(tmp_path: Path) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    service.db.set_settings({"llm_timeout_seconds": ("120", False)})
    service.db.execute("UPDATE schema_meta SET version = 9")

    service.db.initialize()

    assert service.db.get_setting("llm_timeout_seconds") == "600"
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 10

    service.db.set_settings({"llm_timeout_seconds": ("900", False)})
    service.db.execute("UPDATE schema_meta SET version = 9")
    service.db.initialize()
    assert service.db.get_setting("llm_timeout_seconds") == "900"


def test_review_feedback_changes_the_adaptive_vault_model_fingerprint(
    tmp_path: Path, adaptive_ai
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    adaptive_ai(service)
    config = service._llm_config()
    notes = [parse_note(Path("One.md"), content="# One\nFact.").as_dict()]

    before = service._vault_model_fingerprint(notes, [], config)
    after = service._vault_model_fingerprint(
        notes,
        [
            {
                "source_path": "One.md",
                "outcome": "rejected",
                "relationships": [{"target": "Two", "relationship": "weak"}],
            }
        ],
        config,
    )

    assert before != after


@pytest.mark.asyncio
async def test_adaptive_vault_model_is_persisted_and_reused(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    adaptive_ai(service)
    notes = [parse_note(Path("One.md"), content="# One\nConcrete fact.").as_dict()]
    calls = 0

    async def learn(_self, learned_notes, *, feedback=None):
        nonlocal calls
        calls += 1
        return {
            "vault_summary": "A one-note test vault.",
            "confidence": 0.88,
            "provider": "ollama",
            "model": "test-model",
            "note_count": len(learned_notes),
        }

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", learn)
    first, _feedback = await service._learn_vault_model(notes, "not-a-real-sweep")
    second, _feedback = await service._learn_vault_model(notes, "not-a-real-sweep")
    status = service.vault_model_status()

    assert first == second
    assert calls == 1
    assert status["status"] == "ready"
    assert status["model"]["vault_summary"] == "A one-note test vault."


@pytest.mark.asyncio
async def test_vault_model_learning_failure_is_visible_and_not_cached(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    adaptive_ai(service)

    async def fail(_self, _notes, *, feedback=None):
        raise ValueError("invalid learned model")

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", fail)
    with pytest.raises(ValueError, match="invalid learned model"):
        await service._learn_vault_model(
            [parse_note(Path("One.md"), content="# One").as_dict()], "missing-sweep"
        )

    status = service.vault_model_status()
    assert status["status"] == "failed"
    assert status["error"] == "invalid learned model"


@pytest.mark.asyncio
async def test_maintenance_timeout_is_visible_in_model_and_sweep_errors(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    adaptive_ai(service)
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "llm_timeout_seconds": ("600", False),
        }
    )
    write_note(service.settings.vault_path, "One.md", "# One\nConcrete fact.")

    async def timeout(_self, _notes, *, feedback=None):
        raise LLMRequestTimeoutError(
            "Local AI timed out after 600 seconds while learning the adaptive vault model. "
            "Increase Model timeout in Local AI settings."
        )

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", timeout)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    status = service.vault_sweep(sweep["id"])
    assert status["status"] == "failed"
    assert "timed out after 600 seconds" in status["error"]
    assert "Local AI settings" in status["error"]
    assert "timed out after 600 seconds" in service.vault_model_status()["error"]


def test_relationship_feedback_ignores_malformed_history_and_model_json(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('feedback-sweep', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    valid = service._create_vault_change(
        sweep_id="feedback-sweep",
        note={"path": "One.md", "content": "# One"},
        after_content="# One\n[[Two]]",
        reason="Evidence-backed test",
        evidence=["SOURCE: One", "TARGET: Two"],
        confidence=0.9,
        decision={"relationships": [{"target": "Two", "relationship": "One owns Two"}]},
    )
    malformed = service._create_vault_change(
        sweep_id="feedback-sweep",
        note={"path": "Broken.md", "content": "# Broken"},
        after_content="# Broken",
        reason="Broken history",
        evidence=[],
        confidence=0.1,
    )
    service.db.execute(
        "UPDATE vault_changes SET status = 'applied', reviewed_at = ? WHERE id = ?",
        (now, valid),
    )
    service.db.execute(
        "UPDATE vault_changes SET status = 'rejected', decision_json = '[1]', "
        "reviewed_at = ? WHERE id = ?",
        (now, malformed),
    )
    feedback = service._relationship_feedback()
    assert feedback == [
        {
            "source_path": "One.md",
            "outcome": "applied",
            "relationships": [{"target": "Two", "relationship": "One owns Two"}],
        }
    ]

    service.db.execute(
        "INSERT INTO vault_models(vault_key, status, model_json, updated_at) "
        "VALUES ('local', 'ready', '{broken', ?)",
        (now,),
    )
    assert service.vault_model_status()["model"] == {}


@pytest.mark.asyncio
async def test_sweep_validation_stop_and_agent_progress_guards(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    with pytest.raises(ValueError, match="available Obsidian vault"):
        service.start_vault_sweep("index")
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(settings.vault_path, "One.md", "# One")
    with pytest.raises(ValueError, match="Sweep type"):
        service.start_vault_sweep("invalid")
    with pytest.raises(ValueError, match="change handling"):
        service.start_vault_sweep("maintenance", change_mode="index-only")

    sweep = service.start_vault_sweep("index", change_mode="index-only")
    with pytest.raises(ValueError, match="already running"):
        service.start_vault_sweep("index")
    stopped = service.stop_vault_sweep(sweep["id"])
    assert stopped["stopped"] is True
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.stop_vault_sweep()["stopped"] is False
    with pytest.raises(ValueError, match="not found"):
        service.vault_sweep("missing")
    with pytest.raises(ValueError, match="not found"):
        service.vault_change_diff("missing")
    with pytest.raises(ValueError, match="not found"):
        service.reject_vault_change("missing")
    with pytest.raises(ValueError, match="not found for this computer"):
        service.agent_sweep_progress(
            "wrong-agent", sweep["id"], processed=1, total=1, current_note="One.md"
        )


@pytest.mark.asyncio
async def test_custom_interval_scheduler_starts_due_sweep(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.update_settings(
        {
            "vault_mode": "local",
            "vault_index_schedule_enabled": True,
            "vault_index_schedule_frequency": "custom",
            "vault_index_schedule_interval_hours": 1,
        }
    )
    write_note(settings.vault_path, "Scheduled.md", "# Scheduled")
    await service.scheduler_tick(datetime(2026, 7, 16, 12, 0, tzinfo=UTC))
    assert service._sweep_task is not None
    await service._sweep_task
    recent = service.vault_sweep_status()["recent"][0]
    assert recent["scheduled"] == 1
    assert recent["status"] == "completed"
    assert service.vault_sweep_status()["indexed_notes"] == 1


@pytest.mark.asyncio
async def test_manual_stop_interrupts_large_index_without_erasing_previous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    for index in range(250):
        write_note(
            settings.vault_path, f"Bulk/Note {index:04d}.md", f"# Note {index}\nShared data."
        )
    service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep_status()["indexed_notes"] == 250

    real_parse = parse_note

    def slow_parse(*args, **kwargs):
        time.sleep(0.001)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr("obsync.service.parse_note", slow_parse)
    second = service.start_vault_sweep("index", change_mode="index-only")
    while service.vault_sweep(second["id"])["processed_notes"] < 5:
        await asyncio.sleep(0.005)
    stopped = service.stop_vault_sweep(second["id"])
    assert stopped["stopped"] is True
    assert service._sweep_task is not None
    await service._sweep_task
    assert service.vault_sweep(second["id"])["status"] == "stopped"
    assert service.vault_sweep_status()["indexed_notes"] == 250


def test_schedule_due_respects_timezone_frequency_and_last_run(tmp_path: Path) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    service.update_settings(
        {
            "vault_mode": "local",
            "vault_index_schedule_enabled": True,
            "vault_index_schedule_frequency": "daily",
            "vault_index_schedule_time": "02:00",
            "vault_schedule_timezone": "America/New_York",
        }
    )
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    assert service._schedule_due("index", now) is True
    service.db.execute(
        """
        INSERT INTO vault_sweeps(
            id, sweep_type, vault_key, status, change_mode, scheduled, created_at, updated_at
        ) VALUES ('scheduled-today', 'index', 'local', 'completed', 'index-only', 1, ?, ?)
        """,
        (now.isoformat(), now.isoformat()),
    )
    assert service._schedule_due("index", now) is False


def test_ranker_stress_finds_relevant_notes_in_five_thousand_note_vault() -> None:
    notes = []
    for index in range(5000):
        company = "Pro Quality Plumbing" if index == 4321 else f"Reference Company {index}"
        account = "ACME-7788" if index in {4321, 4444} else f"REF-{index:05d}"
        notes.append(
            {
                "path": f"Knowledge/{company}.md",
                "title": company,
                "tags": ["company"],
                "aliases": [],
                "headings": ["Accounts"],
                "properties": {},
                "entities": [f"account:{account.casefold()}", company],
                "content": f"# {company}\nAccount number {account}.",
            }
        )
    started = time.perf_counter()
    ranked = rank_notes(
        "Applications/Acme Account.pdf",
        "Pro Quality Plumbing application for account number ACME-7788.",
        notes,
        limit=100,
    )
    elapsed = time.perf_counter() - started

    assert ranked[0]["title"] == "Pro Quality Plumbing"
    assert any("shared record identifier" in item["reasons"] for item in ranked[:3])
    assert elapsed < 5


def test_adaptive_index_stress_handles_five_thousand_notes_without_quadratic_scan() -> None:
    notes = [
        {
            "path": f"Records/Record {index:04d}.md",
            "title": f"Record {index:04d}",
            "tags": ["record"],
            "aliases": [],
            "headings": ["Details"],
            "properties": {"record_id": f"REF-{index:05d}"},
            "entities": [f"record:ref-{index:05d}"],
            "content": (
                "Standard recurring record template. "
                + (
                    "Project Zephyr account ZX-7788 owned by Client Alpha."
                    if index == 4321
                    else f"Reference identifier REF-{index:05d}."
                )
            ),
        }
        for index in range(5000)
    ]
    started = time.perf_counter()
    index = AdaptiveVaultIndex(notes)
    candidates = index.candidates(
        "Incoming/Zephyr.txt",
        "Client Alpha update for Project Zephyr account ZX-7788.",
        maximum=20,
    )
    elapsed = time.perf_counter() - started

    assert candidates[0]["path"] == "Records/Record 4321.md"
    assert elapsed < 8


def test_streamed_desktop_index_replaces_stale_notes_and_counts_real_changes(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    agent_id = "desktop-agent"
    old_notes = [
        parse_note(Path("Keep.md"), content="# Keep\nStable content.").as_dict(),
        parse_note(Path("Stale.md"), content="# Stale\nRemove me.").as_dict(),
    ]
    service._store_vault_index(agent_id, old_notes, full_rebuild=True)
    sweep_id = "streamed-index"
    now = utc_now()
    service.db.execute(
        """
        INSERT INTO vault_sweeps(
            id, sweep_type, vault_key, status, change_mode, full_rebuild,
            created_at, updated_at
        ) VALUES (?, 'index', ?, 'running', 'index-only', 1, ?, ?)
        """,
        (sweep_id, agent_id, now, now),
    )

    batch = [
        parse_note(Path("Keep.md"), content="# Keep\nStable content.").as_dict(),
        parse_note(Path("New.md"), content="# New\nFresh content.").as_dict(),
    ]
    accepted = service.agent_sweep_notes(agent_id, sweep_id, batch)
    assert accepted == {"ok": True, "accepted": 2, "stop_requested": False}
    assert service.vault_sweep(sweep_id)["changed_notes"] == 1

    finalized = service._finalize_remote_index(agent_id, sweep_id)
    assert finalized == {"notes": 2, "removed": 1}
    assert [note["path"] for note in service._indexed_vault_notes(agent_id)] == [
        "Keep.md",
        "New.md",
    ]

    with pytest.raises(ValueError, match="limited to 50"):
        service.agent_sweep_notes(agent_id, sweep_id, batch * 26)
    service.db.execute("UPDATE vault_sweeps SET status = 'stopping' WHERE id = ?", (sweep_id,))
    assert service.agent_sweep_notes(agent_id, sweep_id, batch)["stop_requested"] is True
    with pytest.raises(ValueError, match="not found for this computer"):
        service.agent_sweep_notes("different-agent", sweep_id, batch)
