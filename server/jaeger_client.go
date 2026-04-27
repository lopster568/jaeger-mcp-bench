// jaeger_client.go talks to Jaeger's HTTP /api/metrics/* endpoints.
// We proxy the existing query API rather than embedding metricstore.Reader
// so this prototype can be developed independently of the Jaeger build.

package main

import (
	"encoding/json"
	"fmt"
	"math"
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
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

// MetricFamily mirrors the OpenMetrics MetricFamily proto shape that
// /api/metrics/{latencies,calls,errors} returns when JSON-marshaled.
// Only the fields the benchmark needs are listed.
type MetricFamily struct {
	Name    string   `json:"name"`
	Type    string   `json:"type"`
	Help    string   `json:"help"`
	Metrics []Metric `json:"metrics"`
}

type Metric struct {
	Labels       []Label       `json:"labels"`
	MetricPoints []MetricPoint `json:"metricPoints"`
}

type Label struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type MetricPoint struct {
	Timestamp  string     `json:"timestamp"`
	GaugeValue GaugeValue `json:"gaugeValue"`
}

type GaugeValue struct {
	DoubleValue float64 `json:"-"`
}

// UnmarshalJSON tolerates Jaeger emitting "NaN" / "Inf" / "-Inf" as strings
// instead of numbers in MetricPoint values when the underlying Prometheus
// query returns missing data for a bucket.
func (g *GaugeValue) UnmarshalJSON(data []byte) error {
	var raw struct {
		DoubleValue any `json:"doubleValue"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	switch v := raw.DoubleValue.(type) {
	case float64:
		g.DoubleValue = v
	case string:
		switch v {
		case "NaN":
			g.DoubleValue = math.NaN()
		case "Inf", "+Inf":
			g.DoubleValue = math.Inf(1)
		case "-Inf":
			g.DoubleValue = math.Inf(-1)
		default:
			parsed, err := strconv.ParseFloat(v, 64)
			if err != nil {
				return fmt.Errorf("GaugeValue.doubleValue %q: %w", v, err)
			}
			g.DoubleValue = parsed
		}
	case nil:
		g.DoubleValue = math.NaN()
	default:
		return fmt.Errorf("GaugeValue.doubleValue: unexpected type %T", v)
	}
	return nil
}

type MetricsQueryParams struct {
	Service          string
	GroupByOperation bool
	EndTimeMs        int64
	LookbackMs       int64
	StepMs           int64
	RatePerMs        int64
	SpanKinds        []string // e.g. "SPAN_KIND_SERVER"
	Quantile         float64  // GetLatencies only
}

func (c *jaegerClient) GetLatencies(p MetricsQueryParams) (*MetricFamily, error) {
	q := c.commonQuery(p)
	q.Set("quantile", strconv.FormatFloat(p.Quantile, 'f', 2, 64))
	return c.fetch("/api/metrics/latencies", q)
}

func (c *jaegerClient) GetCallRates(p MetricsQueryParams) (*MetricFamily, error) {
	return c.fetch("/api/metrics/calls", c.commonQuery(p))
}

func (c *jaegerClient) GetErrorRates(p MetricsQueryParams) (*MetricFamily, error) {
	return c.fetch("/api/metrics/errors", c.commonQuery(p))
}

func (c *jaegerClient) commonQuery(p MetricsQueryParams) url.Values {
	q := url.Values{}
	q.Set("service", p.Service)
	q.Set("groupByOperation", strconv.FormatBool(p.GroupByOperation))
	if p.EndTimeMs > 0 {
		q.Set("endTs", strconv.FormatInt(p.EndTimeMs, 10))
	}
	q.Set("lookback", strconv.FormatInt(p.LookbackMs, 10))
	q.Set("step", strconv.FormatInt(p.StepMs, 10))
	q.Set("ratePer", strconv.FormatInt(p.RatePerMs, 10))
	for _, k := range p.SpanKinds {
		q.Add("spanKind", k)
	}
	return q
}

func (c *jaegerClient) fetch(path string, q url.Values) (*MetricFamily, error) {
	u := fmt.Sprintf("%s%s?%s", c.baseURL, path, q.Encode())
	resp, err := c.http.Get(u)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", u, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: status %d", u, resp.StatusCode)
	}

	var mf MetricFamily
	if err := json.NewDecoder(resp.Body).Decode(&mf); err != nil {
		return nil, fmt.Errorf("decode %s: %w", u, err)
	}
	return &mf, nil
}
