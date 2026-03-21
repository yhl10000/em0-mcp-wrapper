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
                "collection_name": "mem0_memories",
            },
        },
        "embedder": {
            "provider": "azure_openai",
            "config": {
                "model": "text-embedding-3-small",
                "azure_kwargs": {
                    "api_key": AZURE_OPENAI_KEY,
                    "azure_endpoint": AZURE_OPENAI_ENDPOINT,
                    "azure_deployment": "text-embedding-3-small",
                    "api_version": "2024-02-01",
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


def _get_memory():
    global memory, graph_enabled
    if memory is None:
        from mem0 import Memory
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
    m = _get_memory()
    try:
        all_memories = m.get_all()
        # Group by user_id
        projects: dict[str, int] = {}
        for mem in all_memories.get("results", []):
            uid = mem.get("user_id", "unknown")
            projects[uid] = projects.get(uid, 0) + 1
        return {
            "version": "4.0.0",
            "total_projects": len(projects),
            "total_memories": sum(projects.values()),
            "projects": projects,
        }
    except Exception as e:
        logger.error("stats error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Add Memory ───
@app.post("/v1/memories/")
def add_memory(req: AddMemoryRequest, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    try:
        content = req.messages[0]["content"] if req.messages else ""
        kwargs: dict[str, Any] = {
            "user_id": req.user_id,
            "metadata": req.metadata,
        }
        if req.immutable:
            kwargs["immutable"] = True
        if req.includes:
            kwargs["includes"] = req.includes
        if req.excludes:
            kwargs["excludes"] = req.excludes

        result = m.add(content, **kwargs)
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
            return results if isinstance(results, dict) else {"results": results}

        results = m.search(**kwargs)
        if isinstance(results, dict):
            return results
        return {"results": results if isinstance(results, list) else []}
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
