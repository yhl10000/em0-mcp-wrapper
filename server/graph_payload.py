"""Helpers for shaping Neo4j rows into graph explorer payloads."""

from __future__ import annotations

from typing import Any


def clean_graph_props(props: dict[str, Any] | None) -> dict[str, Any]:
    """Return graph properties without bulky/internal fields."""
    cleaned = dict(props or {})
    cleaned.pop("embedding", None)
    return cleaned


def graph_node_payload(
    element_id: Any,
    labels: list[str] | tuple[str, ...] | None,
    props: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a stable node payload for the admin graph UI."""
    cleaned = clean_graph_props(props)
    group = str(labels[0]) if labels else "Node"
    label = (
        cleaned.get("name")
        or cleaned.get("id")
        or cleaned.get("uuid")
        or cleaned.get("label")
        or element_id
    )
    return {
        "id": str(element_id),
        "label": str(label),
        "group": group,
        "properties": cleaned,
    }


def graph_edge_payload(
    source: Any,
    target: Any,
    rel_type: Any,
    props: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a stable relationship payload for the admin graph UI."""
    return {
        "from": str(source),
        "to": str(target),
        "label": str(rel_type),
        "properties": clean_graph_props(props),
    }


def merge_graph_payload(
    base: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Merge graph payloads by node id and edge identity."""
    nodes_by_id = {node["id"]: node for node in base.get("nodes", [])}
    for node in incoming.get("nodes", []):
        nodes_by_id[node["id"]] = node

    edges_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in [*base.get("edges", []), *incoming.get("edges", [])]:
        key = (edge.get("from", ""), edge.get("to", ""), edge.get("label", ""))
        edges_by_key[key] = edge

    return {
        "nodes": list(nodes_by_id.values()),
        "edges": list(edges_by_key.values()),
    }
