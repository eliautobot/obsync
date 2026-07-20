from __future__ import annotations

import hashlib

import pytest

from obsync.knowledge_graph import (
    anchor_is_complete_entity_phrase,
    canonical_entity_id,
    graph_relationship_is_eligible,
    markdown_graph_chunks,
    normalize_graph_claims,
    temporal_observation,
)


def test_markdown_graph_chunks_preserve_exact_provenance_and_skip_code() -> None:
    content = (
        "# Operations\n\n"
        "Workspace Backup Plan protects the Ent Forge workspace.\n\n"
        "```text\nWorkspace Backup Plan is only an example here.\n```\n\n"
        "<!-- obsync:maintenance:start -->\n\n"
        "Workspace Backup Plan is generated legacy evidence.\n\n"
        "<!-- obsync:maintenance:end -->\n\n"
        "## Recovery\n\nRestore from the encrypted archive.\n"
    )

    chunks = markdown_graph_chunks("Operations/Recovery.md", content, content_hash="abc")

    assert [item["heading"] for item in chunks] == ["Operations", "Recovery"]
    assert "example here" not in " ".join(item["text"] for item in chunks)
    assert "legacy evidence" not in " ".join(item["text"] for item in chunks)
    for ordinal, chunk in enumerate(chunks):
        assert chunk["ordinal"] == ordinal
        assert content[chunk["start_offset"] : chunk["end_offset"]].strip() == chunk["text"]
        assert chunk["content_hash"] == "abc"
        assert chunk["text_hash"] == hashlib.sha256(chunk["text"].encode()).hexdigest()


@pytest.mark.parametrize(
    "anchor",
    [
        "who want a more",
        "define exact Free vs Pro",
        "around Virtual Office",
        "draft for Eli review",
        "expose host port",
        "goal is to keep Cody",
        "Archo must perform",
    ],
)
def test_incomplete_or_action_clause_anchors_are_rejected(anchor: str) -> None:
    assert anchor_is_complete_entity_phrase(anchor) is False


@pytest.mark.parametrize(
    "anchor",
    ["Workspace Backup Plan", "Ent Forge Outreach Scripts", "Client Alpha", "PQP-4419"],
)
def test_complete_named_entity_anchors_are_allowed(anchor: str) -> None:
    assert anchor_is_complete_entity_phrase(anchor) is True


def test_graph_claims_require_allowed_predicate_grounded_endpoints_and_provenance() -> None:
    evidence = (
        "Deployment Decision supersedes Manual Release Decision after the signed review on "
        "2026-07-19."
    )
    note = {
        "path": "Decisions/2026-07-19-deployment.md",
        "title": "Deployment Decision",
        "aliases": [],
        "properties": {},
        "content": f"# Deployment Decision\n\n{evidence}\n",
        "content_hash": "source-hash",
    }
    raw = {
        "entities": [
            {"name": "Deployment Decision", "type": "decision", "aliases": []},
            {"name": "Manual Release Decision", "type": "decision", "aliases": []},
            {"name": "Invented Platform", "type": "system", "aliases": []},
        ],
        "claims": [
            {
                "source_entity": "Deployment Decision",
                "source_type": "decision",
                "predicate": "supersedes_decision",
                "target_entity": "Manual Release Decision",
                "target_type": "decision",
                "description": (
                    "Deployment Decision supersedes the Manual Release Decision after review."
                ),
                "evidence": evidence,
                "confidence": 0.96,
                "state": "active",
            },
            {
                "source_entity": "Deployment Decision",
                "source_type": "decision",
                "predicate": "related_to",
                "target_entity": "Manual Release Decision",
                "target_type": "decision",
                "evidence": evidence,
                "confidence": 0.99,
            },
            {
                "source_entity": "Deployment Decision",
                "source_type": "decision",
                "predicate": "uses_system",
                "target_entity": "Invented Platform",
                "target_type": "system",
                "evidence": evidence,
                "confidence": 0.99,
            },
        ],
    }

    graph = normalize_graph_claims(note, raw)

    assert len(graph["claims"]) == 1
    claim = graph["claims"][0]
    assert claim["predicate"] == "supersedes_decision"
    assert claim["evidence"] == evidence
    assert claim["content_hash"] == "source-hash"
    assert claim["source_chunk_id"]
    assert claim["valid_from"] == "2026-07-19"
    assert note["content"][claim["start_offset"] : claim["end_offset"]] == evidence
    assert all(item["quote"] in note["content"] for item in graph["mentions"])


def test_graph_claims_do_not_invent_dates_or_external_document_ids() -> None:
    note = {
        "path": "Notes/Current.md",
        "title": "Current",
        "content": "# Current\n\nCurrent references External Plan.\n",
        "content_hash": "hash",
    }
    graph = normalize_graph_claims(
        note,
        {
            "entities": [{"name": "External Plan", "type": "document"}],
            "claims": [
                {
                    "source_entity": "Current",
                    "source_type": "document",
                    "predicate": "references_named_document",
                    "target_entity": "External Plan",
                    "target_type": "document",
                    "evidence": "Current references External Plan.",
                    "confidence": 0.99,
                }
            ],
        },
    )

    assert graph["claims"] == []
    assert temporal_observation(note["path"], {}) == ""


def test_canonical_ids_scope_ambiguous_short_people_but_keep_full_names_stable() -> None:
    assert canonical_entity_id("person", "Alex", scope="Team A") != canonical_entity_id(
        "person", "Alex", scope="Team B"
    )
    assert canonical_entity_id("person", "Alex Rivera", scope="Team A") == canonical_entity_id(
        "person", "Alex Rivera", scope="Team B"
    )


def test_graph_v2_link_gate_requires_the_exact_precomputed_edge() -> None:
    source_document_id = canonical_entity_id("document", "Checklist", scope="Ops/Checklist.md")
    target_document_id = canonical_entity_id(
        "document", "Workspace Backup Plan", scope="Ops/Workspace Backup Plan.md"
    )
    edge = {
        "id": "navigation:approved-edge",
        "source_document_id": source_document_id,
        "target_document_id": target_document_id,
        "source_entity": "Checklist",
        "target_entity": "Workspace Backup Plan",
        "predicate": "references_named_document",
        "anchor": "Workspace Backup Plan",
        "confidence": 0.99,
    }
    source = {
        "knowledge_graph": {
            "graph_version": 2,
            "entity_nodes": [{"id": source_document_id, "name": "Checklist", "type": "document"}],
        }
    }
    candidate = {
        "anchor_options": [
            {
                "text": "Workspace Backup Plan",
                "context": "Create Workspace Backup Plan before launch.",
                "graph_specificity": 0.94,
                "canonical_entity_id": target_document_id,
            }
        ],
        "knowledge_graph": {
            "graph_version": 2,
            "entity_nodes": [
                {"id": target_document_id, "name": "Workspace Backup Plan", "type": "document"}
            ],
            "supported_edges": [edge],
        },
    }
    relationship = {
        "graph_edge_id": edge["id"],
        "source_entity": "Checklist",
        "target_entity": "Workspace Backup Plan",
        "predicate": "references_named_document",
        "anchor": "Workspace Backup Plan",
        "anchor_context": "Create Workspace Backup Plan before launch.",
    }

    assert graph_relationship_is_eligible(source, candidate, relationship) is True
    assert (
        graph_relationship_is_eligible(
            source, candidate, {**relationship, "graph_edge_id": "navigation:invented"}
        )
        is False
    )
    assert (
        graph_relationship_is_eligible(
            source, candidate, {**relationship, "predicate": "related_to"}
        )
        is False
    )
