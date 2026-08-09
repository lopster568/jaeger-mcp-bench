"""Run one trial through the Claude Code CLI.

Equivalent invocation (Option C - Claude Pro/Max OAuth path):

    claude -p "<task prompt>" \
        --output-format json \
        --mcp-config <generated_config.json> \
        --strict-mcp-config \
        --settings /tmp/bench-clean-settings.json \
        --disable-slash-commands \
        --exclude-dynamic-system-prompt-sections \
        --model sonnet \
        --allow-dangerously-skip-permissions \
        --max-budget-usd 0.50 \
        --no-session-persistence

We can't use --bare (Pro/Max users don't have ANTHROPIC_API_KEY); instead we
override settings with empty plugins, disable slash-commands, and drop the
dynamic system-prompt sections so the trial baseline is constant across runs.
"""

from __future__ import annotations

import json
import subprocess
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
    # True iff the trial ended because --max-budget-usd was hit (result
    # envelope subtype "error_max_budget_usd") rather than a normal answer.
    # Additive field - existing aggregators read tokens/answer/etc. via
    # dict.get() and ignore unknown keys.
    budget_exhausted: bool = False


def run(
    *,
    prompt: str,
    mcp_config_path: Path,
    model: str = "sonnet",
    timeout_sec: int = 180,
    max_budget_usd: float = 0.50,
) -> TrialResult:
    clean_settings_path = "/tmp/bench-clean-settings.json"
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--settings", clean_settings_path,
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--model", model,
        "--allow-dangerously-skip-permissions",
        "--max-budget-usd", str(max_budget_usd),
        "--no-session-persistence",
    ]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
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
            budget_exhausted=False,
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # `--output-format json` emits a structured result envelope
    # (`{"type": "result", "subtype": ..., ...}`) even on controlled-failure
    # exits, not just clean successes. Verified against the installed
    # claude 2.1.226 binary: its bundled result schema is a Zod enum on
    # `subtype` with exactly these non-success values -
    #   error_during_execution | error_max_turns | error_max_budget_usd |
    #   error_max_structured_output_retries
    # - and a matching switch statement that prints "Exceeded USD budget
    # ($X)" for error_max_budget_usd specifically (distinct from
    # error_max_turns). Parse for `subtype` before branching on returncode,
    # since a controlled-failure exit may still return non-zero while still
    # printing this envelope; that lets budget/turn exhaustion get properly
    # classified instead of falling into the generic "rc=N" bucket below.
    data = None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        parse_error = e
    else:
        parse_error = None

    if isinstance(data, dict) and "subtype" in data:
        subtype = data.get("subtype")
        usage = data.get("usage", {}) or {}
        # num_turns counts conversational turns: user, assistant(tool_use)*, user(tool_result)*, assistant(answer).
        # 1 tool call ≈ 2 extra turns. Approximation: max(0, (num_turns - 1) // 2).
        num_turns = data.get("num_turns", 1) or 1
        tool_calls_approx = max(0, (num_turns - 1) // 2)
        return TrialResult(
            answer=data.get("result", "") or "",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            tool_calls=tool_calls_approx,
            duration_ms=elapsed_ms,
            raw_output=data,
            success=True,
            error=None if subtype == "success" else subtype,
            budget_exhausted=(subtype == "error_max_budget_usd"),
        )

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
            budget_exhausted=False,
        )

    if parse_error is not None:
        return TrialResult(
            answer="",
            input_tokens=None, output_tokens=None,
            cache_read_tokens=None, cache_creation_tokens=None,
            tool_calls=0,
            duration_ms=elapsed_ms,
            raw_output={"stdout": proc.stdout},
            success=False,
            error=f"json decode: {parse_error}",
            budget_exhausted=False,
        )

    # Parsed successfully, returncode 0, but no "subtype" key - unexpected
    # shape under the documented schema; fall back to the old best-effort
    # extraction rather than erroring out.
    usage = data.get("usage", {}) or {}
    num_turns = data.get("num_turns", 1) or 1
    tool_calls_approx = max(0, (num_turns - 1) // 2)
    return TrialResult(
        answer=data.get("result", ""),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        tool_calls=tool_calls_approx,
        duration_ms=elapsed_ms,
        raw_output=data,
        success=True,
        budget_exhausted=False,
    )


