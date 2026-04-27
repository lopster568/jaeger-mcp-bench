"""Generate per-format MCP server config files for the CLIs.

Both `claude` and `gemini` accept a JSON config that registers MCP servers.
The format is similar enough that one builder serves both with a small adapter.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def server_binary_path() -> str:
    """Resolve the bench server binary (built with `go build` in ../server)."""
    here = Path(__file__).parent
    candidate = here.parent / "server" / "jaeger-mcp-bench-server"
    if not candidate.exists():
        raise FileNotFoundError(
            f"server binary not found at {candidate}; run `cd server && go build -o jaeger-mcp-bench-server .`"
        )
    return str(candidate)


def write_claude_config(format_: str, jaeger_url: str = "http://localhost:16686") -> Path:
    """Claude Code MCP config schema.

    https://docs.claude.com/en/docs/claude-code/mcp#configure-mcp-servers
    """
    cfg = {
        "mcpServers": {
            "jaeger-bench": {
                "command": server_binary_path(),
                "args": ["--format", format_, "--jaeger-url", jaeger_url],
                "env": {},
            }
        }
    }
    return _write_temp(cfg, prefix=f"claude-mcp-{format_}-")


def write_gemini_config(format_: str, jaeger_url: str = "http://localhost:16686") -> Path:
    """Gemini CLI MCP config schema.

    Gemini's settings.json supports `mcpServers` similarly, but the runner
    invokes via `--allowed-mcp-server-names jaeger-bench` and the server
    must already be registered globally OR via a project-level config.
    For the harness we write to a temp settings file and point gemini at it.
    """
    cfg = {
        "mcpServers": {
            "jaeger-bench": {
                "command": server_binary_path(),
                "args": ["--format", format_, "--jaeger-url", jaeger_url],
            }
        }
    }
    return _write_temp(cfg, prefix=f"gemini-mcp-{format_}-")


def _write_temp(cfg: dict, prefix: str) -> Path:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    return Path(path)
