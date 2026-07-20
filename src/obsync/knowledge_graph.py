from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_GENERIC_GRAPH_PREDICATES = frozenset(
    {
        "associated_with",
        "category_hub",
        "entity",
        "links_to",
        "related_to",
        "reference",
        "same_as",
        "similar_to",
        "specific_record",
    }
)
_GRAPH_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

GRAPH_ENTITY_TYPES = frozenset(
    {
        "account",
        "category",
        "decision",
        "document",
        "event",
        "identifier",
        "organization",
        "person",
        "place",
        "process",
        "product",
        "project",
        "requirement",
        "system",
        "tag",
        "tool",
    }
)

BASE_GRAPH_PREDICATES = frozenset(
    {
        "belongs_to_category",
        "cataloged_by",
        "catalogs_document",
        "created_by",
        "defines_offer",
        "defines_positioning_for",
        "depends_on",
        "documents_decision",
        "documents_entity",
        "has_requirement",
        "has_tag",
        "implements_decision",
        "implements_strategy",
        "markets_product",
        "mentions_entity",
        "operationalizes_strategy",
        "owns_account",
        "references_named_document",
        "supersedes_decision",
        "uses_system",
    }
)

_ANCHOR_BAD_BOUNDARIES = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "around",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "define",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "less",
        "may",
        "might",
        "more",
        "must",
        "of",
        "on",
        "or",
        "should",
        "than",
        "that",
        "the",
        "then",
        "this",
        "to",
        "want",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
    }
)

_DATE_IN_PATH_RE = re.compile(r"(?<!\d)(20\d{2})[-_/](0[1-9]|1[0-2])[-_/]([0-2]\d|3[01])(?!\d)")


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _stable_id(*parts: str, prefix: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def canonical_entity_id(entity_type: str, name: str, *, scope: str = "") -> str:
    """Return a conservative canonical id without fuzzy-merging unrelated names."""

    clean_type = re.sub(r"[^a-z0-9_]+", "_", str(entity_type).casefold()).strip("_")
    clean_name = _normalized_name(name)
    if clean_type not in GRAPH_ENTITY_TYPES or not clean_name:
        return ""
    if clean_type == "document":
        clean_path = str(scope or name).replace("\\", "/").strip().removesuffix(".md").casefold()
        return f"document:{clean_path}" if clean_path else ""
    # Decisions, requirements, events, and processes frequently reuse generic names. Scope those
    # identities to their folder unless the vault supplies an explicit alias shared elsewhere.
    scoped = clean_type in {"decision", "event", "process", "requirement"}
    if clean_type == "person" and len(clean_name.split()) < 2:
        scoped = True
    if clean_type == "account" and not re.search(r"\d|[-_:/#]", name):
        scoped = True
    identity = f"{_normalized_name(scope)}\0{clean_name}" if scoped and scope else clean_name
    return _stable_id(clean_type, identity, prefix="entity")


def graph_chunk_id(path: str, ordinal: int, text: str) -> str:
    return _stable_id(path.casefold(), str(ordinal), text, prefix="chunk")


def markdown_graph_chunks(
    path: str, content: str, *, content_hash: str = ""
) -> list[dict[str, Any]]:
    """Split Markdown into heading-aware paragraphs with exact source offsets."""

    chunks: list[dict[str, Any]] = []
    heading = ""
    paragraph_start: int | None = None
    paragraph_lines: list[str] = []
    offset = 0
    fenced = False
    maintenance_block = False

    def flush(end_offset: int) -> None:
        nonlocal paragraph_start, paragraph_lines
        if paragraph_start is None:
            return
        raw = "".join(paragraph_lines)
        text = raw.strip()
        if text and not text.startswith("<!-- obsync:maintenance:"):
            ordinal = len(chunks)
            chunks.append(
                {
                    "id": graph_chunk_id(path, ordinal, text),
                    "path": path,
                    "ordinal": ordinal,
                    "heading": heading[:300],
                    "start_offset": paragraph_start,
                    "end_offset": end_offset,
                    "text": text[:20_000],
                    "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "content_hash": content_hash,
                }
            )
        paragraph_start = None
        paragraph_lines = []

    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if "<!-- obsync:maintenance:start -->" in line:
            flush(offset)
            maintenance_block = "<!-- obsync:maintenance:end -->" not in line
            offset += len(line)
            continue
        if maintenance_block:
            if "<!-- obsync:maintenance:end -->" in line:
                maintenance_block = False
            offset += len(line)
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush(offset)
            fenced = not fenced
            offset += len(line)
            continue
        if fenced:
            offset += len(line)
            continue
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading_match:
            flush(offset)
            heading = heading_match.group(1).strip(" #")[:300]
            offset += len(line)
            continue
        if not stripped or stripped == "---":
            flush(offset)
            offset += len(line)
            continue
        if paragraph_start is None:
            paragraph_start = offset
        paragraph_lines.append(line)
        offset += len(line)
    flush(len(content))
    return chunks


def temporal_observation(path: str, properties: dict[str, Any] | None = None) -> str:
    values = properties or {}
    for key in ("date", "created", "created_at", "updated", "updated_at"):
        raw = str(values.get(key, "")).strip()
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}(?:[T ][^\s]+)?", raw):
            return raw[:32]
    match = _DATE_IN_PATH_RE.search(path)
    if match:
        return "-".join(match.groups())
    # Extraction time belongs in graph-state metadata. A missing date must not become an
    # invented factual valid-from time.
    return ""


def anchor_is_complete_entity_phrase(anchor: str) -> bool:
    """Reject fragments and action clauses while allowing complete named entities."""

    clean = re.sub(r"\s+", " ", str(anchor)).strip(" \t\r\n.,:;!?()[]{}\"'`")
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&'’./_-]*", clean)
    if not (1 <= len(words) <= 16) or len(clean) < 4:
        return False
    first = words[0].casefold()
    last = words[-1].casefold()
    if first in _ANCHOR_BAD_BOUNDARIES or last in _ANCHOR_BAD_BOUNDARIES:
        return False
    if re.search(
        r"^(?:add|create|define|draft|expose|keep|perform|remove|review|run|update|use)\b",
        clean,
        re.I,
    ):
        return False
    if re.search(r"\b(?:can|could|may|might|must|should|will|would)\b", clean, re.I):
        return False
    if re.search(r"\b(?:goal|objective|purpose)\s+is\s+to\b", clean, re.I):
        return False
    if re.search(r"\b(?:who|which|that)\s+(?:want|need|is|are|was|were)\b", clean, re.I):
        return False
    if re.search(r"\b(?:more|less|rather)\s*$", clean, re.I):
        return False
    return clean.count("(") == clean.count(")") and clean.count("[") == clean.count("]")


def normalize_graph_claims(
    note: dict[str, Any],
    raw: dict[str, Any],
    *,
    allowed_predicates: set[str] | frozenset[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Ground semantic entities and claims in exact note text and provenance."""

    content = str(note.get("learning_content", note.get("content", "")))
    path = str(note.get("path", ""))
    source_hash = str(note.get("content_hash", ""))
    scope = Path(path).parent.as_posix()
    chunks = markdown_graph_chunks(path, content, content_hash=source_hash)
    predicates = set(BASE_GRAPH_PREDICATES)
    predicates.update(str(item).strip() for item in (allowed_predicates or set()))
    entities: dict[str, dict[str, Any]] = {}
    mentions: list[dict[str, Any]] = []

    def add_entity(name: str, entity_type: str, aliases: list[str] | None = None) -> str:
        clean_name = re.sub(r"\s+", " ", name).strip()[:240]
        clean_type = re.sub(r"[^a-z0-9_]+", "_", entity_type.casefold()).strip("_")
        entity_id = canonical_entity_id(clean_type, clean_name, scope=scope)
        if not entity_id:
            return ""
        current = entities.setdefault(
            entity_id,
            {
                "id": entity_id,
                "name": clean_name,
                "type": clean_type,
                "aliases": [],
                "descriptions": [],
            },
        )
        for alias in aliases or []:
            clean_alias = re.sub(r"\s+", " ", str(alias)).strip()[:200]
            if (
                clean_alias
                and len(clean_alias) >= 2
                and clean_alias.casefold() != clean_name.casefold()
                and clean_alias.casefold()
                not in {
                    "he",
                    "her",
                    "him",
                    "it",
                    "project",
                    "she",
                    "system",
                    "that",
                    "they",
                    "this",
                }
                and re.search(rf"(?<!\w){re.escape(clean_alias)}(?!\w)", content, re.I)
                and clean_alias not in current["aliases"]
            ):
                current["aliases"].append(clean_alias)
        return entity_id

    document_id = canonical_entity_id(
        "document", str(note.get("title", Path(path).stem)), scope=path
    )
    if document_id:
        entities[document_id] = {
            "id": document_id,
            "name": str(note.get("title", Path(path).stem))[:240],
            "type": "document",
            "aliases": list(note.get("aliases", []))[:20],
            "descriptions": [],
        }

    raw_entities = raw.get("entities", []) if isinstance(raw, dict) else []
    if isinstance(raw_entities, list):
        for item in raw_entities[:80]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            entity_id = add_entity(
                name,
                str(item.get("type", "")),
                item.get("aliases", []) if isinstance(item.get("aliases"), list) else [],
            )
            if not entity_id:
                continue
            description = re.sub(r"\s+", " ", str(item.get("description", ""))).strip()[:500]
            if description and description not in entities[entity_id]["descriptions"]:
                entities[entity_id]["descriptions"].append(description)
            for alias in [name, *entities[entity_id]["aliases"]]:
                for match in re.finditer(re.escape(alias), content, re.I):
                    mentions.append(
                        {
                            "entity_id": entity_id,
                            "path": path,
                            "start_offset": match.start(),
                            "end_offset": match.end(),
                            "quote": content[match.start() : match.end()],
                            "content_hash": source_hash,
                            "source_kind": "semantic",
                            "confidence": 1.0,
                        }
                    )
                    break

    claims: list[dict[str, Any]] = []
    raw_claims = raw.get("claims", []) if isinstance(raw, dict) else []
    if isinstance(raw_claims, list):
        for item in raw_claims[:100]:
            if not isinstance(item, dict):
                continue
            predicate = re.sub(
                r"[^a-z0-9_]+", "_", str(item.get("predicate", "")).casefold()
            ).strip("_")
            if (
                not _GRAPH_PREDICATE_RE.fullmatch(predicate)
                or predicate in _GENERIC_GRAPH_PREDICATES
                or predicate not in predicates
            ):
                continue
            source_name = str(item.get("source_entity", "")).strip()
            target_name = str(item.get("target_entity", "")).strip()
            source_type = str(item.get("source_type", "")).strip().casefold().replace("-", "_")
            target_type = str(item.get("target_type", "")).strip().casefold().replace("-", "_")
            source_id = (
                document_id if source_type == "document" else add_entity(source_name, source_type)
            )
            # External document identities require a known vault path. Those references are
            # extracted deterministically from the complete title/alias inventory instead.
            target_id = (
                document_id
                if target_type == "document"
                and _normalized_name(target_name)
                in {
                    _normalized_name(str(note.get("title", ""))),
                    _normalized_name(path),
                    _normalized_name(Path(path).stem),
                }
                else ""
                if target_type == "document"
                else add_entity(target_name, target_type)
            )
            evidence = re.sub(r"\s+", " ", str(item.get("evidence", ""))).strip()[:1200]
            if evidence.casefold().startswith("source:"):
                evidence = evidence.split(":", 1)[1].strip()
            evidence_start = content.casefold().find(evidence.casefold()) if evidence else -1
            if not source_id or not target_id or source_id == target_id or evidence_start < 0:
                continue
            evidence_key = evidence.casefold()
            target_names = [target_name, *entities[target_id].get("aliases", [])]
            if not any(
                value and re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", evidence_key)
                for value in target_names
            ):
                continue
            if source_type != "document":
                source_names = [source_name, *entities[source_id].get("aliases", [])]
                if not any(
                    value
                    and re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", evidence_key)
                    for value in source_names
                ):
                    continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.72:
                continue
            for entity_id, entity_name in ((source_id, source_name), (target_id, target_name)):
                match = re.search(re.escape(entity_name), content, re.I) if entity_name else None
                if match:
                    mentions.append(
                        {
                            "entity_id": entity_id,
                            "path": path,
                            "start_offset": match.start(),
                            "end_offset": match.end(),
                            "quote": content[match.start() : match.end()],
                            "content_hash": source_hash,
                            "source_kind": "semantic",
                            "confidence": confidence,
                        }
                    )
            evidence_end = evidence_start + len(evidence)
            chunk = next(
                (
                    value
                    for value in chunks
                    if value["start_offset"] <= evidence_start < value["end_offset"]
                ),
                None,
            )
            if not chunk:
                continue
            claim_id = _stable_id(
                path.casefold(), source_id, predicate, target_id, evidence.casefold(), prefix="edge"
            )
            claims.append(
                {
                    "id": claim_id,
                    "source_id": source_id,
                    "source_name": entities[source_id]["name"],
                    "source_type": entities[source_id]["type"],
                    "predicate": predicate,
                    "target_id": target_id,
                    "target_name": entities[target_id]["name"],
                    "target_type": entities[target_id]["type"],
                    "description": re.sub(r"\s+", " ", str(item.get("description", ""))).strip()[
                        :800
                    ],
                    "evidence": evidence,
                    "source_path": path,
                    "source_chunk_id": str((chunk or {}).get("id", "")),
                    "start_offset": evidence_start,
                    "end_offset": evidence_end,
                    "content_hash": source_hash,
                    "confidence": round(confidence, 4),
                    "valid_from": str(item.get("valid_from", ""))[:32]
                    or temporal_observation(path, note.get("properties", {})),
                    "valid_to": str(item.get("valid_to", ""))[:32],
                    "state": str(item.get("state", "active")).casefold()
                    if str(item.get("state", "active")).casefold()
                    in {"active", "superseded", "historical"}
                    else "active",
                }
            )

    unique_mentions: dict[tuple[str, int, int], dict[str, Any]] = {}
    for mention in mentions:
        unique_mentions[(mention["entity_id"], mention["start_offset"], mention["end_offset"])] = (
            mention
        )
    return {
        "chunks": chunks,
        "entities": list(entities.values()),
        "mentions": list(unique_mentions.values()),
        "claims": claims,
    }


def feedback_feature_keys(operation: dict[str, Any]) -> list[tuple[str, str]]:
    """Return stable, non-content feedback features for one reviewed operation."""

    kind = str(operation.get("kind", "")).strip().casefold()
    keys = [("kind", kind)] if kind else []
    predicate = str(operation.get("predicate", "")).strip().casefold()
    if predicate:
        keys.append(("predicate", predicate))
    if kind == "inline-link":
        anchor = str(operation.get("anchor", ""))
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&'’./_-]*", anchor)
        keys.append(("anchor_words", str(min(len(words), 12))))
        if not anchor_is_complete_entity_phrase(anchor):
            keys.append(("anchor_shape", "incomplete"))
    elif kind == "frontmatter-tag":
        tag = str(operation.get("tag", operation.get("key", ""))).casefold()
        if tag:
            keys.append(("tag", tag))
    return keys


def _endpoint_values(nodes: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for node in nodes:
        aliases = node.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        for value in [node.get("id", ""), node.get("name", ""), *aliases]:
            clean = str(value).strip().casefold()
            if clean:
                values.add(clean)
    return values


def graph_relationship_is_eligible(
    source_note: dict[str, Any],
    candidate: dict[str, Any],
    relationship: dict[str, Any],
) -> bool:
    """Apply one graph-edge gate to AI and deterministic relationship proposals.

    Older stored decisions do not contain graph context and continue through the v0.18 evidence
    validator. New Maintenance candidates always carry graph context and therefore must provide
    canonical endpoints, a typed predicate, and a specific anchor.
    """

    source_graph = source_note.get("knowledge_graph", {})
    target_graph = candidate.get("knowledge_graph", {})
    strict = bool(source_graph or target_graph)
    if not strict:
        return True
    if not isinstance(source_graph, dict) or not isinstance(target_graph, dict):
        return False
    source_nodes = [item for item in source_graph.get("entity_nodes", []) if isinstance(item, dict)]
    target_nodes = [item for item in target_graph.get("entity_nodes", []) if isinstance(item, dict)]
    target_documents = [item for item in target_nodes if item.get("type") == "document"]
    source_entity = str(relationship.get("source_entity", "")).strip().casefold()
    target_entity = str(relationship.get("target_entity", "")).strip().casefold()
    predicate = str(relationship.get("predicate", "")).strip().casefold().replace("-", "_")
    anchor = str(relationship.get("anchor", "")).strip().casefold()
    options = [
        option
        for option in candidate.get("anchor_options", [])
        if isinstance(option, dict) and str(option.get("text", "")).strip().casefold() == anchor
    ]
    requested_context = str(relationship.get("anchor_context", "")).strip()
    if requested_context:
        options = [
            option
            for option in options
            if str(option.get("context", "")).strip() == requested_context
        ]
    option = options[0] if len(options) == 1 else None
    target_document_id = str((target_documents[0] if target_documents else {}).get("id", ""))
    edge_id = str(relationship.get("graph_edge_id", "")).strip()
    supported_edges = [
        item for item in target_graph.get("supported_edges", []) if isinstance(item, dict)
    ]
    supported_edge = next(
        (
            item
            for item in supported_edges
            if str(item.get("id", "")) == edge_id
            and str(item.get("predicate", "")).casefold() == predicate
            and str(item.get("source_document_id", "")).casefold() in _endpoint_values(source_nodes)
            and str(item.get("target_document_id", "")).casefold() == target_document_id.casefold()
            and str(item.get("anchor", "")).casefold() == anchor
        ),
        None,
    )
    try:
        supported_confidence = float((supported_edge or {}).get("confidence", 0.0))
    except (TypeError, ValueError):
        supported_confidence = 0.0
    try:
        anchor_specificity = float((option or {}).get("graph_specificity", 0.0))
    except (TypeError, ValueError):
        anchor_specificity = 0.0
    graph_v2 = (
        int(source_graph.get("graph_version", 1) or 1) >= 2
        and int(target_graph.get("graph_version", 1) or 1) >= 2
    )
    endpoint_match = (
        bool(supported_edge)
        and source_entity
        in {
            str(supported_edge.get("source_entity", "")).casefold(),
            str(supported_edge.get("source_document_id", "")).casefold(),
        }
        and target_entity
        in {
            str(supported_edge.get("target_entity", "")).casefold(),
            str(supported_edge.get("target_document_id", "")).casefold(),
        }
        if graph_v2
        else source_entity in _endpoint_values(source_nodes)
        and target_entity in _endpoint_values(target_documents)
    )
    return bool(
        endpoint_match
        and _GRAPH_PREDICATE_RE.fullmatch(predicate)
        and predicate not in _GENERIC_GRAPH_PREDICATES
        and option
        and anchor_is_complete_entity_phrase(anchor)
        and anchor_specificity >= 0.7
        and (not graph_v2 or supported_confidence >= 0.72)
        and str(option.get("canonical_entity_id", "")).casefold() == target_document_id.casefold()
        and (not graph_v2 or bool(supported_edge))
    )
