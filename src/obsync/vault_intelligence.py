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
from urllib.parse import unquote, urlsplit

import yaml

from .knowledge_graph import graph_relationship_is_eligible
from .markdown import is_managed_note, note_tags, note_title
from .security import slugify

MAX_INDEXED_NOTE_CHARS = 2_000_000
MAX_INLINE_PHRASE_SCAN_CHARS = 40_000
MAINTENANCE_START = "<!-- obsync:maintenance:start -->"
MAINTENANCE_END = "<!-- obsync:maintenance:end -->"

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_WIKILINK_RE = re.compile(r"!?(?:\[\[)([^\]\n]+?)(?:\]\])")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
_INLINE_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]{1,79})")
_LABELED_IDENTIFIER_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_-]{1,39})"
    r"(?:[ \t]+(?:number|no\.?|id)|[ \t]*[:#-])[ \t:#-]*"
    r"([A-Z0-9][A-Z0-9./_-]{2,})\b"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_TOKEN_WITH_SPAN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'’./_-]*")
_STRUCTURAL_HUB_WORDS = frozenset(
    {"catalog", "dashboard", "hub", "index", "moc", "overview", "readme"}
)
_LOW_INFORMATION_ANCHORS = frozenset(
    {
        "action",
        "author",
        "business",
        "category",
        "current",
        "date",
        "decision",
        "document",
        "id",
        "name",
        "note",
        "number",
        "owner",
        "pending",
        "phase",
        "plan",
        "project",
        "status",
        "updated",
        "version",
    }
)
_GENERIC_ENTITY_ROLE_WORDS = frozenset(
    {
        "account",
        "action",
        "architecture",
        "category",
        "checklist",
        "dashboard",
        "decision",
        "document",
        "guide",
        "hub",
        "index",
        "inventory",
        "model",
        "note",
        "overview",
        "plan",
        "project",
        "readme",
        "record",
        "report",
        "setup",
        "specification",
        "system",
        "template",
    }
)
_ANCHOR_EDGE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "create",
        "draft",
        "expose",
        "for",
        "from",
        "goal",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "perform",
        "the",
        "to",
        "watch",
        "where",
        "with",
    }
)
_DATE_ANCHOR_RE = re.compile(r"^(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})$")
_TIME_ANCHOR_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?$", re.IGNORECASE)
_NUMBER_ANCHOR_RE = re.compile(r"^[#]?(?:0x)?[0-9a-f][0-9a-f.,:/_-]*$", re.IGNORECASE)
_BOILERPLATE_ANCHOR_CONTEXT_RE = re.compile(
    r"\b(?:draft\s+for\b.{0,80}\breview|expose\b.{0,80}\bport|"
    r"goal\s+is\s+to\s+keep|must\s+perform)\b",
    re.IGNORECASE,
)
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


def _free_occurrences(
    content: str,
    text_value: str,
    *,
    protected: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int, str]]:
    if not text_value or len(text_value) > 160 or "\n" in text_value:
        return []
    protected_ranges = protected if protected is not None else _protected_markdown_ranges(content)
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
        if _range_is_free(start, end, protected_ranges):
            matches.append((start, end, content[start:end]))
    return matches


def _anchor_context(content: str, start: int, end: int) -> str:
    """Return the complete source line containing an anchor, without list syntax."""
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end < 0:
        line_end = len(content)
    line = content[line_start:line_end].strip()
    return re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line)[:600]


def _low_information_anchor(
    value: str,
    *,
    document_frequency: Counter[str] | None = None,
    note_count: int = 1,
) -> bool:
    clean = re.sub(r"\s+", " ", value).strip(" \t\r\n.,:;!?()[]{}\"'`")
    if len(clean) < 4 or not any(character.isalpha() for character in clean):
        return True
    folded = clean.casefold()
    if (
        folded in _LOW_INFORMATION_ANCHORS
        or _DATE_ANCHOR_RE.fullmatch(folded)
        or _TIME_ANCHOR_RE.fullmatch(folded)
        or _NUMBER_ANCHOR_RE.fullmatch(folded)
    ):
        return True
    terms = search_terms(clean, maximum=20)
    if not terms:
        return True
    if terms <= _LOW_INFORMATION_ANCHORS:
        return True
    if len(terms) == 1:
        term = next(iter(terms))
        frequency = (document_frequency or Counter()).get(term, 0)
        if term in _LOW_INFORMATION_ANCHORS or frequency / max(1, note_count) >= 0.1:
            return True
    return False


def _document_entity_id(candidate: dict[str, Any]) -> str:
    path = str(candidate.get("path", "")).replace("\\", "/").strip().removesuffix(".md")
    if path:
        return f"document:{path.casefold()}"
    title = normalized_text(str(candidate.get("title", "")))
    return f"document:{title}" if title else ""


def _anchor_graph_specificity(
    value: str,
    candidate: dict[str, Any],
    *,
    document_frequency: Counter[str],
    note_count: int,
    reason: str,
) -> tuple[float, str]:
    """Score whether an anchor identifies the target rather than a generic concept.

    This uses inverse-document-frequency entity weighting. A short phrase such as ``backup plan``
    is not accepted merely because it is an alias: it omits the distinguishing
    ``workspace`` identity from ``Workspace Backup Plan``. Stable identifiers and complete,
    distinctive names remain eligible.
    """

    anchor_terms = search_terms(value, maximum=30)
    if not anchor_terms:
        return 0.0, ""
    title = str(candidate.get("title", "")).strip()
    primary_terms = search_terms(
        title or Path(str(candidate.get("path", ""))).stem.replace("_", " ").replace("-", " "),
        maximum=30,
    )
    informative_primary = primary_terms - _GENERIC_ENTITY_ROLE_WORDS
    informative_anchor = anchor_terms - _GENERIC_ENTITY_ROLE_WORDS
    total = max(1, note_count)
    ratios = [document_frequency.get(term, 0) / total for term in informative_anchor]
    rare_ratio = min(ratios, default=1.0)
    coverage = (
        len(informative_anchor & informative_primary) / len(informative_primary)
        if informative_primary
        else 0.0
    )
    has_identifier = any(character.isdigit() for character in value) or bool(
        re.search(r"\b[A-Za-z][A-Za-z0-9_-]*[:#-][A-Za-z0-9][A-Za-z0-9./_-]{2,}\b", value)
    )
    full_primary_name = bool(primary_terms) and primary_terms <= anchor_terms
    normalized_anchor = normalized_text(value)
    target_knowledge = normalized_text(
        " ".join(
            [
                str(candidate.get("content_excerpt", "")),
                str(candidate.get("content", ""))[:20_000],
            ]
        )
    )
    exact_target_phrase = len(normalized_anchor) >= 8 and normalized_anchor in target_knowledge
    score = 0.0
    if has_identifier and reason == "exact target title, alias, or identifier":
        score = 1.0
    elif full_primary_name and len(informative_primary) >= 2:
        score = 0.94
    elif full_primary_name and len(informative_primary) == 1:
        score = (
            0.86
            if candidate.get("structural_role") == "category-hub"
            else 0.82
            if rare_ratio <= 0.01
            else 0.0
        )
    elif reason == "exact target title, alias, or identifier" and len(informative_anchor) >= 2:
        score = 0.86
    elif coverage >= 0.8 and len(informative_anchor) >= 2:
        score = 0.82
    elif (
        reason == "distinctive phrase shared with the target note"
        and len(informative_anchor) >= 3
        and exact_target_phrase
        and rare_ratio <= 0.05
    ):
        score = 0.74
    if (
        len(informative_anchor) < 2
        and primary_terms & _GENERIC_ENTITY_ROLE_WORDS
        and not has_identifier
        and not full_primary_name
    ):
        score = min(score, 0.45)
    return round(score, 4), _document_entity_id(candidate) if score >= 0.7 else ""


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
    protected_ranges: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Find exact, safe source phrases that could naturally point at one candidate note."""
    content = strip_maintenance_block(source_content)
    descriptors, descriptor_terms = _candidate_descriptor_terms(candidate)
    target_terms = search_terms(
        " ".join(
            [
                *descriptors,
                str(candidate.get("content_excerpt", "")),
                str(candidate.get("content", ""))[:20_000],
            ]
        ),
        maximum=5000,
    )
    if not descriptor_terms and not target_terms:
        return []
    frequency = document_frequency or Counter()
    total = max(1, note_count)
    scored: dict[str, tuple[float, str, str, int, str, float, str]] = {}
    protected = (
        protected_ranges if protected_ranges is not None else _protected_markdown_ranges(content)
    )
    normalized_target_knowledge = normalized_text(
        " ".join(
            [
                str(candidate.get("content_excerpt", "")),
                str(candidate.get("content", ""))[:20_000],
            ]
        )
    )

    def add(
        value: str,
        score: float,
        reason: str,
        *,
        known_occurrence: tuple[int, int, str] | None = None,
    ) -> None:
        clean = value.strip()
        edge_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&'’./_-]*", clean)
        if (
            not (4 <= len(clean) <= 160)
            or clean.startswith("#")
            or "]]" in clean
            or any(marker in clean for marker in ("*", "`", "[", "]", "<", ">"))
            or (
                reason != "exact target title, alias, or identifier"
                and any(marker in clean for marker in (",", ";", ":"))
            )
            or (
                reason != "exact target title, alias, or identifier"
                and edge_words
                and edge_words[0].casefold() in _ANCHOR_EDGE_STOP_WORDS
            )
            or (
                reason != "exact target title, alias, or identifier"
                and edge_words
                and edge_words[-1].casefold() in _ANCHOR_EDGE_STOP_WORDS
            )
            or _low_information_anchor(clean, document_frequency=frequency, note_count=total)
        ):
            return
        occurrences = (
            [known_occurrence]
            if known_occurrence is not None
            else _free_occurrences(content, clean, protected=protected)
        )
        if not occurrences:
            return
        for occurrence, (start, end, actual) in enumerate(occurrences):
            graph_specificity, canonical_entity_id = _anchor_graph_specificity(
                actual,
                candidate,
                document_frequency=frequency,
                note_count=total,
                reason=reason,
            )
            if graph_specificity < 0.7 or not canonical_entity_id:
                continue
            context = _anchor_context(content, start, end)
            if _BOILERPLATE_ANCHOR_CONTEXT_RE.search(context):
                continue
            context_terms = search_terms(context, maximum=100)
            contextual_overlap = context_terms & target_terms
            if reason != "exact target title, alias, or identifier" and len(contextual_overlap) < 2:
                continue
            contextual_score = score + sum(
                math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1
                for term in contextual_overlap
            )
            key = actual.casefold()
            previous = scored.get(key)
            if previous is None or contextual_score > previous[0]:
                scored[key] = (
                    contextual_score,
                    reason,
                    actual,
                    occurrence,
                    context,
                    graph_specificity,
                    canonical_entity_id,
                )

    for descriptor in descriptors:
        descriptor_terms = search_terms(descriptor, maximum=100)
        rarity = sum(
            math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1 for term in descriptor_terms
        )
        add(descriptor, 100.0 + rarity, "exact target title, alias, or identifier")

    def ranked_options() -> list[dict[str, Any]]:
        ranked = sorted(scored.items(), key=lambda item: (-item[1][0], len(item[0]), item[0]))
        return [
            {
                "text": actual,
                "score": round(score, 4),
                "reason": reason,
                "occurrence": occurrence,
                "context": context,
                "graph_specificity": graph_specificity,
                "canonical_entity_id": canonical_entity_id,
            }
            for _key, (
                score,
                reason,
                actual,
                occurrence,
                context,
                graph_specificity,
                canonical_entity_id,
            ) in ranked[:maximum]
        ]

    # Exact titles, aliases, and identifiers are the clearest possible anchors. Once found,
    # scanning every word combination cannot improve safety and becomes expensive on run logs.
    if scored:
        return ranked_options()
    # Large append-only logs can contain hundreds of thousands of possible word sequences. If
    # they do not explicitly name the target, declining the link is safer than mining a generic
    # shared phrase and keeps whole-vault maintenance bounded.
    if len(content) > MAX_INLINE_PHRASE_SCAN_CHARS:
        return []

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
                raw_phrase = content[start:end]
                phrase = raw_phrase.strip(" \t.,:;!?()[]{}\"'")
                if not phrase:
                    continue
                leading = raw_phrase.find(phrase)
                start += leading
                end = start + len(phrase)
                phrase_terms = search_terms(phrase, maximum=30)
                shared = phrase_terms & target_terms
                if not shared:
                    continue
                descriptor_overlap = phrase_terms & descriptor_terms
                normalized_phrase = normalized_text(phrase)
                exact_target_phrase = (
                    len(normalized_phrase) >= 8 and normalized_phrase in normalized_target_knowledge
                )
                rare_descriptor = len(descriptor_overlap) == 1 and any(
                    frequency.get(term, 0) / total <= 0.02 and len(term) >= 5
                    for term in descriptor_overlap
                )
                distinctive_body_phrase = exact_target_phrase and len(phrase_terms) >= 3
                if not (len(descriptor_overlap) >= 2 or rare_descriptor or distinctive_body_phrase):
                    continue
                rarity = {
                    term: math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1
                    for term in shared
                }
                if len(shared) < 2:
                    continue
                extra_terms = phrase_terms - target_terms
                score = sum(rarity.values()) + len(shared) * 2 - len(extra_terms) * 0.35
                add(
                    phrase,
                    score,
                    "distinctive phrase shared with the target note",
                    known_occurrence=(start, end, phrase),
                )
        line_offset += len(line)

    return ranked_options()


def _split_link_target(value: str) -> tuple[str, str]:
    raw = str(value).strip()
    path, _separator, label = raw.partition("|")
    return path.strip().removesuffix(".md"), label.strip()


def _render_inline_link(target: str, anchor: str) -> str:
    path, _label = _split_link_target(target)
    if not path or any(value in path for value in ("\n", "|", "]]")):
        return ""
    return f"[[{path}|{anchor}]]"


def add_inline_link(
    content: str,
    *,
    target: str,
    anchor: str,
    occurrence: int = 0,
    context: str = "",
) -> tuple[str, dict[str, Any] | None]:
    rendered = _render_inline_link(target, anchor)
    if not rendered or rendered in content:
        return content, None
    occurrences = _free_occurrences(content, anchor)
    if not occurrences:
        return content, None
    if context:
        contextual = [
            item for item in occurrences if _anchor_context(content, item[0], item[1]) == context
        ]
        if contextual:
            occurrences = contextual
            occurrence = 0
    if occurrence < 0 or occurrence >= len(occurrences):
        return content, None
    start, end, actual = occurrences[occurrence]
    rendered = _render_inline_link(target, actual)
    updated = content[:start] + rendered + content[end:]
    path, _label = _split_link_target(target)
    return updated, {
        "action": "add",
        "kind": "inline-link",
        "key": path.casefold(),
        "target": path,
        "anchor": actual,
        "anchor_occurrence": occurrence,
        "anchor_context": _anchor_context(content, start, end),
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
            occurrence=int(relationship.get("anchor_occurrence", 0) or 0),
            context=str(relationship.get("anchor_context", "")),
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
    protected = _protected_markdown_ranges(knowledge)
    for match in _INLINE_TAG_RE.finditer(knowledge):
        if not _range_is_free(match.start(), match.end(), protected):
            continue
        tag = match.group(1)
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
    for raw_link in _MARKDOWN_LINK_RE.findall(knowledge):
        target = _markdown_link_target(raw_link)
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


def _markdown_link_target(value: str) -> str:
    raw = str(value).strip().strip("<>")
    if not raw or raw.startswith(("#", "mailto:", "http://", "https://")):
        return ""
    parsed = urlsplit(raw)
    path = unquote(parsed.path).replace("\\", "/").strip()
    while path.startswith("../"):
        path = path[3:]
    return path.removeprefix("./").removesuffix(".md").strip("/")


def note_links_to(source_note: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Resolve full-path, basename, title, wikilink, and Markdown-link target forms."""
    candidate_path = _target_key(str(candidate.get("path", "")))
    candidate_title = str(candidate.get("title", "")).strip().casefold()
    candidate_stem = Path(str(candidate.get("path", ""))).stem.casefold()
    for raw in source_note.get("links", []):
        target = _target_key(str(raw))
        if not target:
            continue
        if target == candidate_path:
            return True
        leaf = Path(target).name.casefold()
        if leaf and leaf in {candidate_title, candidate_stem}:
            return True
    return False


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


def _stable_entity_parts(value: str) -> tuple[str, str, str] | None:
    raw = re.sub(r"\s+", " ", str(value)).strip()
    if ":" not in raw:
        return None
    entity_type, _separator, name = raw.partition(":")
    clean_type = slugify(entity_type, fallback="identifier", max_length=40).replace("-", "_")
    clean_name = name.strip()[:200]
    if not clean_name:
        return None
    entity_id = f"{clean_type}:{clean_name.casefold()}"
    display_type = "email_address" if clean_type == "email" else clean_type
    return entity_id, clean_name, display_type


def knowledge_graph_nodes(
    note: dict[str, Any],
    *,
    entity_document_frequency: Counter[str] | None = None,
    note_count: int = 1,
) -> list[dict[str, Any]]:
    """Build canonical document and durable-identifier nodes for one note."""

    title = str(note.get("title", "")).strip() or Path(str(note.get("path", ""))).stem
    document_id = _document_entity_id(note)
    aliases = [
        re.sub(r"\s+", " ", str(value)).strip()[:160]
        for value in note.get("aliases", [])
        if str(value).strip()
    ][:20]
    nodes: list[dict[str, Any]] = []
    if document_id and title:
        nodes.append(
            {
                "id": document_id,
                "name": title[:200],
                "type": "document",
                "aliases": aliases,
                "evidence": f"title and path of {str(note.get('path', ''))[:300]}",
                "specificity": 1.0,
            }
        )
    frequency = entity_document_frequency or Counter()
    total = max(1, note_count)
    for raw_entity in note.get("entities", []):
        parts = _stable_entity_parts(str(raw_entity))
        if not parts:
            continue
        entity_id, name, entity_type = parts
        ratio = frequency.get(entity_id, 1) / total
        specificity = max(0.0, min(1.0, 1.0 - ratio))
        nodes.append(
            {
                "id": entity_id,
                "name": name,
                "type": entity_type,
                "aliases": [],
                "evidence": f"exact identifier {raw_entity}",
                "specificity": round(specificity, 4),
            }
        )
        if len(nodes) >= 40:
            break
    return nodes


def _explicit_graph_fields(
    source_note: dict[str, Any], candidate: dict[str, Any], *, predicate: str
) -> dict[str, str]:
    if not (source_note.get("knowledge_graph") or candidate.get("knowledge_graph")):
        return {}
    source_nodes = knowledge_graph_nodes(source_note)
    target_nodes = knowledge_graph_nodes(candidate)
    source_document = next((item for item in source_nodes if item["type"] == "document"), {})
    target_document = next((item for item in target_nodes if item["type"] == "document"), {})
    return {
        "source_entity": str(source_document.get("name", "")),
        "target_entity": str(target_document.get("name", "")),
        "predicate": predicate,
    }


def exact_duplicate_groups(notes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group substantive notes with exactly the same knowledge content."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        knowledge = strip_maintenance_block(str(note.get("content", ""))).strip()
        if len(knowledge) < 40:
            continue
        groups.setdefault(content_hash(knowledge), []).append(note)
    duplicates = [group for group in groups.values() if len(group) > 1]
    for group in duplicates:
        group.sort(
            key=lambda item: (
                -len(item.get("backlinks", [])),
                -len(item.get("links", [])),
                len(Path(str(item.get("path", ""))).parts),
                str(item.get("path", "")).casefold(),
            )
        )
    return sorted(duplicates, key=lambda group: str(group[0].get("path", "")).casefold())


def add_index_membership(content: str, *, source_target: str) -> tuple[str, dict[str, Any] | None]:
    """Append one native MOC/index entry without inventing prose or duplicating a link."""
    path, label = _split_link_target(source_target)
    if not path or any(value in path for value in ("\n", "|", "]]")):
        return content, None
    parsed = parse_note(Path("Index.md"), content=content).as_dict()
    candidate = {"path": f"{path}.md", "title": label or Path(path).name}
    if note_links_to(parsed, candidate):
        return content, None
    wikilink = f"[[{path}|{label}]]" if label else f"[[{path}]]"
    rendered = f"- {wikilink}"
    newline = (
        "\r\n" if content.count("\r\n") > content.count("\n") - content.count("\r\n") else "\n"
    )
    base = content.rstrip("\r\n")
    updated = base + newline + newline + rendered + newline
    return updated, {
        "action": "add",
        "kind": "index-membership",
        "key": path.casefold(),
        "target": path,
        "rendered": rendered,
    }


def apply_native_operations(
    content: str, operations: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Apply an explicitly selected operation subset for operation-level Review."""
    updated = content
    applied: list[dict[str, Any]] = []
    for requested in operations:
        kind = str(requested.get("kind", ""))
        action = str(requested.get("action", ""))
        if kind == "inline-link" and action == "add":
            updated, operation = add_inline_link(
                updated,
                target=str(requested.get("target", "")),
                anchor=str(requested.get("anchor", "")),
                occurrence=int(requested.get("anchor_occurrence", 0) or 0),
                context=str(requested.get("anchor_context", "")),
            )
        elif kind == "inline-link" and action == "remove":
            updated, operation = remove_owned_inline_link(updated, requested)
        elif kind == "frontmatter-tag":
            updated, operation = change_native_tag(
                updated,
                tag=str(requested.get("tag") or requested.get("key", "")),
                remove=action == "remove",
                ownership_id=str(requested.get("ownership_id", "")),
            )
        elif kind == "index-membership" and action == "add":
            updated, operation = add_index_membership(
                updated, source_target=str(requested.get("source_target", ""))
            )
        elif kind == "legacy-maintenance-block" and action == "remove":
            stripped = strip_maintenance_block(updated)
            operation = dict(requested) if stripped != updated else None
            updated = stripped
        else:
            operation = None
        if operation:
            merged = {**requested, **operation}
            applied.append(merged)
    return updated, applied


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
        anchor_option = next(
            (
                option
                for option in anchor_options
                if isinstance(option, dict)
                and option.get("reason") == "exact target title, alias, or identifier"
                and str(option.get("text", "")).strip()
            ),
            None,
        )
        anchor = str((anchor_option or {}).get("text", ""))
        if not anchor or len(search_terms(anchor, maximum=20)) < 2:
            continue
        if note_links_to(source_note, candidate):
            continue
        candidate_links = {_target_key(str(link)) for link in candidate.get("links", [])}
        if source_path not in candidate_links:
            continue
        relationship = {
            "target": link_target(candidate),
            "anchor": anchor,
            "anchor_occurrence": int((anchor_option or {}).get("occurrence", 0) or 0),
            "anchor_context": str((anchor_option or {}).get("context", "")),
            "relationship_type": "category-hub",
            "relationship": "This category hub explicitly catalogs the source note",
            "evidence": [
                f"SOURCE: the note explicitly names {anchor}",
                f"TARGET: the hub directly links to {source_title or source_path}",
            ],
            "confidence": 0.99,
            **_explicit_graph_fields(source_note, candidate, predicate="catalogs_document"),
        }
        if graph_relationship_is_eligible(source_note, candidate, relationship):
            relationships.append(relationship)
    return relationships


def explicit_reciprocal_relationships(
    source_note: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return links when both ordinary notes explicitly identify one another.

    This closes a conservative-model gap without turning similarity into a relationship: the
    source must contain an exact safe title/alias anchor for the candidate, and the candidate must
    independently name or link the source record.
    """

    source_descriptors = [
        str(source_note.get("title", "")),
        Path(str(source_note.get("path", ""))).stem.replace("_", " ").replace("-", " "),
        *(str(value) for value in source_note.get("aliases", [])),
    ]
    source_descriptors = [
        re.sub(r"\s+", " ", value).strip(" .,:;#")
        for value in source_descriptors
        if value and not _low_information_anchor(value)
    ]
    relationships: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("structural_role") == "category-hub" or candidate.get("already_linked"):
            continue
        anchor_option = next(
            (
                option
                for option in candidate.get("anchor_options", [])
                if isinstance(option, dict)
                and option.get("reason") == "exact target title, alias, or identifier"
                and str(option.get("text", "")).strip()
            ),
            None,
        )
        anchor = str((anchor_option or {}).get("text", "")).strip()
        if not anchor:
            continue
        candidate_content = strip_maintenance_block(
            str(candidate.get("learning_content", candidate.get("content", "")))
        )
        reciprocal_descriptor = next(
            (
                descriptor
                for descriptor in source_descriptors
                if _free_occurrences(candidate_content, descriptor)
            ),
            "",
        )
        linked_back = note_links_to(candidate, source_note)
        if not reciprocal_descriptor and not linked_back:
            continue
        target_context = ""
        if reciprocal_descriptor:
            start, end, _actual = _free_occurrences(candidate_content, reciprocal_descriptor)[0]
            target_context = _anchor_context(candidate_content, start, end)
        source_context = str((anchor_option or {}).get("context", ""))
        relationship = {
            "target": link_target(candidate),
            "anchor": anchor,
            "anchor_occurrence": int((anchor_option or {}).get("occurrence", 0) or 0),
            "anchor_context": source_context,
            "relationship_type": "specific-record",
            "relationship": (
                "Both notes explicitly identify the same two-sided record relationship"
            ),
            "evidence": [
                f"SOURCE: {source_context or f'the source explicitly names {anchor}'}",
                (
                    f"TARGET: {target_context}"
                    if target_context
                    else "TARGET: the candidate directly links back to the source note"
                ),
            ],
            "confidence": 0.99,
            **_explicit_graph_fields(source_note, candidate, predicate="cross_references_document"),
        }
        if graph_relationship_is_eligible(source_note, candidate, relationship):
            relationships.append(relationship)
    return relationships


def explicit_reference_relationships(
    source_note: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Link exact document names from checklist and README navigation records."""

    classification = " ".join(
        [
            str(source_note.get("title", "")),
            Path(str(source_note.get("path", ""))).stem.replace("_", " ").replace("-", " "),
        ]
    ).casefold()
    if not re.search(r"\b(?:checklist|readme)\b", classification):
        return []
    relationships: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("already_linked"):
            continue
        anchor_option = next(
            (
                option
                for option in candidate.get("anchor_options", [])
                if isinstance(option, dict)
                and option.get("reason") == "exact target title, alias, or identifier"
                and str(option.get("text", "")).strip()
            ),
            None,
        )
        anchor = str((anchor_option or {}).get("text", "")).strip()
        target = link_target(candidate)
        if not anchor or not target:
            continue
        context = str((anchor_option or {}).get("context", ""))
        relationship = {
            "target": target,
            "anchor": anchor,
            "anchor_occurrence": int((anchor_option or {}).get("occurrence", 0) or 0),
            "anchor_context": context,
            "relationship_type": "reference",
            "relationship": "This navigation record explicitly names the target document",
            "evidence": [
                f"SOURCE: {context or f'the source explicitly names {anchor}'}",
                f"TARGET: {candidate.get('title') or target} is the named document",
            ],
            "confidence": 0.99,
            **_explicit_graph_fields(source_note, candidate, predicate="references_named_document"),
        }
        if graph_relationship_is_eligible(source_note, candidate, relationship):
            relationships.append(relationship)
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
    properties = note.get("learning_properties", note.get("properties", {}))
    property_text = " ".join(sorted(_property_scalars(properties)))
    values = [
        str(note.get("title", "")),
        str(note.get("path", "")),
        *(str(value) for value in note.get("aliases", [])),
        *(str(value) for value in note.get("human_tags", note.get("tags", []))),
        *(str(value) for value in note.get("headings", [])),
        *(str(value) for value in note.get("entities", [])),
        property_text,
        strip_maintenance_block(str(note.get("learning_content", note.get("content", "")))),
    ]
    return " ".join(values)


class AdaptiveVaultIndex:
    """Corpus-adaptive retrieval; it proposes candidates but never decides relationships."""

    def __init__(
        self,
        notes: list[dict[str, Any]],
        *,
        noncanonical_paths: set[str] | None = None,
    ):
        self.notes = notes
        self.noncanonical_paths = {
            _target_key(path) for path in (noncanonical_paths or set()) if _target_key(path)
        }
        self.note_count = len(notes)
        self.term_counts: list[Counter[str]] = []
        self.postings: dict[str, list[int]] = {}
        self.tag_counts: Counter[str] = Counter()
        self.tag_paths: dict[str, list[str]] = {}
        self.folder_counts: Counter[str] = Counter()
        self.entity_document_frequency: Counter[str] = Counter()
        document_frequency: Counter[str] = Counter()
        for index, note in enumerate(notes):
            terms = Counter(search_terms(_note_search_text(note)))
            self.term_counts.append(terms)
            for term in terms:
                self.postings.setdefault(term, []).append(index)
                document_frequency[term] += 1
            for raw_tag in note.get("human_tags", note.get("tags", [])):
                tag = normalize_obsidian_tag(str(raw_tag))
                if not tag:
                    continue
                self.tag_counts[tag] += 1
                self.tag_paths.setdefault(tag, []).append(str(note.get("path", "")))
            path = Path(str(note.get("path", "")))
            parent = path.parent.as_posix()
            if parent not in {"", "."}:
                parts = path.parent.parts
                for depth in range(1, min(len(parts), 6) + 1):
                    self.folder_counts[Path(*parts[:depth]).as_posix()] += 1
            stable_entity_ids = {
                parts[0]
                for raw_entity in note.get("entities", [])
                if (parts := _stable_entity_parts(str(raw_entity)))
            }
            self.entity_document_frequency.update(stable_entity_ids)
        self.document_frequency = document_frequency
        total = max(1, len(notes))
        self.idf = {
            term: math.log((total + 1) / (frequency + 1)) + 1
            for term, frequency in document_frequency.items()
        }

    @staticmethod
    def _project_scope(path: str) -> str:
        parents = Path(path).parent.parts
        if not parents:
            return ""
        return Path(*parents[: min(2, len(parents))]).as_posix().casefold()

    def tag_vocabulary_for(self, note: dict[str, Any]) -> list[str]:
        """Return human tags observed in the source's project or named by its content."""

        source_path = str(note.get("path", ""))
        source_scope = self._project_scope(source_path)
        classification_terms = set(
            search_terms(
                " ".join(
                    [
                        source_path,
                        str(note.get("title", "")),
                        *(str(value) for value in note.get("headings", [])),
                    ]
                ),
                maximum=500,
            )
        )
        content_text = re.sub(
            r"[^a-z0-9]+",
            " ",
            strip_maintenance_block(
                str(note.get("learning_content", note.get("content", "")))
            ).casefold(),
        ).strip()
        allowed: list[str] = []
        for tag in self.tag_counts:
            tag_terms = re.findall(r"[a-z0-9]+", tag.casefold())
            same_scope = bool(source_scope) and any(
                self._project_scope(path) == source_scope for path in self.tag_paths.get(tag, [])
            )
            named_by_classification = bool(tag_terms) and set(tag_terms) <= classification_terms
            tag_phrase = " ".join(tag_terms)
            named_project = len(tag_terms) >= 2 and bool(
                re.search(rf"(?:^| ){re.escape(tag_phrase)}(?: |$)", content_text)
            )
            dated_memory_log = tag.casefold() == "memory-log" and bool(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", Path(source_path).stem)
            )
            if same_scope or named_by_classification or named_project or dated_memory_log:
                allowed.append(tag)
        return allowed

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

    def knowledge_graph_for(
        self,
        note: dict[str, Any],
        *,
        source_note: dict[str, Any] | None = None,
        anchor_options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return a compact graph projection used by Maintenance edge validation."""

        nodes = knowledge_graph_nodes(
            note,
            entity_document_frequency=self.entity_document_frequency,
            note_count=self.note_count,
        )
        document = next((item for item in nodes if item["type"] == "document"), {})
        edges: list[dict[str, Any]] = []
        parent = Path(str(note.get("path", ""))).parent.as_posix()
        if document and parent not in {"", "."}:
            edges.append(
                {
                    "source": document["id"],
                    "predicate": "belongs_to_category",
                    "target": f"category:{parent.casefold()}",
                }
            )
        for entity in nodes:
            if entity.get("type") != "document" and document:
                edges.append(
                    {
                        "source": document["id"],
                        "predicate": "mentions_entity",
                        "target": entity["id"],
                    }
                )
        for target in note.get("links", [])[:20]:
            clean_target = _target_key(str(target))
            if clean_target and document:
                edges.append(
                    {
                        "source": document["id"],
                        "predicate": "links_to_document",
                        "target": f"document:{clean_target}",
                    }
                )
        signals: dict[str, Any] = {}
        if source_note is not None:
            source_nodes = knowledge_graph_nodes(
                source_note,
                entity_document_frequency=self.entity_document_frequency,
                note_count=self.note_count,
            )
            source_ids = {
                str(item["id"]) for item in source_nodes if item.get("type") != "document"
            }
            target_ids = {str(item["id"]) for item in nodes if item.get("type") != "document"}
            signals = {
                "source_names_target": bool(anchor_options),
                "source_links_target": note_links_to(source_note, note),
                "target_links_source": note_links_to(note, source_note),
                "shared_entity_ids": sorted(source_ids & target_ids)[:12],
            }
        return {
            "entity_nodes": nodes[:40],
            "structural_edges": edges[:60],
            "signals": signals,
            "policy": "typed edge with exact evidence and graph-specific anchor required",
        }

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
        unique_entity_types = Counter(
            entity_id.split(":", 1)[0] for entity_id in self.entity_document_frequency
        )
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
            "knowledge_graph": {
                "node_counts": {
                    "document": self.note_count,
                    **dict(unique_entity_types.most_common(30)),
                },
                "edge_counts": {
                    "belongs_to_category": sum(
                        1
                        for note in self.notes
                        if Path(str(note.get("path", ""))).parent.as_posix() not in {"", "."}
                    ),
                    "mentions_entity": sum(
                        sum(
                            _stable_entity_parts(str(entity)) is not None
                            for entity in note.get("entities", [])
                        )
                        for note in self.notes
                    ),
                    "links_to_document": sum(len(note.get("links", [])) for note in self.notes),
                },
                "entity_frequency": [
                    {
                        "id": entity_id,
                        "notes": count,
                        "specificity": round(1.0 - count / total, 4),
                    }
                    for entity_id, count in self.entity_document_frequency.most_common(120)
                ],
                "specificity_method": "inverse document frequency",
            },
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
            if _target_key(path) in self.noncanonical_paths:
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
            clean_content = strip_maintenance_block(
                str(note.get("learning_content", note.get("content", "")))
            )
            enriched = dict(note)
            enriched.update(
                {
                    "retrieval_score": round(score, 6),
                    "score": round(score, 6),
                    "reasons": reasons,
                    "link_target": link_target(note),
                    "content_excerpt": _excerpt(clean_content, set(query_terms), maximum=4000),
                    "structural_role": "category-hub" if self._hub_score(note) >= 2 else "note",
                    "already_linked": bool(source_note and note_links_to(source_note, note)),
                }
            )
            scored.append((score, str(note.get("title", "")).casefold(), enriched))
        scored.sort(key=lambda item: (-item[0], item[1], str(item[2].get("path", ""))))
        result = [note for _score, _title, note in scored[: max(0, maximum)]]
        protected_ranges = _protected_markdown_ranges(strip_maintenance_block(text))
        if source_note is not None:
            source_note["knowledge_graph"] = self.knowledge_graph_for(source_note)
        for note in result:
            note["anchor_options"] = inline_anchor_options(
                text,
                note,
                document_frequency=self.document_frequency,
                note_count=self.note_count,
                protected_ranges=protected_ranges,
            )
            note["knowledge_graph"] = self.knowledge_graph_for(
                note,
                source_note=source_note,
                anchor_options=note["anchor_options"],
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
