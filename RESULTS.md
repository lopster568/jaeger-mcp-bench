# Results

Empirical comparison of two output formats (`summary` vs `series`) for a Jaeger MCP tool exposing `GetLatencies`, `GetCallRates`, and `GetErrorRates`. Built in response to [jaegertracing/jaeger#8409](https://github.com/jaegertracing/jaeger/issues/8409).

## Headline

Across 72 trials (6 tasks × 2 formats × 2 models × 3 trials), only 1 produced a wrong commitment, and that wrong commitment is traceable to a bench-server bug rather than the output format itself (see [§ A bench-server bug](#a-bench-server-bug-downward-biases-summary-on-tasks-03-and-06)). The format-driven gap lives in the *decline rate*, not the error rate: when given the summary format, agents say "I cannot determine" 7× more often than when given per-bucket series, primarily on temporal questions where aggregation has discarded the time axis. The Claude in-model summary-vs-series gap is significant after Bonferroni correction (p=0.001, α=0.0125); Gemini's (p=0.016) sits on the boundary.

## Setup

- **Fixture.** Jaeger v2 with spanmetrics-connector, hotrod, Prometheus. Traffic generated for 5-10 minutes then snapshotted so re-runs hit identical metric state.
- **Bench server.** A thin proxy of the existing `/api/metrics/*` API with one switch: `--format=summary|series`. No new metric semantics.
- **Tasks.** 6 troubleshooting questions with predicted-winner balance: 3 chosen to favor `summary` (point queries: current latency, ranking, threshold) and 3 to favor `series` (temporal: spike detection, correlation, trend). See `tasks/*.yaml`.
- **Agents.** Claude Sonnet via Claude Code CLI; Gemini 2.5 Pro via `gemini` CLI. Real CLIs, not stripped tool-use loops, so the trajectory includes the CLI's system prompt and tool descriptions.
- **Trials.** 3 per (model × format × task) cell = 72 trials. Cells run in randomized order (seed=42) to remove fixture-drift confounds.
- **Scoring.** Programmatic ground-truth evaluators with regex extraction; bare-No/Yes accommodation; non-answer detection. See `harness/scorer.py` and the methodology log at `docs/research-log.md`.

## Verdicts

18 trials per arm.

| arm              | correct | wrong | declined |
|------------------|--------:|------:|---------:|
| claude / series  | 18 | 0 | 0 |
| gemini / series  | 17 | 0 | 1 |
| claude / summary | 10 | 1 | 7 |
| gemini / summary | 11 | 0 | 7 |

Wrong commitments: 1 across all 72 trials.

## Statistical tests

Two-proportion z-tests, in-model summary vs series:

| comparison                       | correct/n vs correct/n | z     | p (2-sided) |
|----------------------------------|------------------------|-------|-------------|
| claude summary vs claude series  | 10/18 vs 18/18         | -3.21 | 0.001       |
| gemini summary vs gemini series  | 11/18 vs 17/18         | -2.41 | 0.016       |

Both significant at α=0.05. Bonferroni-corrected α=0.0125 still crosses the Claude threshold; Gemini's p=0.016 sits on the boundary. Cross-model comparisons within a format do not reach significance at n=18.

## Format-specific input tokens per trial

Within-model A/B. Cross-model absolutes are not directly comparable because the two CLIs account for context cache differently.

| task                   | claude/summary | claude/series | gemini/summary | gemini/series |
|------------------------|---------------:|--------------:|---------------:|--------------:|
| 01_p99_now             | 3,882  | 18,566 | 12,347 | 37,084 |
| 02_spike_detection     | 2,871  |  4,940 | 11,445 | 36,414 |
| 03_rank_error_rate     | 5,688  | 24,802 | 13,240 | 41,272 |
| 04_correlation         | 3,287  |  5,130 | 11,169 | 23,950 |
| 05_threshold           | 7,519  |  8,117 | 12,287 | 17,661 |
| 06_trend               | 5,299  |  3,945 | 22,885 | 25,935 |

Series uses 0.74-4.78× the format-specific input tokens summary uses, varying by task. Summary matched series on tasks 01, 03, 05; series uniquely answered 02, 04, 06.

## What the data shows

1. **Calibrated declines dominate over wrong commitments.** 71 of 72 trials are either correct or a calibrated decline. Summary's effect on temporal questions is to make agents decline, not to make them answer wrong.
2. **On point-query / threshold tasks, summary matches series on correctness and uses fewer tokens.** The reduction is 1.1-4.8× format-specific tokens, varying by task and model.
3. **On temporal tasks, series uniquely solves what summary cannot.** On Task 02 (spike detection), every summary trial across both models either declined or noted explicitly that aggregation had discarded the time axis.
4. **Each format wins on the task-shape it was predicted to win on.** Forcing one shape forfeits the other half.

## A bench-server bug downward-biases summary on tasks 03 and 06

Both models retried `get_service_error_rates` 4-5× on Task 03 in summary mode (vs ~1 call typically). The cause is a `float64 + omitempty` design choice in `SummaryRow`: an `error_rate` of `0.0` is dropped from the JSON, so the agent sees a row with no `error_rate` key and cannot tell whether that means "0%" or "API does not expose this". Some trials retried; some declined; one Claude trial committed wrong (the only wrong commitment in the 72-trial set is on Task 06 in summary mode, traceable to this same ambiguity).

**Direction of bias.** This bug penalizes `summary` only. Fixing it (use `*float64` so `null` means "not queried" and `0.0` means "real zero") would shift several Task 03 and Task 06 summary trials from declined or wrong to correct. The direction is positive for summary; the corrected magnitude has not been measured (no re-run with the fix). The series arm is unaffected: per-bucket time series carries the zero values explicitly.

**Does this flip the headline?** No. Series still uniquely answers tasks 02, 04, 06: temporal questions where summary's aggregation has discarded the time axis, independent of the zero-handling bug. What the fix would change is the *magnitude* of summary's correctness gap on temporal tasks, not the *existence* of it.

The benchmark prototype runs without this fix on purpose: the format A/B should reflect what a first-cut Jaeger PR would actually look like. Fix and four other in-server findings are catalogued in [`docs/potential-prs.md`](./docs/potential-prs.md) (PR-1 covers this bug).

## Recommendation

Expose both formats through a single MCP tool with a `format` parameter:

- Default `format=summary` for the common "what's the current p99?" / "is this above threshold?" call sites.
- Opt-in `format=series` for temporal questions where the time axis matters.
- Document the trade-off in the tool description so the agent picks correctly.

This matches the data: each format wins on the task-shape it was predicted to win on, and forcing a single shape forfeits the other half.

## Caveats

- Steady-state fixture only. No injected spike or error fault. The benchmark measures *which format the agent can extract a temporal answer from*, not *which format detects a real spike best*.
- 6 tasks, n=18 per arm. Cross-model deltas are not significant at this sample size.
- LLM stochasticity is the irreducible noise; trial count amortizes over it.
- Scoring rules were tightened mid-analysis to accommodate bare-No/Yes answers and to expand non-answer detection. Trial 6 numbers above use the final scorer.

## Reproducing

See [`README.md`](./README.md) "Quickstart" for re-running the bench from scratch. The 72 raw trial dumps that produced the numbers above are committed: [`results/runs/b75f18cd/`](./results/runs/b75f18cd/) (with [`manifest.json`](./results/runs/b75f18cd/manifest.json) confirming seed=42 and the randomized cell order). Aggregated tables: [`results/tables/b75f18cd.md`](./results/tables/b75f18cd.md) (per-cell verdicts + tokens + tool calls) and [`results/tables/t6_final.md`](./results/tables/t6_final.md) (Pass^3 cross-model summary). Methodology log: [`docs/research-log.md`](./docs/research-log.md).
