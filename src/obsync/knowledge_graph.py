from __future__ import annotations

import re
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
    try:
        anchor_specificity = float((option or {}).get("graph_specificity", 0.0))
    except (TypeError, ValueError):
        anchor_specificity = 0.0
    return bool(
        source_entity in _endpoint_values(source_nodes)
        and target_entity in _endpoint_values(target_documents)
        and _GRAPH_PREDICATE_RE.fullmatch(predicate)
        and predicate not in _GENERIC_GRAPH_PREDICATES
        and option
        and anchor_specificity >= 0.7
        and str(option.get("canonical_entity_id", "")).casefold() == target_document_id.casefold()
    )
