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
  HTTP API (`/api/traces`), returns matching traces as **complete OTLP-shaped span dumps**:
  every span, all attributes, events, links. No summarization, no topology view, no caps
  beyond the caller's `limit`. Tool description states plainly what it returns.

This is the integration most people actually build first: wrap the search endpoint, return
the JSON, let the model figure it out. The arm must be a *fair* naive baseline, so it is not
hobbled: same backend, same data fidelity, one honest tool.

Transport parity: flatserver also speaks streamable HTTP, so MCP client config differs only
in URL. No stdio-vs-HTTP confound.

## Fixture

The existing compose stack (`compose/docker-compose.yml`) with two changes:

1. `jaeger:2.4.0` → `jaeger:2.20.0`, config updated for 2.20 schema plus the `ai:` block.
   (Risk: config format drift between 2.4 and 2.20 — resolve at implementation, verify the
   SPM pipeline still comes up since `get_service_metrics` is absent in 2.20 and tasks here
   are trace-based anyway.)
2. Add `flatserver` as a compose service on the same bridge network.

Traffic: `fixture/load.sh` against hotrod as in arm 1. Hotrod's randomness means trace IDs
differ per fixture run, so no task prompt may reference a literal trace ID; tasks identify
traces by property ("the slowest trace of service X in the last N minutes") and ground
truth is resolved against the live API immediately before the run, exactly as arm 1's
`ground_truth_resolver.py` does for metrics.

**Freeze rule:** traffic generation stops before ground-truth resolution, and no trials run
while `load.sh` is active. Both arms and the resolver then query an identical, static span
store. (Arm 1 tolerated live traffic because its metrics windows were long; trace-level
tasks are less forgiving.)

## Tasks

Six trace-troubleshooting tasks, `tasks/2*.yaml`, balanced by expectation: two predicted
tiered, two predicted flat, two predicted neutral. Sketch (final prompts in the YAMLs):

| id | Task | Predicted | Why |
|---|---|---|---|
| `21_error_root_cause` | Which service and operation is the root cause of errors in the last window? | tiered | `get_trace_errors` answers surgically; flat must scan dumps |
| `22_critical_path` | For the slowest trace of service X, which operation contributes the most self-time? | tiered | `get_critical_path` computes it; flat must derive from raw timestamps |
| `23_trace_shape` | How many spans and which services participate in the slowest trace of X? | neutral | topology tier vs a trivial count over the dump |
| `24_attribute_hunt` | What is the value of attribute A on the failing span of the erroring request? | flat | the dump already contains it; tiered needs search → errors → details |
| `25_dependency` | Which services directly call service X, and how does that path appear in a real trace? | neutral | `get_service_dependencies` vs deriving edges from spans |
| `26_compare_traces` | Compare a fast and a slow trace of the same operation: where does the extra latency come from? | flat | needs breadth across two traces; drill-down costs many calls |

Scoring is programmatic (`scorer.py` handlers per task, same verdict set:
`correct / incorrect / non_answer`). Ground truth for every task is computable from the
jaeger-query HTTP API with deterministic selection rules (max duration in window, max
self-time, exact attribute value), written to the run's snapshot by an extended resolver.

## Matrix and budget

2 arms × 2 models (claude sonnet, gemini-2.5-pro) × 6 tasks × 3 trials = **72 trials**,
seed 42, shuffled cell order, manifest persisted — identical machinery to arm 1
(`orchestrator.py`, arm string routed through `--formats`).

Per-trial budget stays at arm 1's cap. If a flat-arm trial exhausts budget or context, that
is an **outcome, not an error**: recorded as `budget_exhausted` and reported. Silently
raising the cap for one arm would bias the comparison.

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

Aggregators need no changes for per-cell tables; the z-test pair list gets the four
registered pairs above.

## What this arm cannot claim

- Nothing about tool inventories larger than nine, or about `read_skill`-style skill
  content tiering specifically (it is available in the tiered arm but not isolated).
- Nothing about models outside the two tested CLIs, or about non-tracing domains.
- Hotrod is a demo app; trace sizes are modest. If flat wins here, the H4 question at
  production trace sizes stays open — say so rather than extrapolate.
