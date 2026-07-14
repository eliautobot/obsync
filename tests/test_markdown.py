from __future__ import annotations

import pytest

from obsync.llm import Analysis
from obsync.markdown import (
    GENERATED_END,
    GENERATED_START,
    is_managed_note,
    merge_preserving_manual,
    render_markdown,
    set_source_status,
)


def make_note(summary: str = "A useful summary.") -> str:
    return render_markdown(
        document_id="doc-123",
        source_path="Clients/Acme/contract.txt",
        source_name="contract.txt",
        source_hash="abc123",
        source_size=42,
        source_mtime_ns=100,
        machine_name="Office PC",
        root_name="Client Files",
        mime_type="text/plain",
        extractor="text",
        extracted_text="Contract body",
        extraction_warning="",
        truncated=False,
        analysis=Analysis(
            title="Acme Contract",
            summary=summary,
            category="Contracts",
            document_type="contract",
            tags=["acme", "legal"],
            confidence=0.91,
            related_notes=["Acme Client"],
        ),
    )


def test_rendered_note_has_obsidian_structure() -> None:
    note = make_note()
    assert note.startswith("---\n")
    assert "obsync_id: doc-123" in note
    assert "# Acme Contract" in note
    assert "[[Acme Client]]" in note
    assert GENERATED_START in note and GENERATED_END in note
    assert "## My notes" in note


def test_manual_content_survives_update() -> None:
    original = make_note() + "Important personal annotation.\n"
    updated = merge_preserving_manual(original, make_note("A changed summary."))
    assert "A changed summary." in updated
    assert "A useful summary." not in updated
    assert "Important personal annotation." in updated


def test_unmanaged_note_is_never_merged() -> None:
    with pytest.raises(ValueError):
        merge_preserving_manual("# Existing personal note", make_note())


def test_missing_status_can_be_set_and_cleared() -> None:
    missing = set_source_status(make_note(), "source-missing")
    assert "obsync_status: source-missing" in missing
    assert "Source file is missing" in missing
    restored = set_source_status(missing, "active")
    assert "obsync_status: active" in restored
    assert "Source file is missing" not in restored
    assert is_managed_note(restored)
