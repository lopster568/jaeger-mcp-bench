# Potential Jaeger PRs surfaced during the SPM-MCP benchmark

Bugs and improvements discovered while building the benchmark prototype that
should be folded back into the upstream Jaeger MCP server PR (or filed as
follow-ups). Catalogued here so they don't get lost.

---

## PR-1 - `omitempty` hides legitimate-zero values; NaN crashes serialization

**Discovered:** 2026-04-27, run-id `25f6ad39` (zero-hiding) + benchmark smoke test (NaN crash).

This PR has two related symptoms - both rooted in the same `float64 + omitempty` design choice in `SummaryRow`:

### Symptom A - `omitempty` hides legitimate zeros

(see narrative below - original discovery)

### Symptom B - NaN crashes JSON serialization on cold metrics

When the metricstore returns all-NaN buckets (no data in window), `bucketMax` / `bucketAvg` / `callWeightedErrorRate` propagate NaN into the SummaryRow. `omitempty` on `float64` only checks for the zero value - NaN is not zero - so the field is kept. Then `json.Marshal(NaN)` errors with `json: unsupported value: NaN`, the tool returns an error string instead of valid JSON, and the agent sees the raw error.

**Reproduction:** stop the load script, wait ~60 seconds for the rate-window to fall to zero, query `get_service_error_rates` for any service. Server returns the unmarshalable error.

**Why both symptoms collapse into one PR:** the upstream fix is shared. Switching to `*float64` with explicit nil for "no data" and explicit `0.0` for "real zero" handles both:
- Legitimate-zero → `&zero` → renders `"error_rate": 0` (fixes A)
- No data → `nil` → field omitted (fixes B without crashing)

**The benchmark prototype runs WITHOUT this fix** - see `docs/research-log.md` §11. A minimal `nanZero` operational guard converts NaN → 0 just before `omitempty` so the server doesn't crash, while preserving the agent-facing behavior of an absent field. The PR-1 bug is preserved in the research baseline so the format A/B reflects what a first-cut Jaeger PR would actually look like.

**Symptom (from real trial outputs):**

> Task 03 summary mode answer:
> *"The API is consistently returning rows with no `error_rate` field - just the metadata (`service`, `operation`, `window_sec`, `step_sec`). This means one of two things: All four services have a 0% error rate over the last 30 minutes (the metric exists but has no non-zero values), OR the API doesn't expose error rate this way..."*

> Task 06 summary mode answer:
> *"The tool returns no numeric error rate fields in either query - both responses contain only metadata (service, operation, window/step seconds) with no `error_rate` value..."*

The agent saw a JSON row like `{"service":"customer","window_sec":1800,"step_sec":5}` with no `error_rate` key, and could not determine whether that meant "0%" or "API doesn't return this field." Both interpretations are reasonable from the JSON shape alone.

**Root cause:** struct-level `,omitempty` on `SummaryRow.ErrorRate` (and `CallRate`, `P50Ms`, `P95Ms`, `P99Ms`). Go's `encoding/json` strips zero-valued fields when `omitempty` is set, including legitimate zero values like a service that genuinely has 0% errors.

**File:** `server/format.go` - `SummaryRow` struct definition.

**Fix:**

```diff
 type SummaryRow struct {
 	Service     string  `json:"service"`
-	Operation   string  `json:"operation,omitempty"`
-	P50Ms       float64 `json:"p50_ms,omitempty"`
-	P95Ms       float64 `json:"p95_ms,omitempty"`
-	P99Ms       float64 `json:"p99_ms,omitempty"`
-	CallRate    float64 `json:"call_rate,omitempty"`
-	ErrorRate   float64 `json:"error_rate,omitempty"`
+	Operation   string  `json:"operation,omitempty"` // operation legitimately optional
+	P50Ms       *float64 `json:"p50_ms,omitempty"`   // pointer: nil = "not queried", 0.0 = "queried, value 0"
+	P95Ms       *float64 `json:"p95_ms,omitempty"`
+	P99Ms       *float64 `json:"p99_ms,omitempty"`
+	CallRate    *float64 `json:"call_rate,omitempty"`
+	ErrorRate   *float64 `json:"error_rate,omitempty"`
 	WindowSec   int64   `json:"window_sec"`
 	StepSec     int64   `json:"step_sec"`
 }
```

Two-state encoding via `*float64`:
- `null` (or missing) - the metric was not queried for this row
- `0.0` (explicit) - the metric was queried, value is genuinely zero
- positive number - value

This disambiguates "no data" from "data is zero" in the JSON, which is exactly what the agent needs to answer threshold and ranking questions correctly.

**Severity:** correctness bug. Affects any agent task that legitimately encounters zero-rate values, which is common (most operations don't error in healthy systems). Found in 2 of 6 benchmark tasks on first run.

**PR scope:** drop or restructure `omitempty`. ~10 LOC.

---

## PR-2 - `GetErrorRates` zero-traffic re-call inflates Prometheus query count

**Discovered:** during research planning, verified at `internal/storage/metricstore/prometheus/metricstore/reader.go:230` and `TestGetErrorRatesZero`.

**Symptom:** when there are zero error spans in a query window, the Prometheus reader's `GetErrorRates` re-invokes `GetCallRates` internally to distinguish "0% errors" from "no data at all." This means the fan-out cost per MCP-tool call is **6 Prometheus queries** in the zero-error path, not the 5 we documented (3 latency quantiles + 1 call rate + 1 error rate + 1 retry call rate).

**Severity:** performance / cost concern. Not a correctness bug. Worth flagging in the eventual PR's design discussion so reviewers understand fan-out characteristics.

**PR scope:** could memoize the GetCallRates result within a single MCP tool invocation since the bench server already calls GetCallRates explicitly when computing call-weighted error rate. ~20 LOC.

---

## PR-3 - `metricstore.Reader` not exposed via `jaegerquery.Extension`

**Discovered:** research phase, verified at `cmd/jaeger/internal/extension/jaegerquery/extension.go:18-24`.

**Symptom:** the Extension interface exposes `QueryService()` and `TenancyManager()` but not the metrics reader. The bench prototype proxies via Jaeger's HTTP API; the production PR will need an interface extension to call the reader directly.

**File:** `cmd/jaeger/internal/extension/jaegerquery/extension.go`

**Fix:** add `MetricsReader() metricstore.Reader` method to the Extension interface and wire it in `cmd/jaeger/internal/extension/jaegerquery/server.go` analogous to how `QueryService()` works.

**Severity:** API gap. Blocks any in-tree consumer (including jaegermcp) from using the metricstore directly.

**PR scope:** ~20-30 LOC interface extension + accessor + 1 unit test.

---

## PR-4 - Prometheus PromQL queries don't filter by tenant

**Discovered:** research phase, verified across `GetLatencies`/`GetCallRates`/`GetErrorRates` at `internal/storage/metricstore/prometheus/metricstore/reader.go:127-251`.

**Symptom:** the reader receives a tenant-scoped `context.Context` but the PromQL queries it builds don't include a tenant label selector. Tenant flows through ctx, then is silently dropped at the query layer.

**Severity:** pre-existing data-isolation gap. Not introduced by the SPM-MCP work, but a multi-tenant deployment using the new MCP tools would inherit the leak.

**PR scope:** decide in the SPM-MCP PR whether to fix-in-place (~30 LOC) or call out as out-of-scope follow-up. Likely out-of-scope to keep the SPM-MCP PR focused.

---

## PR-5 (potential) - NaN serialization in Jaeger's metrics API JSON

**Discovered:** 2026-04-27 during bench server integration, fix landed locally as `GaugeValue.UnmarshalJSON` accommodation.

**Symptom:** `/api/metrics/latencies` returns `{"gaugeValue": {"doubleValue": "NaN"}}` (string) for buckets with no data, instead of `null` or omission. Standard JSON consumers crash on this. Our bench server has a custom unmarshaler.

**Severity:** ergonomic, not correctness. Any JSON client of the metrics API has to handle this string-NaN convention. Worth raising upstream.

**PR scope:** decide whether to standardize NaN as JSON `null` or document the existing string-NaN behavior. Discussion-first, not code-first.

---

## How this list is maintained

Append new entries here whenever the benchmark surfaces a server-side or
upstream-API issue. Each entry should include:

- Discovery context (run-id, task, exact symptom)
- Root cause if known, or hypothesis if not
- File:line references
- Fix sketch
- Severity assessment
- PR scope estimate

When ready to upstream, cherry-pick the strongest 1-2 items into a PR. Don't
bundle all five - see `feedback_yurishkuro_architecture.md`: yuri prefers
focused PRs, not "I found 5 things while doing X."
