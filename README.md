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
# Install (macOS with Homebrew Python)
brew install pipx
pipx install git+https://github.com/seklabsnet/em0-mcp-wrapper.git

# Or with pip (conda, venv, or --break-system-packages)
pip install git+https://github.com/seklabsnet/em0-mcp-wrapper.git

# Setup (registers MCP server with Claude Code)
em0-setup
```

That's it. Restart Claude Code and the tools are available in **all your projects**.

## Multi-Project Support

Project ID is **auto-detected** from your git repo name — no config needed:

```
~/centauri/       → user_id: "centauri"
~/my-saas-app/    → user_id: "my-saas-app"
~/freelance/acme/ → user_id: "acme"
```

Each project gets its own isolated memory space. Same server, same DB — separated by project ID.

**Priority order:**
1. `MEM0_USER_ID` env var (if set, always wins)
2. Git remote repo name (parsed from `origin` URL)
3. Current directory name (fallback)

## Where to find your API key

`em0-setup` will ask for `MEM0_API_KEY` on first run. Here's where to find it:

| Method | Command |
|--------|---------|
| From an existing machine | `claude mcp get em0` (look for `MEM0_API_KEY=...`) |
| From Azure | `az containerapp show --name mem0-server --resource-group rg-mem0-prod --query "properties.template.containers[0].env[?name=='MEM0_API_KEY'].value" -o tsv` |
| From team | Ask whoever deployed the mem0 server |

## Setup Options

```bash
em0-setup                              # interactive (prompts for API key)
em0-setup --api-key sk-xxx             # pass key directly
em0-setup --user-id custom-id          # override auto-detection
em0-setup --api-url https://custom.url # custom server URL
```

## Manual Registration

If you prefer to register manually instead of using `em0-setup`:

```bash
claude mcp add --scope user --transport stdio em0 \
  --env MEM0_API_URL=https://your-mem0-server.example.com \
  --env MEM0_API_KEY=$MEM0_API_KEY \
  -- em0-mcp
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MEM0_API_URL` | Yes | — | mem0 server URL |
| `MEM0_API_KEY` | Yes | — | API key for authentication |
| `MEM0_USER_ID` | No | auto-detect | Override project ID (git repo name → dir name) |
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
