# em0-mcp-wrapper

MCP server that bridges [Claude Code](https://claude.ai/claude-code) to a self-hosted [mem0](https://github.com/mem0ai/mem0) instance with **knowledge graph** support.

Built on [FastMCP 3.x](https://github.com/jlowin/fastmcp). Backed by PostgreSQL + pgvector + Neo4j.

## What it does

Provides **16 MCP tools** and **3 MCP resources** to Claude Code — persistent memory across sessions, projects, and team members.

### Memory Tools

| Tool | Purpose |
|------|---------|
| `add_memory` | Store knowledge with metadata, immutable flag, conflict detection |
| `search_memory` | Semantic search with freshness scoring + domain/type filters |
| `search_all_projects` | Search across ALL projects without knowing user_id |
| `get_memory` | Get a single memory by ID with full details |
| `update_memory` | Update an existing memory's content |
| `list_memories` | List all memories for a user/project |
| `delete_memory` | Delete a specific memory by ID |
| `memory_history` | View edit history of a memory |
| `memory_stats` | Cross-project stats with graph node/edge counts |
| `compact_memories` | Merge similar memories via LLM summarization |

### Graph Memory Tools (requires Neo4j)

| Tool | Purpose |
|------|---------|
| `get_entities` | List all entities (nodes) in the knowledge graph |
| `get_relations` | List all relationships between entities |
| `search_graph` | Search via relationship traversal ("what depends on X?") |
| `search_cross_project` | Find entities shared across multiple projects |
| `audit_graph` | Dry-run graph quality audit for duplicates, isolated nodes, and relation growth |
| `delete_entity` | Delete an entity and all its relations |

### MCP Resources (passive context)

| Resource | Purpose |
|----------|---------|
| `memory://context/{project_id}` | Auto-context at session start — recent decisions + immutable lessons + graph relations |
| `memory://project/{project_id}/summary` | Project overview — domain distribution + key decisions |
| `memory://project/{project_id}/graph` | Knowledge graph overview — entities + relations |

## Quick Start

### Team Onboarding (one command)

```bash
git clone https://github.com/seklabsnet/em0-mcp-wrapper.git
cd em0-mcp-wrapper
./scripts/setup-em0.sh
```

Script will:
1. Install `em0-mcp` Python package
2. Register em0 MCP server globally (all projects)
3. Add em0 instructions to `~/.claude/CLAUDE.md`
4. Ask for API key (get it from your team admin)

That's it. Open Claude Code in any project — em0 works automatically.

### Manual Setup

```bash
# Install
pip install git+https://github.com/seklabsnet/em0-mcp-wrapper.git

# Register MCP server (no MEM0_USER_ID — auto-detects from git repo)
claude mcp add em0 -s user \
  -e "MEM0_API_URL=https://your-mem0-server.example.com" \
  -e "MEM0_API_KEY=your-api-key" \
  -- em0-mcp
```

## v0.5.0 Intelligence Features

### Freshness Scoring
Search results are ranked by `final_score = semantic × freshness × popularity`:
- Recent memories rank higher than stale ones
- Frequently accessed memories get a popularity boost (up to 1.2x)
- Immutable memories never decay
- Minimum freshness is 0.5 (old memories still discoverable)

### Conflict Detection
When adding a memory, em0 automatically checks for contradicting existing memories:
```
add_memory("Switch to MongoDB")
→ ⚠ CONFLICT: Existing memory "We use PostgreSQL" (similarity: 0.87)
→ Consider updating that memory instead.
```
Immutable memory conflicts get extra warnings.

### Memory Compaction
Merge similar memories into concise summaries:
```
compact_memories(dry_run=True)   → preview what will be merged
compact_memories(dry_run=False)  → actually merge
```
Groups by domain+type, clusters by semantic similarity, summarizes with gpt-4o-mini.

### Cross-Project Search
Search across all projects without knowing which one has the answer:
```
search_all_projects("PostgreSQL configuration")
→ Searches centauri, happybrain, pal-cms, ... all at once
→ Returns best matches with project tags
```

### Webhook Events
Configurable notifications on memory changes:
```bash
WEBHOOK_URLS=https://hooks.slack.com/services/xxx
WEBHOOK_EVENTS=memory.created,memory.updated,memory.conflict
WEBHOOK_SECRET=whsec_xxx
```

## Multi-Project Support

Project ID is **auto-detected** from your git repo name — no config needed:

```
~/centauri/       → user_id: "centauri"
~/happybrain/     → user_id: "happybrain"
~/pal-cms/        → user_id: "pal-cms"
```

Each project gets its own memory space + knowledge graph. Cross-project search works across all.

**Priority order:**
1. `MEM0_USER_ID` env var (if set, always wins)
2. Git remote repo name (parsed from `origin` URL)
3. Current directory name (fallback)

## Usage Guide

### Starting a Session

em0 instructions in `~/.claude/CLAUDE.md` tell Claude to check existing knowledge first:
```
"what do we know about the auth module?"
→ Claude calls search_memory → returns relevant decisions with freshness scores
```

### Saving Knowledge

```
"save this: we chose PostgreSQL because ACID compliance is required"
→ add_memory with conflict detection
→ Stored in pgvector + Neo4j graph
→ Webhook notification sent (if configured)
```

For critical decisions:
```
"save as immutable: embeddings must be 1024 dimensions"
→ Cannot be updated, never decays in freshness scoring
```

### Metadata System

| Field | Values |
|-------|--------|
| **domain** | auth, backend, frontend, infra, ui, devops, general |
| **type** | decision, architecture, business-rule, trade-off, bug-lesson, convention |
| **source** | conversation, code-review, implementation, incident |

### Knowledge Graph

```
add_memory("Erkut decided to use PostgreSQL for ACID compliance")
→ Graph: erkut ──decided──→ postgresql ──required_for──→ acid_compliance
```

Explore:
```
get_entities()         → all people, systems, concepts
get_relations()        → all connections between entities
search_graph()         → traverse relationships
search_cross_project() → find shared entities across projects
```

### Graph Visualizer

Interactive Neo4j visualization at:
```
https://your-mem0-server/admin/graph
```
Enter API key → Load → explore nodes, search, filter by label.

## Environment Variables

### MCP Wrapper (local)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MEM0_API_URL` | Yes | — | mem0 server URL |
| `MEM0_API_KEY` | Yes | — | API key for authentication |
| `MEM0_USER_ID` | No | auto-detect | Override project ID |
| `MEM0_TIMEOUT` | No | `90` | Request timeout (seconds) |
| `MEM0_MAX_LENGTH` | No | `50000` | Max memory content length |

### Server (Azure Container Apps)

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | Yes | pgvector database |
| `AZURE_OPENAI_ENDPOINT/KEY` | Yes | Embeddings + LLM |
| `NEO4J_URI/USERNAME/PASSWORD` | No | Knowledge graph |
| `WEBHOOK_URLS` | No | Comma-separated webhook URLs |
| `WEBHOOK_EVENTS` | No | Event types to fire (default: created,updated,conflict) |
| `WEBHOOK_SECRET` | No | HMAC-SHA256 signing secret |
| `CONFLICT_THRESHOLD` | No | Similarity threshold (default: 0.80) |

## Architecture

```
Claude Code (any project)
  ↓ MCP (stdio)
em0-mcp-wrapper (this repo, local)
  ↓ HTTP
mem0 server (Azure Container Apps)
  ↓              ↓
PostgreSQL     Neo4j
(pgvector)     (knowledge graph)
  ↓
Webhooks → Slack / other agents
```

## Production Stats

```
Projects: 5 (centauri, pal-cms, happybrain, onboarding-survey-engine, em0-mcp-wrapper)
Memories: 278
Graph: 1082 nodes, 1039 edges
Tools: 15, Resources: 3, Tests: 45
```

## Development

```bash
git clone https://github.com/seklabsnet/em0-mcp-wrapper.git
cd em0-mcp-wrapper
pip install -e ".[dev]"
pytest -v   # 45 tests
```

Feature specs: [docs/specs/](docs/specs/)

## License

MIT
