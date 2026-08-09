"""Generate per-format MCP server config files for the CLIs.

Both `claude` and `gemini` accept a JSON config that registers MCP servers.
The format is similar enough that one builder serves both with a small adapter.

Two config shapes:

- **stdio** (arm 1: `summary`, `series`) - the bench server binary, invoked as
  a subprocess. Unchanged from arm 1 byte-for-byte: arm 1 must remain
  re-runnable identically.
- **streamable HTTP** (arm 2: `tiered`, `flat`) - a URL-type MCP server.
  `tiered` points at the real Jaeger v2.20.0 MCP endpoint; `flat` points at
  the flatserver's single-tool endpoint. Schemas differ per CLI and were
  verified empirically against the installed binaries (see the docstrings
  on `write_claude_config` / `write_gemini_config`), not guessed from
  training data:

    claude (2.1.226): `claude mcp add --transport http testhttp
        http://localhost:9999/mcp/ -s local` writes, under
        `projects.<cwd>.mcpServers` in `~/.claude.json`:
            {"type": "http", "url": "http://localhost:9999/mcp/"}
        `--mcp-config` JSON files use the same `mcpServers` entry shape
        (https://docs.claude.com/en/docs/claude-code/mcp#configure-mcp-servers).

    gemini (0.18.4): `gemini mcp add --transport http testhttp
        http://localhost:9999/mcp/ -s project` writes to
        `.gemini/settings.json`:
            {"mcpServers": {"testhttp": {"httpUrl": "http://localhost:9999/mcp/"}}}
        i.e. gemini uses `httpUrl`, not `url`, and no `type` key.

Both CLIs support HTTP-type MCP servers at the versions installed on this
machine, so no workaround is needed - arm 2's transport-parity requirement
(same transport, only the URL differs between `tiered` and `flat`) holds for
both CLIs.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Server alias used in mcpServers config for every format. Kept identical
# across arm 1 (stdio) and arm 2 (http) so the rest of the harness -
# gemini_runner's --allowed-mcp-server-names, trajectory_capture's
# CLEAN_SETTINGS permission ("mcp__jaeger-bench__*") - needs no changes and
# the drill-down classifier's tool-name-prefix stripping stays a single
# convention regardless of which physical server answers the calls.
SERVER_ALIAS = "jaeger-bench"

# Arm 1: stdio, the bench server binary built from server/.
STDIO_FORMATS = frozenset({"summary", "series"})
# Arm 2: streamable HTTP, either the real Jaeger MCP endpoint or flatserver.
HTTP_FORMATS = frozenset({"tiered", "flat"})

# Jaeger v2.20.0 mounts its MCP endpoint at this path off jaeger-query's base
# URL (docs/arm2-design.md: "Endpoint: http://jaeger:16686/api/ai/mcp/").
# jaeger-query serves on the same port (16686) compose already maps to the
# host, so the tiered URL is derived from --jaeger-url rather than needing
# its own flag.
TIERED_MCP_PATH = "/api/ai/mcp/"

# flatserver default: compose maps flatserver on host port 8090 and the
# server mounts MCP at /mcp (server/flatserver/main.go). Overridable via
# --flat-url.
DEFAULT_FLAT_URL = "http://localhost:8090/mcp"


def server_binary_path() -> str:
    """Resolve the bench server binary (built with `go build` in ../server)."""
    here = Path(__file__).parent
    candidate = here.parent / "server" / "jaeger-mcp-bench-server"
    if not candidate.exists():
        raise FileNotFoundError(
            f"server binary not found at {candidate}; run `cd server && go build -o jaeger-mcp-bench-server .`"
        )
    return str(candidate)


def _tiered_url(jaeger_url: str) -> str:
    return jaeger_url.rstrip("/") + TIERED_MCP_PATH


def _resolve_http_url(format_: str, jaeger_url: str, flat_url: str) -> str:
    if format_ == "tiered":
        return _tiered_url(jaeger_url)
    if format_ == "flat":
        return flat_url
    raise ValueError(f"_resolve_http_url called with non-HTTP format: {format_!r}")


def write_claude_config(
    format_: str,
    jaeger_url: str = "http://localhost:16686",
    flat_url: str = DEFAULT_FLAT_URL,
) -> Path:
    """Claude Code MCP config schema.

    stdio (summary/series): unchanged from arm 1.
    http (tiered/flat): `{"type": "http", "url": ...}`, verified against the
    installed claude 2.1.226 binary (see module docstring).

    https://docs.claude.com/en/docs/claude-code/mcp#configure-mcp-servers
    """
    if format_ in STDIO_FORMATS:
        entry = {
            "command": server_binary_path(),
            "args": ["--format", format_, "--jaeger-url", jaeger_url],
            "env": {},
        }
    elif format_ in HTTP_FORMATS:
        entry = {"type": "http", "url": _resolve_http_url(format_, jaeger_url, flat_url)}
    else:
        raise ValueError(f"unknown format: {format_!r}")

    cfg = {"mcpServers": {SERVER_ALIAS: entry}}
    return _write_temp(cfg, prefix=f"claude-mcp-{format_}-")


def write_gemini_config(
    format_: str,
    jaeger_url: str = "http://localhost:16686",
    flat_url: str = DEFAULT_FLAT_URL,
) -> Path:
    """Gemini CLI MCP config schema.

    Gemini's settings.json supports `mcpServers` similarly, but the runner
    invokes via `--allowed-mcp-server-names jaeger-bench` and the server
    must already be registered globally OR via a project-level config.
    For the harness we write to a temp settings file and point gemini at it.

    stdio (summary/series): unchanged from arm 1.
    http (tiered/flat): `{"httpUrl": ...}` - note the field name, `httpUrl`
    not `url`, and no `type` key. Verified against the installed gemini
    0.18.4 binary (see module docstring).
    """
    if format_ in STDIO_FORMATS:
        entry = {
            "command": server_binary_path(),
            "args": ["--format", format_, "--jaeger-url", jaeger_url],
        }
    elif format_ in HTTP_FORMATS:
        entry = {"httpUrl": _resolve_http_url(format_, jaeger_url, flat_url)}
    else:
        raise ValueError(f"unknown format: {format_!r}")

    cfg = {"mcpServers": {SERVER_ALIAS: entry}}
    return _write_temp(cfg, prefix=f"gemini-mcp-{format_}-")


def _write_temp(cfg: dict, prefix: str) -> Path:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    return Path(path)
