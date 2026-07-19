from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obsync.config import Settings
from obsync.llm import LLMAnalyzer, LLMRequestTimeoutError
from obsync.markdown import note_tags, note_title
from obsync.service import ObsyncService, utc_now
from obsync.vault_intelligence import (
    MAINTENANCE_START,
    AdaptiveVaultIndex,
    add_backlinks,
    add_index_membership,
    change_native_tag,
    exact_duplicate_groups,
    existing_note_match,
    explicit_category_hub_relationships,
    explicit_reciprocal_relationships,
    explicit_reference_relationships,
    inline_anchor_options,
    link_target,
    maintenance_content,
    native_maintenance_content,
    note_links_to,
    parse_note,
    rank_notes,
    reapply_owned_operations,
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


def test_anchor_candidates_reject_dates_numbers_metadata_and_keep_contextual_phrases() -> None:
    source = """# Phase 3 - Accounting Core

Status: complete
Owner: Archo
Updated: 2026-06-27 16:29 EDT
Build the double-entry accounting core before financial workflows depend on it.
Ledger posting service accepted at 2026-06-26 16:42 EDT.
The current limit is 256.
"""
    candidate = {
        "path": "LedgeFlow/ARCHITECTURE.md",
        "title": "LedgeFlow Architecture",
        "entities": ["updated:2026-06-26", "limit:256"],
        "content": (
            "The double-entry accounting core is foundational. Accounting events flow through "
            "a ledger posting service."
        ),
    }

    options = inline_anchor_options(source, candidate)
    anchors = {item["text"].casefold() for item in options}

    assert "double-entry accounting core" in anchors
    assert not ({"owner", "status", "current", "2026-06-27", "2026-06-26", "256"} & anchors)
    selected = next(
        item for item in options if item["text"].casefold() == "double-entry accounting core"
    )
    assert "financial workflows" in selected["context"]


def test_anchor_candidates_reject_generic_pending_label() -> None:
    options = inline_anchor_options(
        "The pending work remains blocked until review.\n",
        {
            "path": "LedgeFlow/PENDING.md",
            "title": "Pending",
            "content": "Pending work remains blocked until review.",
        },
    )

    assert "pending" not in {item["text"].casefold() for item in options}


def test_anchor_candidates_reject_shared_phrases_that_do_not_identify_target_context() -> None:
    source = (
        "Sales assets and outreach scripts are backed up.\n"
        "Obsidian vault mirror is the second storage location.\n"
        "The **Free**: limited static office tier remains available.\n"
    )
    setup = {
        "path": "Operations/Setup Checklist.md",
        "title": "Setup Checklist",
        "content": "Create backup plan and finish setup tasks.",
    }
    mirror = {
        "path": "Operations/Obsidian Mirror Queue.md",
        "title": "Obsidian Mirror Queue",
        "content": "Queue of notes pending an Obsidian vault mirror.",
    }
    delivery = {
        "path": "Delivery/Delivery System.md",
        "title": "Delivery System",
        "content": "Define exact Free versus Pro boundaries.",
    }

    assert inline_anchor_options(source, setup) == []
    mirror_options = inline_anchor_options(source, mirror)
    assert mirror_options[0]["text"] == "Obsidian vault mirror"
    assert all("*" not in item["text"] for item in mirror_options)
    assert inline_anchor_options(source, delivery) == []


def test_anchor_candidates_reject_short_generic_target_body_phrases() -> None:
    source = "The Pro tier provides the full experience for customers.\n"
    pricing = {
        "path": "Finance/Pricing Model.md",
        "title": "Pricing Model",
        "content": "Pricing should monetize the full experience without excessive tiers.",
    }

    assert inline_anchor_options(source, pricing, note_count=50) == []


def test_anchor_candidates_trim_sentence_punctuation_and_reject_generic_fragments() -> None:
    source = (
        "Action: drafted the initial pricing, GTM, and 14-day execution roadmap.\n"
        "The memory folder contains an action/decision log.\n"
    )
    roadmap = {
        "path": "Strategy/Master Roadmap.md",
        "title": "Master Roadmap",
        "aliases": [],
        "entities": [],
        "content": "The 14-day execution roadmap defines pricing and GTM milestones.",
    }

    options = inline_anchor_options(source, roadmap, note_count=50)
    anchors = {item["text"] for item in options}

    assert "14-day execution roadmap" in anchors
    assert not any(anchor.endswith(".") for anchor in anchors)
    assert "action/decision" not in anchors


def test_anchor_candidates_reject_sentence_fragments_and_promotional_verbs() -> None:
    source = (
        "Build a visual office where your team can collaborate.\n"
        "Create agents, watch them move across the workspace.\n"
        "Watch them move while work progresses.\n"
        "Use recipient normalization, provider stub metadata, structured output.\n"
        "The double-entry accounting core remains foundational.\n"
    )
    candidate = {
        "path": "Projects/Architecture.md",
        "title": "Product Architecture",
        "content": (
            "The visual office lets agents move across the workspace. Recipient normalization "
            "and provider stub metadata produce structured output. The double-entry accounting "
            "core is foundational."
        ),
    }

    anchors = {
        item["text"].casefold() for item in inline_anchor_options(source, candidate, note_count=511)
    }

    assert "visual office where" not in anchors
    assert "create agents, watch them move" not in anchors
    assert "watch them move" not in anchors
    assert "recipient normalization, provider stub metadata, structured" not in anchors
    assert "double-entry accounting core" in anchors


@pytest.mark.parametrize(
    "phrase",
    [
        "draft for Eli review",
        "expose host port",
        "goal is to keep Cody",
        "Archo must perform",
    ],
)
def test_anchor_candidates_reject_boilerplate_action_phrases(phrase: str) -> None:
    candidate = {
        "path": "Runbooks/Deployment Runbook.md",
        "title": "Deployment Runbook",
        "content": f"The current work item says {phrase} before completion.",
    }

    assert (
        inline_anchor_options(f"The current work item says {phrase} before completion.", candidate)
        == []
    )


def test_readme_notes_are_classified_as_category_hubs() -> None:
    notes = [
        {
            "path": "LedgeFlow/README.md",
            "title": "LedgeFlow",
            "content": "LedgeFlow project home for architecture and milestone documents.",
            "links": [],
        },
        {
            "path": "LedgeFlow/ARCHITECTURE.md",
            "title": "LedgeFlow Architecture",
            "content": "LedgeFlow uses an API-first architecture.",
            "links": [],
        },
    ]
    source = {
        "path": "LedgeFlow/Phase-04.md",
        "title": "Phase 4",
        "content": "Milestone 4 covers the LedgeFlow architecture.",
        "properties": {},
        "links": [],
    }

    candidates = AdaptiveVaultIndex(notes).candidates(
        source["path"], source["content"], source_note=source, maximum=10
    )
    readme = next(item for item in candidates if item["path"] == "LedgeFlow/README.md")

    assert readme["structural_role"] == "category-hub"


def test_anchor_candidates_scan_protected_markdown_once_for_large_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import obsync.vault_intelligence as vault_intelligence

    source = "\n".join(
        f"Run {index}: Project Atlas depends on Architecture A-100." for index in range(6_000)
    )
    candidate = {
        "path": "Architecture/Atlas.md",
        "title": "Project Atlas Architecture",
        "aliases": ["Architecture A-100"],
        "entities": ["project:atlas", "identifier:a-100"],
        "content_excerpt": "Architecture A-100 is the approved design for Project Atlas.",
    }
    real_protected_ranges = vault_intelligence._protected_markdown_ranges
    calls = 0

    def counted(content: str):
        nonlocal calls
        calls += 1
        return real_protected_ranges(content)

    monkeypatch.setattr(vault_intelligence, "_protected_markdown_ranges", counted)
    options = inline_anchor_options(source, candidate, note_count=511)

    assert options
    assert calls == 1


def test_anchor_candidates_bound_phrase_mining_for_medium_large_notes() -> None:
    source = "The durable accounting foundation supports consistent financial workflows.\n" * 1_100
    candidate = {
        "path": "Architecture/Ledger Design.md",
        "title": "Ledger Design",
        "content": (
            "A durable accounting foundation supports consistent financial workflows across "
            "the system."
        ),
    }

    started = time.perf_counter()
    options = inline_anchor_options(source, candidate, note_count=511)
    elapsed = time.perf_counter() - started

    assert options == []
    assert elapsed < 0.5


def test_existing_target_resolution_covers_wikilinks_and_markdown_links() -> None:
    target = {"path": "Projects/Project Atlas.md", "title": "Project Atlas"}
    for content in (
        "See [[Projects/Project Atlas|the project]].",
        "See [[Project Atlas]].",
        "See [the project](../Projects/Project%20Atlas.md#status).",
    ):
        source = parse_note(Path("Notes/Source.md"), content=content).as_dict()
        assert note_links_to(source, target)


def test_exact_duplicate_groups_choose_one_canonical_record() -> None:
    content = "# Daily Record\n\nThe same complete operational record appears in both folders.\n"
    notes = [
        parse_note(Path("Operations/Record.md"), content=content).as_dict(),
        parse_note(Path("Memory/Record.md"), content=content).as_dict(),
        parse_note(Path("Other.md"), content="# Other\n\nDifferent content entirely.\n").as_dict(),
    ]
    notes[0]["backlinks"] = ["Index.md"]

    groups = exact_duplicate_groups(notes)

    assert len(groups) == 1
    assert [item["path"] for item in groups[0]] == [
        "Operations/Record.md",
        "Memory/Record.md",
    ]


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


def test_native_maintenance_uses_inline_links_and_frontmatter_without_visible_block() -> None:
    original = "# Account Application\n\nClient Pro Quality Plumbing manages Acme.\n"
    relationships = [
        {
            "target": "Companies/Pro Quality Plumbing",
            "anchor": "Pro Quality Plumbing",
            "relationship": "the named company manages this account",
            "confidence": 0.94,
        },
        {
            "target": "Clients/Acme",
            "anchor": "Acme",
            "relationship": "the named client owns this account",
            "confidence": 0.93,
        },
    ]

    first, operations = native_maintenance_content(
        original, relationships, suggested_tags=["accounts/active"]
    )
    second, repeated = native_maintenance_content(
        first, relationships, suggested_tags=["accounts/active"]
    )

    assert first == second
    assert repeated == []
    assert MAINTENANCE_START not in first and "Related knowledge" not in first
    assert "[[Companies/Pro Quality Plumbing|Pro Quality Plumbing]]" in first
    assert "[[Clients/Acme|Acme]]" in first
    assert parse_note(Path("Account.md"), content=first).tags == ["accounts/active"]
    assert [item["kind"] for item in operations] == [
        "inline-link",
        "inline-link",
        "frontmatter-tag",
    ]


def test_inline_tag_indexing_ignores_code_routes_and_protected_markdown() -> None:
    note = parse_note(
        Path("Pending.md"),
        content=(
            "# Pending\n\n"
            "Real classification: #ledgeflow\n"
            "UI route: `#estimate-list`\n"
            "Linked route: [open app](https://example.test/#primitives)\n"
            "Existing note: [[Routes#estimate-list|#estimate-list]]\n"
        ),
    )

    assert note.tags == ["ledgeflow"]


def test_native_inline_links_only_touch_safe_existing_body_phrases() -> None:
    original = """---
aliases: [Orion Research]
---
# Orion Research

Already linked: [[Archives/Orion Research Archive|Orion Research Archive]].
Inline code: `Orion Research`

| Organization | Status |
| --- | --- |
| Orion Research | Active |

```text
Orion Research
```

Orion Researcher is a different phrase.
The work was commissioned by Orion Research for Project Atlas.
"""
    relationship = {
        "target": "Organizations/Orion Research",
        "anchor": "Orion Research",
        "relationship": "the named organization commissioned the work",
        "confidence": 0.97,
    }

    updated, operations = native_maintenance_content(original, [relationship])

    assert len(operations) == 1
    assert operations[0]["rendered"] == "[[Organizations/Orion Research|Orion Research]]"
    assert "# Orion Research" in updated
    assert "`Orion Research`" in updated
    assert "| Orion Research | Active |" in updated
    assert "```text\nOrion Research\n```" in updated
    assert (
        "commissioned by [[Organizations/Orion Research|Orion Research]] for Project Atlas"
        in updated
    )
    assert "Orion Researcher is a different phrase" in updated
    assert "[[Archives/Orion Research Archive|Orion Research Archive]]" in updated
    assert updated.count("[[Organizations/Orion Research|Orion Research]]") == 1


def test_native_hierarchical_tags_preserve_unrelated_frontmatter_and_human_comments() -> None:
    original = """---
title: Project Atlas
owner: Eli
tags: [project, status/active]
reviewed: false
---
# Project Atlas
"""

    added, operation = change_native_tag(original, tag="Knowledge/Research")
    assert operation and operation["tag"] == "knowledge/research"
    parsed = parse_note(Path("Project Atlas.md"), content=added)
    assert parsed.tags == ["project", "status/active", "knowledge/research"]
    assert parsed.properties["owner"] == "Eli"
    assert parsed.properties["reviewed"] is False

    removed, removal = change_native_tag(added, tag="knowledge/research", remove=True)
    assert removal and removal["action"] == "remove"
    assert parse_note(Path("Project Atlas.md"), content=removed).tags == [
        "project",
        "status/active",
    ]
    assert "owner: Eli" in removed and "reviewed: false" in removed

    pyyaml_style = """---
title: Project Atlas
tags:
- project
- status/active
owner: Eli
---
# Project Atlas
"""
    updated_pyyaml, pyyaml_operation = change_native_tag(pyyaml_style, tag="knowledge/research")
    assert pyyaml_operation is not None
    assert parse_note(Path("Project Atlas.md"), content=updated_pyyaml).tags == [
        "project",
        "status/active",
        "knowledge/research",
    ]
    assert "owner: Eli" in updated_pyyaml

    commented = """---
title: Project Atlas
tags:
  # Human grouping that must not be rewritten
  - project
---
# Project Atlas
"""
    unchanged, refused = change_native_tag(commented, tag="knowledge/research")
    assert unchanged == commented
    assert refused is None

    inline_comment = """---
title: Project Atlas
tags: [project] # Human explanation that must be preserved
---
# Project Atlas
"""
    unchanged, refused = change_native_tag(inline_comment, tag="knowledge/research")
    assert unchanged == inline_comment
    assert refused is None


def test_native_frontmatter_edits_preserve_windows_crlf_notes() -> None:
    original = (
        "---\r\n"
        "title: Project Atlas\r\n"
        "tags: [project, status/active]\r\n"
        "owner: Eli\r\n"
        "---\r\n"
        "# Project Atlas\r\n"
    )

    added, operation = change_native_tag(original, tag="knowledge/research")

    assert operation and operation["tag"] == "knowledge/research"
    assert "\n" not in added.replace("\r\n", "")
    assert note_title(added, Path("Project Atlas.md")) == "Project Atlas"
    assert note_tags(added) == ["project", "status/active", "knowledge/research"]
    assert parse_note(Path("Project Atlas.md"), content=added).properties["owner"] == "Eli"


def test_owned_operations_rebase_only_edits_still_present_in_the_current_note() -> None:
    current, operations = native_maintenance_content(
        "# Project Atlas\n\nOrion Research commissioned the work.\n",
        [
            {
                "target": "Organizations/Orion Research",
                "anchor": "Orion Research",
                "relationship": "the organization commissioned the project",
                "confidence": 0.97,
            }
        ],
        suggested_tags=["projects/active"],
    )
    regenerated = "# Project Atlas\n\nOrion Research commissioned the revised work.\n"

    preserved = reapply_owned_operations(current, regenerated, operations)
    assert "[[Organizations/Orion Research|Orion Research]]" in preserved
    assert "projects/active" in parse_note(Path("Project Atlas.md"), content=preserved).tags

    human_removed = current.replace(
        "[[Organizations/Orion Research|Orion Research]]", "Orion Research"
    )
    human_removed, _operation = change_native_tag(human_removed, tag="projects/active", remove=True)
    not_resurrected = reapply_owned_operations(human_removed, regenerated, operations)
    assert "[[Organizations/Orion Research|Orion Research]]" not in not_resurrected
    assert (
        "projects/active" not in parse_note(Path("Project Atlas.md"), content=not_resurrected).tags
    )


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


def test_sync_candidate_retrieval_excludes_the_note_currently_being_updated(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    current = write_note(
        settings.vault_path,
        "Projects/Project Atlas.md",
        "# Project Atlas\n\nOrion Research commissioned Project Atlas.\n",
    )
    other = write_note(
        settings.vault_path,
        "Organizations/Orion Research.md",
        "# Orion Research\n\nOrion Research commissioned Project Atlas.\n",
    )
    service._store_vault_index(
        "local",
        [
            parse_note(current, vault=settings.vault_path).as_dict(),
            parse_note(other, vault=settings.vault_path).as_dict(),
        ],
        full_rebuild=True,
    )

    candidates = service._candidate_notes(
        "Projects/atlas.txt",
        "Orion Research commissioned the revised Project Atlas.",
        service._llm_config().active_profile,
        exclude_path="Projects/Project Atlas.md",
    )

    assert candidates
    assert all(candidate["path"] != "Projects/Project Atlas.md" for candidate in candidates)
    assert candidates[0]["path"] == "Organizations/Orion Research.md"


def test_explicit_category_hub_membership_requires_proof_in_both_directions() -> None:
    source = {
        "path": "Reports/Field Report 003.md",
        "title": "Field Report 003",
        "content": "The category home is the Field Report Index.",
    }
    proven = {
        "path": "Indexes/Field Report Index.md",
        "title": "Field Report Index",
        "links": ["Reports/Field Report 003"],
        "structural_role": "category-hub",
        "anchor_options": [
            {
                "text": "Field Report Index",
                "reason": "exact target title, alias, or identifier",
                "score": 105,
            }
        ],
    }
    unproven = {
        **proven,
        "path": "Indexes/Unrelated Index.md",
        "title": "Unrelated Index",
        "links": [],
        "anchor_options": [
            {
                "text": "Unrelated Index",
                "reason": "exact target title, alias, or identifier",
                "score": 105,
            }
        ],
    }

    relationships = explicit_category_hub_relationships(source, [proven, unproven])

    assert relationships == [
        {
            "target": "Indexes/Field Report Index",
            "anchor": "Field Report Index",
            "anchor_occurrence": 0,
            "anchor_context": "",
            "relationship_type": "category-hub",
            "relationship": "This category hub explicitly catalogs the source note",
            "evidence": [
                "SOURCE: the note explicitly names Field Report Index",
                "TARGET: the hub directly links to Field Report 003",
            ],
            "confidence": 0.99,
        }
    ]


def test_explicit_category_hub_rejects_single_word_project_anchor() -> None:
    source = {
        "path": "LedgeFlow/ARCHITECTURE.md",
        "title": "LedgeFlow Architecture",
        "content": "LedgeFlow should be built as an API-first application.",
    }
    hub = {
        "path": "LedgeFlow/README.md",
        "title": "LedgeFlow",
        "links": ["LedgeFlow/ARCHITECTURE"],
        "structural_role": "category-hub",
        "anchor_options": [
            {
                "text": "LedgeFlow",
                "reason": "exact target title, alias, or identifier",
                "score": 105,
            }
        ],
    }

    assert explicit_category_hub_relationships(source, [hub]) == []


def test_explicit_reciprocal_relationship_requires_two_sided_exact_identification() -> None:
    source = parse_note(
        Path("Reports/Field Report 003.md"),
        content=(
            "# Field Report 003\n\n"
            "Orion Research commissioned this field report for Project Atlas.\n"
        ),
    ).as_dict()
    reciprocal = parse_note(
        Path("Organizations/Orion Research.md"),
        content=(
            "# Orion Research\n\n"
            "Orion Research commissioned Project Atlas field work, including Field Report 003.\n"
        ),
    ).as_dict()
    unrelated = parse_note(
        Path("Organizations/Nova Institute.md"),
        content="# Nova Institute\n\nAnother research organization.\n",
    ).as_dict()
    candidates = []
    for candidate in (reciprocal, unrelated):
        candidates.append(
            {
                **candidate,
                "link_target": link_target(candidate),
                "structural_role": "note",
                "already_linked": False,
                "anchor_options": inline_anchor_options(source["content"], candidate),
            }
        )

    relationships = explicit_reciprocal_relationships(source, candidates)

    assert len(relationships) == 1
    assert relationships[0]["target"] == "Organizations/Orion Research"
    assert relationships[0]["anchor"] == "Orion Research"
    assert relationships[0]["anchor_context"].startswith("Orion Research commissioned")


def test_checklist_generic_document_aliases_do_not_become_reference_links() -> None:
    source = parse_note(
        Path("Operations/Setup Checklist.md"),
        content="# Setup Checklist\n\n- [x] Create account inventory\n",
    ).as_dict()
    account = parse_note(
        Path("Operations/Account Inventory.md"),
        content=(
            "---\naliases: [account inventory]\n---\n# Account Inventory\n\nCanonical inventory.\n"
        ),
    ).as_dict()
    candidate = {
        **account,
        "link_target": link_target(account),
        "already_linked": False,
        "anchor_options": inline_anchor_options(source["content"], account),
    }

    relationships = explicit_reference_relationships(source, [candidate])

    assert candidate["anchor_options"] == []
    assert relationships == []


def test_checklist_complete_distinctive_document_name_becomes_reference_link() -> None:
    source = parse_note(
        Path("Operations/Setup Checklist.md"),
        content="# Setup Checklist\n\n- [x] Review Workspace Backup Plan\n",
    ).as_dict()
    plan = parse_note(
        Path("Operations/Workspace Backup Plan.md"),
        content="# Workspace Backup Plan\n\nRecovery design for the workspace.\n",
    ).as_dict()
    candidate = {
        **plan,
        "link_target": link_target(plan),
        "already_linked": False,
        "anchor_options": inline_anchor_options(source["content"], plan),
    }

    relationships = explicit_reference_relationships(source, [candidate])

    assert relationships[0]["anchor"] == "Workspace Backup Plan"
    assert relationships[0]["target"] == "Operations/Workspace Backup Plan"


def test_ordinary_note_mentions_do_not_force_reference_links() -> None:
    source = parse_note(
        Path("Operations/Status.md"),
        content="# Status\n\nThe account inventory was reviewed.\n",
    ).as_dict()
    account = parse_note(
        Path("Operations/Account Inventory.md"),
        content="# Account Inventory\n\nCanonical inventory.\n",
    ).as_dict()
    candidate = {
        **account,
        "link_target": link_target(account),
        "already_linked": False,
        "anchor_options": inline_anchor_options(source["content"], account),
    }

    assert explicit_reference_relationships(source, [candidate]) == []


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
    applied = account.read_text(encoding="utf-8")
    assert MAINTENANCE_START not in applied
    assert "[[Clients/Acme Holdings|Acme Holdings]]" in applied
    ownership = service.db.query_all(
        "SELECT * FROM vault_edit_ownership WHERE path = ? AND status = 'active'",
        ("Accounts/PQP Account.md",),
    )
    assert ownership and all(item["kind"] == "inline-link" for item in ownership)
    undo = await service.undo_vault_sweep(maintenance["id"])
    assert undo["reverted"] >= 1
    assert account.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_index_sweep_is_read_only_and_persists_adaptive_corpus_intelligence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(
        settings.vault_path,
        "Indexes/Field Report Index.md",
        "# Field Report Index\n\n[[Reports/Field Report 001]]\n[[Reports/Field Report 002]]\n",
    )
    for number in range(1, 7):
        write_note(
            settings.vault_path,
            f"Reports/Field Report {number:03d}.md",
            "---\ntags: [field-report]\n---\n"
            f"# Field Report {number:03d}\n\nStandard observations for Sector {number}.\n",
        )
    before = {
        path.relative_to(settings.vault_path).as_posix(): path.read_bytes()
        for path in settings.vault_path.rglob("*.md")
    }

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Index Sweep must not invoke Local AI")

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", forbidden)
    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", forbidden)
    sweep = service.start_vault_sweep("index", change_mode="index-only", full_rebuild=True)
    assert service._sweep_task is not None
    await service._sweep_task

    after = {
        path.relative_to(settings.vault_path).as_posix(): path.read_bytes()
        for path in settings.vault_path.rglob("*.md")
    }
    assert service.vault_sweep(sweep["id"])["status"] == "completed"
    assert after == before
    assert service.list_vault_changes()["total"] == 0
    corpus = service.vault_model_status()["corpus"]
    assert corpus["note_count"] == 7
    assert {item["path"] for item in corpus["folders"]} >= {"Indexes", "Reports"}
    assert corpus["tag_vocabulary"] == [{"tag": "field-report", "notes": 6}]
    assert corpus["existing_category_hubs"][0]["path"] == "Indexes/Field Report Index.md"


@pytest.mark.asyncio
async def test_maintenance_sweep_streams_model_thinking_output_and_decisions(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(settings.vault_path, "Companies/Example.md", "# Example\n\nNamed company record.")

    async def learn(analyzer, notes, *, feedback=None, corpus_profile=None):
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
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
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
async def test_invalid_maintenance_limits_use_the_safe_eight_link_fallback(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "vault_link_limit": ("not-a-number", False),
        }
    )
    write_note(settings.vault_path, "Records/Example.md", "# Example\n\nNamed record.\n")
    observed: dict[str, int] = {}

    async def adjudicate(
        _self,
        source_note,
        candidates,
        *,
        vault_model,
        minimum_confidence,
        maximum_links,
        feedback=None,
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
    ):
        observed["maximum_links"] = maximum_links
        return {
            "source_category": "",
            "source_role": "record",
            "summary": "No supported relationship.",
            "suggested_tags": [],
            "relationships": [],
            "obsolete_owned_links": [],
            "obsolete_owned_tags": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    assert service.vault_sweep(sweep["id"])["status"] == "completed"
    assert observed["maximum_links"] == 8


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
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
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
                            "anchor": candidate["anchor_options"][0]["text"],
                            "relationship_type": "category-hub"
                            if target == "Indexes/Invoice Index"
                            else "entity",
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
    assert "[[Companies/Pro Quality Plumbing|Pro Quality Plumbing]]" in after
    assert "[[Clients/Client Alpha|Client Alpha]]" in after
    assert "[[Indexes/Invoice Index|Invoice Index]]" in after
    assert not any(f"[[Invoices/Invoice {number}|" in after for number in range(5200, 5300))
    assert len(diff["decision"]["relationships"]) == 3


@pytest.mark.asyncio
async def test_category_hubs_prevent_same_type_link_floods_without_domain_hardcoding(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    for number in range(1, 61):
        detail = "Routine field report with observations, status, and follow-up actions."
        if number == 37:
            detail += " Orion Research commissioned Project Atlas. See the Field Report Index."
        write_note(
            settings.vault_path,
            f"Reports/Field Report {number:03d}.md",
            f"# Field Report {number:03d}\n\n{detail}\n",
        )
    write_note(
        settings.vault_path,
        "Organizations/Orion Research.md",
        "# Orion Research\n\nOrganization responsible for Project Atlas.\n",
    )
    write_note(
        settings.vault_path,
        "Indexes/Field Report Index.md",
        "# Field Report Index\n\nCatalog for Project Atlas field reports.\n",
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
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
    ):
        relationships = []
        if source_note["path"] == "Reports/Field Report 037.md":
            allowed = {
                "Organizations/Orion Research": (
                    "entity",
                    "the named organization commissioned it",
                ),
                "Indexes/Field Report Index": ("category-hub", "the explicit index organizes it"),
            }
            for candidate in candidates:
                target = str(candidate.get("link_target", ""))
                if target not in allowed:
                    continue
                relation_type, explanation = allowed[target]
                relationships.append(
                    {
                        "target": target,
                        "anchor": candidate["anchor_options"][0]["text"],
                        "relationship_type": relation_type,
                        "relationship": explanation,
                        "evidence": [
                            f"SOURCE: explicitly names {candidate['title']}",
                            "TARGET: describes its role for Project Atlas",
                        ],
                        "confidence": 0.96,
                    }
                )
        return {
            "source_category": "field-report",
            "source_role": "recurring observation record",
            "summary": "Use the category hub and durable entity, not peer records.",
            "suggested_tags": [],
            "relationships": relationships,
            "obsolete_owned_links": [],
            "obsolete_owned_tags": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    assert service.vault_sweep(sweep["id"])["status"] == "completed"
    change = next(
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["path"] == "Reports/Field Report 037.md"
    )
    after = service.vault_change_diff(change["id"])["after_content"]
    assert "[[Organizations/Orion Research|Orion Research]]" in after
    assert "[[Indexes/Field Report Index|Field Report Index]]" in after
    assert not any(f"[[Reports/Field Report {number:03d}|" in after for number in range(1, 61))


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
        settings.vault_path,
        "Projects/Project Alpha.md",
        "# Project Alpha\nSee the Acme Client Record for Project Alpha.",
    )
    write_note(
        settings.vault_path,
        "Clients/Acme Client Record.md",
        "# Acme Client Record\nProject Alpha client.",
    )
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task
    change = next(
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["path"] == "Projects/Project Alpha.md"
    )
    target.write_text(
        "# Project Alpha\nSee the Acme Client Record for Project Alpha.\nHuman edit after review.",
        encoding="utf-8",
    )

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
    assert MAINTENANCE_START not in target.read_text(encoding="utf-8")
    assert "[[Companies/Pro Quality Plumbing|Pro Quality Plumbing]]" in target.read_text(
        encoding="utf-8"
    )
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
    assert "1 cleanup operation" in change["reason"]
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
    assert service.db.get_setting("vault_link_limit") == "8"


def test_v0151_migration_raises_only_the_legacy_default_ai_timeout(tmp_path: Path) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    service.db.set_settings({"llm_timeout_seconds": ("120", False)})
    service.db.execute("UPDATE schema_meta SET version = 9")

    service.db.initialize()

    assert service.db.get_setting("llm_timeout_seconds") == "600"
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 13

    service.db.set_settings({"llm_timeout_seconds": ("900", False)})
    service.db.execute("UPDATE schema_meta SET version = 9")
    service.db.initialize()
    assert service.db.get_setting("llm_timeout_seconds") == "900"


def test_v011_migration_supersedes_block_recommendations_and_forces_read_only_index(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('block-sweep', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="block-sweep",
        note={"path": "Record.md", "content": "# Record\n"},
        after_content=maintenance_content(
            "# Record\n", [{"target": "Other", "relationship": "old block link"}]
        ),
        reason="Legacy bottom block",
        evidence=["shared category"],
        confidence=0.8,
    )
    service.db.set_settings(
        {
            "vault_index_change_mode": ("review", False),
            "vault_link_limit": ("20", False),
        }
    )
    service.db.execute("UPDATE schema_meta SET version = 10")

    service.db.initialize()

    change = service.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
    assert change and change["status"] == "superseded"
    assert "native inline maintenance" in change["error"]
    assert service.db.get_setting("vault_index_change_mode") == "index-only"
    assert service.db.get_setting("vault_link_limit") == "8"
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 13


def test_v012_migration_rebuilds_metadata_and_supersedes_contextless_recommendations(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('v11-sweep', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="v11-sweep",
        note={"path": "Record.md", "content": "# Record\n"},
        after_content="# Record\n\n[[Unvalidated]]\n",
        reason="Pre-context-validator recommendation",
        evidence=[],
        confidence=0.8,
    )
    service.db.set_settings(
        {
            "vault_maintenance_categories": ('["links", "tags"]', False),
            "vault_metadata_version": ("1", False),
        }
    )
    service.db.execute(
        "INSERT OR REPLACE INTO vault_models(vault_key, status, model_json, fingerprint, "
        "provider, model_name, note_count, error, corpus_json, corpus_fingerprint, updated_at) "
        "VALUES ('local', 'ready', '{}', 'old-model', 'ollama', 'test', 1, '', '{}', "
        "'old-corpus', ?)",
        (now,),
    )
    service.db.execute("UPDATE schema_meta SET version = 11")

    service.db.initialize()

    change = service.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
    model = service.db.query_one("SELECT * FROM vault_models WHERE vault_key = 'local'")
    assert change and change["status"] == "superseded"
    assert "context-grounded maintenance" in change["error"]
    assert service.db.get_setting("vault_maintenance_categories") == (
        '["links", "tags", "organization"]'
    )
    assert service.db.get_setting("vault_metadata_version") == "0"
    assert model and model["status"] == "not-learned"
    assert model["fingerprint"] == ""
    assert model["corpus_fingerprint"] == ""
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 13


def test_v019_migration_supersedes_pre_graph_recommendations_and_model(
    tmp_path: Path,
) -> None:
    service = ObsyncService(
        Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    )
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('v18-sweep', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="v18-sweep",
        note={"path": "Record.md", "content": "# Record\n"},
        after_content="# Record\n\n[[Generic Plan|plan]]\n",
        reason="Pre-graph recommendation",
        evidence=["generic phrase"],
        confidence=0.8,
    )
    service.db.execute(
        "INSERT OR REPLACE INTO vault_models(vault_key, status, model_json, fingerprint, "
        "provider, model_name, note_count, error, corpus_json, corpus_fingerprint, updated_at) "
        "VALUES ('local', 'ready', '{}', 'old-model', 'ollama', 'test', 1, '', '{}', "
        "'current-corpus', ?)",
        (now,),
    )
    service.db.execute("UPDATE schema_meta SET version = 12")

    service.db.initialize()

    change = service.db.query_one("SELECT * FROM vault_changes WHERE id = ?", (change_id,))
    model = service.db.query_one("SELECT * FROM vault_models WHERE vault_key = 'local'")
    assert change and change["status"] == "superseded"
    assert "graph-grounded maintenance" in change["error"]
    assert model and model["status"] == "not-learned"
    assert model["fingerprint"] == ""
    assert model["corpus_fingerprint"] == "current-corpus"
    assert service.db.query_one("SELECT version FROM schema_meta")["version"] == 13


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

    async def learn(_self, learned_notes, *, feedback=None, corpus_profile=None):
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

    async def fail(_self, _notes, *, feedback=None, corpus_profile=None):
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

    async def timeout(_self, _notes, *, feedback=None, corpus_profile=None):
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
            "suggested_tags": [],
            "operations": [],
            "review": {},
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


def test_startup_recovers_sweeps_and_model_learning_interrupted_by_restart(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    first = ObsyncService(settings)
    now = utc_now()
    first.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "full_rebuild, scheduled, current_note, created_at, updated_at) "
        "VALUES ('orphaned', 'maintenance', 'local', 'stopping', 'review', 0, 0, "
        "'Learning adaptive vault model', ?, ?)",
        (now, now),
    )
    first.db.execute(
        "INSERT INTO vault_models(vault_key, status, updated_at) VALUES ('local', 'learning', ?)",
        (now,),
    )

    recovered = ObsyncService(settings)
    sweep = recovered.vault_sweep("orphaned")
    model = recovered.db.query_one("SELECT * FROM vault_models WHERE vault_key = 'local'")

    assert sweep["status"] == "stopped"
    assert sweep["current_note"] == ""
    assert sweep["finished_at"]
    assert "server restart" in sweep["error"]
    assert model and model["status"] == "not-learned"
    assert "server restart" in model["error"]


@pytest.mark.asyncio
async def test_stop_cancels_active_maintenance_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "llm_enabled": ("true", False),
            "llm_provider": ("ollama", False),
            "llm_base_url": ("http://test-model", False),
            "llm_model": ("test-model", False),
        }
    )
    write_note(settings.vault_path, "One.md", "# One\n\nA substantive note for model learning.")
    entered = asyncio.Event()
    never_finishes = asyncio.Event()

    async def hang_during_learning(_self, _notes, *, feedback=None, corpus_profile=None):
        entered.set()
        await never_finishes.wait()
        return {}

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", hang_during_learning)
    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    task = service._sweep_task
    assert task is not None
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopped = service.stop_vault_sweep(sweep["id"])
    assert stopped["stopped"] is True
    await asyncio.wait_for(task, timeout=2)

    finished = service.vault_sweep(sweep["id"])
    model = service.db.query_one("SELECT * FROM vault_models WHERE vault_key = 'local'")
    assert finished["status"] == "stopped"
    assert model and model["status"] == "not-learned"
    assert model["error"] == "Learning stopped by user"
    assert service._sweep_ai_task is None


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


def test_adaptive_index_builds_compact_vault_knowledge_graph_context() -> None:
    source = parse_note(
        Path("Invoices/INV-9.md"),
        content="# Invoice INV-9\n\nInvoice INV-9 bills Client Alpha for account A1-900.\n",
    ).as_dict()
    client = parse_note(
        Path("Clients/Client Alpha.md"),
        content="# Client Alpha\n\nClient Alpha owns account A1-900.\n",
    ).as_dict()
    index = AdaptiveVaultIndex([source, client])

    candidates = index.candidates(
        source["path"],
        source["content"],
        source_note=source,
        exclude_path=source["path"],
    )
    profile = index.corpus_profile()

    assert source["knowledge_graph"]["entity_nodes"][0]["id"] == "document:invoices/inv-9"
    assert candidates[0]["knowledge_graph"]["entity_nodes"][0]["id"] == (
        "document:clients/client alpha"
    )
    assert candidates[0]["knowledge_graph"]["signals"]["source_names_target"] is True
    assert profile["knowledge_graph"]["node_counts"]["document"] == 2
    assert profile["knowledge_graph"]["specificity_method"] == "inverse document frequency"


def test_tag_vocabulary_is_scoped_to_project_or_exact_named_domain() -> None:
    notes = [
        {
            "path": "AI and Automation/LedgeFlow/ARCHITECTURE.md",
            "title": "LedgeFlow Architecture",
            "human_tags": ["architecture"],
            "content": "System layers for LedgeFlow.",
        },
        {
            "path": "Products/Virtual Office/README.md",
            "title": "Virtual Office",
            "human_tags": ["virtual-office"],
            "content": "Visual workspace for AI agents.",
        },
        {
            "path": "AI and Automation/Ent Forge/02_brand_marketing/brand-foundation.md",
            "title": "Brand Foundation",
            "human_tags": ["brand"],
            "content": "Brand system for the product.",
        },
    ]
    source = {
        "path": "AI and Automation/Ent Forge/02_brand_marketing/website-outline.md",
        "title": "Website Outline",
        "headings": ["Hero", "Pricing"],
        "content": "Website structure for the Virtual Office product.",
    }

    vocabulary = AdaptiveVaultIndex(notes).tag_vocabulary_for(source)

    assert "brand" in vocabulary
    assert "virtual-office" in vocabulary
    assert "architecture" not in vocabulary


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


@pytest.mark.asyncio
async def test_operation_level_review_can_apply_tag_without_rejected_link(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    original = "# Account\n\nClient Acme owns account A-100.\n"
    source = write_note(settings.vault_path, "Accounts/Account.md", original)
    note = parse_note(source, vault=settings.vault_path).as_dict()
    after, operations = native_maintenance_content(
        original,
        [
            {
                "target": "Clients/Acme",
                "anchor": "Client Acme",
                "relationship": "Client Acme owns account A-100",
                "confidence": 0.96,
            }
        ],
        suggested_tags=["accounts/active"],
    )
    for operation in operations:
        operation["operation_id"] = f"operation-{operation['kind']}"
        operation["confidence"] = 0.96
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('selective', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    change_id = service._create_vault_change(
        sweep_id="selective",
        note=note,
        after_content=after,
        reason="Test selective operations",
        evidence=[],
        confidence=0.96,
        decision={
            "relationships": [{"target": "Clients/Acme", "confidence": 0.96}],
            "suggested_tags": [{"tag": "accounts/active", "confidence": 0.96}],
            "operations": operations,
        },
    )
    tag_operation = next(item for item in operations if item["kind"] == "frontmatter-tag")

    await service.approve_vault_change(change_id, [tag_operation["operation_id"]])

    applied = source.read_text(encoding="utf-8")
    assert "accounts/active" in note_tags(applied)
    assert "[[Clients/Acme" not in applied
    stored = service.vault_change_diff(change_id)["decision"]
    assert [item["kind"] for item in stored["operations"]] == ["frontmatter-tag"]
    assert stored["review"]["rejected_operation_ids"] == ["operation-inline-link"]

    undo = await service.undo_vault_sweep("selective")
    assert undo["ok"] is True
    assert source.read_text(encoding="utf-8") == original


def test_reparse_discards_phantom_metadata_and_excludes_owned_tags_from_learning(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    content = "---\ntags: [human-tag, generated-tag]\n---\n# Record\n\nCurrent content.\n"
    note = parse_note(Path("Record.md"), content=content).as_dict()
    service._store_vault_index("local", [note], full_rebuild=True)
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_edit_ownership(id, vault_key, path, kind, operation_key, target, "
        "anchor, rendered, status, source_change_id, created_at, updated_at) VALUES "
        "('owned-tag', 'local', 'Record.md', 'frontmatter-tag', 'generated-tag', "
        "'generated-tag', '', 'generated-tag', 'active', '', ?, ?)",
        (now, now),
    )
    service.db.execute(
        "UPDATE vault_notes SET tags_json = '[\"phantom\"]', links_json = '[\"Ghost\"]' "
        "WHERE vault_key = 'local' AND path = 'Record.md'"
    )

    reparsed = service._reparse_indexed_notes("local")

    assert reparsed[0]["tags"] == ["human-tag", "generated-tag"]
    assert reparsed[0]["human_tags"] == ["human-tag"]
    assert reparsed[0]["links"] == []
    assert AdaptiveVaultIndex(reparsed).corpus_profile()["tag_vocabulary"] == [
        {"tag": "human-tag", "notes": 1}
    ]


@pytest.mark.asyncio
async def test_exact_duplicates_are_flagged_and_canonical_selection_is_undoable(
    tmp_path: Path, adaptive_ai
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    content = "# Shared Record\n\nThis complete record is duplicated exactly across two folders.\n"
    write_note(settings.vault_path, "Operations/Shared Record.md", content)
    write_note(settings.vault_path, "Memory/Shared Record.md", content)

    sweep = service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    findings = [
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["change_type"] == "duplicate-finding"
    ]
    assert len(findings) == 1
    operation = findings[0]["decision"]["operations"][0]
    assert operation["kind"] == "canonical-selection"
    await service.approve_vault_change(findings[0]["id"])
    resolution = service.db.query_one(
        "SELECT * FROM vault_duplicate_resolutions WHERE vault_key = 'local'"
    )
    assert resolution and resolution["duplicate_path"] == operation["duplicate_path"]
    assert resolution["canonical_path"] == operation["canonical_path"]

    undo = await service.undo_vault_sweep(sweep["id"])
    assert undo["ok"] is True
    assert service.db.query_one("SELECT * FROM vault_duplicate_resolutions") is None


@pytest.mark.asyncio
async def test_review_only_move_and_index_membership_are_reversible(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    source = write_note(settings.vault_path, "Inbox/Atlas.md", "# Atlas\n\nProject record.\n")
    (settings.vault_path / "Projects").mkdir(parents=True)
    note = parse_note(source, vault=settings.vault_path).as_dict()
    now = utc_now()
    service.db.execute(
        "INSERT INTO vault_sweeps(id, sweep_type, vault_key, status, change_mode, "
        "created_at, updated_at) VALUES ('organize', 'maintenance', 'local', "
        "'completed', 'review', ?, ?)",
        (now, now),
    )
    move = {
        "operation_id": "move-atlas",
        "action": "move",
        "kind": "move-note",
        "key": "inbox/atlas.md",
        "from_path": "Inbox/Atlas.md",
        "to_path": "Projects/Atlas.md",
        "confidence": 0.97,
    }
    change_id = service._create_vault_change(
        sweep_id="organize",
        note=note,
        after_content=note["content"],
        reason="Move to existing Projects folder",
        evidence=[],
        confidence=0.97,
        decision={"operations": [move]},
        change_type="move-note",
    )
    service.db.execute(
        "INSERT INTO vault_edit_ownership(id, vault_key, path, kind, operation_key, target, "
        "anchor, rendered, status, source_change_id, created_at, updated_at) VALUES "
        "('owned-atlas-tag', 'local', 'Inbox/Atlas.md', 'frontmatter-tag', 'project', "
        "'project', '', 'project', 'active', '', ?, ?)",
        (now, now),
    )
    service.db.execute(
        "INSERT INTO vault_duplicate_resolutions(vault_key, duplicate_path, canonical_path, "
        "status, source_change_id, created_at, updated_at) VALUES "
        "('local', 'Archive/Atlas.md', 'Inbox/Atlas.md', 'active', '', ?, ?)",
        (now, now),
    )

    await service.approve_vault_change(change_id)
    assert not source.exists()
    assert (settings.vault_path / "Projects/Atlas.md").is_file()
    assert (
        service.db.query_one("SELECT path FROM vault_edit_ownership WHERE id = 'owned-atlas-tag'")[
            "path"
        ]
        == "Projects/Atlas.md"
    )
    assert (
        service.db.query_one(
            "SELECT canonical_path FROM vault_duplicate_resolutions "
            "WHERE duplicate_path = 'Archive/Atlas.md'"
        )["canonical_path"]
        == "Projects/Atlas.md"
    )
    undo = await service.undo_vault_sweep("organize")
    assert undo["ok"] is True
    assert source.is_file()
    assert (
        service.db.query_one("SELECT path FROM vault_edit_ownership WHERE id = 'owned-atlas-tag'")[
            "path"
        ]
        == "Inbox/Atlas.md"
    )
    assert (
        service.db.query_one(
            "SELECT canonical_path FROM vault_duplicate_resolutions "
            "WHERE duplicate_path = 'Archive/Atlas.md'"
        )["canonical_path"]
        == "Inbox/Atlas.md"
    )

    hub = "# Project Index\n\nCatalog of active projects.\n"
    updated, operation = add_index_membership(hub, source_target="Inbox/Atlas|Atlas")
    assert operation and "[[Inbox/Atlas|Atlas]]" in updated
    repeated, duplicate = add_index_membership(updated, source_target="Inbox/Atlas|Atlas")
    assert repeated == updated
    assert duplicate is None


@pytest.mark.asyncio
async def test_moves_are_not_proposed_for_backlink_targets_or_content_edits(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(
        settings.vault_path,
        "Projects/README.md",
        "---\ntags: [project-record]\n---\n# Projects\n",
    )
    write_note(settings.vault_path, "Inbox/Linked.md", "# Linked\n\nProject Atlas.\n")
    write_note(
        settings.vault_path,
        "References/Linked Reference.md",
        "# Reference\n\nSee [[Inbox/Linked]].\n",
    )
    write_note(settings.vault_path, "Inbox/Edited.md", "# Edited\n\nClient Acme.\n")
    write_note(settings.vault_path, "Clients/Acme.md", "# Acme\n\nClient for Edited.\n")

    async def adjudicate(
        _self,
        source_note,
        candidates,
        *,
        vault_model,
        minimum_confidence,
        maximum_links,
        feedback=None,
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
    ):
        suggested_tags = []
        if source_note["path"] == "Inbox/Edited.md":
            suggested_tags = [
                {
                    "tag": "project-record",
                    "reason": "The source is a stable project record.",
                    "evidence": ["SOURCE: Edited project record"],
                    "confidence": 0.97,
                }
            ]
        return {
            "summary": "Move only when no links would break and no content card conflicts.",
            "suggested_tags": suggested_tags,
            "relationships": [],
            "organization_operations": [
                {
                    "kind": "move-note",
                    "destination_folder": "Projects",
                    "reason": "Existing project folder.",
                    "evidence": ["SOURCE: Project record"],
                    "confidence": 0.97,
                }
            ],
            "index_memberships": [],
            "obsolete_owned_links": [],
            "obsolete_owned_tags": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    changes = service.list_vault_changes(status="pending")["items"]
    assert not any(
        item["change_type"] == "move-note"
        and item["path"] in {"Inbox/Linked.md", "Inbox/Edited.md"}
        for item in changes
    )
    assert any(
        item["change_type"] == "native-maintenance" and item["path"] == "Inbox/Edited.md"
        for item in changes
    )


@pytest.mark.asyncio
async def test_review_decision_lists_only_relationships_that_produce_real_operations(
    tmp_path: Path, adaptive_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", vault_path=tmp_path / "vault", admin_token="")
    service = ObsyncService(settings)
    adaptive_ai(service)
    service.db.set_settings({"vault_confirmed": ("true", False)})
    write_note(
        settings.vault_path,
        "Accounts/Account.md",
        "# Account\n\nClient Alpha owns account A-100.\n",
    )
    write_note(
        settings.vault_path,
        "Clients/Primary.md",
        "---\naliases: [Client Alpha]\n---\n# Primary Client\n\nAccount A-100 owner.\n",
    )
    write_note(
        settings.vault_path,
        "Clients/Archive.md",
        "---\naliases: [Client Alpha]\n---\n# Archived Client\n\nHistorical A-100 record.\n",
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
        owned_operations=None,
        tag_vocabulary=None,
        allowed_folders=None,
    ):
        relationships = []
        if source_note["path"] == "Accounts/Account.md":
            for candidate in candidates:
                if not str(candidate.get("path", "")).startswith("Clients/"):
                    continue
                option = next(
                    item
                    for item in candidate["anchor_options"]
                    if item["text"].casefold() == "client alpha"
                )
                relationships.append(
                    {
                        "target": candidate["link_target"],
                        "anchor": option["text"],
                        "anchor_context": option["context"],
                        "anchor_occurrence": option["occurrence"],
                        "relationship": "Candidate claims ownership of account A-100",
                        "evidence": ["SOURCE: Client Alpha owns A-100", "TARGET: A-100 owner"],
                        "confidence": 0.95,
                    }
                )
        return {
            "summary": "Only operation-backed relationships may be shown.",
            "suggested_tags": [],
            "relationships": relationships,
            "obsolete_owned_links": [],
            "obsolete_owned_tags": [],
        }

    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)
    service.start_vault_sweep("maintenance", change_mode="review")
    assert service._sweep_task is not None
    await service._sweep_task

    change = next(
        item
        for item in service.list_vault_changes(status="pending")["items"]
        if item["path"] == "Accounts/Account.md"
    )
    assert len(change["decision"]["relationships"]) == 1
    assert len(change["decision"]["operations"]) == 1
    assert change["decision"]["operations"][0]["kind"] == "inline-link"
