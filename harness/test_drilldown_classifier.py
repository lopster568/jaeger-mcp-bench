"""Tests for drilldown_classifier.py (arm 2, H3).

Run: harness/.venv/bin/python -m unittest test_drilldown_classifier -v
     (from harness/)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

import drilldown_classifier as dd


def steps(*tools: str):
    return [SimpleNamespace(tool=t) for t in tools]


class BareToolNameTests(unittest.TestCase):
    def test_strips_mcp_prefix(self):
        self.assertEqual(dd.bare_tool_name("mcp__jaeger-bench__get_services"), "get_services")

    def test_strips_mcp_prefix_multi_underscore_tool_name(self):
        self.assertEqual(dd.bare_tool_name("mcp__jaeger-bench__get_span_details"), "get_span_details")

    def test_non_mcp_tool_name_passthrough(self):
        self.assertEqual(dd.bare_tool_name("Bash"), "Bash")

    def test_empty_after_prefix(self):
        # Degenerate but must not crash; empty string can't match any known
        # tool set so it's harmless either way.
        self.assertEqual(dd.bare_tool_name("mcp__"), "")


class ClassifyTrialTests(unittest.TestCase):
    def test_conforms_discovery_then_verbose(self):
        s = steps(
            "mcp__jaeger-bench__get_services",
            "mcp__jaeger-bench__search_traces",
            "mcp__jaeger-bench__get_trace_errors",
        )
        r = dd.classify_trial(s, trial_id="t1")
        self.assertEqual(r.classification, dd.CONFORMS)
        self.assertEqual(r.first_discovery_tool, "get_services")
        self.assertEqual(r.first_discovery_index, 0)
        self.assertEqual(r.first_verbose_tool, "get_trace_errors")
        self.assertEqual(r.first_verbose_index, 2)

    def test_violates_verbose_first_no_discovery(self):
        s = steps(
            "mcp__jaeger-bench__get_span_details",
            "mcp__jaeger-bench__get_services",
        )
        r = dd.classify_trial(s, trial_id="t2")
        self.assertEqual(r.classification, dd.VIOLATES)
        self.assertEqual(r.first_verbose_index, 0)
        # discovery after the verbose call doesn't count, and the loop
        # breaks at the first verbose call so it's never even seen.
        self.assertIsNone(r.first_discovery_index)

    def test_violates_verbose_only_no_discovery_anywhere(self):
        s = steps("mcp__jaeger-bench__get_trace_errors")
        r = dd.classify_trial(s, trial_id="t3")
        self.assertEqual(r.classification, dd.VIOLATES)

    def test_no_verbose_call(self):
        s = steps(
            "mcp__jaeger-bench__get_services",
            "mcp__jaeger-bench__search_traces",
            "mcp__jaeger-bench__get_trace_topology",
        )
        r = dd.classify_trial(s, trial_id="t4")
        self.assertEqual(r.classification, dd.NO_VERBOSE_CALL)
        self.assertIsNone(r.first_verbose_index)

    def test_empty_trajectory_is_no_verbose_call(self):
        r = dd.classify_trial(steps(), trial_id="t5")
        self.assertEqual(r.classification, dd.NO_VERBOSE_CALL)

    def test_only_the_first_verbose_call_matters(self):
        # Discovery happens between two verbose calls - irrelevant, since
        # the classification is decided at the *first* verbose call.
        s = steps(
            "mcp__jaeger-bench__get_span_details",       # verbose, no discovery yet -> violates
            "mcp__jaeger-bench__get_services",             # discovery, but too late
            "mcp__jaeger-bench__get_trace_errors",         # second verbose, irrelevant
        )
        r = dd.classify_trial(s, trial_id="t6")
        self.assertEqual(r.classification, dd.VIOLATES)
        self.assertEqual(r.first_verbose_tool, "get_span_details")

    def test_neutral_tools_do_not_count_as_discovery_or_verbose(self):
        s = steps(
            "mcp__jaeger-bench__get_critical_path",
            "mcp__jaeger-bench__get_service_dependencies",
            "mcp__jaeger-bench__get_span_details",
        )
        r = dd.classify_trial(s, trial_id="t7")
        self.assertEqual(r.classification, dd.VIOLATES)
        self.assertIsNone(r.first_discovery_index)

    def test_read_skill_counts_as_discovery(self):
        s = steps(
            "mcp__jaeger-bench__read_skill",
            "mcp__jaeger-bench__get_span_details",
        )
        r = dd.classify_trial(s, trial_id="t8")
        self.assertEqual(r.classification, dd.CONFORMS)
        self.assertEqual(r.first_discovery_tool, "read_skill")


class ClassifyTrajectoryFileTests(unittest.TestCase):
    def test_loads_and_classifies_from_json(self):
        traj = {
            "steps": [
                {"index": 0, "tool": "mcp__jaeger-bench__get_services", "input_json": "{}",
                 "input_bytes": 2, "result_bytes": 10, "is_error": False},
                {"index": 1, "tool": "mcp__jaeger-bench__get_span_details", "input_json": "{}",
                 "input_bytes": 2, "result_bytes": 500, "is_error": False},
            ],
            "tool_calls": 2, "error_calls": 0, "result_bytes_total": 510,
            "final_answer": "x", "num_turns": 5, "usage": {}, "duration_ms": 1000,
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "trial_0.json"
            p.write_text(json.dumps(traj))
            r = dd.classify_trajectory_file(p)
        self.assertEqual(r.classification, dd.CONFORMS)
        self.assertEqual(r.trial_id, "trial_0")

    def test_explicit_trial_id_overrides_filename(self):
        traj = {"steps": []}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "whatever.json"
            p.write_text(json.dumps(traj))
            r = dd.classify_trajectory_file(p, trial_id="custom-id")
        self.assertEqual(r.trial_id, "custom-id")
        self.assertEqual(r.classification, dd.NO_VERBOSE_CALL)


class AggregateTests(unittest.TestCase):
    def test_proportion_excludes_no_verbose_call_trials(self):
        results = [
            dd.classify_trial(steps("mcp__jaeger-bench__get_services",
                                     "mcp__jaeger-bench__get_trace_errors")),  # conforms
            dd.classify_trial(steps("mcp__jaeger-bench__get_span_details")),  # violates
            dd.classify_trial(steps("mcp__jaeger-bench__get_services")),      # no_verbose_call
        ]
        agg = dd.aggregate(results)
        self.assertEqual(agg.n, 3)
        self.assertEqual(agg.conforms, 1)
        self.assertEqual(agg.violates, 1)
        self.assertEqual(agg.no_verbose_call, 1)
        # denominator is conforms+violates=2, not n=3
        self.assertAlmostEqual(agg.proportion_conforms, 0.5)

    def test_empty_input(self):
        agg = dd.aggregate([])
        self.assertEqual(agg.n, 0)
        self.assertEqual(agg.proportion_conforms, 0.0)

    def test_all_no_verbose_call_gives_zero_proportion_not_divide_by_zero(self):
        results = [
            dd.classify_trial(steps("mcp__jaeger-bench__get_services")),
            dd.classify_trial(steps()),
        ]
        agg = dd.aggregate(results)
        self.assertEqual(agg.no_verbose_call, 2)
        self.assertEqual(agg.proportion_conforms, 0.0)

    def test_all_conform(self):
        results = [
            dd.classify_trial(steps("mcp__jaeger-bench__search_traces",
                                     "mcp__jaeger-bench__get_span_details"))
            for _ in range(4)
        ]
        agg = dd.aggregate(results)
        self.assertEqual(agg.proportion_conforms, 1.0)


if __name__ == "__main__":
    unittest.main()
