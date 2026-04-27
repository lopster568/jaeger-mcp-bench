"""Run one trial through the Gemini CLI.

Equivalent invocation:

    gemini "<task prompt>" \
        --output-format json \
        --yolo \
        --allowed-mcp-server-names jaeger-bench \
        --include-directories <ws> \
        --model gemini-3-pro

The MCP server is registered via Gemini's settings.json (project or global).
We currently write the per-trial config under a temp dir and point gemini
at the temp workspace via --include-directories.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TrialResult:
    answer: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    tool_calls: int
    duration_ms: int
    raw_output: dict[str, Any]
    success: bool
    error: str | None = None


def run(
    *,
    prompt: str,
    mcp_config_path: Path,
    model: str = "gemini-2.5-pro",
    timeout_sec: int = 180,
) -> TrialResult:
    # Gemini reads its MCP config from .gemini/settings.json in cwd.
    # Stage a workspace dir, drop the config there, then run gemini in it.
    ws = Path(tempfile.mkdtemp(prefix="gemini-trial-"))
    try:
        gem_dir = ws / ".gemini"
        gem_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mcp_config_path, gem_dir / "settings.json")

        cmd = [
            "gemini",
            "--output-format", "json",
            "--yolo",
            "--allowed-mcp-server-names", "jaeger-bench",
            "--model", model,
            # Empty stdin so Gemini doesn't wait for piped input
            prompt,
        ]

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired:
            return TrialResult(
                answer="",
                input_tokens=None, output_tokens=None,
                cache_read_tokens=None, cache_creation_tokens=None,
                tool_calls=0,
                duration_ms=timeout_sec * 1000,
                raw_output={},
                success=False,
                error="timeout",
            )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if proc.returncode != 0:
            return TrialResult(
                answer="",
                input_tokens=None, output_tokens=None,
                cache_read_tokens=None, cache_creation_tokens=None,
                tool_calls=0,
                duration_ms=elapsed_ms,
                raw_output={"stderr": proc.stderr, "stdout": proc.stdout},
                success=False,
                error=f"rc={proc.returncode}",
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            return TrialResult(
                answer="",
                input_tokens=None, output_tokens=None,
                cache_read_tokens=None, cache_creation_tokens=None,
                tool_calls=0,
                duration_ms=elapsed_ms,
                raw_output={"stdout": proc.stdout},
                success=False,
                error=f"json decode: {e}",
            )

        # Gemini CLI v0.18 token schema (verified via smoke test):
        #   data.response                                   - final answer text
        #   data.stats.models.<model_name>.tokens           - {prompt, candidates, total, cached, thoughts, tool}
        #   data.stats.tools.totalCalls                     - tool call count
        models_block = (data.get("stats", {}) or {}).get("models", {}) or {}
        # The model name key may be e.g. "gemini-2.5-pro"; pick the first (we
        # only ever invoke one model per run).
        toks = {}
        if models_block:
            first_model = next(iter(models_block.values()), {}) or {}
            toks = first_model.get("tokens", {}) or {}

        tool_calls = ((data.get("stats", {}) or {}).get("tools", {}) or {}).get("totalCalls", 0) or 0

        return TrialResult(
            answer=_extract_answer(data),
            input_tokens=toks.get("prompt"),
            # 'candidates' = output tokens; sometimes 0 in CLI v0.18 if response is single token
            output_tokens=toks.get("candidates"),
            cache_read_tokens=toks.get("cached"),
            cache_creation_tokens=None,   # Gemini CLI doesn't expose cache_creation
            tool_calls=int(tool_calls),
            duration_ms=elapsed_ms,
            raw_output=data,
            success=True,
        )
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _extract_answer(data: dict) -> str:
    """Gemini CLI v0.18 places the final assistant text in `data.response`."""
    if "response" in data:
        return str(data["response"])
    for key in ("text", "result", "answer"):
        if key in data:
            return str(data[key])
    return json.dumps(data)
