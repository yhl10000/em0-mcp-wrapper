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


# ═══════════════════════════════════════════════
# Admin Endpoints
# ═══════════════════════════════════════════════


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
