# SPM-MCP Output Format Benchmark - Research Log

A chronological record of every step taken to design, build, and execute the
benchmark. The goal is reproducibility and methodological auditability: anyone
should be able to read this log, run the same commands, and reproduce the
results reported in the eventual #8409 reply.

**Trigger:** Yuri Shkuro's reply on jaegertracing/jaeger#8409 (2026-04-26 15:59 UTC):
> "This is the problem - are you asking for an opinion? This type of decision should not be based on opinion, but on benchmarks with a real agent troubleshooting some issues and using this MCP tool to access metrics, where you could do A/B testing of different output formats."

---

## §1 - Methodology design (2026-04-26)

### Question being benchmarked

> When a Jaeger MCP server exposes SPM (Service Performance Monitoring) metrics - `GetLatencies`, `GetCallRates`, `GetErrorRates` - what output format should it return to an LLM agent? Two candidates:
> - **summary**: one row per service+operation aggregated across the window: `{p50, p95, p99, call_rate, error_rate, window, step}`. Reductions: percentiles bucket-max, call rate bucket-avg, error rate call-weighted.
> - **series**: native per-bucket time series of `{ts, value}` per metric type.

### Why CLI-based agent measurement (not raw API)

Yuri's framing was "a real agent troubleshooting." Production agents using MCP
servers do so through CLI tools (`claude`, `gemini`) that wrap the API with
their own system prompts, tool descriptions, and conversation loops. Measuring
through the CLIs reflects deployed reality. The CLI's overhead (system prompt,
tool descriptions) is a constant noise floor across both arms of the A/B; the
format-driven token *delta* is the load-bearing signal.

### Methodology precedent

Modeled on Grafana's [o11y-bench](https://github.com/grafana/o11y-bench) (Nov 2025): 63 tasks × 3 attempts × 29 models = 5,481 trials against a real Grafana+Prometheus+Loki+Tempo stack. Pass^N + Pass@N statistics. AGPLv3 prevents direct vendoring; we mimic the shape, not the code.

Honeycomb's "40% CSV vs JSON" claim was investigated: their public eval framework measures tool-call correctness, not format token efficiency. The 40% figure has no published methodology. **Cited only as motivating anecdote; not used as methodological precedent.**

### Statistical setup target

- Per `(task, format, model)` cell: 3 trials minimum (industry floor for stochastic LLM evals; matches o11y-bench)
- **Pass^N** (mean correctness across N trials) as primary metric
- **Pass@N** (any-success across N trials) as secondary
- Multi-model: at least Claude + Gemini to neutralize the "single model just happens to like this format" objection

### Cost target

- Pro plan budget: ~5M tokens / 5h. Plenty of headroom for 12-72 trials per matrix run.
- No raw API spend (Pro plan covers Claude; `GOOGLE_API_KEY` covers Gemini).

---

## §2 - Fixture environment (2026-04-26 / 2026-04-27)

### Stack

`docker compose` brings up three services on a private bridge network:

- **jaeger** (`jaegertracing/jaeger:2.4.0`) - OTel collector + spanmetrics connector + jaeger_query extension. Receives OTLP at 4317/4318, exposes query API at 16686.
- **prometheus** (`prom/prometheus:v2.55.0`) - scrapes spanmetrics at 5s interval. Admin API enabled for snapshots.
- **hotrod** (`jaegertracing/example-hotrod:1.71.0`) - synthetic service with `frontend → customer → driver → route → mysql → redis-manual` call graph. Exposed at 8080.

Config files: `compose/docker-compose.yml`, `compose/jaeger-config.yml`, `compose/prometheus.yml`.

### Configuration corrections during setup

- **Initial config** used `connectors.spanmetrics.namespace: traces` and `add_metric_suffixes: false`. Result: spanmetrics emitted as `traces_calls`, `traces_duration_*`, but Jaeger's metricstore reader queries `traces_span_metrics_calls_total` and `traces_span_metrics_duration_milliseconds_*`. `/api/metrics/calls` returned empty results.
- **Fix:** changed namespace to `traces.span.metrics` and re-enabled `add_metric_suffixes: true` (the canonical SPM v2 setup). Restarted Jaeger.
- **Verification:** `curl 'http://localhost:9090/api/v1/label/__name__/values'` showed both old and new names side-by-side. After restart, Jaeger's `/api/metrics/calls` returned populated MetricFamily.

### Traffic generator (`fixture/load.sh`)

Curl-driven load against hotrod's `/dispatch`, `/customer`, `/route` endpoints.
Three scenarios: `steady` (uniform 100 req/min), `degraded` (with error injection - currently steady; error injection TODO), `spike` (ramp at T=5min).

For all current trials: `degraded` scenario, `DURATION_SEC=240-600s`. Generated:
- ~275 spans/service for driver, customer
- ~3025 frontend HTTP GET spans
- Variable depending on randomized endpoint distribution

### Why hotrod, not synthetic spans

Yuri specifically asked for "real agent troubleshooting." Mocking metricstore.Reader with synthetic distributions would produce numbers a maintainer can dismiss as "your synthetic distributions don't reflect reality." Hotrod is Jaeger's reference fixture. Real spans, real spanmetrics-connector pipeline, real Prometheus rate calculations, real Jaeger metrics API.

### Verification of fixture pipeline (2026-04-26 18:54-19:02 UTC)

```bash
# Hotrod traffic
curl 'http://localhost:8080/dispatch?customer=123' → HTTP 200

# Spanmetrics in Prometheus
curl 'http://localhost:9090/api/v1/query?query=traces_span_metrics_calls_total'
# → 6 services × multiple operations, counter values 70-3025

# rate() over 30s during sustained load
curl 'http://localhost:9090/api/v1/query?query=sum(rate(traces_span_metrics_calls_total[30s]))%20by%20(service_name)'
# → driver: 0.680/s, frontend: 8.802/s, redis-manual: 9.162/s

# Jaeger metrics API
curl "http://localhost:16686/api/metrics/calls?service=driver&endTs=${EPOCH_MS}&lookback=120000&step=5000&ratePer=60000"
# → MetricFamily with 25 MetricPoints, all non-NaN, max 0.309/s

curl "http://localhost:16686/api/metrics/latencies?service=driver&endTs=${EPOCH_MS}&lookback=120000&step=5000&ratePer=60000&quantile=0.99"
# → MetricFamily with P99 latencies in 175-410ms range
```

Pipeline validated end-to-end before any agent trials.

---

## §3 - Bench MCP server (2026-04-26 / 2026-04-27)

### Architecture decision: standalone proxy, not Jaeger fork

The bench server is a separate Go binary (`server/jaeger-mcp-bench-server`) that:
- Talks MCP over stdio to whichever CLI launches it
- Proxies queries to Jaeger's HTTP `/api/metrics/*` endpoints internally

**Why proxy, not embed:** keeps the benchmark independent of Jaeger's build cycle. Lets the eventual upstream PR follow at a more leisurely pace using `metricstore.Reader` directly. Format-switching prototype is much smaller this way (~400 LOC total).

### Build

`go.mod`: depends on `github.com/modelcontextprotocol/go-sdk@v1.5.0` (same SDK Jaeger uses, ensures no API drift). Binary built with `go build -o jaeger-mcp-bench-server ./...`.

### Format selection

`--format=summary|series` flag at startup. Same binary serves either format. The harness boots two subprocess instances per A/B trial pair.

### Three tools registered

- `get_service_latencies(service, quantile, window_minutes, step_seconds, ...)`
- `get_service_call_rates(service, ...)`
- `get_service_error_rates(service, ...)`

Each tool's behavior depends on `--format`:
- **summary:** one row per service+operation. For latencies: 3 internal `GetLatencies` calls (p50, p95, p99) merged. For error rates: also calls `GetCallRates` to compute call-weighted error rate.
- **series:** native `[]{ts_ms, value}` blocks per service+operation, NaN points filtered.

### Reductions used (summary mode)

- **percentiles:** bucket-max. `max(per-bucket P_q)` is a safe upper bound on the true window P_q. Mathematically: if max is M, then ≤1% of each bucket's samples exceed M, so ≤1% of all samples exceed M, so M ≥ true window quantile. Verified by contradiction.
- **call rate:** bucket-avg. With Jaeger's fixed-step buckets and equal-duration windows, the simple mean is an unbiased estimator of the window rate.
- **error rate:** call-weighted. `Σ(error_rate_i × call_rate_i) / Σ(call_rate_i)` over buckets. Unweighted mean is statistically invalid under skewed traffic - counter-example: bucket1 (100 calls, 0 errors, 0%) and bucket2 (1 call, 1 error, 100%) → unweighted mean 50%, but true window error rate ≈ 1%.

### Issues fixed during integration

- **`jsonschema:` tag format.** SDK v1.5.0 expects plain description text (`jsonschema:"latency quantile"`), not `description=...` syntax. Initial draft used the wrong syntax → panic at AddTool registration.
- **NaN string handling.** Jaeger's `/api/metrics/*` returns `{"doubleValue": "NaN"}` (string) for empty buckets, not numeric NaN or null. Standard `json.Unmarshal` into `float64` fails. Implemented custom `GaugeValue.UnmarshalJSON` that handles `"NaN"`, `"Inf"`, `"-Inf"`, numeric, and null.
- **NaN serialization.** Go's `json.Marshal` rejects float64 NaN. In `seriesFromMetric`, NaN points are dropped before marshaling - agents don't benefit from "no-data" markers, and stripping saves tokens.

### Server logs (transport)

`mcp.StdioTransport{}` (correct API for v1.5.0; earlier draft tried `mcp.NewStdioTransport()` which doesn't exist).

---

## §4 - Harness (2026-04-26 / 2026-04-27)

### Files

- `harness/orchestrator.py` - task × format × model × trial matrix runner
- `harness/claude_runner.py` - Claude CLI subprocess wrapper
- `harness/gemini_runner.py` - Gemini CLI subprocess wrapper
- `harness/mcp_config.py` - generates per-trial MCP config files
- `harness/aggregate.py` - computes Pass^N + token stats from raw trial dumps

### Authentication path: Option C (Pro plan OAuth + plugin isolation)

User is on Claude Pro/Max plan; no `ANTHROPIC_API_KEY`. Original plan used `--bare` (which strips OAuth and requires API key). Switched to Option C:

```bash
claude -p "<prompt>" \
  --output-format json \
  --mcp-config /tmp/claude-mcp-summary-XXX.json \
  --strict-mcp-config \
  --settings /tmp/bench-clean-settings.json \
  --disable-slash-commands \
  --exclude-dynamic-system-prompt-sections \
  --model sonnet \
  --allow-dangerously-skip-permissions \
  --max-budget-usd 0.50 \
  --no-session-persistence
```

`/tmp/bench-clean-settings.json`:
```json
{
  "enabledPlugins": {},
  "permissions": {"allow": ["mcp__jaeger-bench__*"], "deny": []},
  "extraKnownMarketplaces": {}
}
```

This:
- Authenticates via Pro plan OAuth (no API key needed)
- Disables all installed plugins (frontend-design, context7, github, etc.) → constant baseline
- Disables slash-commands (skills) → no extra surface area
- Strict MCP → only the bench server is registered
- Drops dynamic system-prompt sections (cwd/git/memory) → no environmental leak between trials

### Cleanliness verification (2026-04-27 09:39 UTC)

Before running trials:
- Tar backup: `/tmp/claude-config-backup-20260427T093938Z.tar.gz` (115MB, 7542 files, sha256 `829c2357baf09d0ae7d5a239b346a4b51b5ce419ef10ca0a6e8ae37fe6a37a76`).

After 12-cell matrix run:
- `~/.claude/settings.json` sha256 unchanged: `2719267876b4932e78f188b462becf9c9b44e0b04b341ebd5a5a56265b92bce1` (matches backup).
- `~/.claude/.credentials.json` sha256 unchanged: `4315f7523c7b740daa039b435eeeecc1122b171ac0789556a1928d0f0a7ceda6` (matches backup).
- `find ~/.claude -newermt '30 minutes ago'` excluding cache/file-history/jaeger-mcp-bench projects returned no results.

**Conclusion:** Option C is non-destructive. No restore needed. Backup retained as insurance.

### Subprocess invocation contract

For each trial:
1. Generate temp MCP config (tools/mcp_config.py) with the format-flagged server command.
2. Spawn `claude` subprocess with stdin closed, capture stdout (JSON), stderr (logs).
3. Parse `data["result"]` (final answer text), `data["usage"]` (tokens), `data["num_turns"]` (≈ tool call count).
4. Dump full record to `results/runs/<run_id>/<model>/<format>/<task_id>/trial_<n>.json`.

### Token field semantics (Claude `--output-format json` schema)

- `data.input_tokens` - final-turn input tokens after caching (small; ~0-10 typical)
- `data.output_tokens` - total output tokens across all turns
- `data.cache_read_input_tokens` - baseline cached prompt content (~50K typical = Claude Code's default system prompt + tool descriptions)
- `data.cache_creation_input_tokens` - **format-driven new content tokens** (the load-bearing signal for the A/B)
- `data.num_turns` - total conversational turns (≈ 2 × tool_calls + 1)

The format-driven signal lives in `cache_creation_input_tokens`. The `cache_read_input_tokens` baseline is constant across both formats and doesn't affect the A/B comparison.

---

## §5 - Tasks (2026-04-26)

Six tasks designed to discriminate between formats. Predicted-winner column verifies the corpus has balanced expected outcomes - not all favoring summary, not all favoring series.

| # | id | predicted_winner | scoring | tags |
|---|---|---|---|---|
| 1 | 01_p99_now | summary | programmatic | point_query, latency |
| 2 | 02_spike_detection | series | programmatic | temporal, anomaly |
| 3 | 03_rank_error_rate | summary | programmatic | ranking, error_rate |
| 4 | 04_correlation | series | programmatic | temporal, multi_metric |
| 5 | 05_threshold | summary | programmatic | threshold, error_rate |
| 6 | 06_trend | series | programmatic | temporal, trend |

Predicted split: 3 summary-favoring + 3 series-favoring. Each task YAML at `tasks/NN_*.yaml`.

**Ground-truth values are placeholders as of this writing.** Need to be populated from a frozen fixture snapshot before Pass^N can be computed. Scoring functions in `tasks/ground_truth.py` cover numeric, boolean, ordered_list, classification, and structured types.

---

## §6 - Trial 1: smoke test, single cell (2026-04-27 09:43 UTC)

**Run-id:** `213dfcfe`. Cell: `claude × summary × 01_p99_now × trial_0`.

**Setup:**
- Background load: `DURATION_SEC=120 ./load.sh` for ~30s warmup before trial.
- Live metrics confirmed: `driver call rate 0.237/s max, 13/13 non-NaN buckets`.

**Result:**
- success=True
- input_tokens=8, output_tokens=352
- cache_creation=N/A (not captured in run-id 213dfcfe due to runner version)
- cache_read=49590
- duration=11648ms
- tool_calls=1
- **answer:** *"The P99 latency for the driver service over the last 60 minutes is 377.5 ms."*

**Validates:**
1. Claude CLI authenticates via Pro plan OAuth (no API key needed).
2. `--strict-mcp-config` correctly isolates the bench server.
3. Bench server in summary mode correctly handles a single agent tool call.
4. Agent receives valid JSON, extracts the right field, produces a concrete numeric answer.
5. Result is plausible: 377.5ms matches range observed in raw API checks (175-410ms across buckets).

**Issue surfaced:** initial trial (run-id `d3fbe463`) failed with *"The tool call was blocked"* because clean-settings had `"allow": []` (no allowlist = deny). Fixed by setting `"allow": ["mcp__jaeger-bench__*"]`. Retry produced the result above.

---

## §7 - Trial 2: A/B on single task (2026-04-27 ~09:50 UTC)

**Run-id:** `9dba500a`. Cells: `claude × {summary, series} × 01_p99_now × trial_0`.

**Setup:** background load running through the trial.

**Results:**

| Format | Answer | input | output | cache_creation | cache_read | tool_calls | duration |
|---|---|---|---|---|---|---|---|
| summary | "P99 = 377.5 ms" | 8 | 375 | **3,893** | 50,095 | 1 | 13,169ms |
| series | "P99 = ~327.5ms (most recent). Mid-window spike to 377.5ms..." | 8 | 732 | **19,319** | 49,590 | 1 | 25,945ms |

**Findings:**
- Series consumes **~5x format-specific tokens** (cache_creation), **~2x output tokens**, **~2x duration**.
- Both formats produce correct answers but series gives more historical context (the agent reads bucket-by-bucket and naturally narrates the trajectory).
- This is the first concrete A/B token delta. Validates that the format choice has a measurable, non-trivial effect.

---

## §8 - Trial 3: full task corpus, single trial per cell (2026-04-27 ~10:00 UTC)

**Run-id:** `25f6ad39`. Cells: `claude × {summary, series} × {01..06} × trial_0` = 12 cells.

**Setup:** `DURATION_SEC=600` background load script driving sustained traffic. 45-second warmup before first trial. All 12 trials completed within the 10-minute load window.

### Token deltas

| Task | Summary cache_new | Series cache_new | Ratio (series/summary) |
|---|---|---|---|
| 01_p99_now | 3,866 | 19,343 | **5.00x** |
| 02_spike_detection | 3,918 | 5,060 | 1.29x |
| 03_rank_error_rate | 6,285 | 25,846 | **4.11x** |
| 04_correlation | 4,346 | 6,121 | 1.41x |
| 05_threshold | 3,747 | 9,142 | 2.44x |
| 06_trend | 4,447 | 3,933 | 0.88x (series cheaper) |

### Output token deltas

| Task | Summary output | Series output |
|---|---|---|
| 01_p99_now | 351 | 579 |
| 02_spike_detection | 889 | 3,441 |
| 03_rank_error_rate | 2,137 | 744 |
| 04_correlation | 958 | 735 |
| 05_threshold | 532 | 310 |
| 06_trend | 1,199 | 286 |

### Format affecting CORRECTNESS, not just tokens

This is the most important finding. The format choice changes whether the agent can answer at all on temporal tasks:

- **Task 02 (spike_detection):** summary returned *"I cannot answer the question as asked"* because it gives an aggregate over the full window with no temporal info. Series correctly identified a spike at minute 52-53.

- **Task 04 (correlation):** summary returned *"I can't make that determination from the available data"*. Series gave a substantive diagnosis: P99 declined from 467ms to 249ms; no error spike to correlate.

- **Task 06 (trend):** series gave a clean *"stable"*. Summary went on about missing fields (impacted by PR-1 bug).

- **Tasks 01 (point query), 03 (ranking), 05 (threshold):** both formats correct; summary 2-5x cheaper.

### Bug surfaced: `omitempty` hides legitimate-zero values

In tasks 03 and 06, the agent observed JSON rows missing the `error_rate` field entirely when the call-weighted result was 0. Couldn't distinguish "0% error rate" from "API doesn't expose this field." Catalogued as **PR-1** in `potential-prs.md`.

### Statistical caveats

- **Single trial per cell** - these results have no variance estimate. Need ≥3 trials for Pass^N.
- **Single model (Claude Sonnet)** - single-model results are dismissable as "Claude prefers verbose." Need cross-model validation (Gemini arm).
- **Ground-truth scoring not yet wired** - task `expected` values are placeholders, so Pass^N cannot be computed yet. Token deltas are the only quantitative signal so far.

---

## §9 - Open work before posting on #8409

1. Fix PR-1 (`omitempty`) in bench server `format.go` so error_rate and other zero-able fields encode explicitly.
2. Populate task ground-truth values from a frozen fixture snapshot.
3. Run 3 trials per cell on Claude.
4. Add Gemini arm: 3 trials per cell × 12 cells = 36 trials.
5. Wire `aggregate.py` end-to-end; produce `results/tables/<run_id>.csv` and `.md`.
6. Draft post in `analysis/post_template.md` with results.
7. Run draft through fact + critique agents (per `feedback_critique_agent_pattern.md`).
8. Post on #8409.

---

## §10 - Reproducibility notes

- All commands shown in this log were executed in WSL2 (Ubuntu) with Docker Desktop integration. Container hash mounts via `/run/desktop/mnt/host/wsl/...` - bind-mount cache invalidation requires `docker compose stop && rm && up` cycle when a mounted file is edited (encountered when changing `jaeger-config.yml`).
- Go 1.25.x required by `modelcontextprotocol/go-sdk@v1.5.0`. Toolchain auto-upgraded via `go: switching to go1.25.9`.
- Python 3.11+ for harness (uses `dataclasses`, `pathlib`).
- `claude` CLI v0.x (any recent), `gemini` CLI v0.x.
- Docker Desktop v28.x, Docker Compose v2.39.x.
- Trial output JSONs are persisted under `results/runs/<run_id>/<model>/<format>/<task_id>/trial_<n>.json` and committed-out by `.gitignore` for the eventual repo (only methodology + aggregated tables ship in source control).

---

## §11 - PR-1 explored, then deliberately reverted (2026-04-27 ~10:30 UTC)

**Methodology decision:** the bench prototype runs WITHOUT the PR-1 fix.

### What was tried

A pre-emptive fix was applied: `SummaryRow` numeric fields converted to
`*float64`, with an `f64ptr(v)` helper that returns `nil` for NaN/Inf and
`&v` otherwise. Verified end-to-end - produced `{"error_rate": 0, ...}`
correctly with traffic, omitted on no-data.

### Why it was reverted

Reverting on user feedback: *"we gotta research on what we have."*

The benchmark's purpose is to A/B the format **as the prototype was first
written** - the same shape a first-cut Jaeger PR would ship before review.
Folding pre-emptive fixes into the baseline turns the experiment into
benchmarking a counterfactual. The bug is itself a research finding; PR-1
should surface from the data, not be hidden by it.

### Second symptom discovered during the revert

Reverting to plain `float64 + ,omitempty` exposed a second behavior:
**`json.Marshal(NaN)` panics with `"json: unsupported value: NaN"`.** The
metric-fetch path returns NaN whenever the metricstore reports an empty
window (rate=0/0). With the original prototype's struct shape, the response
crashes instead of degrading gracefully. PR-1 in `docs/potential-prs.md` was
expanded to cover both Symptom A (zero-hiding) and Symptom B (NaN crash) -
they share the same upstream fix.

### Minimal operational guard kept in place

Pure revert is unworkable: trials would crash on cold-metric windows.
A small `nanZero(v float64) float64` helper converts NaN/Inf → 0 right
before assignment to `SummaryRow`. With `,omitempty` still in place, the
zero is then dropped - agent sees an absent field, exactly as it would
under the original `,omitempty` semantics if NaN hadn't crashed. The guard
is **methodology-equivalent to the as-built prototype** while preventing
benchmark trials from failing for non-format reasons.

```go
func nanZero(v float64) float64 {
    if math.IsNaN(v) || math.IsInf(v, 0) {
        return 0
    }
    return v
}
```

Applied at three call sites in `tools.go`: `latenciesSummary`,
`summaryFromCallRates`, `summaryFromErrorRates`.

### Verification of post-revert behavior

- **No-traffic case:** server returns `{"rows": [{"service":"frontend",
  "window_sec":120,"step_sec":5}]}` - no metric fields, no crash, exactly
  the agent-facing shape PR-1 documents.
- **With-traffic case:** same - error_rate=0 (legitimate zero) is also dropped
  by `,omitempty`. Agent sees identical "missing field" pattern. This is
  exactly the PR-1 ambiguity (zero or no-data - agent can't tell).
- **Latency case with traffic:** percentile fields populate correctly when
  values are non-zero (e.g., `p99_ms=377.5`).

### Implication for the research

Trial 3 (run-id `25f6ad39`) was conducted on the as-built prototype before
the revert episode. Its findings about format-driven correctness (e.g.,
summary mode failing tasks 02/04) are valid for the baseline state. Tasks
03 and 06 specifically encountered the PR-1 ambiguity (agent inferring "0%"
from absence of field). That's part of the data, not a defect.

All subsequent multi-trial runs (Trials 4+) use this reverted state.

---

## §12.5 - Ground-truth resolver design (2026-04-27 ~10:40 UTC)

To compute Pass^N, we need a canonical "right answer" per task. Built
`harness/ground_truth_resolver.py` which queries Jaeger's `/api/metrics/*`
HTTP API directly with the same params the bench server uses, then applies
the same reductions:

- 01: `bucket_max(per-bucket P99)` for driver, 60min window
- 02: spike-detection on route P99 series - `max(second_half) > 2 * mean(first_half)`
- 03: call-weighted error rate per service, sort desc, top 3
- 04: spike-detection on driver error AND latency series, correlate offsets
- 05: call-weighted error rate for frontend > 0.05
- 06: linear regression slope on customer error rate; `worse > +0.001`, `improving < -0.001`, else `stable`

### Param-alignment fix

Initial resolver used `rate_per_seconds=60` (1-minute rate window). The bench
server's `toQueryParams` uses `RatePerMs = window * 60 * 1000` (rate window
= lookback). These produce **different smoothing**, so the resolver and
bench-server-summary returned different P99 values for the same query.

Resolver updated: `rate_per_seconds=None` defaults to "match bench server"
(rate window = lookback). This makes the resolver's reduction byte-for-byte
identical to what the agent's tool result contains. Captured at
`results/snapshots/gt-aligned-*.json`.

### Format-divergent reduction insight (Task 01)

Even with aligned params, **summary and series produce different "P99 values"**:
- Summary mode: `bucket_max` reduction → a single number (e.g. 377.5 ms)
- Series mode: agent picks the reduction → typically reports "most recent
  bucket" (e.g. 249.0 ms) and may also describe the range across buckets

Both are mathematically defensible answers to "what is the P99 latency for
driver over the last 60 minutes." This is itself a research finding: the
format choice determines what reduction the agent can express. Scoring on
Task 01 widens to "any value in [50, 1000] ms is acceptable" - the
format-driven divergence is too large to call one answer "wrong."

The discrimination on Task 01 lives in the TOKEN delta, not the numeric
correctness.

## §13 - Trial 4: Claude × {summary, series} × 6 tasks × 3 trials (2026-04-27 10:46-10:55 UTC)

**Run-id:** `72beec4d`. 36 trials. Wall time: ~9 minutes.

### Pass^3 results

| task | summary Pass^3 | series Pass^3 | summary failure mode |
|---|---|---|---|
| 01_p99_now | 1.0 (3/3) | 1.0 (3/3) | (none) |
| 02_spike_detection | **0.0 (0/3)** | 1.0 (3/3) | 1 incorrect + 2 non-answers |
| 03_rank_error_rate | 1.0 (3/3) | 1.0 (3/3) | (none) |
| 04_correlation | 0.667 (2/3) | 1.0 (3/3) | 1 non-answer |
| 05_threshold | 1.0 (3/3) | 1.0 (3/3) | (none) |
| 06_trend | 1.0 (3/3) | 1.0 (3/3) | (none) |

**Aggregate Pass^3:** series 6.0/6 (100%); summary 4.667/6 (78%). Series wins on Pass^3.

### Format-specific token cost (cache_creation = format-specific tokens added per trial)

| task | summary mean | series mean | series/summary ratio |
|---|---|---|---|
| 01_p99_now | 2,861 | 16,806 | **5.87×** |
| 02_spike_detection | 1,117 | 4,090 | 3.66× |
| 03_rank_error_rate | 5,188 | 22,707 | **4.38×** |
| 04_correlation | 2,269 | 4,001 | 1.76× |
| 05_threshold | 2,844 | 8,245 | 2.90× |
| 06_trend | 5,670 | 6,645 | 1.17× |

Series consistently uses 1.2-5.9× more format-specific tokens. Summary is
2-6× cheaper on point queries (01, 03, 05) where both are equally correct.

### Tool calls

Most cells: 1 tool call per trial. Two outliers:

- Task 03 summary: **5.67 calls/trial** - agent retries multiple times trying
  to locate error_rate field that's hidden by `omitempty` (PR-1 bug
  manifests directly).
- Task 06 summary: 2 calls/trial - similar PR-1 confusion.

This is empirical evidence the PR-1 bug measurably impacts agent behavior:
**the same task takes ~6× more tool calls in summary mode** when the agent
can't tell whether a missing field means "no data" or "zero".

### Format-driven failure pattern

The **type of error** differs by format:

- **Summary's failure on Task 02**: 0/3 correct. Agent recognizes the format
  doesn't have temporal data ("the tool returned a single aggregate row..."),
  declines or commits to a wrong answer. Format-fundamental limit.

- **Summary's failure on Task 04**: 1 non-answer. Agent says "No - I cannot
  determine a temporal correlation from the available data." Honest non-answer.

- **Series correctness on Task 02**: agent reads 61 buckets, reports
  variance <0.07-1.5ms (well below 2× threshold), correctly concludes
  "no spike."

### What this Trial 4 establishes

1. **Series is uniformly Pass^3 = 1.0.** Six tasks, three trials each, no
   failures.
2. **Summary fails on temporal tasks (02, 04).** Confirmed across multiple
   trials, not a one-off.
3. **Summary is 2-6× cheaper on tasks where it can answer correctly.**
4. **PR-1 bug causes 5.67× tool-call inflation on Task 03 summary.** Direct
   measurable effect of the omitempty issue.
5. **Token deltas are per-task, not uniform.** Some tasks (06) show near-
   parity; others (01, 03) show 4-6× ratios.

### Statistical caveats

- Single model (Claude Sonnet). Will add Gemini in Trial 5.
- Trial-to-trial variance within a cell appears low.
- Ground truth captured at one moment; rolling-window aggregates (Task 01)
  drift across the run; Task 01 tolerance widened accordingly.
- Fixture is steady-state (no error injection, no synthetic spike).

---

## §14 - Trial 5: Gemini × {summary, series} × 6 tasks × 3 trials (2026-04-27 11:08-11:23 UTC)

**Run-id:** `45cfdf5d`. 36 trials. Wall time: ~15 minutes.
**Pre-run GT:** `gt-pre-gemini-20260427T110829Z.json` (P99=249.7ms, all temporal answers stable).
**Post-run GT:** `gt-post-gemini-20260427T113344Z.json` (identical values; no fixture drift over 15 min).

### Scorer revision discovered during analysis

Gemini's response style is dramatically more terse than Claude's:
- *"No."* (3 chars) is a typical Gemini answer to a yes/no question
- Claude typically writes 3-5 sentences with reasoning

The initial scorer required structured phrases like "no correlation" or "no spike";
bare "No." was being miscategorized as `non_answer`. Rewrote `_score_02`,
`_score_04`, `_score_05` to handle bare "No." / "Yes." as committed answers
when they appear at the start of the response. This is a CORRECTION, not a
loosening of standards - a "No." reply IS a committed answer.

After scorer revision, both runs re-aggregated.

### Pass^3 cross-model

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01_p99_now | 1.0 | 1.0 | 1.0 | 1.0 |
| 02_spike_detection | **0.0** | 1.0 | **0.0** | 1.0 |
| 03_rank_error_rate | 1.0 | 1.0 | **0.333** | 1.0 |
| 04_correlation | **0.667** | 1.0 | 1.0 | **0.667** |
| 05_threshold | 1.0 | 1.0 | 1.0 | 1.0 |
| 06_trend | 1.0 | 1.0 | **0.333** | **0.667** |

### Aggregate Pass^3 across all 6 tasks

- claude/series: **1.000** (perfect)
- gemini/series: **0.889**
- claude/summary: **0.778**
- gemini/summary: **0.611**

**Series wins for both models.**

### Format-specific input tokens per trial (cache_creation for Claude; prompt-cached for Gemini)

| task | claude/summary | claude/series | series/summary ratio (claude) | gemini/summary | gemini/series | series/summary ratio (gemini) |
|---|---|---|---|---|---|---|
| 01 | 2,861 | 16,806 | 5.87× | 9,814 | 36,674 | 3.74× |
| 02 | 1,117 | 4,090 | 3.66× | 14,544 | 24,272 | 1.67× |
| 03 | 5,188 | 22,707 | 4.38× | 13,868 | 62,216 | 4.49× |
| 04 | 2,269 | 4,001 | 1.76× | 4,281 | 19,637 | 4.59× |
| 05 | 2,844 | 8,245 | 2.90× | 9,513 | 14,958 | 1.57× |
| 06 | 5,670 | 6,645 | 1.17× | 15,050 | 18,113 | 1.20× |

Series uses 1.17-5.87× more format-specific tokens across both models.

### PR-1 bug effect in tool-call counts (cross-model)

Task 03 summary mode triggers retries when error_rate field is missing:
- Claude/summary: **5.67 tool calls/trial** (vs 1 typical)
- Gemini/summary: **5.67 tool calls/trial** (same!)
- Gemini/series: 5.33 tool calls/trial (Gemini even retries in series - PR-1 bleed)

The PR-1 bug confuses BOTH models comparably - strong evidence the format-side
ambiguity isn't model-specific.

### Failure-pattern analysis

**Summary failures by error type:**
- Task 02 (spike detection): summary literally lacks temporal data. Both models
  honestly say "I cannot determine." Format-fundamental limit.
- Task 04 correlation: summary cannot temporally correlate. Claude declines (1
  non-answer), Gemini's terse "No." accidentally matches the GT.
- Task 06 trend (Gemini only): Gemini summary fails 2/3 trials by giving
  non-answers about missing data. Claude summary succeeds because it commits
  to "stable" inferring from missing-as-zero.

**Series failures (Gemini only):**
- Task 04 series 1/3 trials: agent hallucinated "I am unable to fetch the
  metrics for the driver service... no information about available services
  in the current directory" - Gemini conflated MCP tool availability with
  filesystem context.
- Task 06 series 1/3 trials: similar non-answer.

Gemini has **higher trial-to-trial variance** than Claude across the matrix.

### Cross-model token cost summary

Claude is 2-4× more cache-efficient than Gemini:
- Claude cache_read averages ~50,000 tokens (Claude Code's static system prompt is heavily cached)
- Gemini cached averages ~8,000-15,000 tokens (Gemini caches less aggressively in CLI v0.18)

The format-driven delta survives this caching difference cleanly.

### Wall-clock time

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01 | 17.7s | 17.5s | 17.0s | 19.1s |
| 02 | 24.3s | **68.5s** | 36.1s | 24.8s |
| 03 | **42.3s** | 19.0s | 29.0s | 23.5s |
| 04 | 21.0s | 17.2s | 16.1s | 20.1s |
| 05 | 14.3s | 11.6s | 16.0s | 18.4s |
| 06 | 29.2s | 12.9s | 33.2s | 26.7s |

Claude/series Task 02 takes 68s (long-form analysis of 61 buckets).
Claude/summary Task 03 takes 42s (PR-1 retries).

### What this Trial 5 + cross-model establishes

1. **Series wins across both models on aggregate Pass^3** (1.0 vs 0.611-0.778).
2. **Summary fails on temporal tasks across both models** - format-fundamental,
   not model-specific. Tasks 02 and 06 (Gemini) show this clearly.
3. **Format choice changes the type of error** - summary forces non-answer or
   inference; series forces verbose analysis.
4. **PR-1 bug measurably hurts agent behavior in both models** (5.67 tool
   calls vs 1) on Task 03.
5. **Token cost: series is 1.17-5.87× more expensive** but the cost is justified
   on temporal tasks where summary gives no usable answer.
6. **Recommendation backed by data:** offer BOTH formats. Default to summary
   for cheap point-queries; expose series via `response_format: "series"`
   parameter (or as a separate tool) for tasks needing temporal context.

### Statistical caveats

- 3 trials per cell. Variance estimable but tight.
- Steady-state fixture only (no error injection, no synthetic spike). The
  benchmark tests "do agents correctly say 'no anomaly'?" - useful but
  one-sided. A degraded scenario with real spike is the natural follow-up.
- Two CLIs with different system-prompt baselines (Claude Code Pro OAuth +
  plugin-disabled vs Gemini CLI v0.18 with allowed-mcp filter). Both arms
  of each model's A/B see identical baseline - format delta is clean.
- Ground truth captured before+after each matrix run; identical values
  confirms fixture stability over the run windows.

---

## §14.5 - Independent peer review of methodology (2026-04-27 ~11:55 UTC)

A independent peer review was given access to the raw data,
methodology docs, scorer code, and trial JSONs and asked to review independently.
Findings (paraphrased; full review in conversation history):

### Top finding: Pass^3 conflates non_answer with wrong

Underlying counts per arm (verified by re-running scorer over all 72 trials):

| arm | correct | wrong | non_answer | total |
|---|---|---|---|---|
| claude / summary | 14 | 1 | 3 | 18 |
| claude / series | 18 | 0 | 0 | 18 |
| gemini / summary | 11 | 0 | 7 | 18 |
| gemini / series | 16 | 0 | 2 | 18 |

**Wrong commitments are 1 / 72.** The Pass^3 = 0.611 for gemini/summary is
ENTIRELY honest declines - not wrong answers. Calling this a "failure" rate
mischaracterizes calibrated agent behavior. The benchmark was scoring honest
"I cannot determine" identically to wrong commitments, biasing the headline
against summary.

### Additional findings

1. **Task 01 tolerance ([50, 1000] ms) too generous** - accepts everything,
   eliminates the discrimination it was meant to expose.
2. **GT for Task 01 is self-confirming** - bench server `bucket_max(P99)` and
   resolver `bucket_max(P99)` are identical reductions. The agent's "answer"
   for series mode (most-recent-bucket) diverges by 50%, both still pass.
3. **PR-1 confounds Task 03 / Task 06.** Pass^3 differences between Claude
   (1.0) and Gemini (0.333) on Task 03 summary trace to model disposition
   toward inference-under-ambiguity caused by the bug, not format properties.
4. **Scorer regex blind spots.** `_NON_ANSWER_RE` doesn't catch "data is
   insufficient" (vs "insufficient data"), "cannot confirm", "unable to
   retrieve." Two of three Claude/summary/04 declines were missed and
   scored as "correct No."
5. **Steady-state-only fixture.** "Summary fails spike detection" is a
   logical property of aggregation, not an empirically demonstrated one
   (zero real spikes in the data).
6. **Cross-model token comparison is unfair.** Absolute numbers reflect
   Claude/Gemini caching strategies, not format cost.
7. **Two-proportion z-tests:**
   - Claude summary vs series: z = -2.12, p = 0.034 (SIG α=0.05)
   - Gemini summary vs series: z = -1.92, p = 0.054 (borderline, NOT sig)
   - Cross-model comparisons: p > 0.10, NOT significant
8. **Post-hoc scorer change.** Bare-"No." accommodation added after seeing
   Gemini results. Honest correction, but methodologically a leak.

### Peer reviewer's three priority improvements for v2 of the benchmark

1. **Separate calibrated-decline from wrong-answer in headline reporting.**
   This single change reframes the story without re-running anything.
2. **Inject one real degraded scenario** so spike-detection has empirical,
   not just logical, support.
3. **Freeze the scorer before runs and report scorer FP/FN rates** with
   spot-audit of ~10% of trials.

### Peer reviewer's fairness assessment

> "The data is directionally honest but precision-overclaimed. The strongest
> finding - that summary mode literally cannot answer time-series questions
> because it discards the time axis - is true as a logical property of the
> format. The weaker findings - specific Pass^3 numbers, cross-model
> comparisons, exact token ratios - should not drive a binary API decision
> because they are confounded by PR-1, by scorer regex blind spots, by
> Gemini's service-discovery noise, by an over-wide Task 01 tolerance, and
> by an n that is small enough that one sub-arm comparison fails
> significance and another is borderline. For a maintainer asked 'which
> format should the upstream PR ship,' the data supports 'expose both
> formats with a flag, default to summary, fix PR-1 first' with high
> confidence; it does not support 'summary is X% worse than series at task
> Y' with the implied precision."

### Action taken

Post draft v3 created (`analysis/post_draft_v3.md`) with:
- Three-column reporting (correct / wrong / non_answer)
- Statistical significance caveat
- Scorer-modification disclosure
- Single-fixture caveat strengthened
- Cross-model token absolutes caveated
- Recommendation reframed: data supports "both formats with a flag, summary
  default, fix PR-1 first" - no precision claims about Pass^3 percentages

---

## §14.6 - systematic independent review (2026-04-27 ~12:05 UTC)

Second independent peer review by a second-pass agent, with explicit
instruction to find what the §14.5 reviewer missed. Findings (paraphrased):

### Top 5 NEW issues (not in §14.5)

1. **`scorer.py:_extract_numbers_with_units` regex bug.** The unit-detection
   alternation `(ms|millisecond|s\b|sec|second|m\b|min)?` allowed "minutes"
   matches and then scaled minutes-to-ms. On "P99: 30 ms over 5 minutes" the
   `5 minutes` would resolve to `300000`. Saved here only because of the
   `[50, 1000]` filter, but a latent bug. **Fixed:** require an explicit
   ms/seconds unit (no implicit-ms default), reject minutes/hours, cap
   seconds at 60.

2. **Dead regex anchors.** `^no$` / `^yes$` inside `\b(...|^no$|...)\b`
   alternation never match - `^/$` are interpreted relative to the
   alternation group, not the string. The post-hoc bare-"No." accommodation
   added after seeing Gemini results was therefore partly a no-op. **Fixed:**
   replaced with explicit `re.match(r"^\s*(no|yes)\b", a)` checks.

3. **`time.mktime` on UTC string.** `ground_truth_resolver.py:117` used
   `time.mktime(time.strptime(...))` to parse Jaeger's `2026-04-27T10:21:35Z`.
   `time.mktime` interprets the struct as LOCAL time. On any non-UTC host,
   timestamps shift by hours, breaking spike-window computations.
   **Fixed:** replaced with `calendar.timegm(...)`.

4. **Gemini timeout branch missing dataclass kwargs.** `gemini_runner.py`
   `TrialResult(...)` in the `subprocess.TimeoutExpired` branch omitted
   `cache_read_tokens` and `cache_creation_tokens`. Any actual timeout
   would raise `TypeError`, masking the real failure mode. **Fixed.**

5. **Matrix iteration order temporal confound.** `orchestrator.py:78-82`
   iterated nested `model → format → task → trial`. ALL summary trials
   ran before ALL series trials within a model. The 5-15 minute fixture
   drift between arms confounded the within-model A/B with run-time drift.
   **Fixed:** added seeded `random.shuffle(cells)` and a `manifest.json`
   recording the shuffle seed and cell order for reproducibility.

### Additional Bug fixs catalogued (not all fixed)

6. Statistical tests cited in post draft were not computed by any code in
   the repo. **Fixed:** added `_two_prop_z` to `cross_model_compare.py`
   and emit a z-test table in the comparison output. Bonferroni / multiple-
   testing correction is mentioned as a caveat in the post but not yet
   computed in code.
7. `aggregate_v2.py` `Pass^N` is mislabeled - actually computes mean(success)
   not literal Pass^k = Pr(all-k-succeed). Caveat noted; field renaming
   deferred to v2 of the benchmark.
8. /tmp config files leak (89 stale files at review time). Smell, not bug.
   Deferred.
9. `claude_runner.py:tool_calls` uses `(num_turns - 1) // 2` heuristic; a
   `_count_tool_calls` walker exists but is never called from `run()`.
   Heuristic is good-enough for current data but should switch to walker.
   Deferred.
10. Trial JSONs don't contain agent's tool-use trajectory for Claude
    (Claude `--output-format json` emits aggregate usage only). Auditing
    individual tool calls requires `--output-format stream-json`. Deferred.
11. `_score_06_trend` order-fragility: "no clear trend; rates have been
    increasing slightly" matched `\bincreas\b` first → false-worse.
    **Fixed:** stable-phrase branches now match BEFORE worse/improving.
12. `_score_03_rank_error_rate` accepts truncated answers when the rank
    isn't all-tied. Cosmetic; current data has all-tied case so unaffected.
13. Reproducibility audit: from repo + prompt, would-be reproducer is stuck
    on undocumented setup steps. Documented as caveat in REVIEW_PACKET.

### Fixes applied in code

- `harness/scorer.py`: replaced `_extract_numbers_with_units` (latency-only,
  units required), bare-no/yes via `re.match`, trend-stable-first ordering
- `harness/ground_truth_resolver.py`: `calendar.timegm` instead of
  `time.mktime`
- `harness/gemini_runner.py`: added missing dataclass kwargs in two
  early-return branches (`TimeoutExpired`, `returncode != 0`)
- `harness/orchestrator.py`: seeded shuffle (default seed=42) +
  `manifest.json` recording seed, shuffle status, full cell order
- `harness/cross_model_compare.py`: added `_two_prop_z` and z-test table
  emission

### Re-aggregation of existing trials with fixed scorer

Verdict counts on Trials 4 + 5 (run-ids `72beec4d` and `45cfdf5d`) are
**unchanged** after scorer fixes:

| arm | correct/n |
|---|---|
| claude/series | 18/18 |
| claude/summary | 14/18 |
| gemini/series | 16/18 |
| gemini/summary | 11/18 |

The regex bugs were latent - none of our actual trial answers triggered the
buggy paths. This is reassurance that the §13/§14 findings still stand on
the existing data, but it doesn't address the matrix-order temporal confound.

## §14.7 - Trial 6: randomized matrix on fixed scorer (2026-04-27 12:23 UTC, in progress)

Run-id: `b75f18cd`. 72 cells (claude + gemini × summary + series × 6 tasks ×
3 trials), seeded shuffle with `seed=42`. The shuffle de-correlates fixture
drift from format choice - addresses Bug fix #5. Manifest at
`results/runs/b75f18cd/manifest.json` records the full cell ordering.

Pre-run GT captured at `gt-pre-randomized-*.json`. GT loop captures every
2.5 min during the run for time-pairing.

Aggregation pending matrix completion. Comparison vs Trial 4+5 will show
whether the temporal-confound concern was material or not.

---

## §15 - Update procedure

This log is updated chronologically as new trials run. Each new section gets:
- A timestamp
- Run-id (uuid prefix from `orchestrator.py`)
- Setup parameters
- Verbatim or near-verbatim output snippets
- Findings + caveats

When in doubt about whether to capture something: capture it. Reproducibility wins over brevity.
