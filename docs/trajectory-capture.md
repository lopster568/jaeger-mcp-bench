# Trajectory capture via stream-json

The published run (`b75f18cd`) is outcome-only: `--output-format json` emits
aggregate usage, so `tool_calls` was approximated from `num_turns` (see
`claude_runner.py`). `harness/trajectory_capture.py` is the exact capture
path: it runs one trial with `--output-format stream-json`, stores the raw
event stream, and parses it into an ordered trajectory - per call: tool name,
input, result bytes entering context, error flag - plus the final answer and
usage. This is the input that trajectory metrics (steps to evidence,
per-call error rate, context bloat) need.

## Validated sample

Self-test against a live CLI run (claude 2.1.220, no MCP server, one Bash
call), from `trajectory_capture.py capture`:

| # | tool | input | result bytes | error |
|---|------|-------|--------------|-------|
| 0 | `Bash` | `{"command": "echo trajectory-test-42", "description": "Echo test string"}` | 18 | False |

- exact tool calls: **1** (errored: 0)
- `num_turns` reported: 2, so the published run's heuristic
  `max(0, (num_turns - 1) // 2)` would have estimated **0** tool calls for
  this trial. The event stream gives the true count and ordering; the
  heuristic cannot.

## Running against the fixture

```bash
# fixture up (compose/) and server built (server/), then:
cd harness
python trajectory_capture.py capture \
    --prompt "<task prompt>" \
    --format series \
    --out ../results/trajectories/<name>
```

Outputs under `--out`: `events.jsonl` (raw stream), `trajectory.json`,
`trajectory.md`. Re-parse without spending tokens:

```bash
python trajectory_capture.py parse ../results/trajectories/<name>/events.jsonl
```

Notes:

- `--verbose` is required by the CLI for stream-json in print mode.
- Budget: the first turn alone can cost ~$0.2 equivalent when the local
  environment injects hook context; the default `--max-budget-usd 0.50`
  accommodates a short trial, raise it for longer tasks.
- Gemini equivalent (ordered events from the `gemini` CLI) is not implemented
  here; its `--output-format json` stats give unordered per-tool totals only.
