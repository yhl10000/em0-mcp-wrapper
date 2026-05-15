"""HTTP client for the mem0 REST API. All error handling lives here."""

import asyncio
import logging

import httpx

from . import config

logger = logging.getLogger("em0-mcp")

MAX_RETRIES = 2
RETRY_DELAY = 5  # seconds between retries


def _headers() -> dict:
    # Self-hosted Mem0 OSS server uses X-API-Key, not Bearer.
    return {
        "X-API-Key": config.MEM0_API_KEY,
        "Content-Type": "application/json",
    }


def _not_supported(feature: str) -> dict:
    return {
        "error": "not_supported",
        "feature": feature,
        "hint": (
            "This endpoint is not exposed by the self-hosted Mem0 OSS server. "
            "Available tools: add_memory, search_memory, list_memories, get_memory, "
            "update_memory, delete_memory."
        ),
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
        "infer": config.INFER_MEMORIES,
    }
    if immutable:
        payload["immutable"] = True
    if includes:
        payload["includes"] = includes
    if excludes:
        payload["excludes"] = excludes
    result = await request("POST", "/memories", json=payload)
    if (
        config.INFER_MEMORIES
        and isinstance(result, dict)
        and result.get("results") == []
    ):
        fallback_payload = dict(payload)
        fallback_payload["infer"] = False
        fallback_result = await request("POST", "/memories", json=fallback_payload)
        if isinstance(fallback_result, dict):
            fallback_result["warning"] = (
                "infer=true returned no extracted memories; retried with infer=false "
                "to preserve the original content. Troubleshoot the Mem0 LLM extraction "
                "prompt/model before removing this fallback."
            )
            fallback_result["infer_fallback"] = True
        return fallback_result
    return result


async def get_memory(memory_id: str) -> dict:
    return await request("GET", f"/memories/{memory_id}")


async def update_memory(memory_id: str, content: str) -> dict:
    # Self-hosted mem0 OSS server expects {"text": "..."} (not "data") on PUT /memories/{id}.
    # Using the wrong field name returns HTTP 422: "body.text Field required".
    payload = {"text": content}
    return await request("PUT", f"/memories/{memory_id}", json=payload)


async def search_memory(
    query: str,
    user_id: str,
    limit: int = 5,
    filters: dict | None = None,
) -> dict:
    # Self-hosted server requires user_id inside filters, not at top level.
    merged_filters: dict = dict(filters) if filters else {}
    if user_id:
        merged_filters["user_id"] = user_id
    payload: dict = {"query": query, "limit": limit, "filters": merged_filters}
    return await request("POST", "/search", json=payload)


async def list_memories(user_id: str) -> dict:
    return await request("GET", "/memories", params={"user_id": user_id})


async def delete_memory(memory_id: str) -> dict:
    return await request("DELETE", f"/memories/{memory_id}")


async def memory_history(memory_id: str) -> dict:
    return await request("GET", f"/memories/{memory_id}/history")


async def get_stats() -> dict:
    return _not_supported("memory_stats (/stats)")


# ─── Graph Memory ───


async def get_entities(user_id: str) -> dict:
    return _not_supported("get_entities")


async def get_relations(user_id: str) -> dict:
    return _not_supported("get_relations")


async def search_graph(query: str, user_id: str, limit: int = 5) -> dict:
    return _not_supported("search_graph")


async def delete_entity(user_id: str, entity_name: str) -> dict:
    return _not_supported("delete_entity")


# ─── Resources ───


async def get_context(project_id: str) -> dict:
    return _not_supported("memory://context")


async def get_project_summary(project_id: str) -> dict:
    return _not_supported("memory://project/summary")


async def get_graph_summary(project_id: str) -> dict:
    return _not_supported("memory://project/graph")


async def audit_graph(user_id: str = "", duplicate_limit: int = 25) -> dict:
    return _not_supported("audit_graph")


async def search_all_projects(query: str, limit: int = 5) -> dict:
    return _not_supported("search_all_projects")


async def search_cross_project(
    query: str, user_id: str, limit: int = 10,
) -> dict:
    return _not_supported("search_cross_project")


async def compact_memories(
    user_id: str,
    dry_run: bool = True,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.85,
) -> dict:
    return _not_supported("compact_memories")
