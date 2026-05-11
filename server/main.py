"""mem0 REST API Server — wraps mem0 Python library with FastAPI.

Endpoints:
  /health                        → health check
  /stats                         → cross-project statistics
  /v1/memories/                  → add (POST), list (GET), delete all (DELETE)
  /v1/memories/search/           → semantic search
  /v1/memories/{id}/             → get (GET), update (PUT), delete (DELETE)
  /v1/memories/{id}/history/     → edit history
  /v1/entities/                  → list graph entities (GET)
  /v1/relations/                 → list graph relations (GET)
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from graph_payload import graph_edge_payload, graph_node_payload
from mem0_compat import search_memory as _mem0_search
from pydantic import BaseModel
from scoring import apply_freshness as _apply_freshness

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mem0-server")

# ─── Config from env vars ───
MEM0_API_KEY = os.environ.get("MEM0_API_KEY", "")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "mem0")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "mem0admin")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY", "")

NEO4J_URI = os.environ.get("NEO4J_URI", "")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

# Webhook config
WEBHOOK_URLS = [u.strip() for u in os.environ.get("WEBHOOK_URLS", "").split(",") if u.strip()]
WEBHOOK_EVENTS = set(
    os.environ.get("WEBHOOK_EVENTS", "memory.created,memory.updated,memory.conflict").split(",")
)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _build_config() -> dict:
    """Build mem0 config dict from environment variables."""
    config: dict[str, Any] = {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": POSTGRES_HOST,
                "port": int(POSTGRES_PORT),
                "dbname": POSTGRES_DB,
                "user": POSTGRES_USER,
                "password": POSTGRES_PASSWORD,
                "collection_name": "mem0_v3",
            },
        },
        "embedder": {
            "provider": "azure_openai",
            "config": {
                "model": "text-embedding-3-small",
                "embedding_dims": 1024,
                "azure_kwargs": {
                    "api_key": AZURE_OPENAI_KEY,
                    "azure_endpoint": AZURE_OPENAI_ENDPOINT,
                    "azure_deployment": "text-embedding-3-small",
                    "api_version": "2024-02-01",
                    "dimensions": 1024,
                },
            },
        },
        "llm": {
            "provider": "azure_openai",
            "config": {
                "model": "gpt-4o-mini",
                "azure_kwargs": {
                    "api_key": AZURE_OPENAI_KEY,
                    "azure_endpoint": AZURE_OPENAI_ENDPOINT,
                    "azure_deployment": "gpt-4o-mini",
                    "api_version": "2024-02-01",
                },
            },
        },
        "history_db_path": "/data/mem0_history.db",
    }

    # Add graph store if Neo4j is configured
    if NEO4J_URI:
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": NEO4J_URI,
                "username": NEO4J_USERNAME,
                "password": NEO4J_PASSWORD,
            },
        }
        logger.info("Graph store enabled: Neo4j at %s", NEO4J_URI)
    else:
        logger.info("Graph store disabled (NEO4J_URI not set)")

    logger.info("Config built: vector=%s, embedder=%s, llm=%s, graph=%s",
                config["vector_store"]["provider"],
                config["embedder"]["provider"],
                config["llm"]["provider"],
                "neo4j" if NEO4J_URI else "none")

    return config


# ─── Memory instance (lazy init) ───
memory = None
graph_enabled = False


def _patch_all_embeddings(dims: int):
    """Monkey-patch openai Embeddings class to always pass dimensions."""
    from openai.resources import Embeddings

    if hasattr(Embeddings, "_original_create"):
        return  # Already patched

    Embeddings._original_create = Embeddings.create

    def patched_create(self, *args, **kwargs):
        kwargs["dimensions"] = dims
        return Embeddings._original_create(self, *args, **kwargs)

    Embeddings.create = patched_create
    logger.info("Globally patched all OpenAI Embeddings to dimensions=%d", dims)


def _get_memory():
    global memory, graph_enabled
    if memory is None:
        from mem0 import Memory
        # Patch BEFORE memory init so graph memory's embedder also gets 1024d
        _patch_all_embeddings(1024)
        config = _build_config()
        try:
            memory = Memory.from_config(config)
            graph_enabled = bool(NEO4J_URI)
            logger.info("Memory initialized (graph=%s)", graph_enabled)
        except Exception as e:
            # If graph fails, try without it
            if NEO4J_URI and "neo4j" in str(e).lower():
                logger.warning("Neo4j connection failed: %s — retrying without graph", e)
                config.pop("graph_store", None)
                memory = Memory.from_config(config)
                graph_enabled = False
                logger.info("Memory initialized WITHOUT graph (Neo4j unreachable)")
            else:
                raise
    return memory


# ─── Auth ───
def _check_auth(authorization: str):
    if not MEM0_API_KEY:
        return
    token = authorization.replace("Bearer ", "") if authorization else ""
    if token != MEM0_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _get_neo4j_driver():
    """Create a Neo4j driver for admin graph endpoints."""
    if not NEO4J_URI:
        raise HTTPException(
            status_code=501,
            detail="Graph memory not enabled (NEO4J_URI not configured)",
        )
    from neo4j import GraphDatabase

    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))


# ─── Request/Response models ───
class AddMemoryRequest(BaseModel):
    messages: list[dict]
    user_id: str = ""
    metadata: dict = {}
    immutable: bool = False
    includes: str = ""
    excludes: str = ""


class SearchRequest(BaseModel):
    query: str
    user_id: str = ""
    limit: int = 5
    filters: dict | None = None
    api_version: str = "v1"


class UpdateMemoryRequest(BaseModel):
    data: str


def _track_access(m, memory_ids: list[str]):
    """Update last_accessed_at and access_count for retrieved memories."""
    now_iso = datetime.now(timezone.utc).isoformat()
    for mid in memory_ids:
        try:
            mem = m.get(mid)
            if not isinstance(mem, dict):
                continue
            meta = mem.get("metadata", {})
            meta["last_accessed_at"] = now_iso
            meta["access_count"] = meta.get("access_count", 0) + 1
            m.update(mid, data=mem.get("memory", ""), metadata=meta)
        except Exception as e:
            logger.debug("access tracking skipped for %s: %s", mid, e)


# ─── Webhook Dispatcher ───

def _dispatch_webhook(event: str, payload: dict):
    """Fire-and-forget webhook notification. Non-blocking."""
    if event not in WEBHOOK_EVENTS or not WEBHOOK_URLS:
        return

    import hashlib
    import hmac
    import threading

    def _send():
        import json as _json

        raw_payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if WEBHOOK_SECRET:
            raw_body = _json.dumps(raw_payload)
            sig = hmac.new(
                WEBHOOK_SECRET.encode(), raw_body.encode(), hashlib.sha256,
            ).hexdigest()
            headers["X-Signature-256"] = f"sha256={sig}"

        for url in WEBHOOK_URLS:
            try:
                import httpx as _httpx

                # Slack needs {"text": "..."} format
                if "hooks.slack.com" in url:
                    emoji = {
                        "memory.created": ":brain:",
                        "memory.updated": ":pencil2:",
                        "memory.deleted": ":wastebasket:",
                        "memory.conflict": ":warning:",
                    }.get(event, ":pushpin:")
                    project = payload.get("user_id", "?")
                    domain = payload.get("domain", "")
                    mtype = payload.get("type", "")
                    content = payload.get("content", payload.get("new_content", "?"))
                    tag = f"[{domain}/{mtype}]" if domain else ""
                    body = _json.dumps({
                        "text": f"{emoji} *{event}* | {project} {tag}\n>{content[:300]}"
                    })
                else:
                    body = _json.dumps(raw_payload)

                with _httpx.Client(timeout=10) as c:
                    resp = c.post(url, content=body, headers=headers)
                    logger.info("Webhook %s → %s: %d", event, url[:50], resp.status_code)
            except Exception as e:
                logger.warning("Webhook failed %s → %s: %s", event, url[:50], e)

    threading.Thread(target=_send, daemon=True).start()


# ─── App ───
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("mem0 server starting...")
    yield
    logger.info("mem0 server shutting down")


app = FastAPI(title="mem0-server", version="4.0.0", lifespan=lifespan)


# ─── Health ───
@app.get("/health")
def health():
    m = _get_memory()
    return {
        "status": "ok",
        "version": "4.0.0",
        "vector_store": "pgvector",
        "embedder": "text-embedding-3-small",
        "graph_enabled": graph_enabled,
        "memory_initialized": m is not None,
    }


# ─── Stats ───
@app.get("/stats")
def stats(authorization: str = Header("")):
    _check_auth(authorization)
    try:
        m = _get_memory()

        # Step 1: Discover all user_ids from Neo4j (dynamic, no hardcoding)
        all_user_ids: set[str] = set()
        graph_stats = {}

        if NEO4J_URI:
            try:
                from neo4j import GraphDatabase
                driver = GraphDatabase.driver(
                    NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
                )
                with driver.session() as session:
                    # Get all distinct user_ids from graph
                    uid_result = session.run(
                        "MATCH (n) WHERE n.user_id IS NOT NULL "
                        "RETURN DISTINCT n.user_id AS uid"
                    ).data()
                    for row in uid_result:
                        if row.get("uid"):
                            all_user_ids.add(row["uid"])

                    node_count = session.run(
                        "MATCH (n) RETURN count(n) AS c"
                    ).single()["c"]
                    rel_count = session.run(
                        "MATCH ()-[r]->() RETURN count(r) AS c"
                    ).single()["c"]
                    graph_stats = {
                        "nodes": node_count,
                        "edges": rel_count,
                    }
                driver.close()
            except Exception as ge:
                logger.warning("Graph stats failed: %s", ge)

        # Also include known IDs as fallback
        all_user_ids.update([
            "centauri", "centauri-ios", "centauri-backend",
            "happybrain", "em0-mcp-wrapper", "happy-brain",
            "pallasite", "seklabs",
        ])

        # Step 2: Count memories per user_id via mem0
        projects: dict[str, int] = {}
        for uid in sorted(all_user_ids):
            try:
                result = m.get_all(user_id=uid)
                items = result.get("results", []) if isinstance(result, dict) else result
                count = len(items) if isinstance(items, list) else 0
                if count > 0:
                    projects[uid] = count
            except Exception:
                pass

        return {
            "version": "5.0.0",
            "total_projects": len(projects),
            "total_memories": sum(projects.values()),
            "projects": projects,
            "graph": graph_stats,
        }
    except Exception as e:
        logger.error("stats error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Conflict Detection ───

CONFLICT_THRESHOLD = float(os.environ.get("CONFLICT_THRESHOLD", "0.80"))


def _normalize_text(text: str) -> str:
    """Simple normalization for dedup comparison."""
    return " ".join(text.lower().split())


def _check_conflicts(
    m, content: str, user_id: str, threshold: float = CONFLICT_THRESHOLD,
) -> list[dict]:
    """Find existing memories that may conflict with new content."""
    try:
        results = _mem0_search(m, query=content, user_id=user_id, limit=3)
        items = results.get("results", []) if isinstance(results, dict) else results

        conflicts = []
        for item in items:
            score = item.get("score", 0)
            existing = item.get("memory", "")

            if score < threshold:
                continue

            # Same content = dedup, not conflict
            if _normalize_text(content) == _normalize_text(existing):
                continue

            conflict_entry = {
                "existing_memory": existing,
                "existing_id": item.get("id", "?"),
                "similarity_score": round(score, 3),
                "suggestion": "Consider updating this memory instead.",
            }

            # Extra warning if conflicting with immutable
            if item.get("metadata", {}).get("immutable"):
                conflict_entry["suggestion"] = (
                    "IMMUTABLE memory — cannot be updated. "
                    "Verify this new information is correct before adding."
                )

            conflicts.append(conflict_entry)

        return conflicts
    except Exception as e:
        logger.warning("Conflict check failed (non-blocking): %s", e)
        return []


# ─── Add Memory ───
@app.post("/v1/memories/")
def add_memory(req: AddMemoryRequest, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        content = req.messages[0]["content"] if req.messages else ""
        metadata = dict(req.metadata) if req.metadata else {}
        if req.immutable:
            metadata["immutable"] = True
        kwargs: dict[str, Any] = {
            "user_id": req.user_id,
            "metadata": metadata,
        }
        if req.includes:
            kwargs["includes"] = req.includes
        if req.excludes:
            kwargs["excludes"] = req.excludes

        # Conflict detection (non-blocking, runs before add)
        conflicts = _check_conflicts(m, content, req.user_id)

        result = m.add(content, **kwargs)

        # Attach conflicts to response
        if conflicts:
            if isinstance(result, dict):
                result["conflicts"] = conflicts
                result["conflict_warning"] = (
                    f"{len(conflicts)} potential conflict(s) found. "
                    "Review existing memories."
                )
            else:
                result = {
                    "results": result if isinstance(result, list) else [],
                    "conflicts": conflicts,
                    "conflict_warning": (
                        f"{len(conflicts)} potential conflict(s) found. "
                        "Review existing memories."
                    ),
                }

        # Webhook: memory.created
        _dispatch_webhook("memory.created", {
            "user_id": req.user_id,
            "content": content[:500],
            "domain": metadata.get("domain", ""),
            "type": metadata.get("type", ""),
            "immutable": req.immutable,
        })

        # Webhook: memory.conflict
        if conflicts:
            _dispatch_webhook("memory.conflict", {
                "user_id": req.user_id,
                "new_content": content[:300],
                "conflicts": [
                    {"existing": c["existing_memory"][:200], "score": c["similarity_score"]}
                    for c in conflicts
                ],
            })

        # Normalize response
        if isinstance(result, dict):
            return result
        return {"results": result if isinstance(result, list) else []}
    except Exception as e:
        logger.error("add_memory error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Search All Projects ───

class SearchAllRequest(BaseModel):
    query: str
    limit: int = 5


def _keyword_relevance(query: str, memory_text: str) -> float:
    """Score how well memory text matches query keywords (0.0 to 1.0)."""
    query_words = set(query.lower().split())
    # Remove very short / stop words
    query_words = {w for w in query_words if len(w) > 2}
    if not query_words:
        return 0.5  # Can't determine, neutral

    memory_lower = memory_text.lower()
    matches = sum(1 for w in query_words if w in memory_lower)
    return matches / len(query_words)


@app.post("/v1/memories/search-all/")
def search_all_projects(req: SearchAllRequest, authorization: str = Header("")):
    """Search across ALL projects — no user_id needed.

    Discovers all known user_ids from Neo4j graph, then searches each project.
    Returns aggregated results ranked by relevance (keyword match + semantic + freshness).
    """
    _check_auth(authorization)
    m = _get_memory()
    try:
        # Discover user_ids from Neo4j
        user_ids: set[str] = set()
        if NEO4J_URI:
            try:
                from neo4j import GraphDatabase
                driver = GraphDatabase.driver(
                    NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
                )
                with driver.session() as session:
                    rows = session.run(
                        "MATCH (n) WHERE n.user_id IS NOT NULL "
                        "RETURN DISTINCT n.user_id AS uid"
                    ).data()
                    for row in rows:
                        if row.get("uid"):
                            user_ids.add(row["uid"])
                driver.close()
            except Exception:
                pass

        # Fallback known IDs
        user_ids.update([
            "centauri", "happybrain", "em0-mcp-wrapper",
            "centauri-ios", "centauri-backend", "happy-brain",
            "pallasite", "seklabs",
        ])

        # Search each project (limit=3 per project to reduce noise)
        per_project_limit = min(req.limit, 3)
        all_results = []
        for uid in user_ids:
            try:
                results = _mem0_search(
                    m, query=req.query, user_id=uid, limit=per_project_limit,
                )
                items = (
                    results.get("results", [])
                    if isinstance(results, dict) else results
                )
                if isinstance(items, list):
                    for item in items:
                        item["_project"] = uid
                    all_results.extend(items)
            except Exception:
                pass

        # Score with keyword relevance boost
        for item in all_results:
            memory_text = item.get("memory", "")
            semantic = item.get("score", 0)
            keyword_rel = _keyword_relevance(req.query, memory_text)

            # Combined score: semantic matters, but keyword match is king
            # keyword_rel=1.0 → full boost, keyword_rel=0.0 → heavy penalty
            item["_keyword_relevance"] = round(keyword_rel, 2)
            item["_combined_score"] = round(
                semantic * (0.3 + 0.7 * keyword_rel), 4
            )

        # Sort by combined score, filter out zero-keyword matches
        all_results.sort(
            key=lambda x: x.get("_combined_score", 0), reverse=True,
        )

        # Only keep results with at least some keyword relevance
        relevant = [
            r for r in all_results if r.get("_keyword_relevance", 0) > 0
        ]
        # If nothing matches keywords, fall back to semantic-only top results
        if not relevant:
            relevant = all_results

        top_results = relevant[:req.limit]

        # Apply freshness scoring on top results
        top_results = _apply_freshness(top_results)

        return {
            "query": req.query,
            "projects_searched": len(user_ids),
            "total_matches": len(relevant),
            "results": top_results,
        }
    except Exception as e:
        logger.error("search_all error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Search Memory ───
@app.post("/v1/memories/search/")
def search_memory(req: SearchRequest, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        # Use v2 API for graph-enhanced search
        if req.api_version == "v2" and graph_enabled:
            results = _mem0_search(
                m,
                query=req.query,
                user_id=req.user_id,
                limit=req.limit,
                filters=req.filters,
            )
            # Also get graph relations
            try:
                graph_results = _mem0_search(
                    m,
                    query=req.query,
                    user_id=req.user_id,
                    limit=req.limit,
                    filters=req.filters,
                    version="v2",
                )
                if isinstance(graph_results, dict) and "relations" in graph_results:
                    if isinstance(results, dict):
                        results["relations"] = graph_results["relations"]
                    else:
                        results = {
                            "results": results if isinstance(results, list) else [],
                            "relations": graph_results["relations"],
                        }
            except Exception as ge:
                logger.warning("Graph search failed, returning vector only: %s", ge)

            # Apply freshness scoring
            if isinstance(results, dict) and "results" in results:
                results["results"] = _apply_freshness(results["results"])
                _track_access(m, [i.get("id") for i in results["results"] if i.get("id")])
            return results if isinstance(results, dict) else {"results": results}

        results = _mem0_search(
            m,
            query=req.query,
            user_id=req.user_id,
            limit=req.limit,
            filters=req.filters,
        )
        if isinstance(results, dict):
            items = results.get("results", [])
            results["results"] = _apply_freshness(items)
            _track_access(m, [i.get("id") for i in results["results"] if i.get("id")])
            return results
        # List response
        items = results if isinstance(results, list) else []
        items = _apply_freshness(items)
        _track_access(m, [i.get("id") for i in items if i.get("id")])
        return {"results": items}
    except Exception as e:
        logger.error("search_memory error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── List Memories ───
@app.get("/v1/memories/")
def list_memories(
    user_id: str = Query(""),
    authorization: str = Header(""),
):
    _check_auth(authorization)
    m = _get_memory()
    try:
        results = m.get_all(user_id=user_id)
        if isinstance(results, dict):
            return results
        return {"results": results if isinstance(results, list) else []}
    except Exception as e:
        logger.error("list_memories error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Get Memory ───
@app.get("/v1/memories/{memory_id}/")
def get_memory(memory_id: str, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        result = m.get(memory_id)
        return result
    except Exception as e:
        logger.error("get_memory error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Update Memory ───
@app.put("/v1/memories/{memory_id}/")
def update_memory(
    memory_id: str,
    req: UpdateMemoryRequest,
    authorization: str = Header(""),
):
    _check_auth(authorization)
    m = _get_memory()
    try:
        result = m.update(memory_id, data=req.data)
        _dispatch_webhook("memory.updated", {
            "memory_id": memory_id,
            "new_content": req.data[:500],
        })
        return result
    except Exception as e:
        logger.error("update_memory error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Delete Memory ───
@app.delete("/v1/memories/{memory_id}/")
def delete_memory(memory_id: str, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        m.delete(memory_id)
        _dispatch_webhook("memory.deleted", {"memory_id": memory_id})
        return {"status": "deleted"}
    except Exception as e:
        logger.error("delete_memory error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Delete All Memories ───
@app.delete("/v1/memories/")
def delete_all_memories(
    user_id: str = Query(""),
    authorization: str = Header(""),
):
    _check_auth(authorization)
    m = _get_memory()
    try:
        m.delete_all(user_id=user_id)
        return {"status": "deleted_all"}
    except Exception as e:
        logger.error("delete_all error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Memory History ───
@app.get("/v1/memories/{memory_id}/history/")
def memory_history(memory_id: str, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        result = m.history(memory_id)
        return result
    except Exception as e:
        logger.error("memory_history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════
# Graph Memory Endpoints (requires Neo4j)
# ═══════════════════════════════════════════════


@app.get("/v1/entities/")
def get_entities(
    user_id: str = Query(""),
    authorization: str = Header(""),
):
    """List all entities (nodes) in the knowledge graph."""
    _check_auth(authorization)
    if not NEO4J_URI:
        raise HTTPException(
            status_code=501,
            detail="Graph memory not enabled (NEO4J_URI not configured)",
        )
    m = _get_memory()
    try:
        # mem0's graph.get_all returns entities and relations
        filters = {"user_id": user_id} if user_id else {}
        graph_data = m.graph.get_all(filters=filters)

        # Extract unique entities from relations
        entities: dict[str, dict] = {}
        if isinstance(graph_data, list):
            for item in graph_data:
                src = item.get("source", "")
                src_type = item.get("source_type", "")
                tgt = item.get("target", "")
                tgt_type = item.get("target_type", "")
                if src and src not in entities:
                    entities[src] = {"name": src, "type": src_type}
                if tgt and tgt not in entities:
                    entities[tgt] = {"name": tgt, "type": tgt_type}
        return {"results": list(entities.values())}
    except Exception as e:
        logger.error("get_entities error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/relations/")
def get_relations(
    user_id: str = Query(""),
    authorization: str = Header(""),
):
    """List all relationships in the knowledge graph."""
    _check_auth(authorization)
    if not NEO4J_URI:
        raise HTTPException(
            status_code=501,
            detail="Graph memory not enabled (NEO4J_URI not configured)",
        )
    m = _get_memory()
    try:
        filters = {"user_id": user_id} if user_id else {}
        graph_data = m.graph.get_all(filters=filters)

        relations = []
        if isinstance(graph_data, list):
            for item in graph_data:
                relations.append({
                    "source": item.get("source", ""),
                    "source_type": item.get("source_type", ""),
                    "relationship": item.get("relation", item.get("relationship", "")),
                    "target": item.get("target", ""),
                    "target_type": item.get("target_type", ""),
                })
        return {"results": relations}
    except Exception as e:
        logger.error("get_relations error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Cross-Project Graph Search ───

class CrossProjectSearchRequest(BaseModel):
    query: str
    user_id: str = ""
    limit: int = 10


@app.post("/v1/search/cross-project")
def search_cross_project(req: CrossProjectSearchRequest, authorization: str = Header("")):
    """Find how entities from current project connect to other projects.

    Searches the Neo4j graph for entities that appear in multiple projects,
    then returns their cross-project relationships.
    """
    _check_auth(authorization)
    if not NEO4J_URI:
        raise HTTPException(
            status_code=501,
            detail="Graph memory not enabled (NEO4J_URI not configured)",
        )
    m = _get_memory()
    try:
        current_project = req.user_id

        # Step 1: Find entities relevant to the query in current project
        search_results = _mem0_search(
            m, query=req.query, user_id=current_project, limit=5,
        )
        search_items = (
            search_results.get("results", [])
            if isinstance(search_results, dict)
            else search_results
        )

        # Step 2: Get all graph data for current project
        filters = {"user_id": current_project} if current_project else {}
        graph_data = m.graph.get_all(filters=filters)

        # Collect entity names from current project
        project_entities = set()
        if isinstance(graph_data, list):
            for item in graph_data:
                src = item.get("source", "")
                tgt = item.get("target", "")
                if src:
                    project_entities.add(src.lower())
                if tgt:
                    project_entities.add(tgt.lower())

        # Step 3: Discover other projects from Neo4j + fallback
        other_projects: set[str] = set()
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
            )
            with driver.session() as session:
                rows = session.run(
                    "MATCH (n) WHERE n.user_id IS NOT NULL "
                    "RETURN DISTINCT n.user_id AS uid"
                ).data()
                for row in rows:
                    uid = row.get("uid", "")
                    if uid and uid != current_project:
                        other_projects.add(uid)
            driver.close()
        except Exception:
            pass
        # Fallback known IDs
        other_projects.update({
            uid for uid in [
                "centauri", "happybrain", "em0-mcp-wrapper",
                "centauri-ios", "centauri-backend",
                "pallasite", "seklabs", "pal-cms",
                "onboarding-survey-engine",
            ] if uid != current_project
        })

        cross_relations = []
        for other_project in other_projects:
            try:
                other_filters = {"user_id": other_project}
                other_graph = m.graph.get_all(filters=other_filters)
                if not isinstance(other_graph, list):
                    continue

                for item in other_graph:
                    src = item.get("source", "")
                    tgt = item.get("target", "")
                    rel = item.get("relation", item.get("relationship", ""))

                    # Check if any entity from current project appears here
                    src_match = src.lower() in project_entities
                    tgt_match = tgt.lower() in project_entities

                    if src_match or tgt_match:
                        cross_relations.append({
                            "entity": src if src_match else tgt,
                            "relation": rel,
                            "connected_to": tgt if src_match else src,
                            "other_project": other_project,
                            "direction": "outgoing" if src_match else "incoming",
                        })

                    if len(cross_relations) >= req.limit:
                        break
            except Exception as pe:
                logger.debug("Cross-project search skipped %s: %s", other_project, pe)

            if len(cross_relations) >= req.limit:
                break

        return {
            "current_project": current_project,
            "entities_in_project": len(project_entities),
            "other_projects_checked": len(other_projects),
            "cross_relations": cross_relations[:req.limit],
            "search_context": [
                i.get("memory", "")[:100] for i in search_items[:3]
            ],
        }
    except Exception as e:
        logger.error("search_cross_project error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════
# Resource Endpoints (for MCP Resources)
# ═══════════════════════════════════════════════


@app.get("/v1/context/{project_id}")
def auto_context(project_id: str, authorization: str = Header("")):
    """Build automatic context for a project — used by MCP Resource at session start."""
    _check_auth(authorization)
    m = _get_memory()
    try:
        # 1. Recent decisions & architecture memories
        recent = _mem0_search(
            m,
            query=f"{project_id} decisions architecture conventions",
            user_id=project_id,
            limit=5,
        )
        recent_items = (
            recent.get("results", []) if isinstance(recent, dict) else recent
        )
        recent_items = _apply_freshness(
            recent_items if isinstance(recent_items, list) else []
        )

        # 2. Immutable memories (bug lessons) — always included
        all_mems = m.get_all(user_id=project_id)
        all_items = all_mems.get("results", []) if isinstance(all_mems, dict) else []
        immutable_items = [
            mem for mem in all_items
            if mem.get("metadata", {}).get("immutable") is True
        ]

        # 3. Graph relations summary (if enabled)
        graph_relations = []
        if graph_enabled:
            try:
                filters = {"user_id": project_id}
                graph_data = m.graph.get_all(filters=filters)
                if isinstance(graph_data, list):
                    graph_relations = [
                        {
                            "source": item.get("source", ""),
                            "relation": item.get("relation", ""),
                            "target": item.get("target", ""),
                        }
                        for item in graph_data[:15]  # Cap at 15 relations
                    ]
            except Exception as ge:
                logger.warning("Graph context fetch failed: %s", ge)

        return {
            "project": project_id,
            "recent_decisions": [
                {
                    "memory": r.get("memory", ""),
                    "domain": r.get("metadata", {}).get("domain", ""),
                    "type": r.get("metadata", {}).get("type", ""),
                    "freshness": r.get("freshness"),
                }
                for r in recent_items[:5]
            ],
            "immutable_lessons": [
                {
                    "memory": im.get("memory", ""),
                    "domain": im.get("metadata", {}).get("domain", ""),
                }
                for im in immutable_items
            ],
            "graph_relations": graph_relations,
            "stats": {
                "total_memories": len(all_items),
                "immutable_count": len(immutable_items),
                "graph_relations_count": len(graph_relations),
            },
        }
    except Exception as e:
        logger.error("auto_context error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/resources/summary/{project_id}")
def project_summary(project_id: str, authorization: str = Header("")):
    """Project memory summary — domain distribution and key decisions."""
    _check_auth(authorization)
    m = _get_memory()
    try:
        all_mems = m.get_all(user_id=project_id)
        all_items = all_mems.get("results", []) if isinstance(all_mems, dict) else []

        # Group by domain
        domains: dict[str, int] = {}
        for mem in all_items:
            domain = mem.get("metadata", {}).get("domain", "uncategorized")
            domains[domain] = domains.get(domain, 0) + 1

        # Key decisions
        decisions = [
            mem.get("memory", "")[:200]
            for mem in all_items
            if mem.get("metadata", {}).get("type") == "decision"
        ][:10]

        # Last updated
        timestamps = [
            ts for mem in all_items
            for ts in [mem.get("updated_at") or mem.get("created_at")]
            if ts is not None
        ]
        last_updated = max(timestamps) if timestamps else "unknown"

        return {
            "project": project_id,
            "total_memories": len(all_items),
            "domains": domains,
            "key_decisions": decisions,
            "last_updated": last_updated,
        }
    except Exception as e:
        logger.error("project_summary error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/resources/graph-summary/{project_id}")
def graph_summary(project_id: str, authorization: str = Header("")):
    """Knowledge graph summary — entities and relations overview."""
    _check_auth(authorization)
    if not NEO4J_URI:
        return {"project": project_id, "error": "Graph not enabled"}
    m = _get_memory()
    try:
        filters = {"user_id": project_id} if project_id else {}
        graph_data = m.graph.get_all(filters=filters)

        entities: dict[str, list[str]] = {}
        relations = []
        if isinstance(graph_data, list):
            for item in graph_data:
                src = item.get("source", "")
                src_type = item.get("source_type", "entity")
                tgt = item.get("target", "")
                tgt_type = item.get("target_type", "entity")
                if src:
                    entities.setdefault(src_type, []).append(src)
                if tgt:
                    entities.setdefault(tgt_type, []).append(tgt)
                relations.append({
                    "source": src,
                    "relation": item.get("relation", ""),
                    "target": tgt,
                })

        # Deduplicate entity lists
        entities = {k: sorted(set(v)) for k, v in entities.items()}

        return {
            "project": project_id,
            "entity_types": {k: len(v) for k, v in entities.items()},
            "entities": entities,
            "relations": relations[:50],
            "total_relations": len(relations),
        }
    except Exception as e:
        logger.error("graph_summary error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════
# Admin Endpoints
# ═══════════════════════════════════════════════


class CompactRequest(BaseModel):
    user_id: str = ""
    dry_run: bool = True
    min_cluster_size: int = 2
    similarity_threshold: float = 0.25  # Jaccard word overlap


def _summarize_cluster(memories: list[dict]) -> str:
    """Merge a cluster of similar memories into one using LLM."""
    from openai import AzureOpenAI as _AzureOpenAI

    az_client = _AzureOpenAI(
        api_key=AZURE_OPENAI_KEY,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_deployment="gpt-4o-mini",
        api_version="2024-02-01",
    )

    contents = "\n".join(f"- {m.get('memory', '')}" for m in memories)

    response = az_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a knowledge compactor. Merge the following related memories "
                    "into a single, concise memory that preserves ALL important information. "
                    "Do not lose any decisions, trade-offs, or technical details. "
                    "Output only the merged memory text, nothing else."
                ),
            },
            {"role": "user", "content": f"Memories to merge:\n{contents}"},
        ],
        max_tokens=500,
        temperature=0.1,
    )
    return response.choices[0].message.content.strip()


def _text_similarity(text_a: str, text_b: str) -> float:
    """Jaccard similarity between two texts (word-level). O(1) per pair."""
    a_words = set(text_a.lower().split())
    b_words = set(text_b.lower().split())
    if not a_words or not b_words:
        return 0.0
    intersection = len(a_words & b_words)
    union = len(a_words | b_words)
    return intersection / union if union > 0 else 0.0


def _cluster_by_similarity(
    m, memories: list[dict], threshold: float,
) -> list[list[dict]]:
    """Group memories into clusters by text similarity. O(n²) but no API calls."""
    used = set()
    clusters = []

    for i, mem_a in enumerate(memories):
        if i in used:
            continue
        cluster = [mem_a]
        used.add(i)
        text_a = mem_a.get("memory", "")

        for j, mem_b in enumerate(memories):
            if j in used:
                continue
            text_b = mem_b.get("memory", "")
            similarity = _text_similarity(text_a, text_b)
            if similarity >= threshold:
                cluster.append(mem_b)
                used.add(j)

        clusters.append(cluster)

    return clusters


@app.post("/admin/compact")
def compact_memories(req: CompactRequest, authorization: str = Header("")):
    """Compact similar memories within domain+type groups.

    dry_run=True shows the plan without applying. dry_run=False merges memories.
    """
    _check_auth(authorization)
    m = _get_memory()
    try:
        uid = req.user_id
        all_mems = m.get_all(user_id=uid).get("results", []) if uid else m.get_all().get("results", [])

        # Group by domain+type
        groups: dict[str, list[dict]] = {}
        for mem in all_mems:
            meta = mem.get("metadata", {})
            # Skip immutable memories
            if meta.get("immutable"):
                continue
            key = f"{meta.get('domain', 'unknown')}:{meta.get('type', 'unknown')}"
            groups.setdefault(key, []).append(mem)

        compaction_plan = []
        total_merged = 0
        total_saved = 0

        for key, mems in groups.items():
            if len(mems) < req.min_cluster_size:
                continue

            clusters = _cluster_by_similarity(m, mems, req.similarity_threshold)

            for cluster in clusters:
                if len(cluster) < req.min_cluster_size:
                    continue

                if req.dry_run:
                    compaction_plan.append({
                        "group": key,
                        "memories_to_merge": len(cluster),
                        "preview": [c.get("memory", "")[:100] for c in cluster],
                    })
                else:
                    # LLM summarize (with rate limit protection)
                    if total_merged > 0:
                        time.sleep(3)  # Avoid Azure OpenAI 429
                    summary = _summarize_cluster(cluster)
                    domain, mtype = key.split(":", 1)
                    merged_ids = [c.get("id") for c in cluster if c.get("id")]

                    # Add compacted memory
                    m.add(
                        summary,
                        user_id=uid or cluster[0].get("user_id", ""),
                        metadata={
                            "domain": domain,
                            "type": mtype,
                            "source": "compaction",
                            "merged_count": len(cluster),
                            "merged_ids": merged_ids,
                        },
                    )

                    # Delete originals
                    for c in cluster:
                        cid = c.get("id")
                        if cid:
                            try:
                                m.delete(cid)
                            except Exception:
                                pass

                    total_merged += len(cluster)
                    total_saved += len(cluster) - 1
                    compaction_plan.append({
                        "group": key,
                        "merged": len(cluster),
                        "into_summary": summary[:200],
                    })

        return {
            "dry_run": req.dry_run,
            "plan": compaction_plan,
            "total_groups_analyzed": len(groups),
            "total_merged": total_merged,
            "memories_saved": total_saved,
        }
    except Exception as e:
        logger.error("compact_memories error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/reset-graph")
def reset_graph(authorization: str = Header("")):
    """Delete all nodes and relationships in Neo4j. Use with caution."""
    _check_auth(authorization)
    if not NEO4J_URI:
        raise HTTPException(
            status_code=501,
            detail="Graph memory not enabled (NEO4J_URI not configured)",
        )
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
        with driver.session() as session:
            # Count before delete
            count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            # Delete all nodes and relationships
            session.run("MATCH (n) DETACH DELETE n")
            # Drop any vector indexes
            indexes = session.run("SHOW INDEXES YIELD name, type WHERE type = 'VECTOR' RETURN name").data()
            for idx in indexes:
                session.run(f"DROP INDEX {idx['name']}")
            logger.info("Neo4j reset: deleted %d nodes, dropped %d vector indexes", count, len(indexes))
        driver.close()
        return {
            "status": "reset_complete",
            "deleted_nodes": count,
            "dropped_vector_indexes": len(indexes),
        }
    except Exception as e:
        logger.error("reset_graph error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-audit")
def graph_audit(
    user_id: str = Query(""),
    duplicate_limit: int = Query(25, ge=1, le=100),
    authorization: str = Header(""),
):
    """Dry-run graph quality audit.

    This endpoint never deletes or mutates graph data. It gives operators a
    compact view of growth, duplication, isolated nodes, and project-boundary
    edges before deciding whether cleanup or compaction is needed.
    """
    _check_auth(authorization)
    if not NEO4J_URI:
        return {
            "graph_enabled": False,
            "dry_run": True,
            "user_id": user_id,
            "summary": {
                "nodes": 0,
                "edges": 0,
                "isolated_nodes": 0,
                "self_loops": 0,
                "cross_project_edges": 0,
            },
            "duplicate_entities": [],
            "relation_types": [],
            "property_coverage": [],
            "recommendations": ["Set NEO4J_URI to enable graph audit."],
        }

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
        params = {"user_id": user_id, "duplicate_limit": duplicate_limit}
        with driver.session() as session:
            node_rows = session.run(
                """
                MATCH (n)
                WHERE $user_id = '' OR n.user_id = $user_id
                RETURN count(n) AS count
                """,
                params,
            ).data()
            edge_rows = session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE $user_id = ''
                   OR a.user_id = $user_id
                   OR b.user_id = $user_id
                RETURN count(r) AS count
                """,
                params,
            ).data()
            isolated_rows = session.run(
                """
                MATCH (n)
                WHERE ($user_id = '' OR n.user_id = $user_id)
                  AND NOT (n)--()
                RETURN count(n) AS count
                """,
                params,
            ).data()
            self_loop_rows = session.run(
                """
                MATCH (n)-[r]->(n)
                WHERE $user_id = '' OR n.user_id = $user_id
                RETURN count(r) AS count
                """,
                params,
            ).data()
            cross_project_rows = session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE a.user_id IS NOT NULL
                  AND b.user_id IS NOT NULL
                  AND a.user_id <> b.user_id
                  AND ($user_id = '' OR a.user_id = $user_id OR b.user_id = $user_id)
                RETURN count(r) AS count
                """,
                params,
            ).data()
            duplicate_entities = session.run(
                """
                MATCH (n)
                WHERE $user_id = '' OR n.user_id = $user_id
                WITH n, coalesce(n.name, n.id, n.uuid, n.label, n.source, n.target) AS entity_key
                WHERE entity_key IS NOT NULL
                WITH toLower(toString(entity_key)) AS key,
                     labels(n) AS labels,
                     count(n) AS count,
                     collect(elementId(n))[0..5] AS sample_node_ids
                WHERE count > 1
                RETURN key, labels, count, sample_node_ids
                ORDER BY count DESC
                LIMIT $duplicate_limit
                """,
                params,
            ).data()
            relation_types = session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE $user_id = ''
                   OR a.user_id = $user_id
                   OR b.user_id = $user_id
                RETURN type(r) AS type, count(r) AS count
                ORDER BY count DESC
                LIMIT 50
                """,
                params,
            ).data()
            property_coverage = session.run(
                """
                MATCH (n)
                WHERE $user_id = '' OR n.user_id = $user_id
                UNWIND keys(n) AS property
                RETURN property, count(*) AS count
                ORDER BY count DESC
                LIMIT 50
                """,
                params,
            ).data()
        driver.close()

        def _count(rows: list[dict]) -> int:
            return int(rows[0].get("count", 0)) if rows else 0

        summary = {
            "nodes": _count(node_rows),
            "edges": _count(edge_rows),
            "isolated_nodes": _count(isolated_rows),
            "self_loops": _count(self_loop_rows),
            "cross_project_edges": _count(cross_project_rows),
        }
        recommendations = []
        if duplicate_entities:
            recommendations.append("Review duplicate_entities before the next compaction run.")
        if summary["isolated_nodes"]:
            recommendations.append("Inspect isolated_nodes; they may be stale extraction artifacts.")
        if summary["self_loops"]:
            recommendations.append("Inspect self_loops; they often indicate noisy relation extraction.")
        if not property_coverage:
            recommendations.append("No node properties found for this scope.")
        if not recommendations:
            recommendations.append("No obvious graph hygiene issues detected in this audit.")

        return {
            "graph_enabled": True,
            "dry_run": True,
            "user_id": user_id,
            "summary": summary,
            "duplicate_entities": duplicate_entities,
            "relation_types": relation_types,
            "property_coverage": property_coverage,
            "recommendations": recommendations,
        }
    except Exception as e:
        logger.error("graph_audit error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _graph_filter_stats(session, user_id: str) -> dict:
    label_rows = session.run(
        """
        MATCH (n)
        WHERE $user_id = '' OR n.user_id = $user_id
        UNWIND labels(n) AS label
        RETURN label, count(*) AS count
        ORDER BY count DESC
        LIMIT 50
        """,
        {"user_id": user_id},
    ).data()
    relation_rows = session.run(
        """
        MATCH (a)-[r]->(b)
        WHERE $user_id = ''
           OR a.user_id = $user_id
           OR b.user_id = $user_id
        RETURN type(r) AS type, count(r) AS count
        ORDER BY count DESC
        LIMIT 50
        """,
        {"user_id": user_id},
    ).data()
    return {
        "labels": label_rows,
        "relations": relation_rows,
    }


def _graph_payload_from_rows(
    node_rows: list[dict],
    rel_rows: list[dict],
) -> dict[str, list[dict]]:
    nodes = [
        graph_node_payload(row["id"], row.get("labels"), row.get("props"))
        for row in node_rows
    ]
    edges = [
        graph_edge_payload(row["source"], row["target"], row["type"], row.get("props"))
        for row in rel_rows
    ]
    return {"nodes": nodes, "edges": edges}


@app.get("/admin/graph-slice")
def graph_slice(
    user_id: str = Query(""),
    label: str = Query(""),
    relation: str = Query(""),
    q: str = Query(""),
    limit: int = Query(300, ge=1, le=2000),
    authorization: str = Header(""),
):
    """Return a bounded graph slice for the v2 explorer."""
    _check_auth(authorization)
    try:
        driver = _get_neo4j_driver()
        params = {
            "user_id": user_id,
            "label": label,
            "relation": relation,
            "q": q.strip().lower(),
            "limit": limit,
            "edge_limit": min(limit * 4, 5000),
        }
        with driver.session() as session:
            node_rows = session.run(
                """
                MATCH (n)
                WHERE ($user_id = '' OR n.user_id = $user_id)
                  AND ($label = '' OR $label IN labels(n))
                  AND (
                    $q = ''
                    OR toLower(toString(coalesce(n.name, n.id, n.uuid, n.label, n.source, n.target, '')))
                       CONTAINS $q
                  )
                RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props
                LIMIT $limit
                """,
                params,
            ).data()
            node_ids = [row["id"] for row in node_rows]
            rel_rows = []
            if node_ids:
                rel_rows = session.run(
                    """
                    MATCH (a)-[r]->(b)
                    WHERE elementId(a) IN $node_ids
                      AND elementId(b) IN $node_ids
                      AND ($relation = '' OR type(r) = $relation)
                    RETURN elementId(a) AS source,
                           elementId(b) AS target,
                           type(r) AS type,
                           properties(r) AS props
                    LIMIT $edge_limit
                    """,
                    {**params, "node_ids": node_ids},
                ).data()
            filters = _graph_filter_stats(session, user_id)
        driver.close()

        payload = _graph_payload_from_rows(node_rows, rel_rows)
        payload["filters"] = filters
        payload["stats"] = {
            "nodes": len(payload["nodes"]),
            "edges": len(payload["edges"]),
            "limited": len(payload["nodes"]) >= limit,
        }
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_slice error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-search")
def graph_search(
    q: str = Query(..., min_length=1),
    user_id: str = Query(""),
    limit: int = Query(25, ge=1, le=100),
    authorization: str = Header(""),
):
    """Search graph nodes by common identity fields."""
    _check_auth(authorization)
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            rows = session.run(
                """
                MATCH (n)
                WHERE ($user_id = '' OR n.user_id = $user_id)
                  AND toLower(toString(coalesce(n.name, n.id, n.uuid, n.label, n.source, n.target, '')))
                      CONTAINS $q
                RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props
                LIMIT $limit
                """,
                {"q": q.strip().lower(), "user_id": user_id, "limit": limit},
            ).data()
        driver.close()
        return {
            "nodes": [
                graph_node_payload(row["id"], row.get("labels"), row.get("props"))
                for row in rows
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_search error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-neighbors/{node_id}")
def graph_neighbors(
    node_id: str,
    depth: int = Query(1, ge=1, le=2),
    limit: int = Query(250, ge=1, le=1000),
    authorization: str = Header(""),
):
    """Return a node neighborhood, capped for interactive expansion."""
    _check_auth(authorization)
    try:
        driver = _get_neo4j_driver()
        depth = max(1, min(depth, 2))
        with driver.session() as session:
            node_rows = session.run(
                f"""
                MATCH (center)
                WHERE elementId(center) = $node_id
                MATCH p = (center)-[*1..{depth}]-(n)
                WITH p LIMIT $limit
                UNWIND nodes(p) AS node
                RETURN DISTINCT elementId(node) AS id,
                       labels(node) AS labels,
                       properties(node) AS props
                """,
                {"node_id": node_id, "limit": limit},
            ).data()
            rel_rows = session.run(
                f"""
                MATCH (center)
                WHERE elementId(center) = $node_id
                MATCH p = (center)-[*1..{depth}]-(n)
                WITH p LIMIT $limit
                UNWIND relationships(p) AS rel
                RETURN DISTINCT elementId(startNode(rel)) AS source,
                       elementId(endNode(rel)) AS target,
                       type(rel) AS type,
                       properties(rel) AS props
                """,
                {"node_id": node_id, "limit": limit},
            ).data()
        driver.close()
        payload = _graph_payload_from_rows(node_rows, rel_rows)
        payload["stats"] = {
            "nodes": len(payload["nodes"]),
            "edges": len(payload["edges"]),
            "depth": depth,
        }
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_neighbors error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-path")
def graph_path(
    from_id: str = Query(...),
    to_id: str = Query(...),
    max_depth: int = Query(4, ge=1, le=6),
    authorization: str = Header(""),
):
    """Return the shortest path between two graph nodes."""
    _check_auth(authorization)
    try:
        driver = _get_neo4j_driver()
        max_depth = max(1, min(max_depth, 6))
        with driver.session() as session:
            node_rows = session.run(
                f"""
                MATCH (a), (b)
                WHERE elementId(a) = $from_id AND elementId(b) = $to_id
                MATCH p = shortestPath((a)-[*1..{max_depth}]-(b))
                UNWIND nodes(p) AS node
                RETURN DISTINCT elementId(node) AS id,
                       labels(node) AS labels,
                       properties(node) AS props
                """,
                {"from_id": from_id, "to_id": to_id},
            ).data()
            rel_rows = session.run(
                f"""
                MATCH (a), (b)
                WHERE elementId(a) = $from_id AND elementId(b) = $to_id
                MATCH p = shortestPath((a)-[*1..{max_depth}]-(b))
                UNWIND relationships(p) AS rel
                RETURN DISTINCT elementId(startNode(rel)) AS source,
                       elementId(endNode(rel)) AS target,
                       type(rel) AS type,
                       properties(rel) AS props
                """,
                {"from_id": from_id, "to_id": to_id},
            ).data()
        driver.close()
        payload = _graph_payload_from_rows(node_rows, rel_rows)
        payload["stats"] = {
            "nodes": len(payload["nodes"]),
            "edges": len(payload["edges"]),
            "max_depth": max_depth,
            "found": bool(payload["nodes"]),
        }
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_path error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-data")
def graph_data(authorization: str = Header("")):
    """Return all Neo4j nodes and relationships as JSON for visualization."""
    _check_auth(authorization)
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            # Get all nodes with elementId (Neo4j 5.x)
            nodes_result = session.run(
                "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
            ).data()
            # Get all relationships
            rels_result = session.run(
                "MATCH (a)-[r]->(b) RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type, properties(r) AS props"
            ).data()
        driver.close()

        nodes = [
            graph_node_payload(n["id"], n.get("labels"), n.get("props"))
            for n in nodes_result
        ]
        edges = [
            graph_edge_payload(r["source"], r["target"], r["type"], r.get("props"))
            for r in rels_result
        ]

        return {"nodes": nodes, "edges": edges}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_data error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-debug")
def graph_debug(authorization: str = Header("")):
    """Debug: show raw Neo4j structure."""
    _check_auth(authorization)
    if not NEO4J_URI:
        raise HTTPException(status_code=501, detail="Graph not enabled")
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
        with driver.session() as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            labels = session.run("CALL db.labels()").data()
            rel_types = session.run("CALL db.relationshipTypes()").data()
            sample_nodes = session.run("MATCH (n) RETURN labels(n) AS labels, properties(n) AS props LIMIT 5").data()
            # Strip embeddings from sample
            for sn in sample_nodes:
                if sn.get("props"):
                    sn["props"].pop("embedding", None)
            sample_rels = session.run("MATCH (a)-[r]->(b) RETURN labels(a) AS from_labels, type(r) AS type, labels(b) AS to_labels LIMIT 5").data()
        driver.close()
        return {
            "node_count": node_count,
            "rel_count": rel_count,
            "labels": labels,
            "relationship_types": rel_types,
            "sample_nodes": sample_nodes,
            "sample_rels": sample_rels,
        }
    except Exception as e:
        logger.error("graph_debug error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/graph-v2", response_class=HTMLResponse)
def graph_visualizer_v2():
    """Interactive bounded graph explorer."""
    return """<!DOCTYPE html>
<html><head>
<title>em0 Graph Explorer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:#0b0f14; color:#dbe4ee; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; overflow:hidden; display:flex; flex-direction:column; }
  button,input,select { font:inherit; }
  #topbar { min-height:56px; display:flex; flex-wrap:wrap; align-items:center; gap:10px; padding:10px 14px; background:#111821; border-bottom:1px solid #263241; }
  #brand { font-weight:700; color:#7cc7ff; white-space:nowrap; }
  #topbar input,#topbar select { height:34px; color:#dbe4ee; background:#0b0f14; border:1px solid #2f3f50; border-radius:6px; padding:0 10px; min-width:0; }
  #apiKey { width:190px; }
  #project { width:150px; }
  #search { width:220px; }
  #limit { width:86px; }
  #labelFilter,#relationFilter { width:150px; }
  button { height:34px; border:1px solid #2f3f50; border-radius:6px; color:#e8f1fb; background:#1b2733; padding:0 12px; cursor:pointer; }
  button:hover { border-color:#7cc7ff; color:#7cc7ff; }
  button.primary { background:#16794f; border-color:#229d68; color:#fff; }
  button.primary:hover { background:#1e8d5e; color:#fff; }
  button.warn { background:#58331a; border-color:#9a5a28; }
  #status { margin-left:auto; color:#8ea0b4; font-size:12px; white-space:nowrap; }
  #shell { flex:1 1 auto; display:grid; grid-template-columns:minmax(0,1fr) 360px; min-height:0; }
  #graph { position:relative; min-width:0; min-height:0; overflow:hidden; }
  #network { position:absolute; inset:0; width:100%; height:100%; }
  #empty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#8ea0b4; font-size:14px; pointer-events:none; }
  #side { min-width:0; background:#111821; border-left:1px solid #263241; overflow:auto; }
  .panel { padding:14px; border-bottom:1px solid #263241; }
  .title { color:#8ea0b4; font-size:11px; text-transform:uppercase; letter-spacing:.04em; margin-bottom:8px; }
  .name { color:#f2f7fd; font-size:18px; font-weight:700; overflow-wrap:anywhere; margin-bottom:6px; }
  .pill { display:inline-flex; align-items:center; height:22px; padding:0 8px; border-radius:999px; font-size:12px; border:1px solid currentColor; margin:0 6px 8px 0; }
  .grid { display:grid; grid-template-columns:minmax(88px,120px) 1fr; gap:6px 10px; font-size:12px; }
  .key { color:#8ea0b4; }
  .val { color:#dbe4ee; overflow-wrap:anywhere; text-align:right; }
  .row { display:flex; gap:8px; align-items:center; justify-content:space-between; padding:7px 0; border-bottom:1px solid #1e2a36; font-size:13px; }
  .muted { color:#8ea0b4; }
  .error { color:#ff8a80; }
  .ok { color:#8ef0b1; }
  .small { font-size:12px; }
  #audit .row { align-items:flex-start; }
  @media (max-width: 900px) {
    #shell { grid-template-columns:1fr; grid-template-rows:minmax(320px,1fr) 300px; }
    #side { border-left:none; border-top:1px solid #263241; }
    #apiKey,#project,#search,#labelFilter,#relationFilter { width:calc(50vw - 24px); min-width:150px; }
    #status { margin-left:0; width:100%; }
  }
</style>
</head><body>
<div id="topbar">
  <div id="brand">em0 Graph</div>
  <input id="apiKey" type="password" placeholder="API key" onkeydown="if(event.key==='Enter')loadSlice()">
  <input id="project" placeholder="Project" onkeydown="if(event.key==='Enter')loadSlice()">
  <input id="search" placeholder="Search" onkeydown="if(event.key==='Enter')loadSlice()">
  <select id="labelFilter"><option value="">All labels</option></select>
  <select id="relationFilter"><option value="">All relations</option></select>
  <input id="limit" type="number" min="1" max="2000" value="300">
  <button class="primary" onclick="loadSlice()">Load</button>
  <button onclick="runAudit()">Audit</button>
  <button onclick="fitGraph()">Fit</button>
  <button class="warn" onclick="clearPath()">Path</button>
  <div id="status">idle</div>
</div>
<div id="shell">
  <div id="graph"><div id="network"></div><div id="empty">Enter API key and load a slice</div></div>
  <div id="side">
    <div class="panel" id="summary">
      <div class="title">Summary</div>
      <div class="grid">
        <div class="key">Nodes</div><div class="val" id="nodeCount">0</div>
        <div class="key">Edges</div><div class="val" id="edgeCount">0</div>
        <div class="key">Selected</div><div class="val" id="selectedCount">0</div>
      </div>
    </div>
    <div class="panel" id="detail">
      <div class="title">Node</div>
      <div class="muted small">No selection</div>
    </div>
    <div class="panel" id="preview">
      <div class="title">Data Preview</div>
      <div class="muted small">No data loaded</div>
    </div>
    <div class="panel" id="audit">
      <div class="title">Audit</div>
      <div class="muted small">Not run</div>
    </div>
  </div>
</div>
<script>
let network, nodes, edges, currentData = {nodes: [], edges: []};
let selectedPath = [];
const colors = ['#7cc7ff','#f58f7c','#8ef0b1','#d6a6ff','#ffd166','#68d8d6','#ff9bc8','#a7c7e7','#f4a261','#b8f2e6'];
const groupColors = {};
let colorIndex = 0;

function apiHeaders() {
  return { Authorization: 'Bearer ' + document.getElementById('apiKey').value.trim() };
}

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.className = cls || '';
  el.textContent = text;
}

function colorFor(group) {
  if (!groupColors[group]) {
    groupColors[group] = colors[colorIndex % colors.length];
    colorIndex += 1;
  }
  return groupColors[group];
}

function queryString(params) {
  const qs = new URLSearchParams();
  Object.keys(params).forEach(k => {
    if (params[k] !== undefined && params[k] !== null && params[k] !== '') qs.set(k, params[k]);
  });
  return qs.toString();
}

async function loadSlice() {
  const key = document.getElementById('apiKey').value.trim();
  if (!key) {
    setStatus('API key required', 'error');
    return;
  }
  localStorage.setItem('em0GraphApiKey', key);
  setStatus('loading', '');
  const params = {
    user_id: document.getElementById('project').value.trim(),
    q: document.getElementById('search').value.trim(),
    label: document.getElementById('labelFilter').value,
    relation: document.getElementById('relationFilter').value,
    limit: document.getElementById('limit').value || 300
  };
  try {
    const data = await fetchJson('/admin/graph-slice?' + queryString(params));
    currentData = { nodes: data.nodes || [], edges: data.edges || [] };
    renderPreview(currentData);
    renderGraph(currentData);
    populateFilters(data.filters || {});
    updateSummary(data.stats || {});
    setStatus((data.stats.nodes || 0) + ' nodes / ' + (data.stats.edges || 0) + ' edges', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function populateFilters(filters) {
  const labelSel = document.getElementById('labelFilter');
  const relSel = document.getElementById('relationFilter');
  const activeLabel = labelSel.value;
  const activeRel = relSel.value;
  labelSel.innerHTML = '<option value="">All labels</option>' + (filters.labels || []).map(r =>
    '<option value="' + escAttr(r.label) + '">' + esc(r.label) + ' (' + r.count + ')</option>'
  ).join('');
  relSel.innerHTML = '<option value="">All relations</option>' + (filters.relations || []).map(r =>
    '<option value="' + escAttr(r.type) + '">' + esc(r.type) + ' (' + r.count + ')</option>'
  ).join('');
  labelSel.value = activeLabel;
  relSel.value = activeRel;
}

function renderGraph(data) {
  const empty = document.getElementById('empty');
  const container = document.getElementById('network');
  if (!container) {
    setStatus('graph container missing', 'error');
    return;
  }
  if (network && typeof network.destroy === 'function') {
    network.destroy();
  }
  network = null;
  container.innerHTML = '';
  if (empty) {
    empty.textContent = data.nodes.length ? '' : 'No nodes matched this slice';
    empty.style.display = data.nodes.length ? 'none' : 'flex';
  }
  if (typeof vis === 'undefined') {
    renderFallbackGraph(data);
    return;
  }
  if (!data.nodes.length) {
    updateSummary({});
    return;
  }
  const degree = {};
  data.edges.forEach(e => {
    degree[e.from] = (degree[e.from] || 0) + 1;
    degree[e.to] = (degree[e.to] || 0) + 1;
  });
  nodes = new vis.DataSet(data.nodes.map(n => {
    const c = colorFor(n.group);
    return {
      id: n.id,
      label: n.label,
      group: n.group,
      props: n.properties || {},
      title: esc(n.label),
      size: Math.max(11, Math.min(38, 11 + (degree[n.id] || 0) * 3)),
      color: { background: c, border: c, highlight: { background: '#f2f7fd', border: c } },
      font: { color: '#dbe4ee', size: 13, face: '-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif' },
      borderWidth: 2,
      shape: 'dot'
    };
  }));
  edges = new vis.DataSet(data.edges.map((e, i) => ({
    id: 'edge-' + i + '-' + e.from + '-' + e.to + '-' + e.label,
    from: e.from,
    to: e.to,
    label: e.label,
    props: e.properties || {},
    arrows: { to: { enabled: true, scaleFactor: .55 } },
    color: { color: '#304255', highlight: '#7cc7ff', hover: '#7cc7ff' },
    font: { color: '#8ea0b4', size: 10, strokeWidth: 0 },
    smooth: { type: 'continuous' },
    width: 1.4
  })));
  network = new vis.Network(container, { nodes, edges }, {
    layout: { improvedLayout: data.nodes.length < 180 },
    physics: {
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -72, centralGravity: .009, springLength: 130, springConstant: .024, damping: .42 },
      stabilization: { iterations: 260, fit: true }
    },
    interaction: { hover: true, multiselect: true, tooltipDelay: 180, zoomView: true, dragView: true }
  });
  network.on('click', event => {
    const ids = event.nodes || [];
    document.getElementById('selectedCount').textContent = ids.length;
    if (ids.length) showNode(ids[0]);
  });
  network.on('doubleClick', event => {
    if (event.nodes && event.nodes[0]) expandNode(event.nodes[0]);
  });
  network.once('stabilizationIterationsDone', () => fitGraph());
  window.setTimeout(() => fitGraph(), 250);
  updateSummary({});
}

function updateSummary(stats) {
  document.getElementById('nodeCount').textContent = currentData.nodes.length;
  document.getElementById('edgeCount').textContent = currentData.edges.length;
  if (stats.limited) setStatus('limited to ' + currentData.nodes.length + ' nodes', '');
}

function showNode(id) {
  const node = nodes.get(id);
  if (!node) return;
  const c = colorFor(node.group);
  let html = '<div class="title">Node</div>';
  html += '<div class="name">' + esc(node.label) + '</div>';
  html += '<span class="pill" style="color:' + c + '">' + esc(node.group) + '</span>';
  html += '<div style="display:flex;gap:8px;margin:8px 0 12px">';
  html += '<button onclick="expandNode(\\'' + jsString(id) + '\\')">Expand</button>';
  html += '<button onclick="pickForPath(\\'' + jsString(id) + '\\')">Pick</button>';
  html += '</div><div class="grid">';
  Object.keys(node.props || {}).sort().forEach(k => {
    const v = String(node.props[k]);
    html += '<div class="key">' + esc(k) + '</div><div class="val">' + esc(v.slice(0, 160)) + '</div>';
  });
  html += '</div>';
  document.getElementById('detail').innerHTML = html;
}

function renderPreview(data) {
  const el = document.getElementById('preview');
  let html = '<div class="title">Data Preview</div>';
  html += '<div class="row"><span class="muted">Loaded nodes</span><strong>' + data.nodes.length + '</strong></div>';
  html += '<div class="row"><span class="muted">Loaded edges</span><strong>' + data.edges.length + '</strong></div>';
  html += '<div class="title" style="margin-top:14px">First Nodes</div>';
  data.nodes.slice(0, 12).forEach(n => {
    html += '<div class="row"><span>' + esc(n.label) + '</span><span class="muted small">' + esc(n.group) + '</span></div>';
  });
  if (!data.nodes.length) html += '<div class="muted small">No nodes matched this slice.</div>';
  el.innerHTML = html;
}

function renderFallbackGraph(data) {
  const graph = document.getElementById('network');
  if (!graph) return;
  graph.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.style.cssText = 'height:100%;overflow:auto;padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;background:#0b0f14';
  data.nodes.forEach(n => {
    const item = document.createElement('button');
    item.type = 'button';
    item.style.cssText = 'height:auto;text-align:left;padding:12px;border:1px solid #2f3f50;background:#111821;color:#dbe4ee;border-radius:6px';
    item.innerHTML = '<strong>' + esc(n.label) + '</strong><div class="muted small">' + esc(n.group) + '</div>';
    item.onclick = () => showFallbackNode(n.id);
    wrap.appendChild(item);
  });
  graph.appendChild(wrap);
  setStatus('renderer unavailable; showing list fallback', 'error');
}

function showFallbackNode(id) {
  const node = currentData.nodes.find(n => n.id === id);
  if (!node) return;
  let html = '<div class="title">Node</div>';
  html += '<div class="name">' + esc(node.label) + '</div>';
  html += '<span class="pill">' + esc(node.group) + '</span>';
  html += '<div style="display:flex;gap:8px;margin:8px 0 12px">';
  html += '<button onclick="expandNode(\\'' + jsString(id) + '\\')">Expand</button>';
  html += '<button onclick="pickForPath(\\'' + jsString(id) + '\\')">Pick</button>';
  html += '</div><div class="grid">';
  Object.keys(node.properties || {}).sort().forEach(k => {
    html += '<div class="key">' + esc(k) + '</div><div class="val">' + esc(String(node.properties[k]).slice(0, 160)) + '</div>';
  });
  html += '</div>';
  document.getElementById('detail').innerHTML = html;
}

async function expandNode(id) {
  setStatus('expanding', '');
  try {
    const data = await fetchJson('/admin/graph-neighbors/' + encodeURIComponent(id) + '?depth=1&limit=300');
    mergeGraph(data);
    renderPreview(currentData);
    renderGraph(currentData);
    if (network) network.selectNodes([id]);
    setStatus('expanded ' + (data.stats.nodes || 0) + ' nodes', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function mergeGraph(data) {
  const byNode = {};
  currentData.nodes.concat(data.nodes || []).forEach(n => { byNode[n.id] = n; });
  const byEdge = {};
  currentData.edges.concat(data.edges || []).forEach(e => {
    byEdge[e.from + '|' + e.to + '|' + e.label] = e;
  });
  currentData = { nodes: Object.values(byNode), edges: Object.values(byEdge) };
}

function pickForPath(id) {
  selectedPath = selectedPath.filter(x => x !== id);
  selectedPath.push(id);
  if (selectedPath.length > 2) selectedPath.shift();
  setStatus(selectedPath.length === 2 ? 'path ready' : 'path start picked', '');
  if (selectedPath.length === 2) loadPath();
}

async function loadPath() {
  const params = queryString({ from_id: selectedPath[0], to_id: selectedPath[1], max_depth: 5 });
  setStatus('path loading', '');
  try {
    const data = await fetchJson('/admin/graph-path?' + params);
    if (!data.stats.found) {
      setStatus('path not found', 'error');
      return;
    }
    mergeGraph(data);
    renderPreview(currentData);
    renderGraph(currentData);
    if (network) network.selectNodes((data.nodes || []).map(n => n.id));
    setStatus('path found', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function clearPath() {
  selectedPath = [];
  setStatus('path cleared', '');
}

async function runAudit() {
  setStatus('audit loading', '');
  const params = queryString({ user_id: document.getElementById('project').value.trim(), duplicate_limit: 15 });
  try {
    const data = await fetchJson('/admin/graph-audit?' + params);
    renderAudit(data);
    setStatus('audit done', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function renderAudit(data) {
  const s = data.summary || {};
  let html = '<div class="title">Audit</div>';
  html += row('Nodes', s.nodes || 0);
  html += row('Edges', s.edges || 0);
  html += row('Isolated', s.isolated_nodes || 0);
  html += row('Self loops', s.self_loops || 0);
  html += row('Cross project', s.cross_project_edges || 0);
  html += '<div class="title" style="margin-top:14px">Recommendations</div>';
  (data.recommendations || []).forEach(item => {
    html += '<div class="row"><div class="muted small">' + esc(item) + '</div></div>';
  });
  document.getElementById('audit').innerHTML = html;
}

function row(k, v) {
  return '<div class="row"><span class="muted">' + esc(k) + '</span><strong>' + esc(String(v)) + '</strong></div>';
}

function fitGraph() {
  if (network) network.fit({ animation: { duration: 350, easingFunction: 'easeInOutQuad' } });
}

async function fetchJson(url) {
  const res = await fetch(url, { headers: apiHeaders() });
  if (res.status === 401) throw new Error('API key missing or invalid');
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body.detail ? ': ' + body.detail : '';
    } catch (_) {}
    throw new Error('HTTP ' + res.status + detail);
  }
  return res.json();
}

function initGraphExplorer() {
  try {
    const savedKey = localStorage.getItem('em0GraphApiKey');
    if (savedKey) document.getElementById('apiKey').value = savedKey;
  } catch (_) {}
  if (typeof vis === 'undefined') {
    setStatus('graph renderer failed to load', 'error');
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function escAttr(s) {
  return esc(s).replace(/"/g, '&quot;');
}

function jsString(s) {
  const slash = String.fromCharCode(92);
  return String(s).replaceAll(slash, slash + slash).replaceAll("'", slash + "'");
}

initGraphExplorer();
</script>
</body></html>"""

@app.get("/admin/graph", response_class=HTMLResponse)
def graph_visualizer():
    """Interactive graph visualization — Neo4j Desktop-like experience."""
    return """<!DOCTYPE html>
<html><head>
<title>em0 Knowledge Graph</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0d1117; color:#c9d1d9; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; overflow:hidden; }

  /* Toolbar */
  #toolbar { height:48px; background:#161b22; border-bottom:1px solid #30363d; display:flex; align-items:center; padding:0 16px; gap:12px; }
  #toolbar h1 { font-size:15px; color:#58a6ff; font-weight:600; white-space:nowrap; }
  #toolbar .sep { width:1px; height:24px; background:#30363d; }
  #toolbar input { background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:6px 10px; border-radius:6px; font-size:13px; }
  #toolbar input:focus { border-color:#58a6ff; outline:none; }
  #apikey { width:220px; }
  #search { width:200px; }
  #toolbar button { background:#238636; color:#fff; border:none; padding:6px 14px; border-radius:6px; cursor:pointer; font-size:13px; font-weight:500; }
  #toolbar button:hover { background:#2ea043; }
  #toolbar button.secondary { background:#30363d; }
  #toolbar button.secondary:hover { background:#484f58; }
  #toolbar .stats { font-size:12px; color:#8b949e; margin-left:auto; white-space:nowrap; }

  /* Layout */
  #main { display:flex; height:calc(100vh - 48px); }
  #graph { flex:1; position:relative; }
  #loading { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); color:#8b949e; font-size:14px; }

  /* Sidebar */
  #sidebar { width:320px; background:#161b22; border-left:1px solid #30363d; overflow-y:auto; display:none; flex-shrink:0; }
  #sidebar.open { display:block; }
  #sidebar .panel { padding:16px; border-bottom:1px solid #30363d; }
  #sidebar .panel-title { font-size:11px; text-transform:uppercase; letter-spacing:0.5px; color:#8b949e; margin-bottom:8px; font-weight:600; }
  #sidebar .node-name { font-size:18px; font-weight:600; color:#f0f6fc; margin-bottom:4px; word-break:break-word; }
  #sidebar .node-label { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:500; margin-bottom:12px; }
  #sidebar .prop-row { display:flex; justify-content:space-between; padding:4px 0; font-size:13px; border-bottom:1px solid #21262d; }
  #sidebar .prop-key { color:#8b949e; }
  #sidebar .prop-val { color:#c9d1d9; max-width:180px; word-break:break-all; text-align:right; }
  #sidebar .rel-item { padding:6px 0; font-size:13px; border-bottom:1px solid #21262d; cursor:pointer; }
  #sidebar .rel-item:hover { color:#58a6ff; }
  #sidebar .rel-arrow { color:#8b949e; font-size:11px; }
  #sidebar .close-btn { float:right; background:none; border:none; color:#8b949e; cursor:pointer; font-size:16px; padding:0 4px; }
  #sidebar .close-btn:hover { color:#f0f6fc; }

  /* Filter bar */
  #filters { padding:6px 16px; background:#161b22; border-bottom:1px solid #30363d; display:none; gap:6px; flex-wrap:wrap; align-items:center; }
  #filters.open { display:flex; }
  #filters .label-tag { padding:3px 10px; border-radius:12px; font-size:12px; cursor:pointer; border:1px solid #30363d; transition:all 0.15s; user-select:none; }
  #filters .label-tag:hover { border-color:#58a6ff; }
  #filters .label-tag.active { border-color:#58a6ff; background:#58a6ff22; }
  #filters .filter-title { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; margin-right:4px; }

  /* Zoom controls */
  #zoom-controls { position:fixed; bottom:20px; right:20px; display:flex; flex-direction:column; gap:6px; z-index:9999; }
  #zoom-controls button { width:40px; height:40px; border-radius:8px; border:1px solid #30363d; background:#161b22; color:#c9d1d9; font-size:20px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all 0.15s; box-shadow:0 2px 8px rgba(0,0,0,0.4); }
  #zoom-controls button:hover { background:#30363d; border-color:#58a6ff; color:#58a6ff; }
  #zoom-controls button:active { background:#58a6ff22; }
</style>
</head><body>

<div id="toolbar">
  <h1>em0 Knowledge Graph</h1>
  <div class="sep"></div>
  <input type="password" id="apikey" placeholder="API Key" onkeydown="if(event.key==='Enter')loadGraph()"/>
  <button onclick="loadGraph()">Load</button>
  <div class="sep"></div>
  <input type="text" id="search" placeholder="Search nodes..." oninput="searchNodes(this.value)"/>
  <button class="secondary" onclick="resetView()">Reset View</button>
  <button class="secondary" onclick="toggleFilters()">Labels</button>
  <span class="stats" id="stats"></span>
</div>

<div id="filters"></div>

<div id="main">
  <div id="graph"><div id="loading">Enter API key and press Load</div></div>
  <div id="zoom-controls">
    <button onclick="zoomIn()" title="Zoom in">+</button>
    <button onclick="zoomOut()" title="Zoom out">&minus;</button>
  </div>
  <div id="sidebar">
    <div class="panel" id="node-detail"></div>
    <div class="panel" id="node-rels"></div>
  </div>
</div>

<script>
let network, nodes, edges, allData, apiKey;
const PALETTE = [
  '#58a6ff','#f78166','#3fb950','#d2a8ff','#f0883e',
  '#79c0ff','#56d364','#ff7b72','#e3b341','#a5d6ff',
  '#bc8cff','#7ee787','#ffa657','#ff9bce','#9ecbff'
];
const labelColors = {};
let colorIdx = 0;
function colorFor(group) {
  if (!labelColors[group]) { labelColors[group] = PALETTE[colorIdx % PALETTE.length]; colorIdx++; }
  return labelColors[group];
}

async function loadGraph() {
  apiKey = document.getElementById('apikey').value;
  document.getElementById('loading').textContent = 'Loading...';
  try {
    const res = await fetch('/admin/graph-data', { headers: { Authorization: 'Bearer ' + apiKey } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    allData = await res.json();
    renderGraph(allData);
    buildFilters(allData);
    document.getElementById('loading').style.display = 'none';
  } catch (e) {
    document.getElementById('loading').textContent = 'Error: ' + e.message;
  }
}

function renderGraph(data) {
  // Count connections per node
  const connCount = {};
  data.edges.forEach(e => {
    connCount[e.from] = (connCount[e.from]||0) + 1;
    connCount[e.to] = (connCount[e.to]||0) + 1;
  });

  nodes = new vis.DataSet(data.nodes.map(n => {
    const c = colorFor(n.group);
    const conns = connCount[n.id] || 0;
    return {
      id: n.id, label: n.label, group: n.group, _props: n.properties,
      size: Math.max(12, Math.min(40, 12 + conns * 4)),
      color: { background: c, border: c+'88', highlight: { background: '#f0f6fc', border: c } },
      font: { color: '#c9d1d9', size: 13, face: '-apple-system,sans-serif' },
      borderWidth: 2, shape: 'dot'
    };
  }));
  edges = new vis.DataSet(data.edges.map((e,i) => ({
    id: 'e'+i, from: e.from, to: e.to, label: e.label, _props: e.properties,
    color: { color: '#30363d', highlight: '#58a6ff', hover: '#58a6ff' },
    font: { color: '#6e7681', size: 10, strokeWidth: 0, face: '-apple-system,sans-serif' },
    arrows: { to: { enabled: true, scaleFactor: 0.6 } },
    smooth: { type: 'continuous' }, width: 1.5, hoverWidth: 0.5
  })));

  const container = document.getElementById('graph');
  network = new vis.Network(container, { nodes, edges }, {
    physics: {
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -60, centralGravity: 0.008, springLength: 120, springConstant: 0.02, damping: 0.4 },
      stabilization: { iterations: 300, fit: true }
    },
    interaction: { hover: true, tooltipDelay: 200, zoomView: false, dragView: true, multiselect: true, navigationButtons: false, keyboard: { enabled: true, bindToWindow: false } },
    layout: { improvedLayout: data.nodes.length < 100 }
  });

  network.on('click', function(p) {
    if (p.nodes.length) showNodeDetail(p.nodes[0]);
    else closeSidebar();
  });
  network.on('doubleClick', function(p) {
    if (p.nodes.length) expandNode(p.nodes[0]);
  });

  document.getElementById('stats').textContent = data.nodes.length + ' nodes  ·  ' + data.edges.length + ' edges';
}

function showNodeDetail(nodeId) {
  const node = nodes.get(nodeId);
  if (!node) return;
  const sb = document.getElementById('sidebar');
  const c = colorFor(node.group);

  // Properties panel
  let propsHtml = '<button class="close-btn" onclick="closeSidebar()">x</button>';
  propsHtml += '<div class="panel-title">Node</div>';
  propsHtml += '<div class="node-name">' + esc(node.label) + '</div>';
  propsHtml += '<span class="node-label" style="background:' + c + '33;color:' + c + '">' + esc(node.group) + '</span>';
  const props = node._props || {};
  Object.keys(props).forEach(k => {
    if (k === 'embedding') return;
    propsHtml += '<div class="prop-row"><span class="prop-key">' + esc(k) + '</span><span class="prop-val">' + esc(String(props[k]).substring(0,80)) + '</span></div>';
  });
  document.getElementById('node-detail').innerHTML = propsHtml;

  // Relationships panel
  const connected = network.getConnectedEdges(nodeId);
  let relsHtml = '<div class="panel-title">Relationships (' + connected.length + ')</div>';
  connected.forEach(eid => {
    const edge = edges.get(eid);
    if (!edge) return;
    const otherId = edge.from === nodeId ? edge.to : edge.from;
    const other = nodes.get(otherId);
    if (!other) return;
    const dir = edge.from === nodeId;
    relsHtml += '<div class="rel-item" onclick="focusNode(\\'' + otherId.replace(/'/g,"\\\\'") + '\\')">';
    if (dir) relsHtml += esc(node.label) + ' <span class="rel-arrow">—[' + esc(edge.label) + ']—></span> <strong>' + esc(other.label) + '</strong>';
    else relsHtml += '<strong>' + esc(other.label) + '</strong> <span class="rel-arrow">—[' + esc(edge.label) + ']—></span> ' + esc(node.label);
    relsHtml += '</div>';
  });
  document.getElementById('node-rels').innerHTML = relsHtml;
  sb.classList.add('open');

  // Highlight
  network.selectNodes([nodeId]);
}

function focusNode(nodeId) {
  network.focus(nodeId, { scale: 1.2, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
  network.selectNodes([nodeId]);
  showNodeDetail(nodeId);
}

function expandNode(nodeId) {
  network.focus(nodeId, { scale: 1.5, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
  const connNodes = network.getConnectedNodes(nodeId);
  network.selectNodes([nodeId, ...connNodes]);
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  network && network.unselectAll();
}

function searchNodes(q) {
  if (!nodes) return;
  if (!q.trim()) { network.unselectAll(); return; }
  const matches = nodes.get().filter(n => n.label.toLowerCase().includes(q.toLowerCase()));
  if (matches.length) {
    network.selectNodes(matches.map(m => m.id));
    if (matches.length === 1) focusNode(matches[0].id);
  }
}

function resetView() {
  if (!network) return;
  network.fit({ animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
  network.unselectAll();
  closeSidebar();
  document.getElementById('search').value = '';
  // Reset label filters
  document.querySelectorAll('.label-tag').forEach(t => t.classList.add('active'));
  if (allData) renderGraph(allData);
}

function buildFilters(data) {
  const groups = [...new Set(data.nodes.map(n => n.group))].sort();
  const el = document.getElementById('filters');
  let html = '<span class="filter-title">Labels:</span>';
  groups.forEach(g => {
    const c = colorFor(g);
    html += '<span class="label-tag active" style="color:' + c + '" data-group="' + esc(g) + '" onclick="toggleLabel(this)">' + esc(g) + ' (' + data.nodes.filter(n=>n.group===g).length + ')</span>';
  });
  el.innerHTML = html;
  el.classList.add('open');
}

function toggleLabel(el) {
  el.classList.toggle('active');
  applyFilters();
}

function toggleFilters() {
  document.getElementById('filters').classList.toggle('open');
}

function applyFilters() {
  const active = new Set([...document.querySelectorAll('.label-tag.active')].map(t => t.dataset.group));
  const filtered = {
    nodes: allData.nodes.filter(n => active.has(n.group)),
    edges: allData.edges.filter(e => {
      const fromNode = allData.nodes.find(n => n.id === e.from);
      const toNode = allData.nodes.find(n => n.id === e.to);
      return fromNode && toNode && active.has(fromNode.group) && active.has(toNode.group);
    })
  };
  renderGraph(filtered);
}

function zoomIn() {
  if (!network) return;
  const scale = network.getScale() * 1.3;
  network.moveTo({ scale, animation: { duration: 200, easingFunction: 'easeInOutQuad' } });
}
function zoomOut() {
  if (!network) return;
  const scale = network.getScale() / 1.3;
  network.moveTo({ scale, animation: { duration: 200, easingFunction: 'easeInOutQuad' } });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
</script>
</body></html>"""
