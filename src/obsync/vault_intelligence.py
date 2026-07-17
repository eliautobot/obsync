from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from .markdown import is_managed_note, note_tags, note_title
from .security import slugify

MAX_INDEXED_NOTE_CHARS = 2_000_000
MAINTENANCE_START = "<!-- obsync:maintenance:start -->"
MAINTENANCE_END = "<!-- obsync:maintenance:end -->"

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_WIKILINK_RE = re.compile(r"!?(?:\[\[)([^\]\n]+?)(?:\]\])")
_INLINE_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]{1,79})")
_LABELED_IDENTIFIER_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_-]{1,39})"
    r"(?:[ \t]+(?:number|no\.?|id)|[ \t]*[:#-])[ \t:#-]*"
    r"([A-Z0-9][A-Z0-9./_-]{2,})\b"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_TOKEN_WITH_SPAN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'’./_-]*")
_STRUCTURAL_HUB_WORDS = frozenset({"catalog", "dashboard", "hub", "index", "moc", "overview"})
STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "and",
        "are",
        "before",
        "being",
        "but",
        "can",
        "document",
        "documents",
        "for",
        "from",
        "have",
        "into",
        "its",
        "not",
        "obsync",
        "other",
        "our",
        "should",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "was",
        "were",
        "will",
        "with",
        "you",
    }
)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def strip_maintenance_block(content: str) -> str:
    """Remove model-generated relationship data before indexing or inference."""
    if MAINTENANCE_START not in content:
        return content
    pattern = re.escape(MAINTENANCE_START) + r".*?" + re.escape(MAINTENANCE_END)
    return re.sub(pattern, "", content, count=1, flags=re.DOTALL).rstrip() + "\n"


def normalized_text(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold()))


def search_terms(value: str, *, maximum: int = 5000) -> set[str]:
    terms: set[str] = set()
    for word in _WORD_RE.findall(value.casefold()):
        clean = word.strip("_-")
        if clean and clean not in STOP_WORDS:
            terms.add(clean)
            if len(terms) >= maximum:
                break
    return terms


def normalize_obsidian_tag(value: str) -> str:
    """Normalize a native Obsidian tag while preserving hierarchical slash segments."""
    segments = [slugify(part, fallback="", max_length=40) for part in str(value).split("/")]
    clean = "/".join(segment for segment in segments if segment)[:80].strip("/")
    return clean


def _frontmatter_span(content: str) -> tuple[int, int] | None:
    if not content.startswith(("---\n", "---\r\n")):
        return None
    match = re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", content, flags=re.DOTALL)
    return match.span() if match else None


def _protected_markdown_ranges(content: str) -> list[tuple[int, int]]:
    """Return regions where inserting an inline wikilink would change Markdown semantics."""
    ranges: list[tuple[int, int]] = []
    frontmatter = _frontmatter_span(content)
    if frontmatter:
        ranges.append(frontmatter)
    patterns = (
        r"<!--.*?-->",
        r"(?ms)^(?:```|~~~).*?^(?:```|~~~)[ \t]*$",
        r"`+[^`\n]*`+",
        r"!?\[\[[^\]\n]+?\]\]",
        r"!?\[[^\]\n]*\]\([^\)\n]+\)",
    )
    for pattern in patterns:
        ranges.extend(match.span() for match in re.finditer(pattern, content, flags=re.DOTALL))
    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        # Headings are navigation labels, and tables need escaped pipes in aliased wikilinks.
        table_like = (
            stripped.startswith("|")
            or stripped.rstrip().endswith("|")
            or (line.count("|") >= 2 and "[[" not in line)
        )
        if stripped.startswith("#") or table_like:
            ranges.append((offset, offset + len(line)))
        offset += len(line)
    ranges.sort()
    return ranges


def _range_is_free(start: int, end: int, protected: list[tuple[int, int]]) -> bool:
    return not any(
        start < protected_end and end > protected_start
        for protected_start, protected_end in protected
    )


def _free_occurrences(content: str, text_value: str) -> list[tuple[int, int, str]]:
    if not text_value or len(text_value) > 160 or "\n" in text_value:
        return []
    protected = _protected_markdown_ranges(content)
    matches: list[tuple[int, int, str]] = []
    for match in re.finditer(re.escape(text_value), content, flags=re.IGNORECASE):
        start, end = match.span()
        if (
            start > 0
            and (text_value[0].isalnum() or text_value[0] == "_")
            and (content[start - 1].isalnum() or content[start - 1] == "_")
        ):
            continue
        if (
            end < len(content)
            and (text_value[-1].isalnum() or text_value[-1] == "_")
            and (content[end].isalnum() or content[end] == "_")
        ):
            continue
        if _range_is_free(start, end, protected):
            matches.append((start, end, content[start:end]))
    return matches


def _candidate_descriptor_terms(candidate: dict[str, Any]) -> tuple[list[str], set[str]]:
    descriptors = [
        str(candidate.get("title", "")),
        Path(str(candidate.get("path", ""))).stem.replace("_", " ").replace("-", " "),
        *(str(value) for value in candidate.get("aliases", [])),
    ]
    for entity in candidate.get("entities", []):
        value = str(entity)
        descriptors.append(value.split(":", 1)[1] if ":" in value else value)
    clean_descriptors: list[str] = []
    terms: set[str] = set()
    for descriptor in descriptors:
        clean = re.sub(r"\s+", " ", descriptor).strip(" .,:;#")[:160]
        if len(clean) < 3 or clean.casefold() in {item.casefold() for item in clean_descriptors}:
            continue
        clean_descriptors.append(clean)
        terms.update(search_terms(clean, maximum=100))
    return clean_descriptors[:30], terms


def inline_anchor_options(
    source_content: str,
    candidate: dict[str, Any],
    *,
    document_frequency: Counter[str] | None = None,
    note_count: int = 1,
    maximum: int = 8,
) -> list[dict[str, Any]]:
    """Find exact, safe source phrases that could naturally point at one candidate note."""
    content = strip_maintenance_block(source_content)
    descriptors, target_terms = _candidate_descriptor_terms(candidate)
    if not target_terms:
        return []
    frequency = document_frequency or Counter()
    total = max(1, note_count)
    scored: dict[str, tuple[float, str, str]] = {}

    def add(value: str, score: float, reason: str) -> None:
        clean = value.strip()
        if not (3 <= len(clean) <= 160) or clean.startswith("#") or "]]" in clean:
            return
        occurrences = _free_occurrences(content, clean)
        if not occurrences:
            return
        actual = occurrences[0][2]
        key = actual.casefold()
        previous = scored.get(key)
        if previous is None or score > previous[0]:
            scored[key] = (score, reason, actual)

    for descriptor in descriptors:
        descriptor_terms = search_terms(descriptor, maximum=100)
        rarity = sum(
            math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1 for term in descriptor_terms
        )
        add(descriptor, 100.0 + rarity, "exact target title, alias, or identifier")

    protected = _protected_markdown_ranges(content)
    line_offset = 0
    for line in content.splitlines(keepends=True):
        tokens = list(_TOKEN_WITH_SPAN_RE.finditer(line))
        for start_index in range(len(tokens)):
            for length in range(1, min(6, len(tokens) - start_index) + 1):
                first = tokens[start_index]
                last = tokens[start_index + length - 1]
                start = line_offset + first.start()
                end = line_offset + last.end()
                if not _range_is_free(start, end, protected):
                    continue
                phrase = content[start:end]
                phrase_terms = search_terms(phrase, maximum=30)
                shared = phrase_terms & target_terms
                if not shared:
                    continue
                rarity = {
                    term: math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1
                    for term in shared
                }
                rare_single = len(shared) == 1 and any(
                    frequency.get(term, 0) / total <= 0.05 and len(term) >= 4 for term in shared
                )
                if len(shared) < 2 and not rare_single:
                    continue
                extra_terms = phrase_terms - target_terms
                score = sum(rarity.values()) + len(shared) * 2 - len(extra_terms) * 0.35
                add(phrase, score, "distinctive phrase shared with the target note")
        line_offset += len(line)

    ranked = sorted(scored.items(), key=lambda item: (-item[1][0], len(item[0]), item[0]))
    return [
        {"text": actual, "score": round(score, 4), "reason": reason}
        for _key, (score, reason, actual) in ranked[:maximum]
    ]


def _split_link_target(value: str) -> tuple[str, str]:
    raw = str(value).strip()
    path, _separator, label = raw.partition("|")
    return path.strip().removesuffix(".md"), label.strip()


def _render_inline_link(target: str, anchor: str) -> str:
    path, _label = _split_link_target(target)
    if not path or any(value in path for value in ("\n", "|", "]]")):
        return ""
    return f"[[{path}|{anchor}]]"


def add_inline_link(content: str, *, target: str, anchor: str) -> tuple[str, dict[str, Any] | None]:
    rendered = _render_inline_link(target, anchor)
    if not rendered or rendered in content:
        return content, None
    occurrences = _free_occurrences(content, anchor)
    if not occurrences:
        return content, None
    start, end, actual = occurrences[0]
    rendered = _render_inline_link(target, actual)
    updated = content[:start] + rendered + content[end:]
    path, _label = _split_link_target(target)
    return updated, {
        "action": "add",
        "kind": "inline-link",
        "key": path.casefold(),
        "target": path,
        "anchor": actual,
        "rendered": rendered,
    }


def remove_owned_inline_link(
    content: str, operation: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    rendered = str(operation.get("rendered", ""))
    anchor = str(operation.get("anchor", ""))
    if not rendered or content.count(rendered) != 1:
        return content, None
    return content.replace(rendered, anchor, 1), {
        "action": "remove",
        "kind": "inline-link",
        "key": str(operation.get("key") or operation.get("target", "")).casefold(),
        "target": str(operation.get("target", "")),
        "anchor": anchor,
        "rendered": rendered,
        "ownership_id": str(operation.get("id", "")),
    }


def _replace_frontmatter_tags(content: str, tags: list[str]) -> str:
    original = content
    crlf_count = content.count("\r\n")
    use_crlf = crlf_count > content.count("\n") - crlf_count
    if use_crlf:
        content = content.replace("\r\n", "\n")

    def restore(value: str) -> str:
        return value.replace("\n", "\r\n") if use_crlf else value

    normalized: list[str] = []
    for value in tags:
        tag = normalize_obsidian_tag(value)
        if tag and tag.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(tag)
    if not content.startswith("---\n"):
        if not normalized:
            return original
        block = "tags:\n" + "\n".join(f"  - {tag}" for tag in normalized)
        return restore(f"---\n{block}\n---\n{content}")
    span = _frontmatter_span(content)
    if not span:
        return original
    frontmatter = content[4 : span[1] - (4 if content[span[1] - 1] == "\n" else 3)]
    lines = frontmatter.splitlines()
    tag_start = next(
        (index for index, line in enumerate(lines) if re.match(r"^tags\s*:", line)), None
    )
    tag_end = tag_start
    if tag_start is not None:
        tag_end = tag_start + 1
        while tag_end < len(lines) and (
            not lines[tag_end].strip()
            or lines[tag_end].startswith((" ", "\t"))
            or re.match(r"^-\s+", lines[tag_end])
            or lines[tag_end].lstrip().startswith("#")
        ):
            tag_end += 1
        # Comments and YAML anchors inside the tag declaration are human context; fail closed.
        if any(
            line.lstrip().startswith("#") or "#" in line or "&" in line
            for line in lines[tag_start:tag_end]
        ):
            return original
    replacement = ["tags:", *(f"  - {tag}" for tag in normalized)] if normalized else []
    if tag_start is None:
        lines.extend(replacement)
    else:
        lines[tag_start:tag_end] = replacement
    if not any(line.strip() for line in lines):
        return restore(content[span[1] :])
    rebuilt = "---\n" + "\n".join(lines).rstrip() + "\n---"
    if content[span[1] - 1 : span[1]] == "\n":
        rebuilt += "\n"
    return restore(rebuilt + content[span[1] :])


def change_native_tag(
    content: str, *, tag: str, remove: bool = False, ownership_id: str = ""
) -> tuple[str, dict[str, Any] | None]:
    clean = normalize_obsidian_tag(tag)
    if not clean:
        return content, None
    current = note_tags(content.replace("\r\n", "\n"))
    current_keys = {value.casefold() for value in current}
    if remove:
        if clean.casefold() not in current_keys:
            return content, None
        tags = [value for value in current if value.casefold() != clean.casefold()]
        action = "remove"
    else:
        if clean.casefold() in current_keys:
            return content, None
        tags = [*current, clean]
        action = "add"
    updated = _replace_frontmatter_tags(content, tags)
    if updated == content:
        return content, None
    return updated, {
        "action": action,
        "kind": "frontmatter-tag",
        "key": clean.casefold(),
        "tag": clean,
        "rendered": clean,
        "ownership_id": ownership_id,
    }


def native_maintenance_content(
    content: str,
    relationships: list[dict[str, Any]],
    *,
    suggested_tags: list[str] | None = None,
    owned_removals: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Apply only native inline links/frontmatter tags and return operation-level audit data."""
    updated = strip_maintenance_block(content)
    operations: list[dict[str, Any]] = []
    if updated != content:
        operations.append({"action": "remove", "kind": "legacy-maintenance-block", "key": "legacy"})
    for owned in owned_removals or []:
        if owned.get("kind") == "inline-link":
            updated, operation = remove_owned_inline_link(updated, owned)
        elif owned.get("kind") == "frontmatter-tag":
            updated, operation = change_native_tag(
                updated,
                tag=str(owned.get("tag") or owned.get("key", "")),
                remove=True,
                ownership_id=str(owned.get("id", "")),
            )
        else:
            operation = None
        if operation:
            operations.append(operation)
    for relationship in relationships:
        updated, operation = add_inline_link(
            updated,
            target=str(relationship.get("target", "")),
            anchor=str(relationship.get("anchor", "")),
        )
        if operation:
            operation["relationship"] = str(relationship.get("relationship", ""))
            operation["confidence"] = float(relationship.get("confidence", 0.0))
            operations.append(operation)
    for tag in suggested_tags or []:
        updated, operation = change_native_tag(updated, tag=tag)
        if operation:
            operations.append(operation)
    return updated, operations


def reapply_owned_operations(
    current_content: str, generated_content: str, operations: list[dict[str, Any]]
) -> str:
    """Rebase owned edits across sync without resurrecting user removals."""
    updated = generated_content
    for operation in operations:
        kind = str(operation.get("kind", ""))
        if kind == "inline-link":
            rendered = str(operation.get("rendered", ""))
            anchor = str(operation.get("anchor", ""))
            if (
                rendered
                and anchor
                and rendered in current_content
                and rendered not in updated
                and anchor in updated
            ):
                updated, _added = add_inline_link(
                    updated,
                    target=str(operation.get("target", "")),
                    anchor=anchor,
                )
        elif kind == "frontmatter-tag":
            tag = str(operation.get("tag") or operation.get("key", ""))
            if tag and tag.casefold() in {value.casefold() for value in note_tags(current_content)}:
                updated, _added = change_native_tag(updated, tag=tag)
    return updated


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Normalize YAML values before they cross the Desktop JSON boundary."""
    if depth >= 20:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {
            str(key)[:200]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:1000]
        }
    if isinstance(value, set):
        return [_json_safe(item, depth=depth + 1) for item in sorted(value, key=str)[:1000]]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:1000]]
    return str(value)


def _frontmatter(content: str) -> dict[str, Any]:
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}
    try:
        raw, _body = normalized[4:].split("\n---", 1)
        values = yaml.safe_load(raw) or {}
    except (ValueError, yaml.YAMLError):
        return {}
    if not isinstance(values, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in values.items():
        clean_key = str(key).strip()[:100]
        if clean_key:
            result[clean_key] = _json_safe(value)
        if len(result) >= 100:
            break
    return result


def _string_list(value: Any, *, maximum: int = 100, length: int = 200) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result: list[str] = []
    for item in values:
        clean = str(item).strip()[:length]
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= maximum:
            break
    return result


def extract_entities(
    content: str, *, title: str = "", aliases: list[str] | None = None
) -> list[str]:
    entities: list[str] = []

    def add(value: str) -> None:
        clean = re.sub(r"\s+", " ", value).strip(" .,:;#")[:200]
        if len(clean) >= 3 and clean.casefold() not in {item.casefold() for item in entities}:
            entities.append(clean)

    if title:
        add(title)
    for alias in aliases or []:
        add(alias)
    for label, identifier in _LABELED_IDENTIFIER_RE.findall(content[:MAX_INDEXED_NOTE_CHARS]):
        if not any(character.isdigit() for character in identifier):
            continue
        add(f"{label.casefold()}:{identifier.casefold()}")
    for email in _EMAIL_RE.findall(content[:MAX_INDEXED_NOTE_CHARS]):
        add(f"email:{email.casefold()}")
    return entities[:300]


@dataclass(slots=True)
class IndexedNote:
    path: str
    title: str
    tags: list[str]
    aliases: list[str]
    headings: list[str]
    links: list[str]
    backlinks: list[str]
    properties: dict[str, Any]
    entities: list[str]
    content: str
    content_hash: str
    modified_ns: int
    size: int
    managed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_note(
    path: Path,
    *,
    vault: Path | None = None,
    content: str | None = None,
    modified_ns: int | None = None,
) -> IndexedNote:
    raw = path.read_text(encoding="utf-8") if content is None else content
    bounded = raw[:MAX_INDEXED_NOTE_CHARS]
    knowledge = strip_maintenance_block(bounded)
    properties = _frontmatter(bounded)
    aliases = _string_list(properties.get("aliases", properties.get("alias", [])), maximum=100)
    tags = note_tags(knowledge.replace("\r\n", "\n"))
    for tag in _INLINE_TAG_RE.findall(knowledge):
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= 200:
            break
    headings = [heading.strip()[:300] for heading in _HEADING_RE.findall(knowledge)[:500]]
    links: list[str] = []
    for raw_link in _WIKILINK_RE.findall(knowledge):
        target = raw_link.split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in links:
            links.append(target[:500])
        if len(links) >= 1000:
            break
    stat = path.stat() if path.exists() else None
    relative = path.relative_to(vault).as_posix() if vault else path.as_posix()
    title = note_title(bounded, path)
    return IndexedNote(
        path=relative,
        title=title,
        tags=tags,
        aliases=aliases,
        headings=headings,
        links=links,
        backlinks=[],
        properties=properties,
        entities=extract_entities(knowledge, title=title, aliases=aliases),
        content=bounded,
        content_hash=content_hash(raw),
        modified_ns=modified_ns
        if modified_ns is not None
        else int(stat.st_mtime_ns if stat else 0),
        size=len(raw.encode("utf-8")),
        managed=is_managed_note(bounded),
    )


def _target_key(value: str) -> str:
    clean = value.replace("\\", "/").strip().removesuffix(".md").strip("/")
    return clean.casefold()


def add_backlinks(notes: list[dict[str, Any]]) -> None:
    by_path = {_target_key(str(note.get("path", ""))): note for note in notes}
    by_title: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        for value in [note.get("title", ""), Path(str(note.get("path", ""))).stem]:
            key = str(value).strip().casefold()
            if key:
                by_title.setdefault(key, []).append(note)
        note["backlinks"] = []
    for source in notes:
        for raw_target in source.get("links", []):
            target = by_path.get(_target_key(str(raw_target)))
            if target is None:
                matches = by_title.get(Path(str(raw_target)).name.casefold(), [])
                target = matches[0] if len(matches) == 1 else None
            if target is not None and source.get("path") not in target["backlinks"]:
                target["backlinks"].append(source.get("path"))


def link_target(note: dict[str, Any]) -> str:
    path = str(note.get("path", "")).strip().removesuffix(".md")
    title = str(note.get("title", "")).strip() or Path(path).name
    if not path:
        return title
    return f"{path}|{title}" if Path(path).name.casefold() != title.casefold() else path


def explicit_category_hub_relationships(
    source_note: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return category-hub links proven by an exact source mention and a hub-to-source link."""
    source_path = _target_key(str(source_note.get("path", "")))
    source_title = str(source_note.get("title", "")).strip()
    relationships: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("structural_role") != "category-hub":
            continue
        anchor_options = candidate.get("anchor_options", [])
        anchor = next(
            (
                str(option.get("text", ""))
                for option in anchor_options
                if isinstance(option, dict)
                and option.get("reason") == "exact target title, alias, or identifier"
                and str(option.get("text", "")).strip()
            ),
            "",
        )
        if not anchor:
            continue
        candidate_links = {_target_key(str(link)) for link in candidate.get("links", [])}
        if source_path not in candidate_links:
            continue
        relationships.append(
            {
                "target": link_target(candidate),
                "anchor": anchor,
                "relationship_type": "category-hub",
                "relationship": "This category hub explicitly catalogs the source note",
                "evidence": [
                    f"SOURCE: the note explicitly names {anchor}",
                    f"TARGET: the hub directly links to {source_title or source_path}",
                ],
                "confidence": 0.99,
            }
        )
    return relationships


def _identifier_entities(entities: list[str]) -> set[str]:
    return {entity.casefold() for entity in entities if ":" in entity}


def _excerpt(content: str, terms: set[str], *, maximum: int = 2400) -> str:
    if len(content) <= maximum:
        return content.strip()
    lowered = content.casefold()
    positions = [lowered.find(term) for term in terms if len(term) >= 4 and term in lowered]
    start = max(0, min(positions) - 300) if positions else 0
    return content[start : start + maximum].strip()


def rank_notes(
    source_path: str,
    text: str,
    notes: list[dict[str, Any]],
    *,
    limit: int = 100,
    exclude_path: str = "",
) -> list[dict[str, Any]]:
    text = strip_maintenance_block(text)
    source_title = Path(source_path).stem.replace("_", " ").replace("-", " ").strip()
    source_terms = search_terms(f"{source_path} {text}")
    source_entities = extract_entities(text, title=source_title)
    source_entity_keys = {entity.casefold() for entity in source_entities}
    source_ids = _identifier_entities(source_entities)
    normalized_source_title = normalized_text(source_title)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for note in notes:
        if exclude_path and str(note.get("path", "")).casefold() == exclude_path.casefold():
            continue
        title = str(note.get("title", "")).strip()
        content = strip_maintenance_block(str(note.get("content", "")))
        aliases = [str(value) for value in note.get("aliases", [])]
        tags = [str(value) for value in note.get("tags", [])]
        entities = [str(value) for value in note.get("entities", [])]
        note_terms = search_terms(" ".join([title, *aliases, *tags, *entities, content]))
        overlap = source_terms & note_terms
        entity_overlap = source_entity_keys & {entity.casefold() for entity in entities}
        id_overlap = source_ids & _identifier_entities(entities)
        reasons: list[str] = []
        score = min(len(overlap), 30) * 0.6
        if normalized_source_title and normalized_source_title == normalized_text(title):
            score += 35
            reasons.append("matching title")
        title_phrase = title.casefold()
        if len(title_phrase) >= 4 and title_phrase in f"{source_path} {text}".casefold():
            score += 24
            reasons.append(f"document mentions {title}")
        alias_hit = next(
            (alias for alias in aliases if len(alias) >= 4 and alias.casefold() in text.casefold()),
            "",
        )
        if alias_hit:
            score += 20
            reasons.append(f"document mentions alias {alias_hit}")
        if id_overlap:
            score += 40 + 15 * min(len(id_overlap), 3)
            reasons.append("shared record identifier")
        if entity_overlap:
            score += 9 * min(len(entity_overlap), 8)
            reasons.append("shared named entities")
        if overlap:
            reasons.append(f"{len(overlap)} shared search terms")
        if score <= 0:
            continue
        enriched = dict(note)
        enriched.update(
            {
                "score": round(score, 3),
                "reasons": reasons[:6],
                "link_target": link_target(note),
                "content_excerpt": _excerpt(content, source_terms),
            }
        )
        scored.append((score, title.casefold(), enriched))
    scored.sort(key=lambda item: (-item[0], item[1], str(item[2].get("path", ""))))
    return [note for _score, _title, note in scored[: max(0, limit)]]


def existing_note_match(
    source_path: str,
    text: str,
    source_hash: str,
    notes: list[dict[str, Any]],
    *,
    current_destination: str = "",
) -> dict[str, Any] | None:
    ranked = rank_notes(source_path, text, notes, limit=20, exclude_path=current_destination)
    normalized_source = normalized_text(text[:MAX_INDEXED_NOTE_CHARS])
    source_title = normalized_text(Path(source_path).stem.replace("_", " ").replace("-", " "))
    source_entities = extract_entities(text, title=source_title)
    source_ids = _identifier_entities(source_entities)
    candidates: list[dict[str, Any]] = []
    for note in ranked:
        properties = note.get("properties", {}) if isinstance(note.get("properties"), dict) else {}
        note_source_hash = str(properties.get("obsync_hash", ""))
        note_normalized = normalized_text(str(note.get("content", ""))[:MAX_INDEXED_NOTE_CHARS])
        title_equal = source_title and source_title == normalized_text(str(note.get("title", "")))
        note_ids = _identifier_entities([str(item) for item in note.get("entities", [])])
        shared_ids = source_ids & note_ids
        similarity = 0.0
        if normalized_source and note_normalized:
            if normalized_source == note_normalized:
                similarity = 1.0
            elif title_equal or shared_ids:
                similarity = SequenceMatcher(
                    None, normalized_source[:100_000], note_normalized[:100_000]
                ).ratio()
        strength = ""
        confidence = 0.0
        evidence = list(note.get("reasons", []))
        if note_source_hash and note_source_hash == source_hash:
            strength, confidence = "exact", 1.0
            evidence.insert(0, "same source SHA-256")
        elif similarity == 1.0:
            strength, confidence = "exact", 0.99
            evidence.insert(0, "same normalized content")
        elif shared_ids and (title_equal or similarity >= 0.45):
            strength, confidence = "strong", min(0.98, 0.86 + 0.03 * len(shared_ids))
            evidence.insert(0, "same stable record identifier")
        elif title_equal and similarity >= 0.72:
            strength, confidence = "strong", min(0.95, 0.75 + similarity * 0.2)
            evidence.insert(0, "matching title and highly similar content")
        elif title_equal:
            strength, confidence = "possible", 0.68
            evidence.insert(0, "matching title")
        if strength:
            candidates.append(
                {
                    **note,
                    "strength": strength,
                    "confidence": round(confidence, 3),
                    "evidence": evidence[:8],
                    "similarity": round(similarity, 3),
                }
            )
    candidates.sort(key=lambda item: (-float(item["confidence"]), -float(item.get("score", 0))))
    if not candidates:
        return None
    best = candidates[0]
    if len(candidates) > 1 and candidates[1]["confidence"] >= best["confidence"] - 0.03:
        best = {**best, "strength": "ambiguous", "confidence": min(best["confidence"], 0.6)}
        best["evidence"] = [*best["evidence"], "multiple vault notes scored similarly"]
    return best


def _property_scalars(value: Any, *, prefix: str = "", depth: int = 0) -> set[str]:
    if depth >= 5:
        return set()
    if isinstance(value, dict):
        result: set[str] = set()
        for key, item in list(value.items())[:100]:
            clean_key = normalized_text(str(key))[:80]
            result.update(
                _property_scalars(
                    item,
                    prefix=f"{prefix}.{clean_key}" if prefix and clean_key else clean_key,
                    depth=depth + 1,
                )
            )
        return result
    if isinstance(value, (list, tuple, set)):
        result: set[str] = set()
        for item in list(value)[:100]:
            result.update(_property_scalars(item, prefix=prefix, depth=depth + 1))
        return result
    clean = normalized_text(str(value))[:200]
    if not prefix or len(clean) < 3:
        return set()
    return {f"{prefix.casefold()}={clean}"}


def _note_search_text(note: dict[str, Any]) -> str:
    properties = note.get("properties", {})
    property_text = " ".join(sorted(_property_scalars(properties)))
    values = [
        str(note.get("title", "")),
        str(note.get("path", "")),
        *(str(value) for value in note.get("aliases", [])),
        *(str(value) for value in note.get("tags", [])),
        *(str(value) for value in note.get("headings", [])),
        *(str(value) for value in note.get("entities", [])),
        property_text,
        strip_maintenance_block(str(note.get("content", ""))),
    ]
    return " ".join(values)


class AdaptiveVaultIndex:
    """Corpus-adaptive retrieval; it proposes candidates but never decides relationships."""

    def __init__(self, notes: list[dict[str, Any]]):
        self.notes = notes
        self.note_count = len(notes)
        self.term_counts: list[Counter[str]] = []
        self.postings: dict[str, list[int]] = {}
        self.tag_counts: Counter[str] = Counter()
        self.folder_counts: Counter[str] = Counter()
        document_frequency: Counter[str] = Counter()
        for index, note in enumerate(notes):
            terms = Counter(search_terms(_note_search_text(note)))
            self.term_counts.append(terms)
            for term in terms:
                self.postings.setdefault(term, []).append(index)
                document_frequency[term] += 1
            self.tag_counts.update(
                normalize_obsidian_tag(str(tag))
                for tag in note.get("tags", [])
                if normalize_obsidian_tag(str(tag))
            )
            path = Path(str(note.get("path", "")))
            parent = path.parent.as_posix()
            if parent not in {"", "."}:
                parts = path.parent.parts
                for depth in range(1, min(len(parts), 6) + 1):
                    self.folder_counts[Path(*parts[:depth]).as_posix()] += 1
        self.document_frequency = document_frequency
        total = max(1, len(notes))
        self.idf = {
            term: math.log((total + 1) / (frequency + 1)) + 1
            for term, frequency in document_frequency.items()
        }

    @staticmethod
    def _hub_score(note: dict[str, Any]) -> int:
        title_terms = search_terms(
            f"{note.get('title', '')} {Path(str(note.get('path', ''))).stem}", maximum=50
        )
        score = 2 if title_terms & _STRUCTURAL_HUB_WORDS else 0
        outgoing = len(note.get("links", []))
        if outgoing >= 10:
            score += 2
        elif outgoing >= 5:
            score += 1
        return score

    def corpus_profile(self) -> dict[str, Any]:
        total = max(1, self.note_count)
        high_frequency = [
            {"term": term, "notes": count, "ratio": round(count / total, 4)}
            for term, count in sorted(
                self.document_frequency.items(), key=lambda item: (-item[1], item[0])
            )
            if count >= max(3, math.ceil(total * 0.05))
        ][:120]
        hubs = [
            {
                "path": str(note.get("path", "")),
                "title": str(note.get("title", "")),
                "outgoing_links": len(note.get("links", [])),
            }
            for note in self.notes
            if self._hub_score(note) >= 2
        ][:100]
        return {
            "note_count": self.note_count,
            "folders": [
                {"path": path, "notes": count}
                for path, count in self.folder_counts.most_common(150)
            ],
            "tag_vocabulary": [
                {"tag": tag, "notes": count} for tag, count in self.tag_counts.most_common(150)
            ],
            "high_frequency_terms": high_frequency,
            "existing_category_hubs": hubs,
        }

    def candidates(
        self,
        source_path: str,
        text: str,
        *,
        exclude_path: str = "",
        maximum: int = 20,
        source_note: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        source_text = _note_search_text(source_note) if source_note else f"{source_path} {text}"
        query_terms = Counter(search_terms(source_text))
        indexes: set[int] = set()
        for term in sorted(query_terms, key=lambda value: (-self.idf.get(value, 0), value)):
            indexes.update(self.postings.get(term, []))
            if len(indexes) >= 2000:
                break
        source_properties = _property_scalars((source_note or {}).get("properties", {}))
        query_weight = sum(self.idf.get(term, 1) ** 2 for term in query_terms) ** 0.5 or 1.0
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for index in indexes:
            note = self.notes[index]
            path = str(note.get("path", ""))
            if exclude_path and path.casefold() == exclude_path.casefold():
                continue
            counts = self.term_counts[index]
            overlap = set(query_terms) & set(counts)
            if not overlap:
                continue
            dot = sum(self.idf.get(term, 1) ** 2 for term in overlap)
            note_weight = sum(self.idf.get(term, 1) ** 2 for term in counts) ** 0.5 or 1.0
            similarity = dot / (query_weight * note_weight)
            shared_properties = source_properties & _property_scalars(note.get("properties", {}))
            score = similarity + min(len(shared_properties), 5) * 0.05
            reasons = [f"corpus similarity {similarity:.3f}"]
            if shared_properties:
                reasons.append(f"{len(shared_properties)} exact shared property value(s)")
            clean_content = strip_maintenance_block(str(note.get("content", "")))
            enriched = dict(note)
            enriched.update(
                {
                    "retrieval_score": round(score, 6),
                    "score": round(score, 6),
                    "reasons": reasons,
                    "link_target": link_target(note),
                    "content_excerpt": _excerpt(clean_content, set(query_terms), maximum=4000),
                    "structural_role": "category-hub" if self._hub_score(note) >= 2 else "note",
                }
            )
            scored.append((score, str(note.get("title", "")).casefold(), enriched))
        scored.sort(key=lambda item: (-item[0], item[1], str(item[2].get("path", ""))))
        result = [note for _score, _title, note in scored[: max(0, maximum)]]
        for note in result:
            note["anchor_options"] = inline_anchor_options(
                text,
                note,
                document_frequency=self.document_frequency,
                note_count=self.note_count,
            )
        return result


def maintenance_content(
    content: str,
    related: list[dict[str, Any]],
    *,
    include_tags: bool = True,
    suggested_tags: list[str] | None = None,
) -> str:
    links: list[tuple[str, str]] = []
    for note in related:
        target = str(note.get("target") or link_target(note)).strip()
        relationship = str(note.get("relationship", "")).strip()[:160]
        if target and target not in {item[0] for item in links}:
            links.append((target, relationship))
    tags: list[str] = []
    if include_tags:
        tag_sources: list[Any] = list(suggested_tags or [])
        if suggested_tags is None:
            for note in related:
                tag_sources.extend(note.get("tags", []))
        for value in tag_sources:
            tag = slugify(str(value), fallback="", max_length=40)
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 20:
                break
    if not links and not tags:
        return strip_maintenance_block(content)
    lines = [MAINTENANCE_START, "", "## Related knowledge", ""]
    lines.extend(
        f"- [[{target}]]" + (f" — {relationship}" if relationship else "")
        for target, relationship in links
    )
    if tags:
        lines.extend(["", "Related tags: " + " ".join(f"#{tag}" for tag in tags)])
    lines.extend(
        [
            "",
            "> [!info] Maintained by Obsync",
            "> This relationship block is refreshed by vault maintenance sweeps.",
            "",
            MAINTENANCE_END,
        ]
    )
    block = "\n".join(lines)
    if MAINTENANCE_START in content and MAINTENANCE_END in content:
        return re.sub(
            re.escape(MAINTENANCE_START) + r".*?" + re.escape(MAINTENANCE_END),
            block,
            content,
            count=1,
            flags=re.DOTALL,
        )
    return content.rstrip() + "\n\n" + block + "\n"


def serialize_note_for_db(note: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(note.get("path", "")),
        str(note.get("title", "")),
        json.dumps(note.get("tags", []), ensure_ascii=False),
        json.dumps(note.get("aliases", []), ensure_ascii=False),
        json.dumps(note.get("headings", []), ensure_ascii=False),
        json.dumps(note.get("links", []), ensure_ascii=False),
        json.dumps(note.get("backlinks", []), ensure_ascii=False),
        json.dumps(note.get("properties", {}), ensure_ascii=False, default=str),
        json.dumps(note.get("entities", []), ensure_ascii=False),
        str(note.get("content", "")),
        str(note.get("content_hash", "")),
        int(note.get("modified_ns", 0)),
        int(note.get("size", 0)),
        int(bool(note.get("managed"))),
    )


def decode_db_note(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for source, target, fallback in (
        ("tags_json", "tags", []),
        ("aliases_json", "aliases", []),
        ("headings_json", "headings", []),
        ("links_json", "links", []),
        ("backlinks_json", "backlinks", []),
        ("properties_json", "properties", {}),
        ("entities_json", "entities", []),
    ):
        try:
            result[target] = json.loads(result.pop(source, "") or json.dumps(fallback))
        except (TypeError, json.JSONDecodeError):
            result[target] = fallback
    result["managed"] = bool(result.get("managed"))
    return result
