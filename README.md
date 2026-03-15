# em0-mcp-wrapper

MCP server that bridges [Claude Code](https://claude.ai/claude-code) to a self-hosted [mem0](https://github.com/mem0ai/mem0) instance.

## What it does

Provides 4 MCP tools to Claude Code:

| Tool | Purpose |
|------|---------|
| `add_memory` | Store knowledge (decisions, trade-offs, lessons) |
| `search_memory` | Semantic search across stored knowledge |
| `list_memories` | List all memories for a user/project |
| `delete_memory` | Delete a specific memory by ID |

## Quick Start

```bash
# Install
pip install git+https://github.com/seklabsnet/em0-mcp-wrapper.git

# Setup (registers MCP server with Claude Code)
em0-setup

# For a different project
em0-setup --user-id my-other-project
```

That's it. Restart Claude Code and the tools are available.

## Setup Options

```bash
em0-setup                              # interactive (prompts for API key)
em0-setup --api-key sk-xxx             # pass key directly
em0-setup --user-id my-project         # set project scope
em0-setup --api-url https://custom.url # custom server URL
```

The setup registers the MCP server at **user scope** — works in all your projects, not just one.

## Manual Registration

If you prefer to register manually instead of using `em0-setup`:

```bash
claude mcp add --scope user --transport stdio em0 \
  --env MEM0_API_URL=https://your-mem0-server.example.com \
  --env MEM0_API_KEY=$MEM0_API_KEY \
  --env MEM0_USER_ID=your-team \
  -- em0-mcp
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MEM0_API_URL` | Yes | — | mem0 server URL |
| `MEM0_API_KEY` | Yes | — | API key for authentication |
| `MEM0_USER_ID` | No | `centauri` | Default user/project scope |
| `MEM0_TIMEOUT` | No | `90` | Request timeout (seconds) |

## Development

```bash
git clone https://github.com/seklabsnet/em0-mcp-wrapper.git
cd em0-mcp-wrapper
pip install -e ".[dev]"
pytest -v
ruff check src/ tests/
```

## License

MIT
