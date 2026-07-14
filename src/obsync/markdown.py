from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .llm import Analysis

GENERATED_START = "<!-- obsync:generated:start -->"
GENERATED_END = "<!-- obsync:generated:end -->"
MANUAL_HEADING = "## My notes"


def _yaml_frontmatter(properties: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(
        properties,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{dumped}\n---"


def _clean_document_text(value: str) -> str:
    return value.replace(GENERATED_START, "[Obsync marker removed]").replace(
        GENERATED_END, "[Obsync marker removed]"
    )


def render_markdown(
    *,
    document_id: str,
    source_path: str,
    source_name: str,
    source_hash: str,
    source_size: int,
    source_mtime_ns: int,
    machine_name: str,
    root_name: str,
    mime_type: str,
    extractor: str,
    extracted_text: str,
    extraction_warning: str,
    truncated: bool,
    analysis: Analysis,
    status: str = "active",
) -> str:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    properties: dict[str, Any] = {
        "obsync_id": document_id,
        "obsync_status": status,
        "obsync_source": source_path,
        "obsync_machine": machine_name,
        "obsync_root": root_name,
        "obsync_hash": source_hash,
        "obsync_mime": mime_type,
        "obsync_updated": now,
        "type": analysis.document_type,
        "category": analysis.category,
        "tags": sorted(set(["obsync", *analysis.tags])),
    }

    lines = [
        _yaml_frontmatter(properties),
        "",
        f"# {analysis.title}",
        "",
        GENERATED_START,
        "",
        "> [!info] Synced by Obsync",
        f"> Source: `{source_path}`  ",
        f"> Machine: **{machine_name}** · Watched folder: **{root_name}**  ",
        f"> Status: **{status}** · Confidence: **{round(analysis.confidence * 100)}%**",
        "",
        "## Summary",
        "",
        analysis.summary or f"Synced source file `{source_name}`.",
    ]

    if analysis.related_notes:
        lines.extend(
            [
                "",
                "## Related notes",
                "",
                *(f"- [[{title}]]" for title in analysis.related_notes),
            ]
        )

    if extraction_warning:
        lines.extend(["", f"> [!warning] Extraction note\n> {extraction_warning}"])

    if status == "source-missing":
        lines.extend(
            [
                "",
                "> [!warning] Source file is missing",
                (
                    "> Obsync could not find this file during the latest scan. "
                    "The note is kept safely."
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Source details",
            "",
            f"- File: `{source_name}`",
            f"- Format: `{mime_type}`",
            f"- Size: `{source_size}` bytes",
            f"- Modified (ns): `{source_mtime_ns}`",
            f"- SHA-256: `{source_hash}`",
            f"- Extractor: `{extractor}`",
        ]
    )
    if truncated:
        lines.append("- Extraction was truncated by the configured character limit")

    if extracted_text:
        safe_text = _clean_document_text(extracted_text)
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Extracted source content</summary>",
                "",
                safe_text,
                "",
                "</details>",
            ]
        )

    lines.extend(
        [
            "",
            GENERATED_END,
            "",
            MANUAL_HEADING,
            "",
            "_Anything written below this heading is preserved when the source changes._",
            "",
        ]
    )
    return "\n".join(lines)


def merge_preserving_manual(existing: str, generated: str) -> str:
    """Replace Obsync-managed content and preserve the user's section below the end marker."""
    if not existing.strip():
        return generated
    if GENERATED_END not in existing:
        raise ValueError("Existing note is not managed by Obsync")

    old_tail = existing.split(GENERATED_END, 1)[1]
    if MANUAL_HEADING in old_tail:
        manual = old_tail.split(MANUAL_HEADING, 1)[1]
        generated_prefix = generated.split(MANUAL_HEADING, 1)[0]
        return f"{generated_prefix}{MANUAL_HEADING}{manual}"
    return generated


def is_managed_note(content: str) -> bool:
    return GENERATED_START in content and GENERATED_END in content


def set_source_status(content: str, status: str) -> str:
    if not is_managed_note(content):
        raise ValueError("Existing note is not managed by Obsync")
    updated = re.sub(r"(?m)^obsync_status:\s*.*$", f"obsync_status: {status}", content, count=1)
    updated = re.sub(
        r"(?m)^> Status: \*\*.*?\*\* · Confidence:",
        f"> Status: **{status}** · Confidence:",
        updated,
        count=1,
    )
    marker = "> [!warning] Source file is missing"
    if status == "source-missing" and marker not in updated:
        insertion = (
            "\n> [!warning] Source file is missing\n"
            "> Obsync could not find this file during the latest scan. The note is kept safely.\n"
        )
        updated = updated.replace("\n## Source details", f"{insertion}\n## Source details", 1)
    elif status != "source-missing" and marker in updated:
        updated = re.sub(
            r"\n> \[!warning\] Source file is missing\n"
            r"> Obsync could not find this file during the latest scan\. "
            r"The note is kept safely\.\n",
            "\n",
            updated,
            count=1,
        )
    return updated


def note_title_from_path(path: Path) -> str:
    return re.sub(r"[-_]", " ", path.stem).strip()
