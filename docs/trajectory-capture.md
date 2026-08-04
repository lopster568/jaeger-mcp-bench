# Trajectory capture via stream-json

The published run (`b75f18cd`) is outcome-only: `--output-format json` emits
aggregate usage, so `tool_calls` was approximated from `num_turns` (see
`claude_runner.py`). `harness/trajectory_capture.py` is the exact capture
path: it runs one trial with `--output-format stream-json`, stores the raw
event stream, and parses it into an ordered trajectory - per call: tool name,
input, result bytes entering context, error flag - plus the final answer and
usage. This is the input that trajectory metrics (steps to evidence,
per-call error rate, context bloat) need.

## Captured fixture sample (real MCP tools)

One live trial against the compose fixture (claude 2.1.220, Task 01 prompt,
`series` format), committed at `results/trajectories/01_p99_now_series/`:

| # | tool | input | result bytes | error |
|---|------|-------|--------------|-------|
| 0 | `ToolSearch` | `{"max_results": 5, "query": "select:mcp__jaeger-bench__get_service_latencies"}` | 83 | False |
| 1 | `mcp__jaeger-bench__get_service_latencies` | `{"quantile": 0.99, "service": "driver", "window_minutes": 60}` | 2445 | False |

- exact tool calls: **2** (errored: 0); tool-result bytes entering context: **2528**
- Final answer: "~249.4 ms (most recent data point: 249.39 ms) ... ranged
  from about 248.5 ms to 249.9 ms". Checked against `/api/metrics/latencies`
  directly at capture time: last point 249.389, min 248.5, max 249.86 - the
  answer matches the API to the decimal.
- Step 0 is the CLI loading the MCP tool schema on demand - itself a
  discovery cost the aggregate-only output format never showed.
- `num_turns` reported: 3, so the published run's heuristic
  `max(0, (num_turns - 1) // 2)` estimates **1** tool call where the event
  stream shows the true **2**. (An earlier no-MCP self-test showed the same
  undercount: heuristic 0, actual 1.) The event stream gives the true count
  and ordering; the heuristic cannot.

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
`trajectory.md`. The committed sample's `events.jsonl` keeps every
assistant/user/result event (the full trajectory) but drops `system/*`
events, which carry local CLI environment configuration rather than
trajectory data. Re-parse without spending tokens:

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
