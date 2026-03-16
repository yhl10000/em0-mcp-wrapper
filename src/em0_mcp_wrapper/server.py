"""MCP Server — exposes mem0 tools to Claude Code.

Usage:
  em0-mcp                           # pyproject.toml scripts entry point
  python -m em0_mcp_wrapper.server  # direct execution
"""

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

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


# ─── Tool 1: Add Memory ───
@mcp.tool()
async def add_memory(
    content: str,
    user_id: str = "",
    domain: str = "",
    memory_type: str = "",
) -> str:
    """Store knowledge in em0 persistent memory.

    Use when a decision is made, a bug root cause is found,
    a trade-off is discussed, or a business rule is shared.

    Args:
        content: The knowledge to remember (e.g. "We chose Prisma over TypeORM because...")
        domain: Feature area — home-feed, auth, poi-system, payments, social, journey, matching, notifications, settings, general, backend, frontend, devops, infra
        memory_type: Type — decision, architecture, business-rule, trade-off, bug-lesson, user-insight, preference, convention
        user_id: User/project scope (empty = default from config)
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("add_memory: user=%s domain=%s", uid, domain)
    result = await client.add_memory(
        content=content,
        user_id=uid,
        metadata={"domain": domain, "type": memory_type},
    )
    # Interpret empty results — mem0 returns [] when content is deduplicated
    if "results" in result and len(result["results"]) == 0:
        result["message"] = "Already known — mem0 deduplicated this (similar memory exists)."
    return _dump(result)


# ─── Tool 2: Search Memory ───
@mcp.tool()
async def search_memory(
    query: str,
    user_id: str = "",
    limit: int = 5,
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
    """
    uid = user_id or config.DEFAULT_USER_ID
    logger.info("search_memory: query='%s' user=%s", query, uid)
    result = await client.search_memory(query=query, user_id=uid, limit=limit)
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
            lines.append(
                f"{i}. [{domain_tag}/{type_tag}] {m.get('memory', '')}\n"
                f"   score={m.get('score', '?'):.2f} | source={source} | id={m.get('id', '?')}"
            )
        return "\n".join(lines)
    return _dump(result)


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
    return _dump(result)


# ─── Tool 4: Delete Memory ───
@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete a specific memory by ID.

    Args:
        memory_id: UUID of the memory to delete (get from search or list)
    """
    logger.info("delete_memory: id=%s", memory_id)
    result = await client.delete_memory(memory_id=memory_id)
    return _dump(result)


# ─── Tool 5: Stats ───
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
    return "\n".join(lines)


# ─── Entrypoint ───
def main():
    logger.info("em0 MCP wrapper starting → %s", config.MEM0_API_URL)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
