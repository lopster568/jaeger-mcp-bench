# jaeger-mcp-bench

A/B benchmark of MCP tool output formats for Jaeger SPM (Service Performance Monitoring) metrics. Built in response to [jaegertracing/jaeger#8409](https://github.com/jaegertracing/jaeger/issues/8409) where the maintainer requested benchmarks rather than opinion.

## What this measures

For a Jaeger MCP tool exposing `GetLatencies` / `GetCallRates` / `GetErrorRates`, two output shapes:

- **summary** - one row per service+operation, aggregated across the window: `{p50, p95, p99, call_rate, error_rate, window, step}`. Reductions: percentiles bucket-max, call rate bucket-avg, error rate call-weighted.
- **series** - per-bucket time series of `{ts, value}` per metric type. Native shape of `metricstore.Reader`.

Across **6 troubleshooting tasks**, **2 model families** (Claude Code + Gemini CLI), and **3 trials per cell** = 72 trials, captures: diagnostic correctness, tokens consumed, tool calls used, wall-clock time.

## Why CLIs instead of raw API

The maintainer's framing was "a real agent troubleshooting some issues and using this MCP tool." `claude` and `gemini` CLIs are the actual agent surfaces deployed in production by users running MCP servers. Measuring through CLIs:

- Tests the agent as deployed, not a stripped tool-use loop
- Includes the CLI's system prompt + tool descriptions in the trajectory
- Reproducible by anyone with the CLIs installed and API keys configured
- Format-driven token deltas are the signal; CLI overhead is constant across both arms of the A/B

## Layout

```
compose/        Docker stack: Jaeger + hotrod + Prometheus
fixture/        Traffic generator + Prometheus TSDB snapshot script
server/         Go MCP server (proxies Jaeger /api/metrics/*, format-switchable)
tasks/          YAML task definitions + ground-truth evaluators
harness/        Python orchestrator that drives the CLIs, resolves ground truth, and scores answers
docs/           Methodology log + potential upstream PRs catalog
results/        Committed: the published run's raw trial dumps + aggregated tables (`runs/b75f18cd/`, `tables/b75f18cd.*`, `tables/t6_final.*`)
                Re-runs land in `runs/<new_id>/` and `tables/<new_id>.*` (gitignored by default)
RESULTS.md      Headline numbers, statistical tests, bug-disclosure
```

## Prerequisites

- Docker + docker-compose
- Go 1.22+
- Python 3.11+
- `claude` CLI (with API key configured in env)
- `gemini` CLI (with API key configured in env)
- `jq` for shell-side JSON parsing

## Quickstart

```bash
# 1. Stand up the fixture
cd compose && docker-compose up -d
cd ../fixture && ./load.sh        # 5-10 minutes of traffic
./snapshot.sh                     # optional: freeze the Prometheus TSDB for archival

# 2. Build the format-switchable MCP server
cd ../server && go build -o jaeger-mcp-bench-server ./...

# 3. Capture ground truth (queries /api/metrics with the same params and
#    reductions the agent's tool uses; run while the fixture is steady)
cd ../harness && python ground_truth_resolver.py --output ../results/snapshots/gt-<timestamp>.json

# 4. Run the benchmark (writes to results/runs/<new_run_id>/)
python orchestrator.py --models claude,gemini --formats summary,series --trials 3

# 5. Aggregate (writes results/tables/<run_id>.{csv,md})
python aggregate_v2.py --run-id <new_run_id> --ground-truth ../results/snapshots/gt-<timestamp>.json
```

## Raw trial data

The 72 trial dumps behind [`RESULTS.md`](./RESULTS.md) are at [`results/runs/b75f18cd/`](./results/runs/b75f18cd/) with [`manifest.json`](./results/runs/b75f18cd/manifest.json) (seed=42, randomized cell order). Aggregated tables: [`results/tables/b75f18cd.md`](./results/tables/b75f18cd.md), [`results/tables/t6_final.md`](./results/tables/t6_final.md).

## Cost estimate

- 6 tasks × 2 formats × 2 models × 3 trials = **72 trajectories**
- Each trajectory: ~3-15 turns, ~3k-30k tokens total
- Sonnet pricing ~$3/M in, $15/M out; Gemini Pro similar order
- **Total: ~$5-30 in API spend**

## Reproducibility

The published run uses `seed=42`; the 72 raw trial JSONs, per-trial ground-truth snapshots from `/api/metrics`, and the manifest are all committed. A fresh run of the bench will produce similar (not numerically identical) metric values and similar (not numerically identical) LLM responses; expect summary-vs-series correctness gaps in the same direction and rough magnitude as run `b75f18cd`.

## Known limitations

- **Outcome-only scoring in the published run.** Trial dumps record the final answer plus aggregate usage. Claude's `--output-format json` emits no per-step tool-call sequence, so step-level trajectory metrics (steps to evidence, per-call error rate) are not computable from the stored `b75f18cd` data; Gemini dumps carry only unordered per-tool totals. The exact capture path now exists as [`harness/trajectory_capture.py`](./harness/trajectory_capture.py) (stream-json events parsed into ordered tool calls with per-step result sizes and error flags; see [`docs/trajectory-capture.md`](./docs/trajectory-capture.md)) but has not been used for a full re-run.
- **No fault injection.** All six tasks are questions over a steady-state hotrod fixture. `fixture/load.sh` reserves scenario names for it, but error injection is not implemented.
- **Regex answer scoring.** Per-task extraction regexes are brittle, and Task 01's numeric tolerance is wide enough that it rarely discriminates.
- **Known server bug preserved by design.** `SummaryRow`'s `float64` + `omitempty` drops legitimate zero values (see [`RESULTS.md`](./RESULTS.md) and `docs/potential-prs.md` PR-1); the published run measures the as-built prototype with this bug intact, and its bias direction is analyzed in the results.
- **Bonferroni correction is prose-only.** Raw p-values are compared against 0.05/4 in `RESULTS.md`; no code computes the correction.

These are the natural extension paths for a broader evaluation harness: full trajectory capture, deterministic fault scenarios with known ground-truth root causes, and structured answer contracts instead of regex extraction.

## Status

Scaffolded and run end-to-end 2026-04-26 to 2026-04-27. The 72-trial randomized run `b75f18cd` (Claude Sonnet + Gemini 2.5 Pro, seed=42) is the canonical published set; numbers in [`RESULTS.md`](./RESULTS.md) are derived from it. Methodology log: [`docs/research-log.md`](./docs/research-log.md). Server-side issues surfaced during construction: [`docs/potential-prs.md`](./docs/potential-prs.md).

## License

Apache 2.0 - see [LICENSE](./LICENSE).
