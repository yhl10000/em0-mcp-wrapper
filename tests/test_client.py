"""Tests for the mem0 API client (mocked HTTP)."""

import json

import httpx
import pytest
import respx

from em0_mcp_wrapper import client, config

# Set config for tests
config.MEM0_API_URL = "https://test-mem0.example.com"
config.MEM0_API_KEY = "test-key"
config.REQUEST_TIMEOUT = 5

# Speed up retry tests
client.MAX_RETRIES = 2
client.RETRY_DELAY = 0


@respx.mock
@pytest.mark.asyncio
async def test_add_memory():
    respx.post("https://test-mem0.example.com/v1/memories/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "abc", "event": "ADD"}]})
    )
    result = await client.add_memory("test content", "user1", {"domain": "auth"})
    assert "results" in result
    assert result["results"][0]["id"] == "abc"


@respx.mock
@pytest.mark.asyncio
async def test_add_memory_immutable():
    route = respx.post("https://test-mem0.example.com/v1/memories/")
    route.mock(
        return_value=httpx.Response(200, json={"results": [{"id": "imm1", "event": "ADD"}]})
    )
    result = await client.add_memory(
        "critical decision", "user1", {"domain": "arch"}, immutable=True
    )
    assert "results" in result
    body = json.loads(route.calls[0].request.content)
    assert body["immutable"] is True


@respx.mock
@pytest.mark.asyncio
async def test_search_memory():
    respx.post("https://test-mem0.example.com/v1/memories/search/").mock(
        return_value=httpx.Response(
            200, json={"results": [{"memory": "found it", "score": 0.9}]}
        )
    )
    result = await client.search_memory("test query", "user1", limit=5)
    assert "results" in result
    assert result["results"][0]["score"] == 0.9


@respx.mock
@pytest.mark.asyncio
async def test_search_memory_with_filters():
    route = respx.post("https://test-mem0.example.com/v1/memories/search/")
    route.mock(return_value=httpx.Response(200, json={"results": []}))
    filters = {"metadata.domain": "auth"}
    await client.search_memory("test", "user1", filters=filters)
    body = json.loads(route.calls[0].request.content)
    assert body["filters"] == filters


@respx.mock
@pytest.mark.asyncio
async def test_list_memories():
    respx.get("https://test-mem0.example.com/v1/memories/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    result = await client.list_memories("user1")
    assert result == {"results": []}


@respx.mock
@pytest.mark.asyncio
async def test_get_memory():
    respx.get("https://test-mem0.example.com/v1/memories/abc123/").mock(
        return_value=httpx.Response(200, json={"id": "abc123", "memory": "test data"})
    )
    result = await client.get_memory("abc123")
    assert result["id"] == "abc123"


@respx.mock
@pytest.mark.asyncio
async def test_update_memory():
    route = respx.put("https://test-mem0.example.com/memories/abc123").mock(
        return_value=httpx.Response(200, json={"id": "abc123", "event": "UPDATE"})
    )
    result = await client.update_memory("abc123", "updated content")
    assert result["event"] == "UPDATE"
    # Regression guard: self-hosted mem0 OSS expects "text", not "data".
    # Wrong field name returns HTTP 422 "body.text Field required".
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body == {"text": "updated content"}


@respx.mock
@pytest.mark.asyncio
async def test_delete_memory():
    respx.delete("https://test-mem0.example.com/v1/memories/abc123/").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )
    result = await client.delete_memory("abc123")
    assert result["status"] == "deleted"


@respx.mock
@pytest.mark.asyncio
async def test_memory_history():
    respx.get("https://test-mem0.example.com/v1/memories/abc123/history/").mock(
        return_value=httpx.Response(200, json=[
            {
                "old_memory": "v1",
                "new_memory": "v2",
                "event": "UPDATE",
                "created_at": "2026-03-01",
            }
        ])
    )
    result = await client.memory_history("abc123")
    assert isinstance(result, list)
    assert result[0]["event"] == "UPDATE"


@respx.mock
@pytest.mark.asyncio
async def test_get_entities():
    respx.get("https://test-mem0.example.com/v1/entities/").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"name": "PostgreSQL", "type": "database"},
                {"name": "Erkut", "type": "person"},
            ]
        })
    )
    result = await client.get_entities("user1")
    assert len(result["results"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_relations():
    respx.get("https://test-mem0.example.com/v1/relations/").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "source": "Erkut",
                    "relationship": "decided",
                    "target": "PostgreSQL",
                }
            ]
        })
    )
    result = await client.get_relations("user1")
    assert len(result["results"]) == 1
    assert result["results"][0]["relationship"] == "decided"


@respx.mock
@pytest.mark.asyncio
async def test_search_graph():
    route = respx.post("https://test-mem0.example.com/v1/memories/search/")
    route.mock(return_value=httpx.Response(200, json={
        "results": [{"memory": "chose PostgreSQL", "score": 0.9}],
        "relations": [
            {
                "source": "Erkut",
                "relationship": "decided",
                "target": "PostgreSQL",
                "score": 0.85,
            }
        ],
    }))
    result = await client.search_graph("database decisions", "user1")
    assert "relations" in result
    assert len(result["relations"]) == 1
    body = json.loads(route.calls[0].request.content)
    assert body["api_version"] == "v2"


@respx.mock
@pytest.mark.asyncio
async def test_delete_entity():
    respx.delete("https://test-mem0.example.com/v1/entities/PostgreSQL/").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )
    result = await client.delete_entity("user1", "PostgreSQL")
    assert result["status"] == "deleted"


@respx.mock
@pytest.mark.asyncio
async def test_timeout_retries_then_fails():
    respx.post("https://test-mem0.example.com/v1/memories/search/").mock(
        side_effect=httpx.TimeoutException("timeout")
    )
    result = await client.search_memory("test", "user1")
    assert "error" in result
    assert "timed out" in result["error"]
    assert "hint" in result


@respx.mock
@pytest.mark.asyncio
async def test_timeout_retry_succeeds():
    route = respx.post("https://test-mem0.example.com/v1/memories/search/")
    route.side_effect = [
        httpx.TimeoutException("cold start"),
        httpx.Response(200, json={"results": [{"memory": "found", "score": 0.8}]}),
    ]
    result = await client.search_memory("test", "user1")
    assert "results" in result
    assert result["results"][0]["score"] == 0.8


@respx.mock
@pytest.mark.asyncio
async def test_connect_error_retries():
    respx.post("https://test-mem0.example.com/v1/memories/search/").mock(
        side_effect=httpx.ConnectError("refused")
    )
    result = await client.search_memory("test", "user1")
    assert "error" in result
    assert "Cannot connect" in result["error"]


@respx.mock
@pytest.mark.asyncio
async def test_http_error():
    respx.post("https://test-mem0.example.com/v1/memories/search/").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await client.search_memory("test", "user1")
    assert "error" in result
    assert "401" in result["error"]


# ─── Resource Client Tests ───


@respx.mock
@pytest.mark.asyncio
async def test_get_context():
    respx.get("https://test-mem0.example.com/v1/context/myproject").mock(
        return_value=httpx.Response(200, json={
            "project": "myproject",
            "recent_decisions": [
                {"memory": "Use PostgreSQL", "domain": "backend", "type": "decision"}
            ],
            "immutable_lessons": [
                {"memory": "Always use 1024d embeddings", "domain": "infra"}
            ],
            "graph_relations": [],
            "stats": {"total_memories": 42, "immutable_count": 3, "graph_relations_count": 0},
        })
    )
    result = await client.get_context("myproject")
    assert result["project"] == "myproject"
    assert len(result["recent_decisions"]) == 1
    assert result["stats"]["total_memories"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_get_project_summary():
    respx.get("https://test-mem0.example.com/v1/resources/summary/myproject").mock(
        return_value=httpx.Response(200, json={
            "project": "myproject",
            "total_memories": 47,
            "domains": {"auth": 12, "backend": 8},
            "key_decisions": ["Use JWT"],
            "last_updated": "2026-04-01",
        })
    )
    result = await client.get_project_summary("myproject")
    assert result["total_memories"] == 47
    assert "auth" in result["domains"]


@respx.mock
@pytest.mark.asyncio
async def test_get_graph_summary():
    respx.get("https://test-mem0.example.com/v1/resources/graph-summary/myproject").mock(
        return_value=httpx.Response(200, json={
            "project": "myproject",
            "entity_types": {"person": 2, "service": 3},
            "entities": {"person": ["Erkut", "Kaan"], "service": ["AuthService"]},
            "relations": [{"source": "Erkut", "relation": "decided", "target": "PostgreSQL"}],
            "total_relations": 1,
        })
    )
    result = await client.get_graph_summary("myproject")
    assert result["entity_types"]["person"] == 2
    assert "Erkut" in result["entities"]["person"]


@respx.mock
@pytest.mark.asyncio
async def test_audit_graph():
    route = respx.get("https://test-mem0.example.com/admin/graph-audit")
    route.mock(return_value=httpx.Response(200, json={
        "graph_enabled": True,
        "dry_run": True,
        "user_id": "myproject",
        "summary": {
            "nodes": 10,
            "edges": 12,
            "isolated_nodes": 1,
            "self_loops": 0,
            "cross_project_edges": 2,
        },
        "duplicate_entities": [],
        "relation_types": [],
        "property_coverage": [],
        "recommendations": ["Inspect isolated_nodes."],
    }))
    result = await client.audit_graph("myproject", duplicate_limit=10)
    assert result["dry_run"] is True
    assert result["summary"]["isolated_nodes"] == 1
    assert route.calls[0].request.url.params["user_id"] == "myproject"
    assert route.calls[0].request.url.params["duplicate_limit"] == "10"


# ─── Compaction Client Tests ───


# ─── Cross-Project Search Tests ───


@respx.mock
@pytest.mark.asyncio
async def test_search_cross_project():
    respx.post("https://test-mem0.example.com/v1/search/cross-project").mock(
        return_value=httpx.Response(200, json={
            "current_project": "centauri",
            "entities_in_project": 15,
            "other_projects_checked": 2,
            "cross_relations": [
                {
                    "entity": "postgresql",
                    "relation": "uses",
                    "connected_to": "pgvector",
                    "other_project": "em0-mcp-wrapper",
                    "direction": "outgoing",
                },
                {
                    "entity": "erkut",
                    "relation": "decided",
                    "connected_to": "swiftui",
                    "other_project": "centauri-ios",
                    "direction": "outgoing",
                },
            ],
            "search_context": ["PostgreSQL v15 is the database"],
        })
    )
    result = await client.search_cross_project("PostgreSQL", "centauri")
    assert result["current_project"] == "centauri"
    assert len(result["cross_relations"]) == 2
    assert result["cross_relations"][0]["other_project"] == "em0-mcp-wrapper"


@respx.mock
@pytest.mark.asyncio
async def test_search_cross_project_no_results():
    respx.post("https://test-mem0.example.com/v1/search/cross-project").mock(
        return_value=httpx.Response(200, json={
            "current_project": "solo-project",
            "entities_in_project": 5,
            "other_projects_checked": 0,
            "cross_relations": [],
            "search_context": [],
        })
    )
    result = await client.search_cross_project("something", "solo-project")
    assert result["cross_relations"] == []
    assert result["other_projects_checked"] == 0


# ─── Compaction Client Tests ───


@respx.mock
@pytest.mark.asyncio
async def test_compact_memories_dry_run():
    respx.post("https://test-mem0.example.com/admin/compact").mock(
        return_value=httpx.Response(200, json={
            "dry_run": True,
            "plan": [
                {
                    "group": "backend:decision",
                    "memories_to_merge": 4,
                    "preview": [
                        "Use PostgreSQL",
                        "PostgreSQL v15",
                        "DB is PostgreSQL",
                        "Chose PostgreSQL",
                    ],
                }
            ],
            "total_groups_analyzed": 5,
            "total_merged": 0,
            "memories_saved": 0,
        })
    )
    result = await client.compact_memories("myproject", dry_run=True)
    assert result["dry_run"] is True
    assert len(result["plan"]) == 1
    assert result["plan"][0]["memories_to_merge"] == 4


@respx.mock
@pytest.mark.asyncio
async def test_compact_memories_apply():
    route = respx.post("https://test-mem0.example.com/admin/compact")
    route.mock(return_value=httpx.Response(200, json={
        "dry_run": False,
        "plan": [
            {
                "group": "backend:decision",
                "merged": 4,
                "into_summary": "PostgreSQL v15 is the database",
            }
        ],
        "total_groups_analyzed": 5,
        "total_merged": 4,
        "memories_saved": 3,
    }))
    result = await client.compact_memories("myproject", dry_run=False)
    assert result["dry_run"] is False
    assert result["total_merged"] == 4
    assert result["memories_saved"] == 3
    body = json.loads(route.calls[0].request.content)
    assert body["dry_run"] is False
