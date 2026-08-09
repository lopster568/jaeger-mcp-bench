package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

func TestTracesQuery_Defaults(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	q, err := tracesQuery(now, getTraceDataInput{Service: "frontend"})
	if err != nil {
		t.Fatalf("tracesQuery: %v", err)
	}

	if got := q.Get("service"); got != "frontend" {
		t.Errorf("service = %q, want %q", got, "frontend")
	}
	if got := q.Get("limit"); got != strconv.Itoa(defaultLimit) {
		t.Errorf("limit = %q, want default %d", got, defaultLimit)
	}
	if q.Has("tags") {
		t.Errorf("tags should be absent when errors_only is false, got %q", q.Get("tags"))
	}

	wantStart := now.Add(-defaultLookbackMinutes * time.Minute).UnixMicro()
	gotStart, err := strconv.ParseInt(q.Get("start"), 10, 64)
	if err != nil {
		t.Fatalf("parse start: %v", err)
	}
	if gotStart != wantStart {
		t.Errorf("start = %d, want %d (default lookback %d minutes)", gotStart, wantStart, defaultLookbackMinutes)
	}

	wantEnd := now.UnixMicro()
	gotEnd, err := strconv.ParseInt(q.Get("end"), 10, 64)
	if err != nil {
		t.Fatalf("parse end: %v", err)
	}
	if gotEnd != wantEnd {
		t.Errorf("end = %d, want %d", gotEnd, wantEnd)
	}
}

func TestTracesQuery_CustomLookbackAndLimit(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	q, err := tracesQuery(now, getTraceDataInput{Service: "hotrod", LookbackMinutes: 15, Limit: 500})
	if err != nil {
		t.Fatalf("tracesQuery: %v", err)
	}

	if got := q.Get("limit"); got != "500" {
		t.Errorf("limit = %q, want %q (caller-supplied limit must be honored, not capped)", got, "500")
	}
	wantStart := now.Add(-15 * time.Minute).UnixMicro()
	gotStart, _ := strconv.ParseInt(q.Get("start"), 10, 64)
	if gotStart != wantStart {
		t.Errorf("start = %d, want %d", gotStart, wantStart)
	}
}

func TestTracesQuery_ErrorsOnlySetsTagFilter(t *testing.T) {
	now := time.Now()
	q, err := tracesQuery(now, getTraceDataInput{Service: "frontend", ErrorsOnly: true})
	if err != nil {
		t.Fatalf("tracesQuery: %v", err)
	}
	if got, want := q.Get("tags"), `{"error":"true"}`; got != want {
		t.Errorf("tags = %q, want %q", got, want)
	}
}

func TestTracesQuery_RequiresService(t *testing.T) {
	if _, err := tracesQuery(time.Now(), getTraceDataInput{}); err == nil {
		t.Fatal("expected error for missing service, got nil")
	}
}

func TestTracesQuery_RejectsAbsurdLookback(t *testing.T) {
	in := getTraceDataInput{Service: "frontend", LookbackMinutes: maxLookbackMinutes + 1}
	if _, err := tracesQuery(time.Now(), in); err == nil {
		t.Fatal("expected error for lookback beyond maxLookbackMinutes, got nil")
	}
}

// TestFetchTraces_PassesBodyThroughVerbatim asserts the flat arm's core
// property under test: the response bytes jaeger-query returns are neither
// trimmed nor restructured before reaching the caller.
func TestFetchTraces_PassesBodyThroughVerbatim(t *testing.T) {
	const raw = `{"data":[{"traceID":"abc123","spans":[{"spanID":"1","tags":[{"key":"http.status_code","value":500}]}]}],"total":1,"limit":20,"offset":0,"errors":null}`

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/traces" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(raw))
	}))
	defer ts.Close()

	client := newJaegerClient(ts.URL)
	q, err := tracesQuery(time.Now(), getTraceDataInput{Service: "frontend"})
	if err != nil {
		t.Fatalf("tracesQuery: %v", err)
	}

	body, err := client.fetchTraces(context.Background(), q)
	if err != nil {
		t.Fatalf("fetchTraces: %v", err)
	}
	if string(body) != raw {
		t.Errorf("fetchTraces body = %s, want verbatim %s", body, raw)
	}
}

func TestFetchTraces_NonOKStatusIsError(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte("boom"))
	}))
	defer ts.Close()

	client := newJaegerClient(ts.URL)
	q, _ := tracesQuery(time.Now(), getTraceDataInput{Service: "frontend"})
	if _, err := client.fetchTraces(context.Background(), q); err == nil {
		t.Fatal("expected error for 500 response, got nil")
	}
}
