"""Compute canonical ground-truth answers for each benchmark task.

Strategy: query the Jaeger /api/metrics/* HTTP API with the same params the
agent's tool would use, apply the same reductions (matching summary mode for
scalars; raw series for temporal questions), and write the result to a
per-run ground_truth_values.json.

This file is run ONCE before each benchmark run, while the fixture is in
steady-state. The captured values are then valid for trials within the next
~5 minutes (the metric-rate window's smoothing absorbs minor drift).

Task → resolver mapping:

  01_p99_now            : numeric    - driver P99 over 60min, bucket-max
  02_spike_detection    : structured - route P99 over 60min, spike vs. baseline
  03_rank_error_rate    : ordered    - top 3 by 30min call-weighted error rate
  04_correlation        : structured - driver error+latency spike correlation
  05_threshold          : boolean    - frontend error rate > 5% over 30min
  06_trend              : classify   - customer error rate slope over 60min

Arm 2 (docs/arm2-design.md) adds trace-based ground truth, queried against
jaeger-query's HTTP /api/traces and /api/dependencies - the same backend
both the tiered MCP tools and the flat single-tool server read from. See the
"Arm 2" section below for the selection/computation rule of each:

  21_error_root_cause   : structured - deepest error span across all services, 24h
  22_critical_path      : structured - max self-time op on the slowest frontend trace, 24h
  23_trace_shape        : structured - span count + service set of slowest driver trace, 24h
  24_attribute_hunt     : string     - param.driverID on the earliest failing span, latest erroring driver trace, 24h
  25_dependency         : structured - direct CHILD_OF callers of mysql, 24h
  26_compare_traces     : structured - largest critical-path self-time delta, fast vs slow /dispatch, 24h

Usage:
    python ground_truth_resolver.py \\
        --jaeger-url http://localhost:16686 \\
        --output ../results/runs/<run_id>/ground_truth.json
"""

from __future__ import annotations

import calendar
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click


@dataclass
class Series:
    """A time series of {ts_ms, value} per service+operation."""
    labels: dict[str, str]
    points: list[tuple[int, float]]   # (ts_ms, value), NaN dropped

    def values(self) -> list[float]:
        return [v for _, v in self.points]

    def numeric_values(self) -> list[float]:
        return [v for v in self.values() if not math.isnan(v)]


def fetch_metric(
    jaeger_url: str,
    endpoint: str,        # 'latencies' | 'calls' | 'errors'
    *,
    service: str,
    window_minutes: int,
    step_seconds: int = 5,
    rate_per_seconds: int | None = None,    # None → match bench server: ratePer = lookback
    quantile: float | None = None,
    group_by_operation: bool = False,
    end_ts_ms: int | None = None,
) -> list[Series]:
    """One call to /api/metrics/{endpoint}, returns list of Series.

    PARAM CHOICE: rate_per_seconds defaults to None, which means "use the
    same value as lookback" - matching the bench server's `toQueryParams`
    behavior (RatePerMs = window * 60 * 1000). This makes the resolver's
    answer and the agent's tool result use IDENTICAL Jaeger query params,
    so we measure what the agent actually saw, not a parallel-universe
    reading.
    """
    if end_ts_ms is None:
        end_ts_ms = int(time.time() * 1000)
    rate_per_ms = (
        rate_per_seconds * 1000 if rate_per_seconds is not None
        else window_minutes * 60 * 1000  # match bench server
    )
    params = {
        "service": service,
        "endTs": str(end_ts_ms),
        "lookback": str(window_minutes * 60 * 1000),
        "step": str(step_seconds * 1000),
        "ratePer": str(rate_per_ms),
        "groupByOperation": "true" if group_by_operation else "false",
    }
    if quantile is not None:
        params["quantile"] = f"{quantile:.2f}"
    url = f"{jaeger_url}/api/metrics/{endpoint}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    out = []
    for m in data.get("metrics", []):
        labels = {l["name"]: l["value"] for l in m.get("labels", [])}
        pts = []
        for p in m.get("metricPoints", []):
            v = p.get("gaugeValue", {}).get("doubleValue")
            ts = p.get("timestamp", "")
            # Jaeger emits NaN as the string "NaN"; coerce to float NaN so the rest
            # of the resolver treats it uniformly.
            if isinstance(v, str):
                if v == "NaN":
                    v = float("nan")
                else:
                    try:
                        v = float(v)
                    except ValueError:
                        v = float("nan")
            elif v is None:
                v = float("nan")
            # ts is ISO-8601 like "2026-04-27T10:21:35.614Z"
            try:
                # Bug fix: time.mktime interprets struct_time as LOCAL time;
                # Jaeger emits ISO-8601 UTC (trailing Z). Use calendar.timegm which
                # is the UTC-aware inverse of gmtime() per stdlib docs.
                ts_clean = ts.rstrip("Z").split(".")[0]
                ts_ms = calendar.timegm(time.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")) * 1000
            except Exception:
                ts_ms = 0
            pts.append((ts_ms, v))
        out.append(Series(labels=labels, points=pts))
    return out


def bucket_max(values: list[float]) -> float:
    """Bucket-max reduction matching summary-mode percentile computation."""
    nums = [v for v in values if not math.isnan(v)]
    if not nums:
        return float("nan")
    return max(nums)


def call_weighted_error_rate(err_pts: list[float], call_pts: list[float]) -> float:
    """Call-weighted error rate = Σ(error_rate_i × call_rate_i) / Σ(call_rate_i)."""
    num, den = 0.0, 0.0
    n = min(len(err_pts), len(call_pts))
    for i in range(n):
        e, c = err_pts[i], call_pts[i]
        if math.isnan(e) or math.isnan(c):
            continue
        num += e * c
        den += c
    return num / den if den > 0 else float("nan")


def detect_spike(
    series_values: list[float],
    factor: float = 2.0,
) -> tuple[bool, int | None, float | None]:
    """Return (spike_detected, spike_idx_within_second_half, spike_magnitude_factor).

    Baseline = mean of first half. Spike = any bucket in second half > baseline*factor.
    """
    nums = [v for v in series_values if not math.isnan(v)]
    if len(nums) < 4:
        return (False, None, None)
    mid = len(nums) // 2
    baseline = sum(nums[:mid]) / mid if mid > 0 else 0
    if baseline == 0:
        return (False, None, None)
    second = nums[mid:]
    max_v = max(second)
    if max_v > baseline * factor:
        idx_in_second = second.index(max_v)
        return (True, mid + idx_in_second, max_v / baseline)
    return (False, None, None)


def compute_trend_slope(values: list[float]) -> tuple[str, float]:
    """Linear regression slope in units-per-bucket. Returns (label, slope_per_bucket).

    Labels:
        - 'worse'     if slope > +0.001 (per-bucket)
        - 'improving' if slope < -0.001
        - 'stable'   otherwise (or if too few points / all-NaN)
    """
    nums = [(i, v) for i, v in enumerate(values) if not math.isnan(v)]
    if len(nums) < 4:
        return ("stable", 0.0)
    n = len(nums)
    xs = [x for x, _ in nums]
    ys = [y for _, y in nums]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    cov_xy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    slope = cov_xy / var_x if var_x > 0 else 0.0
    if slope > 0.001:
        return ("worse", slope)
    if slope < -0.001:
        return ("improving", slope)
    return ("stable", slope)


# ─── per-task resolvers ────────────────────────────────────────────────────

def resolve_01_p99_now(jaeger_url: str) -> dict:
    s = fetch_metric(jaeger_url, "latencies", service="driver",
                     window_minutes=60, quantile=0.99)
    if not s:
        return {"value": None, "error": "no metric series for driver"}
    p99 = bucket_max(s[0].values())
    return {"value": p99, "unit": "ms", "method": "bucket_max(per_bucket_p99)"}


def resolve_02_spike_detection(jaeger_url: str) -> dict:
    s = fetch_metric(jaeger_url, "latencies", service="route",
                     window_minutes=60, quantile=0.99)
    if not s:
        return {"value": {"spike_detected": False}, "error": "no metric series for route"}
    spike, idx, mag = detect_spike(s[0].values(), factor=2.0)
    pts = s[0].points
    spike_window_start_ms = pts[idx][0] if (spike and idx is not None and idx < len(pts)) else None
    return {
        "value": {
            "spike_detected": spike,
            "spike_window_start_ms": spike_window_start_ms,
            "spike_magnitude_factor": round(mag, 2) if mag else None,
        },
        "method": "max(second_half) / mean(first_half), threshold=2x",
    }


def resolve_03_rank_error_rate(jaeger_url: str) -> dict:
    services = ["frontend", "customer", "driver", "route"]
    rates = []
    for svc in services:
        err_s = fetch_metric(jaeger_url, "errors", service=svc, window_minutes=30)
        call_s = fetch_metric(jaeger_url, "calls", service=svc, window_minutes=30)
        if not err_s or not call_s:
            rates.append((svc, 0.0))
            continue
        rate = call_weighted_error_rate(err_s[0].values(), call_s[0].values())
        rates.append((svc, 0.0 if math.isnan(rate) else rate))
    rates.sort(key=lambda x: -x[1])
    return {
        "value": [s for s, _ in rates[:3]],
        "rates_full": {s: round(r, 6) for s, r in rates},
        "method": "call_weighted_error_rate per service, sort desc, take top 3",
    }


def resolve_04_correlation(jaeger_url: str) -> dict:
    err_s = fetch_metric(jaeger_url, "errors", service="driver", window_minutes=30)
    lat_s = fetch_metric(jaeger_url, "latencies", service="driver",
                         window_minutes=30, quantile=0.99)
    if not err_s or not lat_s:
        return {"value": {"correlated": False, "offset_minutes": None},
                "error": "missing metric series for driver"}
    err_spike, err_idx, _ = detect_spike(err_s[0].values(), factor=2.0)
    lat_spike, lat_idx, _ = detect_spike(lat_s[0].values(), factor=2.0)
    correlated = err_spike and lat_spike
    if correlated and err_idx is not None and lat_idx is not None:
        # Bucket size in minutes derives from step config; tasks default to step=5s.
        offset_buckets = abs(err_idx - lat_idx)
        offset_minutes = round(offset_buckets * 5 / 60.0, 2)
    else:
        offset_minutes = None
    return {
        "value": {"correlated": correlated, "offset_minutes": offset_minutes},
        "details": {"err_spike": err_spike, "lat_spike": lat_spike},
        "method": "spike-detection on each, offset = bucket index delta * step",
    }


def resolve_05_threshold(jaeger_url: str) -> dict:
    err_s = fetch_metric(jaeger_url, "errors", service="frontend", window_minutes=30)
    call_s = fetch_metric(jaeger_url, "calls", service="frontend", window_minutes=30)
    if not err_s or not call_s:
        return {"value": False, "error": "missing metric series for frontend"}
    rate = call_weighted_error_rate(err_s[0].values(), call_s[0].values())
    rate_clean = 0.0 if math.isnan(rate) else rate
    return {
        "value": rate_clean > 0.05,
        "actual_rate": round(rate_clean, 6),
        "threshold": 0.05,
        "method": "call_weighted_error_rate > 0.05",
    }


def resolve_06_trend(jaeger_url: str) -> dict:
    err_s = fetch_metric(jaeger_url, "errors", service="customer", window_minutes=60)
    if not err_s:
        return {"value": "stable", "error": "no metric series for customer"}
    label, slope = compute_trend_slope(err_s[0].values())
    return {
        "value": label,
        "slope_per_bucket": round(slope, 6),
        "method": "linear regression slope; worse>+0.001, improving<-0.001",
    }


# ─── Arm 2: trace-based ground truth (docs/arm2-design.md) ────────────────
#
# Both arm 2 MCP surfaces (tiered's nine tools, flat's single get_trace_data)
# ultimately read the same jaeger_storage memstore through jaeger-query's
# QueryService. This resolver queries that same backend one layer down, over
# jaeger-query's public HTTP API (GET /api/traces, GET /api/dependencies -
# see internal/uimodel/model.go and http_handler.go in the Jaeger source),
# so ground truth is computed from the identical span data either arm's tool
# calls would see, not a parallel reduction.
#
# Hotrod service/call map (v2.20.0, verified against examples/hotrod source
# - see docs/research-log.md and the task authoring notes for citations):
#   frontend  --HTTP--> customer   (GET /customer)
#   frontend  --gRPC--> driver     (driver.DriverService/FindNearest)
#   frontend  --HTTP--> route      (GET /route, once per driver candidate)
#   customer  --span--> mysql      ("SQL SELECT", manual client span)
#   driver    --span--> redis-manual ("FindDriverIDs", "GetDriver", manual
#                                      client spans)
# mysql and redis-manual are each their own service.name resource (not
# child spans of customer/driver's own process) - six real hotrod services
# total, though arm 1's tasks only ever query the four HTTP-facing ones.
#
# The fixture's only fault injection is a deterministic periodic timeout in
# driver's redis-manual "GetDriver" client span (every 6th call to it,
# process-wide); this is *discovered empirically* by resolve_21/24 below by
# scanning real error spans, not hardcoded, so the resolver stays correct
# even if the fixture's fault mechanism changes.

# 24 hours, matching every arm-2 task prompt ("the last 24 hours"). The store
# is frozen before resolution (design doc, Window rule), so a 24h window is
# stable for every trial in a multi-hour serial run - a 60-minute window
# would decay trial by trial relative to this frozen ground truth.
ARM2_WINDOW_MINUTES = 24 * 60

# The fixture protocol caps load at <=100 traces per task-relevant service
# (design doc, Fixture): 100 is the tiered arm's MaxSearchResults server cap
# and within the flat tool's caller-settable limit, so both arms can
# enumerate the full candidate set an extremum task selects over. Ground
# truth computed over a larger set would judge agents against traces they
# cannot all see; the resolver refuses to write it.
ARM2_VOLUME_CAP = 100

HOTROD_SERVICES = ["frontend", "customer", "driver", "route", "mysql", "redis-manual"]
ATTRIBUTE_HUNT_KEY = "param.driverID"
DEPENDENCY_TARGET_SERVICE = "mysql"
COMPARE_ROOT_SERVICE = "frontend"
COMPARE_ROOT_OPERATION = "GET /dispatch"


class Arm2VolumeError(RuntimeError):
    """Raised when the fixture holds more traces than the volume cap allows.

    Deliberately fatal: writing ground truth over an unenumerable candidate
    set would corrupt every extremum task, so the operator must re-run the
    fixture with less load instead.
    """


def fetch_arm2_traces(
    jaeger_url: str,
    service: str,
    *,
    operation: str | None = None,
) -> list[dict]:
    """Arm-2 trace fetch: exhaustive over the frozen 24h window, guarded by
    the volume cap (see ARM2_VOLUME_CAP)."""
    traces = fetch_traces(
        jaeger_url, service,
        window_minutes=ARM2_WINDOW_MINUTES, operation=operation, limit=5000,
    )
    if len(traces) > ARM2_VOLUME_CAP:
        raise Arm2VolumeError(
            f"service={service}"
            + (f" operation={operation}" if operation else "")
            + f": {len(traces)} traces in the {ARM2_WINDOW_MINUTES}min window"
            f" exceeds the fixture volume cap of {ARM2_VOLUME_CAP}"
            " (design doc, Fixture section). Re-run the fixture with less"
            " load (fresh stack, ~80 dispatch requests), then resolve again."
        )
    return traces


# ─── Arm 2: HTTP client for jaeger-query's trace/dependency API ───────────

def _now_us() -> int:
    return int(time.time() * 1_000_000)


def fetch_traces(
    jaeger_url: str,
    service: str,
    *,
    window_minutes: int,
    operation: str | None = None,
    # Ground truth must be exhaustive over the frozen window: extremum tasks
    # ("slowest trace") are wrong-by-construction if this truncates. A 600s
    # fixture load can exceed 200 frontend traces, so the ceiling sits far
    # above any realistic volume. If a resolve ever returns exactly `limit`
    # traces, ground truth is suspect - re-check volume before trusting it.
    limit: int = 5000,
    end_us: int | None = None,
) -> list[dict]:
    """One call to GET /api/traces. Returns the raw list of trace dicts in
    the shape internal/uimodel/model.go's Trace/Span/Process serialize to:
    {traceID, spans: [{spanID, operationName, startTime(us), duration(us),
    tags: [{key,type,value}], references: [{refType,spanID}], processID,
    logs, ...}], processes: {processID: {serviceName, tags}}, warnings}.

    This is the same jaeger-query HTTP endpoint flatserver's get_trace_data
    calls (docs/arm2-design.md) and the same backend the tiered tools' Go
    QueryService reads from, just one API layer removed.
    """
    if end_us is None:
        end_us = _now_us()
    start_us = end_us - window_minutes * 60 * 1_000_000
    params = {
        "service": service,
        "start": str(start_us),
        "end": str(end_us),
        "limit": str(limit),
    }
    if operation:
        params["operation"] = operation
    url = f"{jaeger_url}/api/traces?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read())
    return data.get("data") or []


def fetch_dependencies(
    jaeger_url: str,
    *,
    window_minutes: int,
    service: str | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    """One call to GET /api/dependencies. Returns a list of
    {parent, child, callCount} dicts."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    params = {"endTs": str(end_ms), "lookback": str(window_minutes * 60 * 1000)}
    if service:
        params["service"] = service
    url = f"{jaeger_url}/api/dependencies?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read())
    return data.get("data") or []


# ─── Arm 2: span/trace helpers over the /api/traces JSON shape ────────────

def _tag_value(tags: list[dict] | None, key: str):
    for t in tags or []:
        if t.get("key") == key:
            return t.get("value")
    return None


def is_error_span(span: dict) -> bool:
    """Mirrors the tiered get_trace_errors tool's span.Status().Code() ==
    StatusCodeError check. /api/traces serializes OTel error status as a
    boolean tag {"key":"error","type":"bool","value":true}
    (opentelemetry-collector-contrib's jaeger translator,
    getErrorTagFromStatusCode), with a parallel string tag
    "otel.status_code"="ERROR". Checked both ways for robustness; either
    alone is sufficient.
    """
    v = _tag_value(span.get("tags"), "error")
    if isinstance(v, bool) and v:
        return True
    if isinstance(v, str) and v.strip().lower() == "true":
        return True
    sc = _tag_value(span.get("tags"), "otel.status_code")
    return isinstance(sc, str) and sc.upper() == "ERROR"


def span_service(span: dict, processes: dict) -> str:
    proc = processes.get(span.get("processID")) or {}
    if proc.get("serviceName"):
        return proc["serviceName"]
    return (span.get("process") or {}).get("serviceName", "unknown")


def span_end_us(span: dict) -> int:
    return int(span["startTime"]) + int(span["duration"])


def trace_duration_us(trace: dict) -> int:
    """Total trace duration: max span end - min span start across ALL spans
    in the trace (every service), matching the tiered tools' TotalDurationUs
    (get_critical_path) / DurationUs (search_traces)."""
    spans = trace.get("spans") or []
    if not spans:
        return 0
    starts = [int(s["startTime"]) for s in spans]
    ends = [span_end_us(s) for s in spans]
    return max(ends) - min(starts)


def trace_services(trace: dict) -> list[str]:
    processes = trace.get("processes") or {}
    return sorted({span_service(s, processes) for s in trace.get("spans") or []})


def parent_span_id(span: dict) -> str | None:
    """A span's parent per its CHILD_OF reference. NOT the top-level
    `parentSpanID` field - internal/uimodel/model.go marks that field
    deprecated and it is not populated by /api/traces; references[] is the
    live source of truth."""
    for ref in span.get("references") or []:
        if ref.get("refType") == "CHILD_OF":
            return ref.get("spanID")
    return None


def span_depth(span_id: str, spans_by_id: dict[str, dict]) -> int:
    """Number of CHILD_OF ancestors between span_id and the trace root
    (root = depth 0). Cycle-guarded, though real traces are trees."""
    depth = 0
    seen: set[str] = set()
    cur = span_id
    while True:
        span = spans_by_id.get(cur)
        if span is None:
            return depth
        pid = parent_span_id(span)
        if not pid or pid not in spans_by_id or pid in seen:
            return depth
        seen.add(pid)
        depth += 1
        cur = pid


# ─── Arm 2: critical-path / self-time computation ──────────────────────────
#
# Python port of Jaeger's critical-path algorithm, mirrored from
# cmd/jaeger/internal/extension/jaegerquery/internal/mcptools/internal/
# criticalpath/{cpspan,find_lfc,criticalpath,sanitize}.go at v2.20.0 (the
# same code the tiered get_critical_path tool calls). This is the ground
# truth used by tasks 22 and 26.

@dataclass
class _CPSpan:
    span_id: str
    parent_id: str | None
    start_us: int
    duration_us: int
    children: list[str]


@dataclass
class _Section:
    span_id: str
    start_us: int
    end_us: int


def _build_cpspan_map(trace: dict) -> dict[str, _CPSpan]:
    by_id: dict[str, _CPSpan] = {}
    for s in trace.get("spans") or []:
        by_id[s["spanID"]] = _CPSpan(
            span_id=s["spanID"],
            parent_id=parent_span_id(s),
            start_us=int(s["startTime"]),
            duration_us=int(s["duration"]),
            children=[],
        )
    for cps in by_id.values():
        if cps.parent_id and cps.parent_id in by_id:
            by_id[cps.parent_id].children.append(cps.span_id)
    return by_id


def _sanitize_overflowing_children(span_map: dict[str, _CPSpan]) -> dict[str, _CPSpan]:
    """Port of criticalpath/sanitize.go's removeOverflowingChildren: clips
    (or drops) child spans whose time range falls outside their parent's.

    DOCUMENTED SIMPLIFICATION: the Go source iterates spanMap's keys in Go
    map order, which is randomized per process - a child's clip decision can
    therefore read its parent's start/duration either before or after that
    parent's own clip against ITS parent, depending on iteration order. This
    is a latent order-dependency in the upstream algorithm, not an
    intentional design choice (nothing in the Go code or its tests pins the
    order). Rather than reproduce that non-determinism, this port processes
    top-down via BFS from the root, so every parent is fully resolved before
    its children are examined - the outer-to-inner clip order the code's own
    comments/diagrams describe. For hotrod's well-formed nested spans this
    function is a no-op in the overwhelming majority of traces; the
    simplification only has any effect on spans that already violate
    parent/child time containment.

    SECOND DOCUMENTED DIFFERENCE: spans whose parent_id is absent from the
    map are treated as roots and kept here, whereas the Go source deletes
    them ("Drop the child spans of dropped parent span"). Output-equivalent
    for the critical path: traversal starts at the true root and never
    reaches orphan subtrees, so the computed sections are identical either
    way - but the retained map contents differ, so anything new that
    iterates the returned map must not assume Go parity there.
    """
    roots = [s for s in span_map.values() if not s.parent_id or s.parent_id not in span_map]
    keep: set[str] = {s.span_id for s in roots}
    queue: list[_CPSpan] = list(roots)
    while queue:
        cur = queue.pop(0)
        for child_id in list(cur.children):
            child = span_map.get(child_id)
            if child is None:
                continue
            child_end = child.start_us + child.duration_us
            parent_end = cur.start_us + cur.duration_us
            dropped = False
            if child.start_us >= cur.start_us:
                if child.start_us >= parent_end:
                    dropped = True
                elif child_end > parent_end:
                    child.duration_us = parent_end - child.start_us
            else:
                if child_end <= cur.start_us:
                    dropped = True
                elif child_end <= parent_end:
                    child.start_us = cur.start_us
                    child.duration_us = child_end - cur.start_us
                else:
                    child.start_us = cur.start_us
                    child.duration_us = parent_end - cur.start_us
            if dropped:
                cur.children = [c for c in cur.children if c != child_id]
                continue
            keep.add(child_id)
            queue.append(child)
    return {sid: sp for sid, sp in span_map.items() if sid in keep}


def _find_lfc(
    span_map: dict[str, _CPSpan],
    current: _CPSpan,
    returning_child_start: int | None,
) -> _CPSpan | None:
    """Port of find_lfc.go's findLastFinishingChildSpan."""
    best: _CPSpan | None = None
    best_end = 0
    for cid in current.children:
        child = span_map.get(cid)
        if child is None:
            continue
        child_end = child.start_us + child.duration_us
        if returning_child_start is not None:
            if child_end < returning_child_start and child_end > best_end:
                best_end = child_end
                best = child
        else:
            if child_end > best_end:
                best_end = child_end
                best = child
    return best


def _compute_critical_path(
    span_map: dict[str, _CPSpan],
    span_id: str,
    sections: list[_Section],
    returning_child_start: int | None = None,
) -> list[_Section]:
    """Port of criticalpath.go's computeCriticalPath."""
    current = span_map.get(span_id)
    if current is None:
        return sections
    lfc = _find_lfc(span_map, current, returning_child_start)
    if lfc is not None:
        end = returning_child_start if returning_child_start is not None else current.start_us + current.duration_us
        start = lfc.start_us + lfc.duration_us
        if start != end:
            sections.append(_Section(span_id, start, end))
        _compute_critical_path(span_map, lfc.span_id, sections, None)
    else:
        end = returning_child_start if returning_child_start is not None else current.start_us + current.duration_us
        start = current.start_us
        if start != end:
            sections.append(_Section(span_id, start, end))
        if current.parent_id and current.parent_id in span_map:
            _compute_critical_path(span_map, current.parent_id, sections, current.start_us)
    return sections


def compute_critical_path(trace: dict) -> list[_Section]:
    """Port of criticalpath.go's ComputeCriticalPathFromTraces. Root is the
    first span (in the trace's own span-list order) with no CHILD_OF
    reference, matching the Go source's first-match-wins scan."""
    spans = trace.get("spans") or []
    root_id = None
    for s in spans:
        if parent_span_id(s) is None:
            root_id = s["spanID"]
            break
    if root_id is None:
        return []
    span_map = _sanitize_overflowing_children(_build_cpspan_map(trace))
    if root_id not in span_map:
        return []
    return _compute_critical_path(span_map, root_id, [])


def self_time_by_operation(trace: dict) -> dict[tuple[str, str], int]:
    """Sum of critical-path section durations (self_time_us in the tiered
    get_critical_path tool's output) grouped by (service, operation). A span
    can contribute more than one disjoint section - the backward walk can
    revisit an ancestor after exhausting a descendant's last-finishing-child
    chain (see criticalpath.go's docstring/diagram) - so this sums sections,
    it does not assume one section per span.
    """
    processes = trace.get("processes") or {}
    span_by_id = {s["spanID"]: s for s in trace.get("spans") or []}
    totals: dict[tuple[str, str], int] = {}
    for sec in compute_critical_path(trace):
        span = span_by_id.get(sec.span_id)
        if span is None:
            continue
        key = (span_service(span, processes), span.get("operationName", ""))
        totals[key] = totals.get(key, 0) + (sec.end_us - sec.start_us)
    return totals


# ─── Arm 2: per-task resolvers ─────────────────────────────────────────────

def resolve_21_error_root_cause(jaeger_url: str) -> dict:
    """Selection: across all hotrod services, over the last 24 hours,
    collect every trace containing >=1 error span (is_error_span). Among
    ALL error spans in ALL such traces, the "root cause" is the one at
    MAXIMUM CHILD_OF-ancestor depth - the deepest point of failure, i.e.
    where the fault was actually injected rather than where it was merely
    observed or retried. Ties broken by earliest trace start time, then
    earliest span start time, for determinism.
    """
    seen_trace_ids: set[str] = set()
    candidates: list[tuple[int, int, int, str, str]] = []  # depth, trace_start, span_start, service, op
    error_trace_count = 0
    total_error_spans = 0
    for svc in HOTROD_SERVICES:
        try:
            traces = fetch_arm2_traces(jaeger_url, svc)
        except Exception:
            continue
        for trace in traces:
            tid = trace.get("traceID")
            if not tid or tid in seen_trace_ids:
                continue
            seen_trace_ids.add(tid)
            spans = trace.get("spans") or []
            if not spans:
                continue
            spans_by_id = {s["spanID"]: s for s in spans}
            processes = trace.get("processes") or {}
            error_spans = [s for s in spans if is_error_span(s)]
            if not error_spans:
                continue
            error_trace_count += 1
            total_error_spans += len(error_spans)
            trace_start = min(int(s["startTime"]) for s in spans)
            for es in error_spans:
                depth = span_depth(es["spanID"], spans_by_id)
                candidates.append((
                    depth, trace_start, int(es["startTime"]),
                    span_service(es, processes), es.get("operationName", ""),
                ))
    if not candidates:
        return {"value": None, "error": "no error spans found in window"}
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    depth, trace_start, span_start, svc, op = candidates[0]
    return {
        "value": {"service": svc, "operation": op},
        "depth": depth,
        "error_trace_count": error_trace_count,
        "total_error_spans": total_error_spans,
        "method": "deepest error span (max CHILD_OF ancestor depth) across all error"
                  " traces of any hotrod service in the last 24 hours; ties broken"
                  " by earliest trace start then earliest span start",
    }


def resolve_22_critical_path(jaeger_url: str, service: str = COMPARE_ROOT_SERVICE) -> dict:
    """Selection: the slowest trace (max trace_duration_us) touching
    `service` in the last 24 hours. Computation: critical-path self-time
    (self_time_by_operation); the winner is the (service, operation) pair
    with the largest total.
    """
    traces = fetch_arm2_traces(jaeger_url, service)
    if not traces:
        return {"value": None, "error": f"no traces found for service={service}"}
    slowest = max(traces, key=trace_duration_us)
    totals = self_time_by_operation(slowest)
    if not totals:
        return {"value": None, "error": "empty critical path", "trace_id": slowest.get("traceID")}
    (svc, op), self_us = max(totals.items(), key=lambda kv: kv[1])
    return {
        "value": {"service": svc, "operation": op},
        "self_time_us": self_us,
        "trace_id": slowest.get("traceID"),
        "trace_duration_us": trace_duration_us(slowest),
        "method": "max total critical-path self-time by (service,operation) on the"
                  f" slowest trace of service={service} in the last 24 hours",
    }


def resolve_23_trace_shape(jaeger_url: str, service: str = "driver") -> dict:
    """Selection: the slowest trace (max trace_duration_us) touching
    `service` in the last 24 hours. Value: its span count and the set of
    distinct services present in it.
    """
    traces = fetch_arm2_traces(jaeger_url, service)
    if not traces:
        return {"value": None, "error": f"no traces found for service={service}"}
    slowest = max(traces, key=trace_duration_us)
    return {
        "value": {
            "span_count": len(slowest.get("spans") or []),
            "services": trace_services(slowest),
        },
        "trace_id": slowest.get("traceID"),
        "trace_duration_us": trace_duration_us(slowest),
        "method": f"span count + service set of the slowest trace of service={service}"
                  " in the last 24 hours",
    }


def resolve_24_attribute_hunt(jaeger_url: str, service: str = "driver") -> dict:
    """Selection: among traces touching `service` in the last 24 hours,
    the one with the LATEST start time that contains an error span carrying
    the ATTRIBUTE_HUNT_KEY tag. Value: that tag's value, verbatim.
    """
    traces = fetch_arm2_traces(jaeger_url, service)
    # Stage 1: the qualifying trace with the LATEST start time. Stage 2:
    # within it, the qualifying span with the EARLIEST start time (tie-break
    # spanID). Hotrod's fault injection (every 6th GetDriver call,
    # process-wide) routinely puts >1 failing span in one trace, so "the
    # failing span" needs this pin or span-store order decides the answer.
    best_trace = None       # (trace_start, trace_id, qualifying_spans)
    for trace in traces:
        spans = trace.get("spans") or []
        if not spans:
            continue
        qualifying = [
            (s, _tag_value(s.get("tags"), ATTRIBUTE_HUNT_KEY))
            for s in spans
            if is_error_span(s) and _tag_value(s.get("tags"), ATTRIBUTE_HUNT_KEY) is not None
        ]
        if not qualifying:
            continue
        trace_start = min(int(s["startTime"]) for s in spans)
        if best_trace is None or trace_start > best_trace[0]:
            best_trace = (trace_start, trace.get("traceID"), qualifying)
    if best_trace is None:
        return {"value": None, "error": f"no error span with tag {ATTRIBUTE_HUNT_KEY} found"}
    _, trace_id, qualifying = best_trace
    qualifying.sort(key=lambda sv: (int(sv[0]["startTime"]), str(sv[0].get("spanID"))))
    span, val = qualifying[0]
    return {
        "value": str(val),
        "attribute_key": ATTRIBUTE_HUNT_KEY,
        "trace_id": trace_id,
        "span_id": span.get("spanID"),
        "span_operation": span.get("operationName"),
        # Audit field (exploratory only): every qualifying span's value in
        # the winning trace, in the pinned order. Scoring uses value alone.
        "all_failing_ids": [str(v) for _, v in qualifying],
        "method": f"tag '{ATTRIBUTE_HUNT_KEY}' on the earliest-starting failing span"
                  f" of the most recent error trace of service={service}"
                  " in the last 24 hours",
    }


def resolve_25_dependency(jaeger_url: str) -> dict:
    """Selection: direct callers of DEPENDENCY_TARGET_SERVICE, computed two
    ways and cross-checked:
      1. /api/dependencies (Parent/Child call-count edges over the window).
      2. Span parent-child pairs actually observed in traces touching the
         service in the window (CHILD_OF references), which also yields a
         real example operation name for "how it appears in a trace".
    Both should agree in the hotrod fixture (one underlying store); if they
    disagree, `agrees=False` flags it, and the span-derived set (backed by
    literal trace data, the same data either MCP arm's tools would see) is
    reported as the value.
    """
    svc = DEPENDENCY_TARGET_SERVICE
    dep_links = fetch_dependencies(jaeger_url, window_minutes=ARM2_WINDOW_MINUTES, service=svc)
    dep_callers = sorted({d["parent"] for d in dep_links if d.get("child") == svc and d.get("parent") != svc})

    traces = fetch_arm2_traces(jaeger_url, svc)
    span_callers: set[str] = set()
    example_operation = None
    for trace in traces:
        spans = trace.get("spans") or []
        processes = trace.get("processes") or {}
        by_id = {s["spanID"]: s for s in spans}
        for s in spans:
            if span_service(s, processes) != svc:
                continue
            pid = parent_span_id(s)
            parent = by_id.get(pid) if pid else None
            if parent is None:
                continue
            caller_service = span_service(parent, processes)
            if caller_service == svc:
                continue
            span_callers.add(caller_service)
            if example_operation is None:
                example_operation = s.get("operationName")

    value_callers = sorted(span_callers) if span_callers else dep_callers
    return {
        "value": {
            "callers": value_callers,
            "target_operation": example_operation,
        },
        "dependency_api_callers": dep_callers,
        "span_derived_callers": sorted(span_callers),
        "agrees": set(dep_callers) == span_callers,
        "method": f"direct CHILD_OF callers of service={svc} observed in traces in the"
                  " last 24 hours, cross-checked against /api/dependencies",
    }


def resolve_26_compare_traces(jaeger_url: str) -> dict:
    """Selection: fastest and slowest trace whose root is
    (COMPARE_ROOT_SERVICE, COMPARE_ROOT_OPERATION) in the last 24 hours
    (by trace_duration_us, same definition as tasks 22/23). Computation:
    critical-path self-time by (service, operation) for each of the two
    traces (self_time_by_operation, shared with task 22); the answer is the
    pair with the largest positive delta (slow - fast; missing on either
    side counts as 0).
    """
    traces = fetch_arm2_traces(
        jaeger_url, COMPARE_ROOT_SERVICE, operation=COMPARE_ROOT_OPERATION,
    )
    if len(traces) < 2:
        return {
            "value": None,
            "error": f"need >=2 traces of {COMPARE_ROOT_SERVICE}/{COMPARE_ROOT_OPERATION},"
                     f" got {len(traces)}",
        }
    fast = min(traces, key=trace_duration_us)
    slow = max(traces, key=trace_duration_us)
    fast_totals = self_time_by_operation(fast)
    slow_totals = self_time_by_operation(slow)
    keys = set(fast_totals) | set(slow_totals)
    if not keys:
        return {"value": None, "error": "empty critical path on fast/slow trace"}

    def delta(k: tuple[str, str]) -> int:
        return slow_totals.get(k, 0) - fast_totals.get(k, 0)

    best_key = max(keys, key=delta)
    return {
        "value": {"service": best_key[0], "operation": best_key[1]},
        "delta_us": delta(best_key),
        "fast_trace_id": fast.get("traceID"),
        "slow_trace_id": slow.get("traceID"),
        "fast_trace_duration_us": trace_duration_us(fast),
        "slow_trace_duration_us": trace_duration_us(slow),
        "method": "largest positive delta in critical-path self-time by (service,operation)"
                  " between the fastest and slowest trace of"
                  f" {COMPARE_ROOT_SERVICE}/{COMPARE_ROOT_OPERATION} in the last 24 hours",
    }


RESOLVERS = {
    "01_p99_now": resolve_01_p99_now,
    "02_spike_detection": resolve_02_spike_detection,
    "03_rank_error_rate": resolve_03_rank_error_rate,
    "04_correlation": resolve_04_correlation,
    "05_threshold": resolve_05_threshold,
    "06_trend": resolve_06_trend,
    "21_error_root_cause": resolve_21_error_root_cause,
    "22_critical_path": resolve_22_critical_path,
    "23_trace_shape": resolve_23_trace_shape,
    "24_attribute_hunt": resolve_24_attribute_hunt,
    "25_dependency": resolve_25_dependency,
    "26_compare_traces": resolve_26_compare_traces,
}


@click.command()
@click.option("--jaeger-url", default="http://localhost:16686")
@click.option("--output", required=True, type=click.Path())
@click.option("--task", multiple=True, help="Limit to specific task(s); default = all")
def main(jaeger_url, output, task):
    tasks_to_run = list(task) if task else list(RESOLVERS.keys())
    results = {}
    for tid in tasks_to_run:
        if tid not in RESOLVERS:
            click.echo(f"unknown task: {tid}", err=True)
            continue
        click.echo(f"resolving {tid}...")
        try:
            results[tid] = RESOLVERS[tid](jaeger_url)
        except Exception as e:
            click.echo(f"  ERROR: {e}", err=True)
            results[tid] = {"value": None, "error": str(e)}
        click.echo(f"  {json.dumps(results[tid], default=str)[:200]}")

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "captured_at_unix": int(time.time()),
            "jaeger_url": jaeger_url,
            "ground_truth": results,
        }, f, indent=2)
    click.echo(f"\nDONE. wrote {out_path}")


if __name__ == "__main__":
    main()
