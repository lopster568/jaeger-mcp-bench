// jaeger_client.go talks to jaeger-query's classic /api/traces HTTP API.
//
// API choice (recorded here since it isn't obvious): jaeger-query serves two
// trace-search APIs - the classic "v1" JSON API used by this file
// (GET /api/traces, handled by APIHandler.search in
// cmd/jaeger/internal/extension/jaegerquery/internal/http_handler.go as of
// jaegertracing/jaeger v2.20.0) and api_v3 (OTLP-JSON over a gRPC-gateway).
// Both return full span data. /api/traces was picked because its handler
// converts every returned trace via uiconv.FromDomain, which embeds the
// complete per-span shape this arm needs (tags/attributes, logs/events,
// references/links, process/resource, warnings) in one well-documented,
// stable JSON shape - and it needs no protobuf/JSON-mapping tooling to
// consume, keeping this "naive integration" prototype genuinely simple.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

type jaegerClient struct {
	baseURL string
	http    *http.Client
}

func newJaegerClient(baseURL string) *jaegerClient {
	return &jaegerClient{
		baseURL: baseURL,
		// Generous ceiling: the design forbids capping this arm, and a large
		// fetch that takes tens of seconds to serialize must not become a
		// tool error only the flat arm can hit. The timeout exists solely to
		// stop a wedged connection from hanging a trial forever.
		http: &http.Client{Timeout: 120 * time.Second},
	}
}

// getTraceDataInput is the get_trace_data tool's input schema.
type getTraceDataInput struct {
	Service         string `json:"service" jsonschema:"service name to fetch traces for"`
	LookbackMinutes int    `json:"lookback_minutes,omitempty" jsonschema:"how far back to search, in minutes (default 60)"`
	Limit           int    `json:"limit,omitempty" jsonschema:"maximum number of traces to return (default 20); no server-side cap is applied beyond this value"`
	ErrorsOnly      bool   `json:"errors_only,omitempty" jsonschema:"if true, only return traces where the queried service has at least one error-status span (errors in other services of a trace do not match)"`
}

const (
	defaultLookbackMinutes = 60
	defaultLimit           = 20
	// maxLookbackMinutes rejects absurd inputs that would overflow the
	// time.Duration multiplication and silently produce an empty result.
	// 30 days is far beyond any fixture window; this is input validation,
	// not a data cap.
	maxLookbackMinutes = 30 * 24 * 60
)

// tracesQuery builds the query params for GET /api/traces from tool input,
// resolving defaults and converting the lookback window into the
// microsecond-epoch start/end pair the API expects.
//
// errors_only is implemented via tags={"error":"true"}, the same mechanism
// Jaeger's own search UI uses for its "Only Errors" checkbox. NOTE the
// scoping (verified against v2.20.0 internal/storage/v2/memory/tenant.go):
// the tag match runs only over spans belonging to the QUERIED service, so a
// trace whose only error spans live in a downstream service does not match.
// The tool's schema description states this. Like every tag filter on this
// endpoint it still returns the whole matching trace (every span), not a
// filtered span list - consistent with this arm making no attempt to
// summarize.
func tracesQuery(now time.Time, in getTraceDataInput) (url.Values, error) {
	if in.Service == "" {
		return nil, fmt.Errorf("service is required")
	}

	lookback := in.LookbackMinutes
	if lookback <= 0 {
		lookback = defaultLookbackMinutes
	}
	if lookback > maxLookbackMinutes {
		return nil, fmt.Errorf("lookback_minutes must be <= %d", maxLookbackMinutes)
	}
	limit := in.Limit
	if limit <= 0 {
		limit = defaultLimit
	}

	end := now
	start := now.Add(-time.Duration(lookback) * time.Minute)

	q := url.Values{}
	q.Set("service", in.Service)
	q.Set("start", strconv.FormatInt(start.UnixMicro(), 10))
	q.Set("end", strconv.FormatInt(end.UnixMicro(), 10))
	q.Set("limit", strconv.Itoa(limit))
	if in.ErrorsOnly {
		q.Set("tags", `{"error":"true"}`)
	}
	return q, nil
}

// fetchTraces calls GET /api/traces and returns the response body verbatim.
// The flat arm makes no attempt to trim, restructure, or summarize what
// jaeger-query returns - that is the point of the naive baseline.
func (c *jaegerClient) fetchTraces(ctx context.Context, q url.Values) ([]byte, error) {
	u := fmt.Sprintf("%s/api/traces?%s", c.baseURL, q.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", u, err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", u, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("GET %s: read body: %w", u, err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: status %d: %s", u, resp.StatusCode, truncate(body, 500))
	}
	if !json.Valid(body) {
		return nil, fmt.Errorf("GET %s: response was not valid JSON", u)
	}
	return body, nil
}

func truncate(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "...(truncated)"
}
