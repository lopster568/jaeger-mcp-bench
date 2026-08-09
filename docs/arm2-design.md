# Arm 2 design: does progressive disclosure actually work?

**Status:** pre-registered design. Committed before any trial runs; the commit hash of this
file is the pre-registration timestamp. Changes after the first scored trial land as dated
addenda below the line, never as edits to the registered sections.

**Question:** Jaeger's MCP server exposes nine tools tiered by verbosity (cheap discovery,
structural overviews, verbose OTLP detail) and instructs agents to drill down. The design
assumes agents will actually do that, and that it beats the naive alternative: one tool that
returns everything. Nobody has measured either half of that assumption. This arm does.

**Relation to arm 1:** arm 1 (run `b75f18cd`, 72 trials) varied the *output format* of a
single tool. Arm 2 holds data constant and varies the *tool inventory shape*. Same harness,
same statistical discipline, new axis.

---

## Hypotheses (registered)

- **H1 (cost):** the tiered inventory consumes materially fewer total tokens per solved task
  than the flat single-tool inventory, because agents fetch structure before detail.
- **H2 (correctness):** diagnostic correctness under the tiered inventory is no worse than
  under the flat inventory. (Directional worry: flat gives the model everything, so flat may
  *win* correctness on small traces while losing cost; that is a publishable result too.)
- **H3 (behavior):** given the tiered inventory, agents follow the drill-down order the
  server instructions describe (broad discovery or topology before span details) in the
  majority of trials. This is the untested claim at the center of the server's design.
- **H4 (failure mode):** the flat inventory produces more context-pressure failures
  (truncated reasoning, budget exhaustion, declines) on large traces.

No prediction is registered for which arm wins overall. Task-level `predicted_winner`
fields are author expectations for corpus balance, exactly as in arm 1, and are not
consumed by scoring.

## The two arms

Both arms query the **same live Jaeger backend** over the **same transport** (streamable
HTTP). The only variable is the tool inventory the agent sees.

### `tiered` — the real thing

`jaegertracing/jaeger:2.20.0`, stock released image, with `ai.enable_mcp: true` in the
mounted config. Endpoint: `http://jaeger:16686/api/ai/mcp/`. This registers exactly the
nine tools shipped in v2.20.0 (`git show v2.20.0:...mcptools/server.go`): `get_services`,
`get_span_names`, `search_traces`, `get_span_details`, `get_trace_errors`,
`get_trace_topology`, `get_critical_path`, `get_service_dependencies`, `read_skill` —
plus the embedded INSTRUCTIONS.md delivered during initialize.

Not a fork, not a patch. Anyone can pull the image and reproduce this arm.

### `flat` — the naive integration

A small Go MCP server (`server/flatserver/`) exposing **one** tool:

- `get_trace_data(service, lookback_minutes, limit, errors_only?)` — calls jaeger-query's
  HTTP API (`/api/traces`) and returns matching traces as **complete span dumps in that
  endpoint's classic v1 JSON shape**: every span, all tags/attributes, logs/events,
  references/links, verbatim. No summarization, no topology view, no caps beyond the
  caller's `limit`. Tool description states plainly what it returns and nothing more — no
  size warnings, no strategy hints.

This is the integration most people actually build first: wrap the search endpoint, return
the JSON, let the model figure it out. The arm must be a *fair* naive baseline, so it is not
hobbled: same backend, same data fidelity, one honest tool.

**The serialization format is part of the treatment, not a controlled variable.** The v1
JSON shape is more token-verbose per attribute than the tiered tools' own output structs.
That means H1's cost comparison measures *the shipped tiered design versus a naive wrapper
as people actually build one* — inventory shape and serialization bundled together — not
tool-count in isolation. Arm 1 already isolated output format as its own variable; arm 2
does not re-isolate it. Claims in the writeup must be phrased accordingly.

**Neutrality rule (both arms):** everything model-visible in the flat server's initialize
payload (server name, version, instructions) and tool schema carries no experiment
vocabulary — no "naive", "baseline", or "arm". Its instructions open with the same one-line
domain framing as the tiered arm's INSTRUCTIONS.md and then stop; drill-down strategy is
the tiered arm's treatment.

Transport parity: flatserver also speaks streamable HTTP, so MCP client config differs only
in URL. No stdio-vs-HTTP confound.

## Fixture

The existing compose stack (`compose/docker-compose.yml`) with two changes:

1. `jaeger:2.4.0` → `jaeger:2.20.0`, config updated for 2.20 schema plus the `ai:` block.
   (Risk: config format drift between 2.4 and 2.20 — resolve at implementation, verify the
   SPM pipeline still comes up since `get_service_metrics` is absent in 2.20 and tasks here
   are trace-based anyway.)
2. Add `flatserver` as a compose service on the same bridge network.

Traffic: `fixture/load.sh` against hotrod as in arm 1, but **volume-capped**: the load run
must leave **at most 100 traces per task-relevant service** in the store (~80 dispatch
requests). 100 is the tiered arm's `MaxSearchResults` server cap and comfortably within the
flat tool's caller-settable `limit`, so *both arms can enumerate the complete candidate set*
— the benchmark then measures whether they do, not whether a server cap happened to hide the
answer. The resolver enforces this: if any task's candidate set exceeds 100 traces it
refuses to write ground truth and tells the operator to re-run the fixture with less load.

Hotrod's randomness means trace IDs differ per fixture run, so no task prompt may reference
a literal trace ID; tasks identify traces by property ("the slowest trace of service X")
and ground truth is resolved against the live API immediately after traffic stops, exactly
as arm 1's `ground_truth_resolver.py` does for metrics.

**Freeze rule:** the run starts from a fresh stack (`docker compose down -v && up`) so the
store contains only this run's traffic; traffic generation stops before ground-truth
resolution; no trials run while `load.sh` is active. Both arms and the resolver then query
an identical, static span store. (Arm 1 tolerated live traffic because its metrics windows
were long; trace-level tasks are less forgiving.)

**Window rule:** every task prompt frames its window as **the last 24 hours**, and the
resolver computes ground truth over the same 24h lookback. With the store frozen, the
entire fixture stays inside "the last 24 hours" for every trial no matter when it starts,
so the candidate set is identical for the first trial and the last — a 60-minute window
would silently decay across a multi-hour serial run, desynchronizing late trials from the
frozen ground truth. The 24h window also matches `get_service_dependencies`' default
lookback, and the fresh-stack rule guarantees it contains nothing but this run's traffic.
The full matrix must therefore complete within ~20 hours of the freeze; at ~72 serial
trials × ≤10 min it does so with a wide margin.

## Tasks

Six trace-troubleshooting tasks, `tasks/2*.yaml`, balanced by expectation: two predicted
tiered, two predicted flat, two predicted neutral. Sketch (final prompts in the YAMLs):

| id | Task | Predicted | Why |
|---|---|---|---|
| `21_error_root_cause` | Which service and operation is the root cause of errors in the last window? | tiered | `get_trace_errors` answers surgically; flat must scan dumps |
| `22_critical_path` | For the slowest trace of service X, which operation contributes the most self-time? | tiered | `get_critical_path` computes it; flat must derive from raw timestamps |
| `23_trace_shape` | How many spans and which services participate in the slowest trace of X? | neutral | topology tier vs a trivial count over the dump |
| `24_attribute_hunt` | What is the value of attribute A on the **earliest-starting** failing span of the erroring request? | flat | the dump already contains it; tiered needs search → errors → details |
| `25_dependency` | Which services directly call service X? (bare list demanded) | neutral | `get_service_dependencies` vs deriving edges from spans |
| `26_compare_traces` | Compare a fast and a slow trace of the same operation: where does the extra latency come from? | flat | needs breadth across two traces; drill-down costs many calls |

Task 24 pins the *earliest-starting* failing span because hotrod's fault injection (every
6th `GetDriver` call, process-wide) routinely produces more than one failing span per
erroring trace; "the failing span" without a tie-break would coin-flip honest answers. The
resolver also records every failing span's attribute value as an audit field (exploratory
only). Task 25's prompt demands a bare list of service names precisely so the scorer can
penalize wrong extras — without that, hedging (listing every service) would score correct
and reward whichever arm over-lists more.

Scoring is programmatic (`scorer.py` handlers per task; verdicts
`correct / incorrect / non_answer`, plus `unscorable` when the resolver could not produce
ground truth for a task — unscorable trials are excluded from pass rates and reported as
their own count, never silently folded into incorrect). Ground truth for every task is
computable from the jaeger-query HTTP API with deterministic selection rules (max duration
in window, max self-time, earliest-start tie-break, exact attribute value), written to the
run's snapshot by an extended resolver.

## Matrix and budget

2 arms × 2 models (claude sonnet, gemini-2.5-pro) × 6 tasks × 3 trials = **72 trials**,
seed 42, shuffled cell order, manifest persisted — identical machinery to arm 1
(`orchestrator.py`, arm string routed through `--formats`).

Per-trial budget stays at arm 1's cap ($0.50, Claude). The per-trial wall-clock timeout is
raised to **600s for both arms** (orchestrator `--timeout-sec`; arm 1 used 180s) so that on
large flat-arm fetches the *budget* is the binding constraint, not the clock — otherwise
context-pressure failures would be recorded as generic timeouts and H4 undercounted. If a
trial exhausts budget or context, that is an **outcome, not an error**: recorded as
`budget_exhausted` and reported per cell by the aggregator (alongside a timeout count).
Silently raising the cap for one arm would bias the comparison. Gemini has no budget-cap
analogue; its exhaustion detection is a documented message-pattern heuristic, and gemini
H4 numbers are reported with that caveat.

## Metrics

Reusing arm 1's capture plus one addition:

- **Correctness:** per-cell correct/incorrect/non_answer, pass^n and pass@n (`aggregate_v2.py`, unchanged).
- **Cost:** normalized token totals per trial (the `cross_model_compare.py` normalization).
- **Tool behavior:** exact per-call name/order/result-bytes via `trajectory_capture.py`
  (Claude; stream-json). Gemini reports totals only — stated as a limitation, as in arm 1.
- **NEW — drill-down conformance (H3):** each captured Claude trajectory in the tiered arm
  is classified: did a structural/discovery call (`get_services`, `search_traces`,
  `get_trace_topology`, `get_span_names`, `read_skill`) precede the first verbose call
  (`get_span_details`, `get_trace_errors`)? Implemented as a step-classifier over
  `ToolStep` records, reported as a proportion with counts.
- **Declines:** `non_answer` rate per arm, the arm-1 finding this extends.

## Statistics (registered comparisons)

Four pairwise two-proportion z-tests on correctness, Bonferroni α = 0.05/4 = 0.0125:

1. claude/tiered vs claude/flat
2. gemini/tiered vs gemini/flat
3. tiered/claude vs tiered/gemini
4. flat/claude vs flat/gemini

Token costs are reported as means with per-cell dispersion, not significance-tested
(n=18/cell is honest for proportions, thin for token variance — same stance arm 1 took).
H3 conformance is reported descriptively with counts. Any comparison not listed here that
appears in the writeup must be labeled exploratory.

## Implementation inventory

| Piece | Where | Status |
|---|---|---|
| Compose bump to 2.20.0 + `ai.enable_mcp` + flatserver service | `compose/` | to build |
| Flat single-tool MCP server (streamable HTTP, Go) | `server/flatserver/` | to build |
| HTTP-type MCP client config for both CLIs (today only stdio `command` configs exist) | `harness/mcp_config.py` | to build |
| Arm routing: `--formats tiered,flat` → per-arm config writers | `harness/orchestrator.py` + `mcp_config.py` | to build |
| 6 task YAMLs | `tasks/2*.yaml` | to build |
| Trace-based ground truth resolution | `harness/ground_truth_resolver.py` | to extend |
| 6 scorer handlers | `harness/scorer.py` | to extend |
| Drill-down conformance classifier | `harness/trajectory_capture.py` or new module | to build |
| `budget_exhausted` outcome capture | runners | to extend |

Aggregator changes: `aggregate_v2.py` gains per-cell `unscorable`, `budget_exhausted` and
`timeout` counts (unscorable excluded from pass rates); a dedicated `arm2_compare.py`
implements the four registered z-test pairs (`cross_model_compare.py` stays arm-1-only —
its tables are hardcoded to summary/series).

## Run protocol (operator steps)

```bash
cd compose && docker compose down -v && docker compose up -d --build   # fresh stack
sleep 30                                                               # stack warm-up
SCENARIO=arm2 REQUEST_COUNT=80 bash ../fixture/load.sh                 # capped load, then FREEZE
cd ../harness
./.venv/bin/python ground_truth_resolver.py \
    --jaeger-url http://localhost:16686 \
    --output ../results/snapshots/gt-arm2-<date>.json                  # hard-fails if volume cap exceeded
./.venv/bin/python orchestrator.py \
    --formats tiered,flat --task-glob '2*' --trials 3 --seed 42 \
    --timeout-sec 600                                                  # 72 trials
./.venv/bin/python aggregate_v2.py --run-id <id> --ground-truth ../results/snapshots/gt-arm2-<date>.json
./.venv/bin/python arm2_compare.py --run-id <id> --ground-truth ../results/snapshots/gt-arm2-<date>.json
```

No traffic between the freeze and the last trial. The whole matrix must finish within ~20h
of the freeze (Window rule); expect 2-6h.

## What this arm cannot claim

- Nothing about tool inventories larger than nine, or about `read_skill`-style skill
  content tiering specifically (it is available in the tiered arm but not isolated).
- Nothing about models outside the two tested CLIs, or about non-tracing domains.
- Hotrod is a demo app; trace sizes are modest. If flat wins here, the H4 question at
  production trace sizes stays open — say so rather than extrapolate.
