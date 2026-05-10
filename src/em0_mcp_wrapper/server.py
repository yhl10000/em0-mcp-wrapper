"""MCP Server — exposes mem0 tools to Claude Code.

Usage:
  em0-mcp                           # pyproject.toml scripts entry point
  python -m em0_mcp_wrapper.server  # direct execution
"""

import json
import logging
import sys

from fastmcp import FastMCP

from . import client, config

# ─── Logging (stderr — stdout is reserved for MCP protocol) ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("em0-mcp")

# ─── Config validation ───
config.validate()

# ─── MCP Server ───
mcp = FastMCP(
    "em0-knowledge-layer",
    instructions=(
        "em0 Knowledge Layer — persistent team memory across sessions. "
        "Stores decisions, trade-offs, architecture choices, and lessons. "
        "Search at session start for context. "
        "Store important decisions with add_memory."
    ),
)


def _dump(data: dict) -> str:
    """JSON serialize — all tools return this."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _validate_content(content: str) -> str | None:
    """Validate memory content. Returns error message or None if valid."""
    if not content or not content.strip():
        return "Content cannot be empty."
    if len(content) > config.MAX_MEMORY_LENGTH:
        return f"Content too long ({len(content)} chars). Max: {config.MAX_MEMORY_LENGTH}."
    return None


# ─── Tool 1: Add Memory ───
@mcp.tool()
async def add_memory(
    content: str,
    user_id: str = "",
    domain: str = "",
    memory_type: str = "",
    source: str = "",
    immutable: bool = False,
) -> str:
    """Store knowledge in em0 persistent memory.

    Use when a decision is made, a bug root cause is found,
    a trade-off is discussed, or a business rule is shared.

    Args:
        content: The knowledge to remember
        domain: Feature area (e.g. auth, backend, frontend, infra)
        memory_type: decision, architecture, business-rule, trade-off,
            bug-lesson, user-insight, preference, convention
        source: conversation, code-review, implementation,
            story-planning, incident, documentation
        user_id: User/project scope (empty = default from config)
        immutable: If true, memory cannot be updated or merged
    """
    error = _validate_content(content)
    if error:
        return _dump({"error": error})

    uid = user_id or config.DEFAULT_USER_ID
    logger.info("add_memory: user=%s domain=%s immutable=%s", uid, domain, immutable)
    result = await client.add_memory(
        content=content,
        user_id=uid,
        metadata={"domain": domain, "type": memory_type, "source": source},
        immutable=immutable,
    )
    # Interpret empty results — mem0 returns [] when content is deduplicated
    if "results" in result and len(result["results"]) == 0:
        result["message"] = "Already known — mem0 deduplicated this (similar memory exists)."

    # Format conflict warnings for readability
    conflicts = result.get("conflicts", [])
    if conflicts:
        lines = [_dump(result), "", "POTENTIAL CONFLICTS:"]
        for c in conflicts:
            lines.append(
                f"  - Existing: \"{c['existing_memory'][:150]}\"\n"
                f"    id={c['existing_id']} similarity={c['similarity_score']}\n"
                f"    -> {c['suggestion']}"
            )
        return "\n".join(lines)

    return _dump(result)


# ─── Tool 2: Search Memory ───
@mcp.tool()
async def search_memory(
    query: str,
    user_id: str = "",
    limit: int = 5,
    filter_domain: str = "",
    filter_type: str = "",
) -> str:
    """Search em0 memory with semantic search.

    No exact match needed — searches by meaning.
    "which ORM?" will find "Prisma was chosen".

    IMPORTANT: Always call this before starting work on a feature
    to check for existing decisions.

    Args:
        query: Natural language search query
        user_id: User/project scope (empty = default from config)
        limit: Max results (default: 5)
        filter_domain: Filter by domain (e.g. "auth", "backend")
        filter_type: Filter by type (e.g. "decision", "architecture")
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("search_memory: query='%s' user=%s", query, uid)

    # Build metadata filters
    filters: dict | None = None
    if filter_domain or filter_type:
        conditions = []
        if filter_domain:
            conditions.append({"metadata.domain": filter_domain})
        if filter_type:
            conditions.append({"metadata.type": filter_type})
        filters = {"AND": conditions} if len(conditions) > 1 else conditions[0]

    result = await client.search_memory(
        query=query, user_id=uid, limit=limit, filters=filters,
    )
    # Format results for readability
    if "results" in result:
        items = result["results"]
        if not items:
            return _dump({"result": f"No memories found for '{query}'."})
        lines = [f"Found {len(items)} memory(ies) for '{query}':\n"]
        for i, m in enumerate(items, 1):
            meta = m.get("metadata", {})
            domain_tag = meta.get("domain", "?")
            type_tag = meta.get("type", "?")
            source = meta.get("source", "?")
            # Use final_score (freshness-adjusted) when available
            final = m.get("final_score")
            semantic = m.get("score", 0)
            freshness = m.get("freshness")
            score_str = (
                f"score={final:.2f} (semantic={semantic:.2f}, fresh={freshness})"
                if final is not None and freshness is not None
                else f"score={semantic:.2f}"
            )
            lines.append(
                f"{i}. [{domain_tag}/{type_tag}] {m.get('memory', '')}\n"
                f"   {score_str}"
                f" | source={source} | id={m.get('id', '?')}"
            )
        # Show graph relations if present (Neo4j enabled)
        relations = result.get("relations", [])
        if relations:
            lines.append(f"\nGraph Relations ({len(relations)}):")
            for r in relations:
                src = r.get("source", "?")
                rel = r.get("relationship", "?")
                tgt = r.get("target", "?")
                score = r.get("score", 0)
                lines.append(
                    f"  {src} ──{rel}──→ {tgt}"
                    f"  (score={score:.2f})"
                )
        return "\n".join(lines)
    return _dump(result)


# ─── Tool 2b: Search All Projects ───
@mcp.tool()
async def search_all_projects(
    query: str,
    limit: int = 5,
) -> str:
    """Search across ALL projects — no user_id needed.

    Use when you don't know which project has the information,
    or want to find knowledge across the entire em0 brain.

    Examples:
    - "What do we know about PostgreSQL?" (finds across centauri, happybrain, etc.)
    - "What auth decisions were made?" (searches all projects)
    - "What's the architecture of pal-csm?" (discovers project automatically)

    Args:
        query: Natural language search query
        limit: Max results across all projects (default: 5)
    """
    logger.info("search_all_projects: query='%s'", query)
    result = await client.search_all_projects(query=query, limit=limit)
    if "error" in result:
        return _dump(result)

    items = result.get("results", [])
    if not items:
        return (
            f"No memories found for '{query}' across "
            f"{result.get('projects_searched', 0)} projects."
        )

    lines = [
        f"Found {result.get('total_matches', 0)} result(s) for '{query}' "
        f"across {result.get('projects_searched', 0)} projects:\n"
    ]
    for i, m in enumerate(items, 1):
        meta = m.get("metadata", {})
        project = m.get("_project", m.get("user_id", "?"))
        domain = meta.get("domain", "?")
        mtype = meta.get("type", "?")
        semantic = m.get("score", 0)
        keyword_rel = m.get("_keyword_relevance")
        combined = m.get("_combined_score")
        final = m.get("final_score")
        freshness = m.get("freshness")

        if combined is not None and keyword_rel is not None:
            score_str = f"relevance={combined:.2f} (keyword={keyword_rel}, semantic={semantic:.2f})"
        elif final is not None and freshness is not None:
            score_str = f"score={final:.2f} (semantic={semantic:.2f}, fresh={freshness})"
        else:
            score_str = f"score={semantic:.2f}"

        lines.append(
            f"{i}. [{project}] [{domain}/{mtype}] {m.get('memory', '')}\n"
            f"   {score_str} | id={m.get('id', '?')}"
        )
    return "\n".join(lines)


# ─── Tool 3: List Memories ───
@mcp.tool()
async def list_memories(user_id: str = "") -> str:
    """List all stored memories.

    Args:
        user_id: User/project scope (empty = default from config)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("list_memories: user=%s", uid)
    result = await client.list_memories(user_id=uid)
    # Show graph relations if present (Neo4j enabled)
    if isinstance(result, dict) and "relations" in result:
        relations = result["relations"]
        if relations:
            result["_graph_relations_count"] = len(relations)
    return _dump(result)


# ─── Tool 4: Get Memory ───
@mcp.tool()
async def get_memory(memory_id: str) -> str:
    """Get a single memory by ID with full details.

    Args:
        memory_id: UUID of the memory (get from search or list results)
    """
    logger.info("get_memory: id=%s", memory_id)
    result = await client.get_memory(memory_id=memory_id)
    return _dump(result)


# ─── Tool 5: Update Memory ───
@mcp.tool()
async def update_memory(memory_id: str, content: str) -> str:
    """Update an existing memory's content.

    Use when a decision changes, information becomes outdated,
    or a memory needs correction. Immutable memories cannot be updated.

    Args:
        memory_id: UUID of the memory to update (get from search or list)
        content: The new content to replace the existing memory
    """
    error = _validate_content(content)
    if error:
        return _dump({"error": error})

    logger.info("update_memory: id=%s", memory_id)
    result = await client.update_memory(memory_id=memory_id, content=content)
    return _dump(result)


# ─── Tool 6: Delete Memory ───
@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete a specific memory by ID.

    Args:
        memory_id: UUID of the memory to delete (get from search or list)
    """
    logger.info("delete_memory: id=%s", memory_id)
    result = await client.delete_memory(memory_id=memory_id)
    return _dump(result)


# ─── Tool 7: Memory History ───
@mcp.tool()
async def memory_history(memory_id: str) -> str:
    """View the edit history of a memory — see how it changed over time.

    Useful for understanding why a decision evolved or was corrected.

    Args:
        memory_id: UUID of the memory (get from search or list)
    """
    logger.info("memory_history: id=%s", memory_id)
    result = await client.memory_history(memory_id=memory_id)
    if "error" in result:
        return _dump(result)
    # Format history entries
    if isinstance(result, list):
        if not result:
            return _dump({"result": "No history found for this memory."})
        lines = [f"History ({len(result)} version(s)):\n"]
        for i, entry in enumerate(result, 1):
            old = entry.get("old_memory", entry.get("previous_value", "—"))
            new = entry.get("new_memory", entry.get("new_value", "—"))
            event = entry.get("event", "UPDATE")
            ts = entry.get("created_at", entry.get("timestamp", "?"))
            lines.append(f"{i}. [{event}] {ts}")
            lines.append(f"   Before: {old}")
            lines.append(f"   After:  {new}")
        return "\n".join(lines)
    return _dump(result)


# ─── Tool 8: Stats ───
@mcp.tool()
async def memory_stats() -> str:
    """Show cross-project statistics — how many projects use mem0, memory count per project.

    Use when the user asks about mem0 usage, how many projects, or overall stats.
    """
    logger.info("memory_stats")
    result = await client.get_stats()
    if "error" in result:
        return _dump(result)
    lines = [f"mem0 Stats (v{result.get('version', '?')}):\n"]
    lines.append(f"Total projects: {result.get('total_projects', 0)}")
    lines.append(f"Total memories: {result.get('total_memories', 0)}\n")
    projects = result.get("projects", {})
    if projects:
        lines.append("Per project:")
        for name, count in projects.items():
            lines.append(f"  {name}: {count} memories")
    graph = result.get("graph", {})
    if graph:
        lines.append("\nKnowledge Graph:")
        lines.append(f"  Nodes: {graph.get('nodes', 0)}")
        lines.append(f"  Edges: {graph.get('edges', 0)}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# Graph Memory Tools (requires Neo4j on mem0 server)
# ═══════════════════════════════════════════════════


# ─── Tool 9: Get Entities ───
@mcp.tool()
async def get_entities(user_id: str = "") -> str:
    """List all entities (nodes) in the knowledge graph.

    Shows people, systems, concepts, and other entities
    extracted from stored memories.

    Args:
        user_id: User/project scope (empty = default from config)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("get_entities: user=%s", uid)
    result = await client.get_entities(user_id=uid)
    if "error" in result:
        return _dump(result)
    # Format entities for readability
    entities = result.get("results", result) if isinstance(result, dict) else result
    if isinstance(entities, list):
        if not entities:
            return _dump({"result": "No entities in graph yet."})
        lines = [f"Knowledge Graph Entities ({len(entities)}):\n"]
        for e in entities:
            name = e.get("name", e.get("entity", "?"))
            etype = e.get("type", e.get("entity_type", "?"))
            lines.append(f"  [{etype}] {name}")
        return "\n".join(lines)
    return _dump(result)


# ─── Tool 10: Get Relations ───
@mcp.tool()
async def get_relations(user_id: str = "") -> str:
    """List all relationships in the knowledge graph.

    Shows how entities are connected — who decided what,
    which service depends on which database, etc.

    Args:
        user_id: User/project scope (empty = default from config)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("get_relations: user=%s", uid)
    result = await client.get_relations(user_id=uid)
    if "error" in result:
        return _dump(result)
    relations = result.get("results", result) if isinstance(result, dict) else result
    if isinstance(relations, list):
        if not relations:
            return _dump({"result": "No relations in graph yet."})
        lines = [f"Knowledge Graph Relations ({len(relations)}):\n"]
        for r in relations:
            src = r.get("source", r.get("from", "?"))
            rel = r.get("relationship", r.get("relation", "?"))
            tgt = r.get("target", r.get("to", "?"))
            lines.append(f"  {src} ──{rel}──→ {tgt}")
        return "\n".join(lines)
    return _dump(result)


# ─── Tool 11: Search Graph ───
@mcp.tool()
async def search_graph(
    query: str,
    user_id: str = "",
    limit: int = 5,
) -> str:
    """Search using the knowledge graph (relationship traversal).

    Unlike search_memory (vector similarity), this traverses
    entity relationships. Best for questions like:
    - "What depends on PostgreSQL?"
    - "What decisions did Erkut make?"
    - "What's connected to the auth service?"

    Args:
        query: Natural language query
        user_id: User/project scope (empty = default from config)
        limit: Max results (default: 5)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("search_graph: query='%s' user=%s", query, uid)
    result = await client.search_graph(query=query, user_id=uid, limit=limit)
    if "error" in result:
        return _dump(result)
    lines = []
    # Vector results (still returned alongside)
    items = result.get("results", [])
    if items:
        lines.append(f"Memory Results ({len(items)}):\n")
        for i, m in enumerate(items, 1):
            lines.append(f"  {i}. {m.get('memory', '?')}")
    # Graph relations — the main value
    relations = result.get("relations", [])
    if relations:
        lines.append(f"\nGraph Relations ({len(relations)}):")
        for r in relations:
            src = r.get("source", "?")
            rel = r.get("relationship", "?")
            tgt = r.get("target", "?")
            score = r.get("score", 0)
            lines.append(
                f"  {src} ──{rel}──→ {tgt}"
                f"  (score={score:.2f})"
            )
    if not lines:
        return _dump({"result": f"No graph results for '{query}'."})
    return "\n".join(lines)


# ─── Tool 12: Delete Entity ───
@mcp.tool()
async def delete_entity(
    entity_name: str,
    user_id: str = "",
) -> str:
    """Delete an entity and all its relations from the knowledge graph.

    WARNING: This removes the entity node and all edges connected to it.

    Args:
        entity_name: Name of the entity to delete
        user_id: User/project scope (empty = default from config)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("delete_entity: entity=%s user=%s", entity_name, uid)
    result = await client.delete_entity(user_id=uid, entity_name=entity_name)
    return _dump(result)


# ─── Tool 13: Cross-Project Search ───
@mcp.tool()
async def search_cross_project(
    query: str,
    user_id: str = "",
    limit: int = 10,
) -> str:
    """Search for entities shared across multiple projects.

    Finds how an entity (e.g. PostgreSQL, Azure, a person) is used
    in other projects. Useful for:
    - "How is PostgreSQL configured in other projects?"
    - "What decisions has Erkut made across all projects?"
    - "Which projects use Redis?"

    Requires Neo4j graph to be enabled.

    Args:
        query: What to search for
        user_id: Current project scope (empty = default)
        limit: Max cross-project relations to return (default: 10)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("search_cross_project: query='%s' user=%s", query, uid)
    result = await client.search_cross_project(query=query, user_id=uid, limit=limit)
    if "error" in result:
        return _dump(result)

    lines = [
        f"Cross-Project Search: '{query}'\n",
        f"Current project: {result.get('current_project', '?')}",
        f"Entities in project graph: {result.get('entities_in_project', 0)}",
        f"Other projects checked: {result.get('other_projects_checked', 0)}\n",
    ]

    cross = result.get("cross_relations", [])
    if cross:
        lines.append(f"Cross-Project Relations ({len(cross)}):")
        for r in cross:
            direction = "-->" if r.get("direction") == "outgoing" else "<--"
            lines.append(
                f"  {r['entity']} {direction}[{r['relation']}]{direction} "
                f"{r['connected_to']}  (project: {r['other_project']})"
            )
    else:
        lines.append("No cross-project connections found.")

    context = result.get("search_context", [])
    if context:
        lines.append("\nSearch context from current project:")
        for c in context:
            lines.append(f"  - {c}")

    return "\n".join(lines)


# ─── Tool 14: Compact Memories ───
@mcp.tool()
async def compact_memories(
    user_id: str = "",
    dry_run: bool = True,
    min_cluster_size: int = 3,
) -> str:
    """Compact similar memories by merging them into concise summaries.

    Reduces memory bloat by grouping similar memories within the same
    domain+type and merging them with an LLM. Immutable memories are skipped.

    IMPORTANT: Run with dry_run=True first to preview what will be merged.
    Then run with dry_run=False to apply.

    Args:
        user_id: Project scope (empty = default from config)
        dry_run: True=preview only, False=actually merge (default: True)
        min_cluster_size: Minimum memories in a group to trigger compaction (default: 3)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("compact_memories: user=%s dry_run=%s", uid, dry_run)
    result = await client.compact_memories(
        user_id=uid,
        dry_run=dry_run,
        min_cluster_size=min_cluster_size,
    )
    if "error" in result:
        return _dump(result)

    lines = []
    if result.get("dry_run"):
        lines.append("COMPACTION PREVIEW (dry_run=True, nothing changed yet):\n")
    else:
        lines.append("COMPACTION APPLIED:\n")

    lines.append(f"Groups analyzed: {result.get('total_groups_analyzed', 0)}")
    lines.append(f"Memories merged: {result.get('total_merged', 0)}")
    lines.append(f"Memories saved: {result.get('memories_saved', 0)}\n")

    plan = result.get("plan", [])
    if plan:
        lines.append("Plan:")
        for p in plan:
            if result.get("dry_run"):
                lines.append(f"  [{p['group']}] {p['memories_to_merge']} memories to merge:")
                for preview in p.get("preview", []):
                    lines.append(f"    - {preview}")
            else:
                lines.append(
                    f"  [{p['group']}] merged {p.get('merged', '?')} → "
                    f"\"{p.get('into_summary', '?')}\""
                )
    else:
        lines.append("No groups eligible for compaction.")

    return "\n".join(lines)


# ─── Tool 15: Audit Graph ───
@mcp.tool()
async def audit_graph(user_id: str = "", duplicate_limit: int = 25) -> str:
    """Run a read-only graph quality audit.

    Use this before graph cleanup, compaction, or data-growth investigations.
    The audit is dry-run only and does not delete or mutate memories.

    Args:
        user_id: Project scope (empty = default from config)
        duplicate_limit: Maximum duplicate entity groups to return (default: 25)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("audit_graph: user=%s duplicate_limit=%s", uid, duplicate_limit)
    result = await client.audit_graph(user_id=uid, duplicate_limit=duplicate_limit)
    if "error" in result:
        return _dump(result)

    if not result.get("graph_enabled"):
        return _dump(result)

    summary = result.get("summary", {})
    lines = [
        "Graph Audit (dry_run=True, nothing changed):\n",
        f"Project scope: {result.get('user_id') or 'all'}",
        f"Nodes: {summary.get('nodes', 0)}",
        f"Edges: {summary.get('edges', 0)}",
        f"Isolated nodes: {summary.get('isolated_nodes', 0)}",
        f"Self-loops: {summary.get('self_loops', 0)}",
        f"Cross-project edges: {summary.get('cross_project_edges', 0)}",
    ]

    duplicates = result.get("duplicate_entities", [])
    if duplicates:
        lines.append("\nDuplicate entity keys:")
        for item in duplicates[:duplicate_limit]:
            labels = ",".join(item.get("labels", [])) or "?"
            lines.append(f"  - {item.get('key', '?')} [{labels}] x{item.get('count', 0)}")

    relation_types = result.get("relation_types", [])
    if relation_types:
        lines.append("\nTop relation types:")
        for item in relation_types[:10]:
            lines.append(f"  - {item.get('type', '?')}: {item.get('count', 0)}")

    recommendations = result.get("recommendations", [])
    if recommendations:
        lines.append("\nRecommendations:")
        for rec in recommendations:
            lines.append(f"  - {rec}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# MCP Resources (passive context, read by clients)
# ═══════════════════════════════════════════════════


# ─── Resource 1: Auto-Context ───
@mcp.resource("memory://context/{project_id}")
async def auto_context_resource(project_id: str) -> str:
    """Session-start context — relevant decisions, immutable lessons, graph relations.

    MCP clients read this resource at session start to load project context
    without manually calling search_memory. Only returns the most relevant
    memories (~500-1000 tokens), not the entire memory store.
    """
    result = await client.get_context(project_id)
    if "error" in result:
        return _dump(result)

    lines = [f"# em0 Context: {result.get('project', '?')}\n"]

    # Stats
    stats = result.get("stats", {})
    lines.append(
        f"*{stats.get('total_memories', 0)} memories, "
        f"{stats.get('immutable_count', 0)} immutable, "
        f"{stats.get('graph_relations_count', 0)} graph relations*\n"
    )

    # Recent decisions
    decisions = result.get("recent_decisions", [])
    if decisions:
        lines.append("## Recent Decisions")
        for d in decisions:
            domain = d.get("domain", "?")
            mtype = d.get("type", "?")
            fresh = d.get("freshness")
            fresh_str = f" (fresh={fresh})" if fresh is not None else ""
            lines.append(f"- [{domain}/{mtype}] {d.get('memory', '')}{fresh_str}")

    # Immutable lessons
    immutables = result.get("immutable_lessons", [])
    if immutables:
        lines.append("\n## Immutable Lessons (always apply)")
        for im in immutables:
            domain = im.get("domain", "?")
            lines.append(f"- [{domain}] {im.get('memory', '')}")

    # Graph relations
    relations = result.get("graph_relations", [])
    if relations:
        lines.append(f"\n## Key Relations ({len(relations)})")
        for r in relations:
            lines.append(f"- {r['source']} --{r['relation']}--> {r['target']}")

    return "\n".join(lines)


# ─── Resource 2: Project Summary ───
@mcp.resource("memory://project/{project_id}/summary")
async def project_summary_resource(project_id: str) -> str:
    """Project memory overview — domain distribution and key decisions."""
    result = await client.get_project_summary(project_id)
    if "error" in result:
        return _dump(result)

    lines = [f"# em0 Summary: {result.get('project', '?')}\n"]
    lines.append(f"Total memories: {result.get('total_memories', 0)}")
    lines.append(f"Last updated: {result.get('last_updated', '?')}\n")

    domains = result.get("domains", {})
    if domains:
        lines.append("## Domains")
        for d, count in sorted(domains.items(), key=lambda x: -x[1]):
            lines.append(f"- {d}: {count} memories")

    decisions = result.get("key_decisions", [])
    if decisions:
        lines.append("\n## Key Decisions")
        for d in decisions:
            lines.append(f"- {d}")

    return "\n".join(lines)


# ─── Resource 3: Graph Overview ───
@mcp.resource("memory://project/{project_id}/graph")
async def graph_overview_resource(project_id: str) -> str:
    """Knowledge graph overview — entities and relationships."""
    result = await client.get_graph_summary(project_id)
    if "error" in result:
        return _dump(result)

    lines = [f"# Knowledge Graph: {result.get('project', '?')}\n"]

    entities = result.get("entities", {})
    if entities:
        lines.append("## Entities")
        for etype, names in entities.items():
            lines.append(f"- [{etype}] ({len(names)}): {', '.join(names[:20])}")

    total = result.get("total_relations", 0)
    relations = result.get("relations", [])
    if relations:
        lines.append(f"\n## Relations ({total} total)")
        for r in relations[:20]:
            lines.append(
                f"- {r.get('source', '?')} --{r.get('relation', '?')}--> "
                f"{r.get('target', '?')}"
            )
        if total > 20:
            lines.append(f"- ... and {total - 20} more")

    return "\n".join(lines)


# ─── Entrypoint ───
def main():
    logger.info("em0 MCP wrapper v0.5.0 starting → %s", config.MEM0_API_URL)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
