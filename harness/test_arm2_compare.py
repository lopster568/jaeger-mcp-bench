"""Unit tests for harness/arm2_compare.py (docs/arm2-design.md's four
registered two-proportion z-tests: claude tiered-vs-flat, gemini
tiered-vs-flat, tiered claude-vs-gemini, flat claude-vs-gemini).

Reuses the same synthetic-run-dir fixture pattern as test_aggregate_v2.py -
a tiny tmpdir of trial_*.json files, no live fixture/CLI needed. Z-test math
itself is exercised against hand-computed values, not just "did it run".

Run: harness/.venv/bin/python -m unittest test_arm2_compare -v
     (from harness/)
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import arm2_compare  # noqa: E402
import scorer  # noqa: E402
from cross_model_compare import _two_prop_z  # noqa: E402


def write_trial(run_dir: Path, *, model: str, fmt: str, task_id: str, trial: int, result: dict) -> None:
    out_dir = run_dir / model / fmt / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": "testrun", "model": model, "format": fmt, "task_id": task_id,
        "trial": trial, "ts_unix": 0, "result": result,
    }
    (out_dir / f"trial_{trial}.json").write_text(json.dumps(record))


def claude_result(answer: str, *, input_tokens=100, output_tokens=50, cache_creation=20,
                   cache_read=10, tool_calls=2, duration_ms=1000) -> dict:
    return {
        "answer": answer,
        "budget_exhausted": False,
        "error": None,
        "raw_output": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
    }


class Arm2CompareAggregateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.ground_truth = {
            "05_threshold": {"value": True},
            "23_trace_shape": {"value": None, "error": "Arm2VolumeError: too many traces"},
        }

    def test_arm2_formats_only_other_formats_ignored(self):
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=0,
                    result=claude_result("Yes."))
        write_trial(self.run_dir, model="claude", fmt="summary", task_id="05_threshold", trial=0,
                    result=claude_result("Yes."))  # arm-1 cell mixed into the same run dir
        rows = arm2_compare.aggregate_run(self.run_dir, self.ground_truth)
        formats = {r["format"] for r in rows}
        self.assertEqual(formats, {"tiered"})

    def test_unscorable_excluded_from_correct_incorrect_and_denominator(self):
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="23_trace_shape", trial=0,
                    result=claude_result("The trace has 7 spans."))
        rows = arm2_compare.aggregate_run(self.run_dir, self.ground_truth)
        row = rows[0]
        self.assertEqual(row["n_trials"], 1)
        self.assertEqual(row["n_unscorable"], 1)
        self.assertEqual(row["n_scorable"], 0)
        self.assertEqual(row["correct"], 0)
        self.assertEqual(row["pass_pow_n"], 0)

    def test_per_cell_token_and_duration_means(self):
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=0,
                    result=claude_result("Yes.", cache_creation=100, duration_ms=500))
        write_trial(self.run_dir, model="claude", fmt="tiered", task_id="05_threshold", trial=1,
                    result=claude_result("Yes.", cache_creation=200, duration_ms=1500))
        rows = arm2_compare.aggregate_run(self.run_dir, self.ground_truth)
        row = rows[0]
        # normalize_tokens maps claude's cache_creation_input_tokens to
        # input_format_tokens (cross_model_compare.py semantics, reused).
        self.assertEqual(row["mean_input_format_tokens"], 150.0)
        self.assertEqual(row["mean_duration_ms"], 1000)


class Arm2CompareZTestTest(unittest.TestCase):
    """Z-test math on a synthetic run: builds a run dir where each of the
    four registered comparisons has a known, hand-computable correct/n."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.ground_truth = {"05_threshold": {"value": True}}

    def _populate(self, model: str, fmt: str, n_correct: int, n_incorrect: int) -> None:
        i = 0
        for _ in range(n_correct):
            write_trial(self.run_dir, model=model, fmt=fmt, task_id="05_threshold", trial=i,
                        result=claude_result("Yes."))
            i += 1
        for _ in range(n_incorrect):
            write_trial(self.run_dir, model=model, fmt=fmt, task_id="05_threshold", trial=i,
                        result=claude_result("No."))
            i += 1

    def test_registered_pairs_and_z_values_match_hand_computation(self):
        # claude/tiered: 8/10 correct, claude/flat: 2/10 correct -
        # a stark difference so the z-test result is unambiguous.
        self._populate("claude", "tiered", 8, 2)
        self._populate("claude", "flat", 2, 8)
        self._populate("gemini", "tiered", 5, 5)
        self._populate("gemini", "flat", 5, 5)

        rows = arm2_compare.aggregate_run(self.run_dir, self.ground_truth)
        arm_counts = arm2_compare.arm_counts_from_rows(rows)

        self.assertEqual(arm_counts[("claude", "tiered")], {"correct": 8, "n": 10})
        self.assertEqual(arm_counts[("claude", "flat")], {"correct": 2, "n": 10})
        self.assertEqual(arm_counts[("gemini", "tiered")], {"correct": 5, "n": 10})
        self.assertEqual(arm_counts[("gemini", "flat")], {"correct": 5, "n": 10})

        z_results = arm2_compare.run_registered_z_tests(arm_counts)
        self.assertEqual(len(z_results), 4)
        labels = [zr["comparison"] for zr in z_results]
        self.assertEqual(labels, [
            "claude tiered vs flat",
            "gemini tiered vs flat",
            "tiered claude vs gemini",
            "flat claude vs gemini",
        ])

        expected_z, expected_p = _two_prop_z(8, 10, 2, 10)
        claude_pair = z_results[0]
        self.assertAlmostEqual(claude_pair["z"], expected_z)
        self.assertAlmostEqual(claude_pair["p"], expected_p)
        self.assertTrue(claude_pair["z"] != 0.0)
        # 8/10 vs 2/10 is a large, significant difference even under the
        # Bonferroni-corrected threshold.
        self.assertLess(claude_pair["p"], arm2_compare.BONFERRONI_ALPHA)
        self.assertTrue(claude_pair["sig_bonferroni"])

        gemini_pair = z_results[1]
        self.assertEqual(gemini_pair["a_correct"], 5)
        self.assertEqual(gemini_pair["b_correct"], 5)
        self.assertAlmostEqual(gemini_pair["z"], 0.0)
        self.assertFalse(gemini_pair["sig_bonferroni"])

    def test_bonferroni_alpha_is_0_0125(self):
        self.assertAlmostEqual(arm2_compare.BONFERRONI_ALPHA, 0.0125)

    def test_zero_trials_for_an_arm_does_not_crash(self):
        self._populate("claude", "tiered", 3, 0)
        # No trials at all for claude/flat, gemini/tiered, gemini/flat.
        rows = arm2_compare.aggregate_run(self.run_dir, self.ground_truth)
        arm_counts = arm2_compare.arm_counts_from_rows(rows)
        z_results = arm2_compare.run_registered_z_tests(arm_counts)
        self.assertEqual(len(z_results), 4)
        for zr in z_results:
            self.assertTrue(math.isfinite(zr["z"]))
            self.assertTrue(math.isfinite(zr["p"]))


if __name__ == "__main__":
    unittest.main()
