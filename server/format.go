// format.go - two output serializers under test.
//
// summary: one row per service+operation, aggregated across the window.
//          percentiles = bucket-max
//          call_rate   = bucket-avg
//          error_rate  = call-weighted across buckets
//
// series:  raw [] {ts, value} per metric type. Native shape from the API.
//          One block per metric type per service+operation pair.

package main

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// SummaryRow as originally drafted. KEPT INTENTIONALLY with `,omitempty` on
// the metric fields even though this hides legitimate-zero values from the
// agent - see docs/potential-prs.md PR-1 and docs/research-log.md §11.
//
// The benchmark runs against the prototype as-built so the format A/B is on
// the same shape a first-cut Jaeger PR would ship before review. The bug is
// a research finding, not a pre-emptive fix. PR-1 is stashed for the
// upstream PR conversation.
type SummaryRow struct {
	Service     string  `json:"service"`
	Operation   string  `json:"operation,omitempty"`
	P50Ms       float64 `json:"p50_ms,omitempty"`
	P95Ms       float64 `json:"p95_ms,omitempty"`
	P99Ms       float64 `json:"p99_ms,omitempty"`
	CallRate    float64 `json:"call_rate,omitempty"`
	ErrorRate   float64 `json:"error_rate,omitempty"`
	WindowSec   int64   `json:"window_sec"`
	StepSec     int64   `json:"step_sec"`
}

// nanZero collapses NaN/Inf to zero so the JSON marshal step doesn't fail.
// This is a MINIMAL OPERATIONAL GUARD, not a correctness fix - with the
// original `,omitempty` semantics, a real-zero value and a NaN value both
// render as an absent field. So this guard preserves the agent-facing
// behavior (field absent) without crashing the server. See PR-2 in
// docs/potential-prs.md for the upstream-correct shape.
func nanZero(v float64) float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0
	}
	return v
}

type SeriesPoint struct {
	TimestampMs int64   `json:"ts_ms"`
	Value       float64 `json:"value"`
}

type SeriesBlock struct {
	Service   string        `json:"service"`
	Operation string        `json:"operation,omitempty"`
	Metric    string        `json:"metric"` // e.g. "p99_latency_ms", "call_rate", "error_rate"
	Points    []SeriesPoint `json:"points"`
}

// bucketMax: max across non-NaN points. NaN if all input is NaN.
func bucketMax(points []MetricPoint) float64 {
	max := math.Inf(-1)
	any := false
	for _, p := range points {
		v := p.GaugeValue.DoubleValue
		if math.IsNaN(v) {
			continue
		}
		if v > max {
			max = v
		}
		any = true
	}
	if !any {
		return math.NaN()
	}
	return max
}

// bucketAvg: mean across non-NaN points. NaN if all input is NaN.
func bucketAvg(points []MetricPoint) float64 {
	sum := 0.0
	n := 0
	for _, p := range points {
		v := p.GaugeValue.DoubleValue
		if math.IsNaN(v) {
			continue
		}
		sum += v
		n++
	}
	if n == 0 {
		return math.NaN()
	}
	return sum / float64(n)
}

// callWeightedErrorRate: Σ(error_rate_i × call_rate_i) / Σ(call_rate_i).
// Pre-aligned by index - both series must come from queries with the same step
// and time range. Falls back to NaN if call rates are all zero or NaN.
func callWeightedErrorRate(errPoints, callPoints []MetricPoint) float64 {
	num, den := 0.0, 0.0
	n := len(errPoints)
	if len(callPoints) < n {
		n = len(callPoints)
	}
	for i := 0; i < n; i++ {
		er := errPoints[i].GaugeValue.DoubleValue
		cr := callPoints[i].GaugeValue.DoubleValue
		if math.IsNaN(er) || math.IsNaN(cr) {
			continue
		}
		num += er * cr
		den += cr
	}
	if den == 0 {
		return math.NaN()
	}
	return num / den
}

// keyOf: combine label vector into a deterministic per-row key.
// MUST sort label set for stability.
func keyOf(labels []Label) string {
	cp := make([]Label, len(labels))
	copy(cp, labels)
	sort.Slice(cp, func(i, j int) bool { return cp[i].Name < cp[j].Name })
	var b strings.Builder
	for i, l := range cp {
		if i > 0 {
			b.WriteString("|")
		}
		fmt.Fprintf(&b, "%s=%s", l.Name, l.Value)
	}
	return b.String()
}

func labelValue(labels []Label, name string) string {
	for _, l := range labels {
		if l.Name == name {
			return l.Value
		}
	}
	return ""
}
