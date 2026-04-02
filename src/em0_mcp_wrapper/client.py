"""HTTP client for the mem0 REST API. All error handling lives here."""

import asyncio
import logging

import httpx

from . import config

logger = logging.getLogger("em0-mcp")

MAX_RETRIES = 2
RETRY_DELAY = 5  # seconds between retries


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.MEM0_API_KEY}",
        "Content-Type": "application/json",
    }


async def request(method: str, path: str, **kwargs) -> dict:
    """Send a request to mem0 API with retry on timeout (cold start tolerance)."""
    url = f"{config.MEM0_API_URL}{path}"
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as c:
                resp = await c.request(method, url, headers=_headers(), **kwargs)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            last_error = "timeout"
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Timeout on attempt %d/%d (cold start?), retrying in %ds: %s %s",
                    attempt, MAX_RETRIES, RETRY_DELAY, method, url,
                )
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error("Timeout after %d attempts: %s %s", MAX_RETRIES, method, url)
        except httpx.HTTPStatusError as e:
            logger.error("HTTP %d: %s %s", e.response.status_code, method, url)
            return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text}
        except httpx.ConnectError:
            last_error = "connect"
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Connection failed attempt %d/%d, retrying in %ds: %s",
                    attempt, MAX_RETRIES, RETRY_DELAY, url,
                )
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error("Cannot connect after %d attempts: %s", MAX_RETRIES, url)

    if last_error == "timeout":
        return {
            "error": "Request timed out after retries",
            "hint": "Server may be cold-starting (scale-to-zero). Wait ~60s and try again.",
            "url": url,
        }
    return {"error": "Cannot connect to mem0 server", "url": url}


# ─── Memory CRUD ───


async def add_memory(
    content: str,
    user_id: str,
    metadata: dict,
    immutable: bool = False,
    includes: str = "",
    excludes: str = "",
) -> dict:
    payload: dict = {
        "messages": [{"role": "user", "content": content}],
        "user_id": user_id,
        "metadata": {k: v for k, v in metadata.items() if v},
    }
    if immutable:
        payload["immutable"] = True
    if includes:
        payload["includes"] = includes
    if excludes:
        payload["excludes"] = excludes
    return await request("POST", "/v1/memories/", json=payload)


async def get_memory(memory_id: str) -> dict:
    return await request("GET", f"/v1/memories/{memory_id}/")


async def update_memory(memory_id: str, content: str) -> dict:
    payload = {"data": content}
    return await request("PUT", f"/v1/memories/{memory_id}/", json=payload)


async def search_memory(
    query: str,
    user_id: str,
    limit: int = 5,
    filters: dict | None = None,
) -> dict:
    payload: dict = {"query": query, "user_id": user_id, "limit": limit}
    if filters:
        payload["filters"] = filters
    return await request("POST", "/v1/memories/search/", json=payload)


async def list_memories(user_id: str) -> dict:
    return await request("GET", "/v1/memories/", params={"user_id": user_id})


async def delete_memory(memory_id: str) -> dict:
    return await request("DELETE", f"/v1/memories/{memory_id}/")


async def memory_history(memory_id: str) -> dict:
    return await request("GET", f"/v1/memories/{memory_id}/history/")


async def get_stats() -> dict:
    return await request("GET", "/stats")


# ─── Graph Memory ───


async def get_entities(user_id: str) -> dict:
    """Get all entities (nodes) from the knowledge graph."""
    return await request("GET", "/v1/entities/", params={"user_id": user_id})


async def get_relations(user_id: str) -> dict:
    """Get all relationships between entities in the knowledge graph."""
    return await request("GET", "/v1/relations/", params={"user_id": user_id})


async def search_graph(query: str, user_id: str, limit: int = 5) -> dict:
    """Search using the knowledge graph (entity-relationship traversal)."""
    payload = {
        "query": query,
        "user_id": user_id,
        "limit": limit,
        "api_version": "v2",
    }
    return await request("POST", "/v1/memories/search/", json=payload)


async def delete_entity(user_id: str, entity_name: str) -> dict:
    """Delete a specific entity and its relations from the knowledge graph."""
    return await request(
        "DELETE", f"/v1/entities/{entity_name}/",
        params={"user_id": user_id},
    )


# ─── Resources ───


async def get_context(project_id: str) -> dict:
    """Get auto-context for a project (session start)."""
    return await request("GET", f"/v1/context/{project_id}")


async def get_project_summary(project_id: str) -> dict:
    """Get project memory summary."""
    return await request("GET", f"/v1/resources/summary/{project_id}")


async def get_graph_summary(project_id: str) -> dict:
    """Get knowledge graph summary."""
    return await request("GET", f"/v1/resources/graph-summary/{project_id}")


async def search_all_projects(query: str, limit: int = 5) -> dict:
    """Search across ALL projects — no user_id needed."""
    return await request(
        "POST", "/v1/memories/search-all/",
        json={"query": query, "limit": limit},
    )


async def search_cross_project(
    query: str, user_id: str, limit: int = 10,
) -> dict:
    """Search for cross-project entity connections."""
    return await request(
        "POST",
        "/v1/search/cross-project",
        json={"query": query, "user_id": user_id, "limit": limit},
    )


async def compact_memories(
    user_id: str,
    dry_run: bool = True,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.85,
) -> dict:
    """Compact similar memories within domain+type groups."""
    return await request(
        "POST",
        "/admin/compact",
        json={
            "user_id": user_id,
            "dry_run": dry_run,
            "min_cluster_size": min_cluster_size,
            "similarity_threshold": similarity_threshold,
        },
    )
