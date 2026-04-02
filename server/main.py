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
from pydantic import BaseModel

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


# ─── Freshness Scoring ───

def _apply_freshness(results: list[dict]) -> list[dict]:
    """Apply temporal decay + popularity scoring to search results."""
    now = datetime.now(timezone.utc)

    for item in results:
        meta = item.get("metadata", {})
        semantic_score = item.get("score", 0)

        # Immutable memories are exempt from decay
        if meta.get("immutable"):
            item["final_score"] = semantic_score
            item["freshness"] = 1.0
            continue

        # Age calculation
        last_access = meta.get("last_accessed_at") or item.get("created_at", "")
        if last_access:
            try:
                last_dt = datetime.fromisoformat(
                    last_access.replace("Z", "+00:00")
                )
                age_days = (now - last_dt).days
            except (ValueError, TypeError):
                age_days = 180
        else:
            age_days = 180

        freshness = max(0.5, 1.0 - (age_days / 365) * 0.5)

        # Popularity bonus — frequently accessed memories are valuable
        access_count = meta.get("access_count", 0)
        popularity = min(1.2, 1.0 + access_count * 0.02)

        final_score = semantic_score * freshness * popularity
        item["final_score"] = round(final_score, 4)
        item["freshness"] = round(freshness, 3)

    # Re-sort by final_score
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return results


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

        body = _json.dumps({
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        })

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if WEBHOOK_SECRET:
            sig = hmac.new(
                WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256,
            ).hexdigest()
            headers["X-Signature-256"] = f"sha256={sig}"

        for url in WEBHOOK_URLS:
            try:
                import httpx as _httpx
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

        # Scan known projects via mem0 API (no raw SQL needed)
        known_ids = [
            "centauri", "centauri-ios", "centauri-backend",
            "happybrain", "em0-mcp-wrapper", "happy-brain",
            "pallasite", "seklabs", "default",
        ]

        projects: dict[str, int] = {}
        for uid in known_ids:
            try:
                result = m.get_all(user_id=uid)
                items = result.get("results", []) if isinstance(result, dict) else result
                count = len(items) if isinstance(items, list) else 0
                if count > 0:
                    projects[uid] = count
            except Exception:
                pass

        # Graph stats (if Neo4j enabled)
        graph_stats = {}
        if NEO4J_URI:
            try:
                from neo4j import GraphDatabase
                driver = GraphDatabase.driver(
                    NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
                )
                with driver.session() as session:
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

        return {
            "version": "5.0.0",
            "total_projects": len(projects),
            "total_memories": sum(projects.values()),
            "projects": projects,
            "graph": graph_stats,
        }
    except Exception as e:
        logger.error("stats error: %s", e, exc_info=True)
        return {
            "version": "5.0.0",
            "error": str(e),
            "error_type": type(e).__name__,
            "db_host": POSTGRES_HOST,
            "db_name": POSTGRES_DB,
            "db_user": POSTGRES_USER,
        }


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
        results = m.search(query=content, user_id=user_id, limit=3)
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


# ─── Search Memory ───
@app.post("/v1/memories/search/")
def search_memory(req: SearchRequest, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        kwargs: dict[str, Any] = {
            "query": req.query,
            "user_id": req.user_id,
            "limit": req.limit,
        }
        if req.filters:
            kwargs["filters"] = req.filters

        # Use v2 API for graph-enhanced search
        if req.api_version == "v2" and graph_enabled:
            results = m.search(**kwargs)
            # Also get graph relations
            try:
                graph_results = m.search(
                    **kwargs,
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

        results = m.search(**kwargs)
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
        search_results = m.search(
            query=req.query, user_id=current_project, limit=5,
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

        # Step 3: Search other projects for same entities
        # Get stats to find all project IDs
        all_memories = m.get_all()
        other_projects = set()
        for mem in all_memories.get("results", []):
            uid = mem.get("user_id", "")
            if uid and uid != current_project:
                other_projects.add(uid)

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
        recent = m.search(
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
            mem.get("updated_at", mem.get("created_at", ""))
            for mem in all_items
            if mem.get("updated_at") or mem.get("created_at")
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
    min_cluster_size: int = 3
    similarity_threshold: float = 0.85


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


def _cluster_by_similarity(
    m, memories: list[dict], threshold: float,
) -> list[list[dict]]:
    """Group memories into clusters by semantic similarity."""
    used = set()
    clusters = []

    for i, mem_a in enumerate(memories):
        if i in used:
            continue
        cluster = [mem_a]
        used.add(i)

        for j, mem_b in enumerate(memories):
            if j in used:
                continue
            # Use semantic search to check similarity
            try:
                results = m.search(
                    query=mem_b.get("memory", ""),
                    user_id=mem_a.get("user_id", ""),
                    limit=1,
                    filters={"id": mem_a.get("id", "")},
                )
                # Fallback: compare via direct search score
                items = results.get("results", []) if isinstance(results, dict) else results
                if items and items[0].get("score", 0) >= threshold:
                    cluster.append(mem_b)
                    used.add(j)
            except Exception:
                # Fallback: simple text overlap
                a_words = set(mem_a.get("memory", "").lower().split())
                b_words = set(mem_b.get("memory", "").lower().split())
                if a_words and b_words:
                    overlap = len(a_words & b_words) / max(len(a_words | b_words), 1)
                    if overlap >= threshold:
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
                    # LLM summarize
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
    m = _get_memory()
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


@app.get("/admin/graph-data")
def graph_data(authorization: str = Header("")):
    """Return all Neo4j nodes and relationships as JSON for visualization."""
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
            # Get all nodes with elementId (Neo4j 5.x)
            nodes_result = session.run(
                "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
            ).data()
            # Get all relationships
            rels_result = session.run(
                "MATCH (a)-[r]->(b) RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type, properties(r) AS props"
            ).data()
        driver.close()

        nodes = []
        for n in nodes_result:
            label = n["labels"][0] if n["labels"] else "Node"
            props = n["props"] or {}
            # Remove embedding vectors from props (too large)
            props.pop("embedding", None)
            name = props.get("name", props.get("id", str(n["id"])))
            nodes.append({"id": n["id"], "label": str(name), "group": label, "properties": props})

        edges = []
        for r in rels_result:
            edges.append({"from": r["source"], "to": r["target"], "label": r["type"], "properties": r["props"] or {}})

        return {"nodes": nodes, "edges": edges}
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


from fastapi.responses import HTMLResponse


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
    interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true, multiselect: true, navigationButtons: false, keyboard: { enabled: true } },
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

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
</script>
</body></html>"""
