"""em0-setup — one-command setup for mem0 MCP integration with Claude Code.

Usage:
  em0-setup                          # interactive (prompts for missing values)
  em0-setup --user-id centauri       # set project scope
  em0-setup --api-key sk-xxx         # pass key directly
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

MEM0_DEFAULT_URL = "https://mem0-server.happygrass-15b6b68c.westeurope.azurecontainerapps.io"


def _get_claude_config_path() -> Path:
    """Find ~/.claude.json (Claude Code user config)."""
    return Path.home() / ".claude.json"


def _register_mcp(api_url: str, api_key: str, user_id: str) -> bool:
    """Write em0 MCP server config directly into ~/.claude.json."""
    config_path = _get_claude_config_path()

    # Read existing config
    if config_path.exists():
        data = json.loads(config_path.read_text())
    else:
        data = {}

    # Add/update em0 in top-level mcpServers
    if "mcpServers" not in data:
        data["mcpServers"] = {}

    data["mcpServers"]["em0"] = {
        "type": "stdio",
        "command": "em0-mcp",
        "args": [],
        "env": {
            "MEM0_API_URL": api_url,
            "MEM0_API_KEY": api_key,
            "MEM0_USER_ID": user_id,
        },
    }

    # Write back
    config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Setup mem0 MCP server for Claude Code",
        prog="em0-setup",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("MEM0_API_URL", MEM0_DEFAULT_URL),
        help=f"mem0 server URL (default: {MEM0_DEFAULT_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MEM0_API_KEY", ""),
        help="mem0 API key (or set MEM0_API_KEY env var)",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("MEM0_USER_ID", "centauri"),
        help="Project/user scope (default: centauri)",
    )
    args = parser.parse_args()

    print("=== em0 MCP Setup ===\n")

    # 1. Get API key
    api_key = args.api_key
    if not api_key:
        try:
            api_key = input("MEM0_API_KEY not set. Enter API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
    if not api_key:
        print("Error: API key required.")
        sys.exit(1)

    print(f"Server:  {args.api_url}")
    print(f"User ID: {args.user_id}")
    print()

    # 2. Check em0-mcp is available
    if not shutil.which("em0-mcp"):
        print("Warning: 'em0-mcp' not found in PATH. Make sure it's installed.")

    # 3. Register MCP server in ~/.claude.json (user scope = works in all projects)
    print("[1/2] Registering MCP server...")
    config_path = _get_claude_config_path()
    if not config_path.exists():
        print(f"Error: {config_path} not found. Install Claude Code first.")
        sys.exit(1)

    _register_mcp(args.api_url, api_key, args.user_id)
    print(f"  Written to {config_path}")
    print("  Scope: user (works in all projects)")

    # 4. Health check
    print("[2/2] Checking server health...")
    try:
        import httpx
        resp = httpx.get(f"{args.api_url}/health", timeout=10)
        data = resp.json()
        status = data.get("status", "unknown")
        version = data.get("version", "?")
        print(f"  Server: {status} (v{version})")
    except Exception:
        print("  Warning: Could not reach server (may be cold-starting)")

    print(f"\nDone! Restart Claude Code to use mem0.")
    print(f"Tools: add_memory, search_memory, list_memories, delete_memory")


if __name__ == "__main__":
    main()
