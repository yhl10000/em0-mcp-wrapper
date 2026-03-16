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


async def add_memory(content: str, user_id: str, metadata: dict) -> dict:
    payload = {
        "messages": [{"role": "user", "content": content}],
        "user_id": user_id,
        "metadata": {k: v for k, v in metadata.items() if v},
    }
    return await request("POST", "/v1/memories/", json=payload)


async def search_memory(query: str, user_id: str, limit: int = 5) -> dict:
    payload = {"query": query, "user_id": user_id, "limit": limit}
    return await request("POST", "/v1/memories/search/", json=payload)


async def list_memories(user_id: str) -> dict:
    return await request("GET", "/v1/memories/", params={"user_id": user_id})


async def delete_memory(memory_id: str) -> dict:
    return await request("DELETE", f"/v1/memories/{memory_id}/")


async def get_stats() -> dict:
    return await request("GET", "/stats")
