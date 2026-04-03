#!/usr/bin/env bash
# em0 Knowledge Layer — Setup & Update Script
#
# First time:  ./scripts/setup-em0.sh
# Update:      ./scripts/setup-em0.sh
#
# Same command for install and update — safe to run multiple times.
#
# What it does:
#   1. Installs/updates em0-mcp Python package (pipx or pip)
#   2. Registers em0 MCP server with Claude Code (global — all projects)
#   3. Adds em0 instructions to ~/.claude/CLAUDE.md
#
# Requirements:
#   - Claude Code CLI (claude command)
#   - Python 3.10+ with pipx or pip
#   - API key from team admin

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_URL="git+https://github.com/seklabsnet/em0-mcp-wrapper.git"
MEM0_URL="https://mem0-server.happygrass-15b6b68c.westeurope.azurecontainerapps.io"

echo ""
echo -e "${BLUE}═══════════════════════════════════════════${NC}"
echo -e "${BLUE}  em0 Knowledge Layer — Setup${NC}"
echo -e "${BLUE}═══════════════════════════════════════════${NC}"
echo ""

# ─── Prerequisites ───
echo -e "${YELLOW}[1/4] Checking prerequisites...${NC}"

if ! command -v claude &> /dev/null; then
    echo -e "${RED}✗ Claude Code CLI not found${NC}"
    echo "  Install: npm install -g @anthropic-ai/claude-code"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Claude Code CLI"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python 3 not found${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Python $(python3 --version | cut -d' ' -f2)"

# ─── Install / Update Package ───
echo ""
echo -e "${YELLOW}[2/4] Installing/updating em0-mcp package...${NC}"

INSTALLED=false

# Strategy: pipx first (isolated, clean), pip fallback
if command -v pipx &> /dev/null; then
    # Check if already installed via pipx
    if pipx list 2>/dev/null | grep -q "em0-mcp-wrapper"; then
        echo -e "  Updating existing pipx install..."
        pipx uninstall em0-mcp-wrapper 2>/dev/null || true
    fi
    # Prefer Python 3.13 (FastMCP has issues with 3.14+)
    PIPX_PYTHON=""
    if command -v python3.13 &> /dev/null; then
        PIPX_PYTHON="--python python3.13"
    elif command -v python3.12 &> /dev/null; then
        PIPX_PYTHON="--python python3.12"
    fi
    if pipx install $PIPX_PYTHON "$REPO_URL" 2>/dev/null; then
        INSTALLED=true
        echo -e "  ${GREEN}✓${NC} Installed via pipx (latest from git)"
    fi
fi

if [[ "$INSTALLED" != "true" ]]; then
    # pip with --force-reinstall to ensure latest
    if pip install --force-reinstall --quiet "$REPO_URL" 2>/dev/null; then
        INSTALLED=true
        echo -e "  ${GREEN}✓${NC} Installed via pip (latest from git)"
    else
        echo -e "${RED}✗ Failed to install. Try: pip install $REPO_URL${NC}"
        exit 1
    fi
fi

# Verify
if command -v em0-mcp &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} em0-mcp command available"
else
    echo -e "${YELLOW}  ⚠ em0-mcp not in PATH — you may need to restart your terminal${NC}"
fi

# ─── Register MCP Server ───
echo ""
echo -e "${YELLOW}[3/4] Registering em0 MCP server...${NC}"

# Always remove old config (might have stale MEM0_USER_ID or old key)
if claude mcp list 2>/dev/null | grep -q "em0"; then
    echo -e "  Removing old em0 config..."
    claude mcp remove em0 -s user 2>/dev/null || true
fi

# Get API key
EXISTING_KEY=""
# Try to find key from old config backup
if [[ -f "$HOME/.claude.json" ]]; then
    EXISTING_KEY=$(python3 -c "
import json
try:
    with open('$HOME/.claude.json') as f:
        d = json.load(f)
    servers = d.get('mcpServers', {})
    env = servers.get('em0', {}).get('env', {})
    print(env.get('MEM0_API_KEY', ''))
except: pass
" 2>/dev/null || echo "")
fi

if [[ -n "$EXISTING_KEY" ]]; then
    echo -e "  Found existing API key: ${EXISTING_KEY:0:10}..."
    read -p "  Use this key? (Y/n): " USE_EXISTING
    if [[ "$USE_EXISTING" == "n" || "$USE_EXISTING" == "N" ]]; then
        EXISTING_KEY=""
    fi
fi

if [[ -z "$EXISTING_KEY" ]]; then
    read -sp "  em0 API Key (get from team admin): " API_KEY
    echo ""
    if [[ -z "$API_KEY" ]]; then
        echo -e "${RED}✗ API key cannot be empty${NC}"
        exit 1
    fi
else
    API_KEY="$EXISTING_KEY"
fi

# Register (no MEM0_USER_ID — auto-detects from git repo per project)
claude mcp add em0 \
    -s user \
    -e "MEM0_API_URL=$MEM0_URL" \
    -e "MEM0_API_KEY=$API_KEY" \
    -- em0-mcp

echo -e "  ${GREEN}✓${NC} em0 registered (global — works in all projects)"
echo -e "  ${GREEN}✓${NC} user_id auto-detects from git repo name"

# ─── Global CLAUDE.md ───
echo ""
echo -e "${YELLOW}[4/4] Updating global CLAUDE.md...${NC}"

CLAUDE_MD="$HOME/.claude/CLAUDE.md"
EM0_MARKER="## em0 Knowledge Layer"

if [[ -f "$CLAUDE_MD" ]] && grep -q "$EM0_MARKER" "$CLAUDE_MD"; then
    # Remove old em0 section and rewrite (ensures latest instructions)
    python3 -c "
import re
with open('$CLAUDE_MD', 'r') as f:
    content = f.read()
# Remove old em0 section
content = re.sub(r'\n*## em0 Knowledge Layer.*?(?=\n## |\Z)', '', content, flags=re.DOTALL)
with open('$CLAUDE_MD', 'w') as f:
    f.write(content.rstrip())
" 2>/dev/null
    echo -e "  Updating existing em0 instructions..."
fi

mkdir -p "$HOME/.claude"
cat >> "$CLAUDE_MD" << 'HEREDOC'

## em0 Knowledge Layer

Bu environment'ta em0 persistent memory sistemi aktif. em0, projeler arasi bilgi grafigi ve semantic hafiza saglar.

### Session Basinda
- `search_memory` ile mevcut projenin son kararlarini ve mimarisini kontrol et
- user_id otomatik algilanir (git repo adindan)

### Calisirken
- Onemli kararlar alindiginda `add_memory` ile kaydet (domain ve type belirt)
- Bug root cause bulundugunda `add_memory` ile `immutable=true` olarak kaydet
- Bilmedigin bir konuda `search_all_projects` ile tum projelerde ara

### Metadata Standartlari
- **domain:** auth, backend, frontend, infra, ui, devops, general
- **type:** decision, architecture, business-rule, trade-off, bug-lesson, convention
- **source:** conversation, code-review, implementation, incident

HEREDOC
echo -e "  ${GREEN}✓${NC} em0 instructions in $CLAUDE_MD"

# ─── Done ───
echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ em0 setup complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo ""
echo "  Open Claude Code in any project — em0 works automatically."
echo "  user_id auto-detects from git repo name."
echo ""
echo "  Test:"
echo "    claude 'em0 da hangi projeler var?'"
echo ""
echo "  Update anytime:"
echo "    ./scripts/setup-em0.sh"
echo ""
