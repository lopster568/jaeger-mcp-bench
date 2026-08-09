"""Unit tests for the arm 2 (trace-based) additions to ground_truth_resolver.py.

These exercise the pure-computation helpers (critical path / self-time,
error-span detection, span depth, trace shape) against small synthetic
/api/traces-shaped fixtures, with no network access - the per-task resolver
functions (resolve_21..26) hit a live jaeger-query and are exercised
end-to-end by the actual benchmark runs instead.

Run: harness/.venv/bin/python -m unittest harness.test_ground_truth_resolver_arm2 -v
     (from the repo root) or `cd harness && ../harness/.venv/bin/python -m
     unittest test_ground_truth_resolver_arm2 -v`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ground_truth_resolver as gtr  # noqa: E402


def make_trace(spans_spec, default_service: str = "svc") -> dict:
    """Build a minimal /api/traces-shaped trace dict (internal/uimodel/model.go
    Trace/Span/Process) from a compact spec.

    spans_spec: list of tuples
        (span_id, parent_id_or_None, start_us, duration_us, operation_name
         [, tags [, service]])
    """
    processes: dict[str, dict] = {}
    spans = []
    for spec in spans_spec:
        span_id, parent_id, start, dur, op = spec[:5]
        tags = spec[5] if len(spec) > 5 and spec[5] is not None else []
        service = spec[6] if len(spec) > 6 else default_service
        pid = f"p_{service}"
        processes.setdefault(pid, {"serviceName": service, "tags": []})
        refs = [{"refType": "CHILD_OF", "traceID": "t1", "spanID": parent_id}] if parent_id else []
        spans.append({
            "traceID": "t1",
            "spanID": span_id,
            "operationName": op,
            "references": refs,
            "startTime": start,
            "duration": dur,
            "tags": tags,
            "logs": [],
            "processID": pid,
        })
    return {"traceID": "t1", "spans": spans, "processes": processes, "warnings": []}


class CriticalPathSelfTimeTest(unittest.TestCase):
    """Mirrors criticalpath.go's own doc example:

        |-------------spanA--------------|
           |--spanB--|    |--spanC--|

    A=[0,100], B=[10,30] (child of A), C=[50,80] (child of A).
    Hand-traced against the Go algorithm (find_lfc.go / criticalpath.go):
    sections = (A,80,100)=20, (C,50,80)=30, (A,30,50)=20, (B,10,30)=20,
    (A,0,10)=10 -> A totals 50, B totals 20, C totals 30, grand total 100
    (the critical path always spans the full root duration for a
    non-overflowing trace).
    """

    def test_docstring_example_self_times(self):
        trace = make_trace([
            ("A", None, 0, 100, "opA"),
            ("B", "A", 10, 20, "opB"),
            ("C", "A", 50, 30, "opC"),
        ])
        totals = gtr.self_time_by_operation(trace)
        self.assertEqual(totals[("svc", "opA")], 50)
        self.assertEqual(totals[("svc", "opB")], 20)
        self.assertEqual(totals[("svc", "opC")], 30)
        self.assertEqual(sum(totals.values()), 100)

    def test_single_root_no_children(self):
        trace = make_trace([("A", None, 0, 42, "opA")])
        totals = gtr.self_time_by_operation(trace)
        self.assertEqual(totals, {("svc", "opA"): 42})

    def test_empty_trace(self):
        trace = {"traceID": "t1", "spans": [], "processes": {}, "warnings": []}
        self.assertEqual(gtr.self_time_by_operation(trace), {})
        self.assertEqual(gtr.compute_critical_path(trace), [])

    def test_overflowing_child_is_clipped_to_parent_bounds(self):
        """Child starts before AND ends after the parent -> sanitize.go's
        'child start before parent and end after parent' branch: clipped to
        exactly the parent's [start, end]."""
        trace = make_trace([
            ("A", None, 10, 50, "opA"),   # [10, 60]
            ("B", "A", 0, 100, "opB"),    # [0, 100], overflows both sides
        ])
        totals = gtr.self_time_by_operation(trace)
        # After clipping, B == [10,60], identical to A's own bounds; all of
        # A's duration is attributed to B (the deepest surviving span) and
        # none of it is left over for A itself.
        self.assertEqual(sum(totals.values()), 50)
        self.assertEqual(totals.get(("svc", "opB")), 50)
        self.assertNotIn(("svc", "opA"), totals)

    def test_child_entirely_outside_parent_is_dropped(self):
        """Child starts at/after the parent's end -> dropped entirely, per
        sanitize.go's 'child outside of parent range' branch."""
        trace = make_trace([
            ("A", None, 0, 10, "opA"),     # [0, 10]
            ("B", "A", 20, 5, "opB"),      # [20, 25], entirely after A ends
        ])
        totals = gtr.self_time_by_operation(trace)
        self.assertEqual(totals, {("svc", "opA"): 10})

    def test_multi_service_self_time_grouping(self):
        trace = make_trace([
            ("A", None, 0, 100, "GET /dispatch", None, "frontend"),
            ("B", "A", 0, 60, "GET /customer", None, "customer"),
        ])
        totals = gtr.self_time_by_operation(trace)
        self.assertIn(("frontend", "GET /dispatch"), totals)
        self.assertIn(("customer", "GET /customer"), totals)
        self.assertEqual(sum(totals.values()), 100)


class ErrorSpanDetectionTest(unittest.TestCase):
    def test_bool_error_tag_true(self):
        span = {"tags": [{"key": "error", "type": "bool", "value": True}]}
        self.assertTrue(gtr.is_error_span(span))

    def test_status_code_error_string_tag(self):
        span = {"tags": [{"key": "otel.status_code", "type": "string", "value": "ERROR"}]}
        self.assertTrue(gtr.is_error_span(span))

    def test_status_code_ok_is_not_an_error(self):
        span = {"tags": [{"key": "otel.status_code", "type": "string", "value": "OK"}]}
        self.assertFalse(gtr.is_error_span(span))

    def test_no_tags_is_not_an_error(self):
        self.assertFalse(gtr.is_error_span({"tags": []}))
        self.assertFalse(gtr.is_error_span({}))


class SpanDepthTest(unittest.TestCase):
    def test_depth_chain(self):
        trace = make_trace([
            ("A", None, 0, 100, "opA"),
            ("B", "A", 0, 50, "opB"),
            ("C", "B", 0, 10, "opC"),
        ])
        by_id = {s["spanID"]: s for s in trace["spans"]}
        self.assertEqual(gtr.span_depth("A", by_id), 0)
        self.assertEqual(gtr.span_depth("B", by_id), 1)
        self.assertEqual(gtr.span_depth("C", by_id), 2)

    def test_depth_of_unknown_span_is_zero(self):
        self.assertEqual(gtr.span_depth("missing", {}), 0)


class TraceShapeHelperTest(unittest.TestCase):
    def test_trace_duration_and_services(self):
        trace = make_trace([
            ("A", None, 0, 100, "opA", None, "frontend"),
            ("B", "A", 20, 30, "opB", None, "customer"),
        ])
        self.assertEqual(gtr.trace_duration_us(trace), 100)
        self.assertEqual(gtr.trace_services(trace), ["customer", "frontend"])
        self.assertEqual(len(trace["spans"]), 2)

    def test_parent_span_id_uses_child_of_reference_not_deprecated_field(self):
        span = {
            "spanID": "B",
            "parentSpanID": "WRONG",  # deprecated field; must be ignored
            "references": [{"refType": "FOLLOWS_FROM", "spanID": "X"},
                            {"refType": "CHILD_OF", "spanID": "A"}],
        }
        self.assertEqual(gtr.parent_span_id(span), "A")

    def test_parent_span_id_none_for_root(self):
        span = {"spanID": "A", "references": []}
        self.assertIsNone(gtr.parent_span_id(span))


class FetchArm2TracesVolumeCapTest(unittest.TestCase):
    """(g) fetch_arm2_traces must refuse (Arm2VolumeError) rather than
    silently write ground truth over an unenumerable candidate set - design
    doc, Fixture section. fetch_traces (the actual HTTP call) is mocked; no
    network access."""

    def test_over_cap_raises_arm2_volume_error(self):
        traces = [{"traceID": f"t{i}", "spans": []} for i in range(101)]
        with mock.patch("ground_truth_resolver.fetch_traces", return_value=traces):
            with self.assertRaises(gtr.Arm2VolumeError):
                gtr.fetch_arm2_traces("http://fake-jaeger", "driver")

    def test_at_cap_does_not_raise(self):
        traces = [{"traceID": f"t{i}", "spans": []} for i in range(100)]
        with mock.patch("ground_truth_resolver.fetch_traces", return_value=traces):
            result = gtr.fetch_arm2_traces("http://fake-jaeger", "driver")
        self.assertEqual(len(result), 100)


class Resolve24EarliestFailingSpanPinTest(unittest.TestCase):
    """(f) resolve_24_attribute_hunt's two-stage pin (design doc, Tasks
    section: "Task 24 pins the earliest-starting failing span"):
      stage 1 - among traces with >=1 qualifying (error + tagged) span,
                the trace with the LATEST start time wins;
      stage 2 - within that trace, the qualifying span with the EARLIEST
                start time wins (tie-break spanID).
    fetch_arm2_traces is mocked directly (resolve_24 calls it as a bare
    module-level name); no network access.
    """

    def _error_tag(self, driver_id: str) -> list[dict]:
        return [
            {"key": "error", "type": "bool", "value": True},
            {"key": gtr.ATTRIBUTE_HUNT_KEY, "type": "string", "value": driver_id},
        ]

    def test_later_trace_wins_and_earliest_span_wins_within_it(self):
        # Trace A: starts earlier (1_000_000us), one qualifying span -
        # must lose to trace B purely on trace start time, even though it's
        # the only trace with just one candidate (no ambiguity to resolve).
        trace_a = make_trace([
            ("A1", None, 1_000_000, 5_000, "opA", self._error_tag("AAA")),
        ], default_service="driver")

        # Trace B: starts later (2_000_000us via its root span) and has TWO
        # qualifying (failing + tagged) spans - the exact "more than one
        # failing span per erroring trace" case the design doc calls out
        # (hotrod's fault injection). B_Y starts before B_X, so B_Y's value
        # must win despite B_X appearing first in the spans list.
        trace_b = make_trace([
            ("B_root", None, 2_000_000, 200_000, "opRoot"),
            ("B_X", "B_root", 2_100_000, 500, "opX", self._error_tag("XVAL")),
            ("B_Y", "B_root", 2_050_000, 500, "opY", self._error_tag("YVAL")),
        ], default_service="driver")

        with mock.patch("ground_truth_resolver.fetch_arm2_traces", return_value=[trace_a, trace_b]):
            result = gtr.resolve_24_attribute_hunt("http://fake-jaeger", service="driver")

        self.assertEqual(result["value"], "YVAL")
        self.assertEqual(result["span_id"], "B_Y")
        # Pinned order within the winning trace: earliest-start first.
        self.assertEqual(result["all_failing_ids"], ["YVAL", "XVAL"])

    def test_no_qualifying_span_anywhere_is_unscorable_shaped(self):
        trace = make_trace([("A1", None, 0, 100, "opA")], default_service="driver")
        with mock.patch("ground_truth_resolver.fetch_arm2_traces", return_value=[trace]):
            result = gtr.resolve_24_attribute_hunt("http://fake-jaeger", service="driver")
        self.assertIsNone(result["value"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
