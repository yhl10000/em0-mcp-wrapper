"""Tests for admin graph payload shaping helpers."""

import importlib.util
import pathlib
import sys

_GRAPH_PAYLOAD_PATH = pathlib.Path(__file__).resolve().parents[1] / "server" / "graph_payload.py"
_GRAPH_PAYLOAD_SPEC = importlib.util.spec_from_file_location(
    "em0_server_graph_payload",
    _GRAPH_PAYLOAD_PATH,
)
assert _GRAPH_PAYLOAD_SPEC is not None
assert _GRAPH_PAYLOAD_SPEC.loader is not None
server_graph_payload = importlib.util.module_from_spec(_GRAPH_PAYLOAD_SPEC)
sys.modules[_GRAPH_PAYLOAD_SPEC.name] = server_graph_payload
_GRAPH_PAYLOAD_SPEC.loader.exec_module(server_graph_payload)


def test_graph_node_payload_strips_embedding_and_chooses_name():
    node = server_graph_payload.graph_node_payload(
        123,
        ["Entity"],
        {"name": "PostgreSQL", "embedding": [0.1, 0.2], "user_id": "proj"},
    )

    assert node == {
        "id": "123",
        "label": "PostgreSQL",
        "group": "Entity",
        "properties": {"name": "PostgreSQL", "user_id": "proj"},
    }


def test_graph_node_payload_falls_back_to_element_id():
    node = server_graph_payload.graph_node_payload("neo4j-id", [], {})

    assert node["id"] == "neo4j-id"
    assert node["label"] == "neo4j-id"
    assert node["group"] == "Node"


def test_graph_edge_payload_strips_embedding():
    edge = server_graph_payload.graph_edge_payload(
        "a",
        "b",
        "DEPENDS_ON",
        {"weight": 2, "embedding": [0.1]},
    )

    assert edge == {
        "from": "a",
        "to": "b",
        "label": "DEPENDS_ON",
        "properties": {"weight": 2},
    }


def test_merge_graph_payload_dedupes_nodes_and_edges():
    merged = server_graph_payload.merge_graph_payload(
        {
            "nodes": [{"id": "a", "label": "Old", "group": "Node", "properties": {}}],
            "edges": [{"from": "a", "to": "b", "label": "KNOWS", "properties": {}}],
        },
        {
            "nodes": [{"id": "a", "label": "New", "group": "Entity", "properties": {}}],
            "edges": [{"from": "a", "to": "b", "label": "KNOWS", "properties": {"rank": 1}}],
        },
    )

    assert merged["nodes"] == [{"id": "a", "label": "New", "group": "Entity", "properties": {}}]
    assert merged["edges"] == [
        {"from": "a", "to": "b", "label": "KNOWS", "properties": {"rank": 1}}
    ]
