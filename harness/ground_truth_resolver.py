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


RESOLVERS = {
    "01_p99_now": resolve_01_p99_now,
    "02_spike_detection": resolve_02_spike_detection,
    "03_rank_error_rate": resolve_03_rank_error_rate,
    "04_correlation": resolve_04_correlation,
    "05_threshold": resolve_05_threshold,
    "06_trend": resolve_06_trend,
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
