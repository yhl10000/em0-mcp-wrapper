"""em0-setup — one-command setup for mem0 MCP integration with Claude Code.

Usage:
  em0-setup                          # interactive (prompts for missing values)
  em0-setup --user-id centauri       # set project scope
  em0-setup --api-key sk-xxx         # pass key directly
"""

import argparse
import os
import shutil
import subprocess
import sys

MEM0_DEFAULT_URL = "https://mem0-server.happygrass-15b6b68c.westeurope.azurecontainerapps.io"


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

    # 1. Check claude CLI exists
    if not shutil.which("claude"):
        print("Error: 'claude' CLI not found. Install Claude Code first.")
        sys.exit(1)

    # 2. Get API key
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

    # 3. Register MCP server with Claude Code (user scope = works in all projects)
    print("[1/2] Registering MCP server...")
    # Remove existing registration first (ignore errors if not found)
    subprocess.run(
        ["claude", "mcp", "remove", "em0", "-s", "user"],
        capture_output=True, text=True,
    )
    # Register: claude mcp add <name> <command> [options]
    result = subprocess.run(
        [
            "claude", "mcp", "add",
            "em0", "em0-mcp",
            "-s", "user",
            "-t", "stdio",
            "-e", f"MEM0_API_URL={args.api_url}",
            "-e", f"MEM0_API_KEY={api_key}",
            "-e", f"MEM0_USER_ID={args.user_id}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error registering MCP: {result.stderr.strip()}")
        sys.exit(1)

    print("  Registered (user scope — works in all projects)")

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
