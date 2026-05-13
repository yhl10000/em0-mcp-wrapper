"""Configuration — reads from environment variables, validates at startup."""

import os
import subprocess
import sys


def _detect_project_id() -> str:
    """Auto-detect project name from git remote or directory name."""
    # 1. Explicit env var wins
    env_id = os.environ.get("MEM0_USER_ID", "")
    if env_id:
        return env_id
    # 2. Try git repo name (e.g. "centauri" from "seklabsnet/centauri.git")
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if url:
            name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            if name:
                return name
    except Exception:
        pass
    # 3. Fallback to current directory name
    return os.path.basename(os.getcwd()) or "default"


MEM0_API_URL: str = os.environ.get("MEM0_API_URL", "").rstrip("/")
MEM0_API_KEY: str = os.environ.get("MEM0_API_KEY", "")
DEFAULT_USER_ID: str = _detect_project_id()
REQUEST_TIMEOUT: int = int(os.environ.get("MEM0_TIMEOUT", "90"))
INFER_MEMORIES: bool = os.environ.get("MEM0_INFER", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Safety limits
MAX_MEMORY_LENGTH: int = int(os.environ.get("MEM0_MAX_LENGTH", "50000"))


def validate() -> None:
    """Check required config, exit with clear error if missing."""
    errors = []
    if not MEM0_API_URL:
        errors.append("MEM0_API_URL")
    if not MEM0_API_KEY:
        errors.append("MEM0_API_KEY")
    if errors:
        print(
            f"ERROR: Missing required environment variables: {', '.join(errors)}\n"
            f"See: .env.example",
            file=sys.stderr,
        )
        sys.exit(1)
