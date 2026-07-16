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
    if not content.startswith("---\n"):
        return {}
    try:
        raw, _body = content[4:].split("\n---", 1)
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
    tags = note_tags(knowledge)
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
        self.term_counts: list[Counter[str]] = []
        self.postings: dict[str, list[int]] = {}
        document_frequency: Counter[str] = Counter()
        for index, note in enumerate(notes):
            terms = Counter(search_terms(_note_search_text(note)))
            self.term_counts.append(terms)
            for term in terms:
                self.postings.setdefault(term, []).append(index)
                document_frequency[term] += 1
        total = max(1, len(notes))
        self.idf = {
            term: math.log((total + 1) / (frequency + 1)) + 1
            for term, frequency in document_frequency.items()
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
                }
            )
            scored.append((score, str(note.get("title", "")).casefold(), enriched))
        scored.sort(key=lambda item: (-item[0], item[1], str(item[2].get("path", ""))))
        return [note for _score, _title, note in scored[: max(0, maximum)]]


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
