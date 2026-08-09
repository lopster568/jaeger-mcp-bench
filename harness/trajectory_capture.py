"""Capture a full agent trajectory from one Claude CLI trial via stream-json.

The published run (b75f18cd) stored final answers plus aggregate usage only;
`--output-format json` emits no per-step tool events, so tool_calls had to be
approximated from num_turns (see claude_runner.py). This script is the exact
capture path: `--output-format stream-json` emits one JSON event per line,
including every tool_use and tool_result block in order, which is what
trajectory metrics (steps to evidence, per-call error rate, context bloat)
need as input.

Usage (fixture up, server built):

    python trajectory_capture.py capture \\
        --prompt "$(python -c 'import yaml;print(yaml.safe_load(open("../tasks/01_p99_now.yaml"))["prompt"])')" \\
        --format series \\
        --out ../results/trajectories/01_p99_now_series

Re-parse an existing capture without spending tokens:

    python trajectory_capture.py parse ../results/trajectories/<name>/events.jsonl
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click

import mcp_config

CLEAN_SETTINGS_PATH = Path("/tmp/bench-clean-settings.json")
CLEAN_SETTINGS = {
    "enabledPlugins": {},
    "permissions": {"allow": ["mcp__jaeger-bench__*"], "deny": []},
    "extraKnownMarketplaces": {},
}


@dataclass
class ToolStep:
    index: int
    tool: str
    input_json: str
    input_bytes: int
    result_bytes: int | None
    is_error: bool | None


@dataclass
class Trajectory:
    steps: list[ToolStep]
    tool_calls: int
    error_calls: int
    result_bytes_total: int
    final_answer: str
    num_turns: int | None
    usage: dict
    duration_ms: int | None


def _content_bytes(content) -> int:
    """Byte size of a tool_result content field (string or content-block list)."""
    if isinstance(content, str):
        return len(content.encode())
    if isinstance(content, list):
        return sum(
            len(str(block.get("text", block)).encode()) if isinstance(block, dict) else len(str(block).encode())
            for block in content
        )
    return len(json.dumps(content).encode())


def parse_events(lines: list[str]) -> Trajectory:
    steps: list[ToolStep] = []
    by_id: dict[str, ToolStep] = {}
    final_answer = ""
    num_turns = None
    usage: dict = {}
    duration_ms = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        if etype == "assistant":
            for block in (event.get("message") or {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    input_json = json.dumps(block.get("input", {}), sort_keys=True)
                    step = ToolStep(
                        index=len(steps),
                        tool=block.get("name", "?"),
                        input_json=input_json,
                        input_bytes=len(input_json.encode()),
                        result_bytes=None,
                        is_error=None,
                    )
                    steps.append(step)
                    if block.get("id"):
                        by_id[block["id"]] = step
        elif etype == "user":
            for block in (event.get("message") or {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = by_id.get(block.get("tool_use_id", ""))
                    if step is not None:
                        step.result_bytes = _content_bytes(block.get("content"))
                        step.is_error = bool(block.get("is_error", False))
        elif etype == "result":
            final_answer = event.get("result", "") or ""
            num_turns = event.get("num_turns")
            usage = event.get("usage", {}) or {}
            duration_ms = event.get("duration_ms")

    return Trajectory(
        steps=steps,
        tool_calls=len(steps),
        error_calls=sum(1 for s in steps if s.is_error),
        result_bytes_total=sum(s.result_bytes or 0 for s in steps),
        final_answer=final_answer,
        num_turns=num_turns,
        usage=usage,
        duration_ms=duration_ms,
    )


def render_markdown(traj: Trajectory, title: str) -> str:
    lines = [
        f"# Trajectory: {title}",
        "",
        f"- exact tool calls: **{traj.tool_calls}** (errored: {traj.error_calls})",
        f"- tool-result bytes entering context: **{traj.result_bytes_total}**",
        f"- num_turns: {traj.num_turns}, duration_ms: {traj.duration_ms}",
        f"- usage: `{json.dumps(traj.usage)}`",
        "",
        "| # | tool | input | result bytes | error |",
        "|---|------|-------|--------------|-------|",
    ]
    for s in traj.steps:
        input_short = s.input_json if len(s.input_json) <= 80 else s.input_json[:77] + "..."
        lines.append(
            f"| {s.index} | `{s.tool}` | `{input_short}` | {s.result_bytes} | {s.is_error} |"
        )
    lines += ["", "## Final answer", "", "> " + traj.final_answer.replace("\n", "\n> "), ""]
    return "\n".join(lines)


def _write_outputs(traj: Trajectory, out_dir: Path, title: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectory.json").write_text(
        json.dumps(asdict(traj), indent=2) + "\n"
    )
    (out_dir / "trajectory.md").write_text(render_markdown(traj, title))
    click.echo(f"wrote {out_dir}/trajectory.json and trajectory.md")
    click.echo(
        f"tool_calls={traj.tool_calls} errors={traj.error_calls} "
        f"result_bytes={traj.result_bytes_total}"
    )


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--prompt", required=True)
@click.option("--format", "format_", default="series",
              type=click.Choice(["summary", "series", "tiered", "flat"]))
@click.option("--jaeger-url", default="http://localhost:16686")
@click.option("--flat-url", default=mcp_config.DEFAULT_FLAT_URL,
              help="Streamable-HTTP MCP endpoint for the 'flat' arm's flatserver "
                   "(mirrors orchestrator.py's --flat-url; only relevant for --format flat).")
@click.option("--model", default="sonnet")
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout-sec", default=180)
@click.option("--max-budget-usd", default=0.50)
@click.option("--mcp-config-path", default=None, type=click.Path(exists=True),
              help="Use an existing MCP config instead of generating one (skips server-binary check)")
def capture(prompt, format_, jaeger_url, flat_url, model, out, timeout_sec, max_budget_usd, mcp_config_path):
    """Run one trial with stream-json and store raw events + parsed trajectory."""
    if not CLEAN_SETTINGS_PATH.exists():
        CLEAN_SETTINGS_PATH.write_text(json.dumps(CLEAN_SETTINGS, indent=2))
    cfg_path = Path(mcp_config_path) if mcp_config_path else mcp_config.write_claude_config(
        format_, jaeger_url, flat_url,
    )

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",  # required by the CLI for stream-json in print mode
        "--mcp-config", str(cfg_path),
        "--strict-mcp-config",
        "--settings", str(CLEAN_SETTINGS_PATH),
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--model", model,
        "--allow-dangerously-skip-permissions",
        "--max-budget-usd", str(max_budget_usd),
        "--no-session-persistence",
    ]

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
    elapsed = int((time.monotonic() - t0) * 1000)
    (out_dir / "events.jsonl").write_text(proc.stdout)
    if proc.returncode != 0:
        (out_dir / "stderr.txt").write_text(proc.stderr)
        # Bug fix: a nonzero exit (budget exhaustion, context overflow, etc.)
        # used to abort here before parsing - but those are exactly the
        # trajectories H3/H4 care about most (docs/arm2-design.md:
        # "budget_exhausted... outcome, not an error"). Warn and keep going
        # instead of raising, so events.jsonl still gets classified into
        # trajectory.json without manual repair; parse_events() already
        # tolerates a truncated/partial event stream (skips unparseable
        # lines), so a best-effort trajectory is still better than none.
        click.echo(
            f"warning: claude rc={proc.returncode}; stderr saved to {out_dir}/stderr.txt"
            " - attempting to parse events.jsonl anyway",
            err=True,
        )

    traj = parse_events(proc.stdout.splitlines())
    if traj.duration_ms is None:
        traj.duration_ms = elapsed
    _write_outputs(traj, out_dir, out_dir.name)


@cli.command()
@click.argument("events_file", type=click.Path(exists=True))
@click.option("--out", default=None, type=click.Path(),
              help="Output dir; defaults to the events file's directory")
def parse(events_file, out):
    """Parse an existing stream-json events file into a trajectory."""
    events_path = Path(events_file)
    lines = events_path.read_text().splitlines()
    traj = parse_events(lines)
    out_dir = Path(out) if out else events_path.parent
    _write_outputs(traj, out_dir, out_dir.name)


if __name__ == "__main__":
    cli()
