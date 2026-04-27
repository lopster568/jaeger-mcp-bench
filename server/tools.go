// tools.go registers the three MCP tools.
//
// One tool per metricstore.Reader API method. The selected output format
// is closed over at registration time so a single binary serves either
// "summary" or "series" depending on the --format flag.

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type metricsCommonInput struct {
	Service          string `json:"service" jsonschema:"service name to query metrics for"`
	GroupByOperation bool   `json:"group_by_operation,omitempty" jsonschema:"group result by span operation name"`
	WindowMinutes    int    `json:"window_minutes,omitempty" jsonschema:"lookback window in minutes (default 60)"`
	StepSeconds      int    `json:"step_seconds,omitempty" jsonschema:"bucket step in seconds (default 5)"`
	SpanKindServer   bool   `json:"span_kind_server,omitempty" jsonschema:"filter to SERVER-kind spans only"`
}

type latenciesInput struct {
	metricsCommonInput
	Quantile float64 `json:"quantile" jsonschema:"latency quantile, 0-1 (e.g. 0.99 for P99)"`
}

func (i metricsCommonInput) toQueryParams() MetricsQueryParams {
	window := i.WindowMinutes
	if window <= 0 {
		window = 60
	}
	step := i.StepSeconds
	if step <= 0 {
		step = 5
	}
	p := MetricsQueryParams{
		Service:          i.Service,
		GroupByOperation: i.GroupByOperation,
		EndTimeMs:        time.Now().UnixMilli(),
		LookbackMs:       int64(window) * 60 * 1000,
		StepMs:           int64(step) * 1000,
		RatePerMs:        int64(window) * 60 * 1000,
	}
	if i.SpanKindServer {
		p.SpanKinds = []string{"SPAN_KIND_SERVER"}
	}
	return p
}

func registerMetricsTools(s *mcp.Server, client *jaegerClient, format string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "get_service_latencies",
		Description: "Service latency percentiles. " +
			describeFormat(format, "p50/p95/p99 across the window", "per-bucket P-quantile points"),
		InputSchema: nil,
	}, func(ctx context.Context, req *mcp.CallToolRequest, in latenciesInput) (*mcp.CallToolResult, any, error) {
		// Latencies summary needs three calls (p50, p95, p99). Series mode honors the requested quantile only.
		switch format {
		case "summary":
			return latenciesSummary(client, in)
		case "series":
			return latenciesSeries(client, in)
		default:
			return errResult(fmt.Errorf("unknown format %q", format)), nil, nil
		}
	})

	mcp.AddTool(s, &mcp.Tool{
		Name: "get_service_call_rates",
		Description: "Service request rate. " +
			describeFormat(format, "average rate across the window", "per-bucket rate points"),
		InputSchema: nil,
	}, func(ctx context.Context, req *mcp.CallToolRequest, in metricsCommonInput) (*mcp.CallToolResult, any, error) {
		mf, err := client.GetCallRates(in.toQueryParams())
		if err != nil {
			return errResult(err), nil, nil
		}
		switch format {
		case "summary":
			return summaryFromCallRates(mf, in), nil, nil
		case "series":
			return seriesFromMetric(mf, "call_rate"), nil, nil
		}
		return errResult(fmt.Errorf("unknown format %q", format)), nil, nil
	})

	mcp.AddTool(s, &mcp.Tool{
		Name: "get_service_error_rates",
		Description: "Service error rate as a fraction of total calls. " +
			describeFormat(format, "call-weighted error rate across the window", "per-bucket fraction points"),
		InputSchema: nil,
	}, func(ctx context.Context, req *mcp.CallToolRequest, in metricsCommonInput) (*mcp.CallToolResult, any, error) {
		errMF, err := client.GetErrorRates(in.toQueryParams())
		if err != nil {
			return errResult(err), nil, nil
		}
		switch format {
		case "summary":
			// Need call rates too for weighted average.
			callMF, err := client.GetCallRates(in.toQueryParams())
			if err != nil {
				return errResult(err), nil, nil
			}
			return summaryFromErrorRates(errMF, callMF, in), nil, nil
		case "series":
			return seriesFromMetric(errMF, "error_rate"), nil, nil
		}
		return errResult(fmt.Errorf("unknown format %q", format)), nil, nil
	})
}

// describeFormat returns the format-specific tail for a tool description so
// agents see what shape to expect even before calling the tool.
func describeFormat(format, summaryDesc, seriesDesc string) string {
	switch format {
	case "summary":
		return "Returns " + summaryDesc + ", aggregated to one row per service+operation."
	case "series":
		return "Returns " + seriesDesc + " (per-bucket time series)."
	}
	return ""
}

// latenciesSummary issues three GetLatencies calls (p50, p95, p99) and merges
// into one row per (service, operation). Reduction: bucket-max per quantile
// (max(per-bucket P_q) is a safe upper bound on the true window P_q).
func latenciesSummary(client *jaegerClient, in latenciesInput) (*mcp.CallToolResult, any, error) {
	p := in.toQueryParams()
	quantiles := []float64{0.50, 0.95, 0.99}

	// Index per-quantile bucket-max values by label-set key.
	// rows[key].P50Ms / P95Ms / P99Ms get filled as we iterate.
	type aggRow struct {
		service   string
		operation string
		p50, p95, p99 float64
	}
	agg := map[string]*aggRow{}
	for _, q := range quantiles {
		p.Quantile = q
		mf, err := client.GetLatencies(p)
		if err != nil {
			return errResult(fmt.Errorf("GetLatencies(quantile=%.2f): %w", q, err)), nil, nil
		}
		for _, m := range mf.Metrics {
			k := keyOf(m.Labels)
			r, ok := agg[k]
			if !ok {
				r = &aggRow{
					service:   labelValue(m.Labels, "service_name"),
					operation: labelValue(m.Labels, "operation"),
				}
				agg[k] = r
			}
			v := bucketMax(m.MetricPoints)
			switch q {
			case 0.50:
				r.p50 = v
			case 0.95:
				r.p95 = v
			case 0.99:
				r.p99 = v
			}
		}
	}

	rows := make([]SummaryRow, 0, len(agg))
	for _, r := range agg {
		rows = append(rows, SummaryRow{
			Service:   r.service,
			Operation: r.operation,
			P50Ms:     nanZero(r.p50),
			P95Ms:     nanZero(r.p95),
			P99Ms:     nanZero(r.p99),
			WindowSec: int64(p.LookbackMs / 1000),
			StepSec:   int64(p.StepMs / 1000),
		})
	}
	return jsonResult(map[string]any{"rows": rows}), nil, nil
}

// latenciesSeries returns per-bucket points for the requested quantile.
func latenciesSeries(client *jaegerClient, in latenciesInput) (*mcp.CallToolResult, any, error) {
	q := in.Quantile
	if q == 0 {
		q = 0.99
	}
	p := in.toQueryParams()
	p.Quantile = q
	mf, err := client.GetLatencies(p)
	if err != nil {
		return errResult(err), nil, nil
	}
	metricName := fmt.Sprintf("p%.0f_latency_ms", q*100)
	return seriesFromMetric(mf, metricName), nil, nil
}

// summaryFromCallRates: one SummaryRow per (service, operation) with bucket-avg call_rate.
func summaryFromCallRates(mf *MetricFamily, in metricsCommonInput) *mcp.CallToolResult {
	rows := []SummaryRow{}
	for _, m := range mf.Metrics {
		rows = append(rows, SummaryRow{
			Service:   labelValue(m.Labels, "service_name"),
			Operation: labelValue(m.Labels, "operation"),
			CallRate:  nanZero(bucketAvg(m.MetricPoints)),
			WindowSec: int64(in.WindowMinutes) * 60,
			StepSec:   int64(in.StepSeconds),
		})
	}
	return jsonResult(map[string]any{"rows": rows})
}

// summaryFromErrorRates: one SummaryRow per (service, operation) with call-weighted error_rate.
func summaryFromErrorRates(errMF, callMF *MetricFamily, in metricsCommonInput) *mcp.CallToolResult {
	// Index call rates by label key so we can look up the matching series.
	callIdx := map[string][]MetricPoint{}
	for _, m := range callMF.Metrics {
		callIdx[keyOf(m.Labels)] = m.MetricPoints
	}
	rows := []SummaryRow{}
	for _, m := range errMF.Metrics {
		callPoints := callIdx[keyOf(m.Labels)]
		rows = append(rows, SummaryRow{
			Service:   labelValue(m.Labels, "service_name"),
			Operation: labelValue(m.Labels, "operation"),
			ErrorRate: nanZero(callWeightedErrorRate(m.MetricPoints, callPoints)),
			WindowSec: int64(in.WindowMinutes) * 60,
			StepSec:   int64(in.StepSeconds),
		})
	}
	return jsonResult(map[string]any{"rows": rows})
}

// seriesFromMetric: native [] {ts, value} blocks per service+operation.
// NaN points are dropped; the agent doesn't benefit from "no-data" markers
// and stripping them saves tokens.
func seriesFromMetric(mf *MetricFamily, metricName string) *mcp.CallToolResult {
	blocks := []SeriesBlock{}
	for _, m := range mf.Metrics {
		pts := []SeriesPoint{}
		for _, p := range m.MetricPoints {
			v := p.GaugeValue.DoubleValue
			if math.IsNaN(v) || math.IsInf(v, 0) {
				continue
			}
			ts, _ := time.Parse(time.RFC3339Nano, p.Timestamp)
			pts = append(pts, SeriesPoint{
				TimestampMs: ts.UnixMilli(),
				Value:       v,
			})
		}
		blocks = append(blocks, SeriesBlock{
			Service:   labelValue(m.Labels, "service_name"),
			Operation: labelValue(m.Labels, "operation"),
			Metric:    metricName,
			Points:    pts,
		})
	}
	return jsonResult(map[string]any{"series": blocks})
}

func jsonResult(v any) *mcp.CallToolResult {
	b, err := json.Marshal(v)
	if err != nil {
		return errResult(err)
	}
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: string(b)}},
	}
}

func errResult(err error) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{&mcp.TextContent{Text: err.Error()}},
	}
}
