# jaeger-mcp-bench

A/B benchmark of MCP tool output formats for Jaeger SPM (Service Performance Monitoring) metrics. Built in response to [jaegertracing/jaeger#8409](https://github.com/jaegertracing/jaeger/issues/8409) where the maintainer requested benchmarks rather than opinion.

## What this measures

For a Jaeger MCP tool exposing `GetLatencies` / `GetCallRates` / `GetErrorRates`, two output shapes:

- **summary** - one row per service+operation, aggregated across the window: `{p50, p95, p99, call_rate, error_rate, window, step}`. Reductions: percentiles bucket-max, call rate bucket-avg, error rate call-weighted.
- **series** - per-bucket time series of `{ts, value}` per metric type. Native shape of `metricstore.Reader`.

Across **N** troubleshooting tasks, **2 model families** (Claude Code + Gemini CLI), and **3 trials per cell**, captures: diagnostic correctness, tokens consumed, tool calls used, wall-clock time.

## Why CLIs instead of raw API

The maintainer's framing was "a real agent troubleshooting some issues and using this MCP tool." `claude` and `gemini` CLIs are the actual agent surfaces deployed in production by users running MCP servers. Measuring through CLIs:

- Tests the agent as deployed, not a stripped tool-use loop
- Includes the CLI's system prompt + tool descriptions in the trajectory
- Reproducible by anyone with the CLIs installed and API keys configured
- Format-driven token deltas are the signal; CLI overhead is constant across both arms of the A/B

## Layout

```
compose/        Docker stack: Jaeger + hotrod + Prometheus
fixture/        Traffic generator + Prometheus snapshot
server/         Go MCP server (proxies Jaeger /api/metrics/*, format-switchable)
tasks/          YAML task definitions + ground-truth evaluators
harness/        Python orchestrator that drives the CLIs + scores trajectories
results/        Raw trajectory dumps + aggregated CSVs
analysis/       Post template for #8409 reply
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
./snapshot.sh                     # freeze metric state

# 2. Build the format-switchable MCP server
cd ../server && go build -o jaeger-mcp-bench-server ./...

# 3. Run the benchmark
cd ../harness && python orchestrator.py --models claude,gemini --formats summary,series --trials 3

# 4. Aggregate
python aggregate.py --input ../results/runs --output ../results/tables/bench.csv

# 5. Read the post draft
cat ../analysis/post_template.md
```

## Cost estimate

- 6 tasks × 2 formats × 2 models × 3 trials = **72 trajectories**
- Each trajectory: ~3-15 turns, ~3k-30k tokens total
- Sonnet pricing ~$3/M in, $15/M out; Gemini Pro similar order
- **Total: ~$5-30 in API spend**

## Reproducibility

All inputs are deterministic-ish:
- Fixture metrics are snapshotted (Prometheus dump) so re-runs hit identical data
- Tasks have programmatic ground-truth evaluators where possible; LLM-judge only for the open-ended task
- Random seeds where possible; report variance across trials

LLM stochasticity is the irreducible noise - that's what the trial count amortizes over.

## Status

Scaffolded 2026-04-26. Trial 6 (72-cell randomized matrix across Claude Sonnet + Gemini 2.5 Pro) ran 2026-04-27 - fully runnable end-to-end. Final results in `results/tables/t6_final.md`. Methodology details in `docs/research-log.md` (sections §1-§14.7). Discovered server-side issues catalogued in `docs/potential-prs.md`.

## License

Apache 2.0 - see [LICENSE](./LICENSE).
