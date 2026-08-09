"""Unit tests for the arm 2 (docs/arm2-design.md) additions to
aggregate_v2.py: n_unscorable / n_budget_exhausted / n_timeout columns, and
unscorable trials excluded from the pass_pow_n denominator.

Builds a tiny synthetic run dir (tmpdir) of trial_*.json files shaped like
orchestrator.py's actual output and calls aggregate() directly - no live
fixture or CLI needed.

Run: harness/.venv/bin/python -m unittest test_aggregate_v2 -v
     (from harness/)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aggregate_v2  # noqa: E402
import scorer  # noqa: E402


def write_trial(run_dir: Path, *, model: str, fmt: str, task_id: str, trial: int, result: dict) -> None:
    out_dir = run_dir / model / fmt / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": "testrun",
        "model": model,
        "format": fmt,
        "task_id": task_id,
        "trial": trial,
        "ts_unix": 0,
        "result": result,
    }
    (out_dir / f"trial_{trial}.json").write_text(json.dumps(record))


class AggregateArm2ColumnsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.ground_truth = {
            "05_threshold": {"value": True},
            # Resolver-failure shape (scorer.py FIX 1): value=None + error.
            "23_trace_shape": {"value": None, "error": "Arm2VolumeError: too many traces"},
        }

    def _row(self, rows, model, fmt, task_id):
        for r in rows:
            if (r["model"], r["format"], r["task_id"]) == (model, fmt, task_id):
                return r
        self.fail(f"no row for {(model, fmt, task_id)} in {[(r['model'], r['format'], r['task_id']) for r in rows]}")

    def test_new_columns_present_and_unscorable_excluded_from_pass_rate(self):
        # 05_threshold cell: one correct, one incorrect, one budget-exhausted
        # (empty answer -> incorrect, but ALSO counted in n_budget_exhausted),
        # one timeout (empty answer -> incorrect, ALSO counted in n_timeout).
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=0,
                    result={"answer": "Yes, above threshold.", "budget_exhausted": False, "error": None})
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=1,
                    result={"answer": "No.", "budget_exhausted": False, "error": None})
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=2,
                    result={"answer": "", "budget_exhausted": True, "error": "error_max_budget_usd"})
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=3,
                    result={"answer": "", "budget_exhausted": False, "error": "timeout"})

        # 23_trace_shape cell: one trial, but ground truth is unscorable
        # (resolver failure) - must be excluded from correct/incorrect/
        # non_answer AND from the pass_pow_n denominator, regardless of the
        # (irrelevant, in this case correct-looking) answer text.
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="23_trace_shape", trial=0,
                    result={"answer": "The trace has 7 spans.", "budget_exhausted": False, "error": None})

        rows = aggregate_v2.aggregate(self.run_dir, self.ground_truth)

        threshold_row = self._row(rows, "claude", "tiered", "05_threshold")
        self.assertEqual(threshold_row["n_trials"], 4)
        self.assertEqual(threshold_row["correct"], 1)
        self.assertEqual(threshold_row["incorrect"], 3)  # "No." + 2 empty answers
        self.assertEqual(threshold_row["n_unscorable"], 0)
        self.assertEqual(threshold_row["n_budget_exhausted"], 1)
        self.assertEqual(threshold_row["n_timeout"], 1)
        # pass_pow_n denominator is scorable trials (4 - 0 unscorable = 4).
        self.assertEqual(threshold_row["pass_pow_n"], round(1 / 4, 3))

        shape_row = self._row(rows, "claude", "tiered", "23_trace_shape")
        self.assertEqual(shape_row["n_trials"], 1)
        self.assertEqual(shape_row["correct"], 0)
        self.assertEqual(shape_row["incorrect"], 0)
        self.assertEqual(shape_row["non_answer"], 0)
        self.assertEqual(shape_row["n_unscorable"], 1)
        # All trials unscorable -> denominator 0 -> pass_pow_n reported as 0,
        # not a ZeroDivisionError.
        self.assertEqual(shape_row["pass_pow_n"], 0)

    def test_unscorable_with_empty_answer_does_not_become_incorrect(self):
        """Regression guard for the empty-answer shortcut: a trial with NO
        answer text at all (e.g. a hard crash) whose ground truth is also
        unscorable must still report unscorable, not silently fall into the
        'no answer text' -> incorrect shortcut that exists for arm-1 parity.
        """
        write_trial(self.run_dir, model="gemini", fmt="flat", task_id="23_trace_shape", trial=0,
                    result={"answer": "", "budget_exhausted": False, "error": "rc=1"})
        rows = aggregate_v2.aggregate(self.run_dir, self.ground_truth)
        row = self._row(rows, "gemini", "flat", "23_trace_shape")
        self.assertEqual(row["n_unscorable"], 1)
        self.assertEqual(row["incorrect"], 0)

    def test_timeout_detection_is_substring_case_insensitive(self):
        write_trial(self.run_dir, model="claude", fmt="flat", task_id="05_threshold", trial=0,
                    result={"answer": "", "budget_exhausted": False, "error": "Timeout after 600s"})
        rows = aggregate_v2.aggregate(self.run_dir, self.ground_truth)
        row = self._row(rows, "claude", "flat", "05_threshold")
        self.assertEqual(row["n_timeout"], 1)

    def test_existing_columns_still_present_arm1_style(self):
        """Additive-only check: none of the pre-existing columns disappeared
        or got renamed."""
        write_trial(self.run_dir, model="claude", fmt="summary", task_id="05_threshold", trial=0,
                    result={"answer": "Yes.", "budget_exhausted": False, "error": None,
                             "input_tokens": 10, "output_tokens": 5, "tool_calls": 1, "duration_ms": 100})
        rows = aggregate_v2.aggregate(self.run_dir, self.ground_truth)
        row = self._row(rows, "claude", "summary", "05_threshold")
        for col in ("n_trials", "correct", "incorrect", "non_answer", "pass_pow_n", "pass_at_n",
                    "mean_input_tokens", "mean_output_tokens", "mean_cache_creation",
                    "mean_cache_read", "mean_tool_calls", "mean_duration_ms",
                    "sample_correct", "sample_incorrect"):
            self.assertIn(col, row)


if __name__ == "__main__":
    unittest.main()
