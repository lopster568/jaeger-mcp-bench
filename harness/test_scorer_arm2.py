"""Unit tests for the arm 2 (docs/arm2-design.md) additions to scorer.py.

Exercises scorer.score() end-to-end (dispatch table + is_non_answer +
per-task handler) against hand-built ground_truth dicts shaped exactly like
harness/ground_truth_resolver.py's resolve_21..26 output
({"value": {...}, ...other diagnostic keys}), one correct / incorrect /
non_answer case per task.

Run: harness/.venv/bin/python -m unittest harness.test_scorer_arm2 -v
     (from the repo root) or `cd harness && ../harness/.venv/bin/python -m
     unittest test_scorer_arm2 -v`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import scorer  # noqa: E402


def gt(task_id: str, value) -> dict:
    return {task_id: {"value": value}}


class UnscorableGuardTest(unittest.TestCase):
    """FIX 1: score() must not crash and must return 'unscorable' - not
    'incorrect' and not an exception - whenever the resolver failed to
    produce ground truth, regardless of what the agent answered."""

    def test_value_none_with_error_is_unscorable_not_a_crash(self):
        g = {"23_trace_shape": {"value": None, "error": "Arm2VolumeError: too many traces"}}
        verdict, expl = scorer.score(
            "23_trace_shape",
            "The trace has 7 spans across the driver and redis-manual services.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_UNSCORABLE)
        self.assertIn("Arm2VolumeError", expl)

    def test_missing_task_entry_is_unscorable(self):
        verdict, _ = scorer.score("23_trace_shape", "7 spans.", {})
        self.assertEqual(verdict, scorer.VERDICT_UNSCORABLE)

    def test_entry_without_value_key_is_unscorable(self):
        g = {"23_trace_shape": {"error": "no qualifying trace"}}
        verdict, _ = scorer.score("23_trace_shape", "7 spans.", g)
        self.assertEqual(verdict, scorer.VERDICT_UNSCORABLE)

    def test_value_none_with_no_error_field_still_unscorable(self):
        g = {"24_attribute_hunt": {"value": None}}
        verdict, expl = scorer.score("24_attribute_hunt", "T7g8f9AbC", g)
        self.assertEqual(verdict, scorer.VERDICT_UNSCORABLE)
        self.assertIn("missing", expl)


class Task21ErrorRootCauseTest(unittest.TestCase):
    expected = {"service": "redis-manual", "operation": "GetDriver"}

    def test_correct(self):
        g = gt("21_error_root_cause", self.expected)
        verdict, _ = scorer.score(
            "21_error_root_cause",
            "The error originates on the redis-manual service, in the GetDriver span.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_wrong_service_and_operation(self):
        g = gt("21_error_root_cause", self.expected)
        verdict, _ = scorer.score(
            "21_error_root_cause",
            "The error originates on the driver service, in the FindNearest span.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("21_error_root_cause", self.expected)
        verdict, _ = scorer.score(
            "21_error_root_cause",
            "I cannot determine the root cause from the available data.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)


class Task22CriticalPathTest(unittest.TestCase):
    expected = {"service": "route", "operation": "GET /route"}

    def test_correct(self):
        g = gt("22_critical_path", self.expected)
        verdict, _ = scorer.score(
            "22_critical_path",
            "route's GET /route operation contributes the most self time.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_names_a_different_operation(self):
        g = gt("22_critical_path", self.expected)
        verdict, _ = scorer.score(
            "22_critical_path",
            "frontend's GET /dispatch handler contributes the most self time.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("22_critical_path", self.expected)
        verdict, _ = scorer.score(
            "22_critical_path",
            "Unable to determine the self-time breakdown from the data returned.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)

    def test_bare_path_accepted_when_full_operation_name_absent(self):
        """FIX 6: for 'METHOD /path'-shaped operations, the bare path token
        ('/route') is accepted when the full 'GET /route' string never
        appears - agents often drop the HTTP verb."""
        g = gt("22_critical_path", self.expected)
        verdict, expl = scorer.score(
            "22_critical_path",
            "route's /route endpoint contributes the most self time.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)


class Task23TraceShapeTest(unittest.TestCase):
    expected = {"span_count": 7, "services": ["driver", "redis-manual"]}

    def test_correct(self):
        g = gt("23_trace_shape", self.expected)
        verdict, _ = scorer.score(
            "23_trace_shape",
            "The trace has 7 spans across the driver and redis-manual services.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_wrong_span_count(self):
        g = gt("23_trace_shape", self.expected)
        verdict, _ = scorer.score(
            "23_trace_shape",
            "The trace has 9 spans across the driver and redis-manual services.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_incorrect_missing_a_service(self):
        g = gt("23_trace_shape", self.expected)
        verdict, _ = scorer.score(
            "23_trace_shape",
            "The trace has 7 spans, all on the driver service.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("23_trace_shape", self.expected)
        verdict, _ = scorer.score(
            "23_trace_shape",
            "I can't tell from the tool output how many spans or services are involved.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)


class SpanCountExtractionTest(unittest.TestCase):
    """FIX 4: span-anchored count extraction (scorer._extract_span_counts),
    replacing the old any-integer-anywhere match."""

    def test_number_then_word_bare(self):
        self.assertEqual(scorer._extract_span_counts("The trace contains 57 spans."), [57])

    def test_number_then_word_total(self):
        self.assertEqual(scorer._extract_span_counts("57 total spans"), [57])

    def test_word_then_number_count_colon(self):
        self.assertEqual(scorer._extract_span_counts("span count: 57"), [57])

    def test_word_then_number_capitalized(self):
        self.assertEqual(scorer._extract_span_counts("Spans: 57"), [57])

    def test_last_24_hours_is_not_a_span_count(self):
        # No "span"/"spans" token anywhere near "24" - must not match.
        self.assertEqual(scorer._extract_span_counts("in the last 24 hours"), [])

    def test_decimal_latency_is_not_a_span_count(self):
        # "57" here is the fractional part of "1.57 s", not a span count,
        # even though the sentence also mentions "span".
        text = "The span response time was 1.57 s."
        self.assertEqual(scorer._extract_span_counts(text), [])

    def test_combined_negatives_do_not_leak_into_scoring(self):
        """End-to-end: an answer containing both classic false-positive
        numbers (a 24h window mention and a 1.57s decimal) alongside the
        real span-anchored count must score on the real count only."""
        expected = {"span_count": 7, "services": ["driver", "redis-manual"]}
        g = gt("23_trace_shape", expected)
        ans = (
            "Over the last 24 hours, the slowest trace (1.57 s) contains 7 "
            "spans across the driver and redis-manual services."
        )
        verdict, expl = scorer.score("23_trace_shape", ans, g)
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_fallback_single_bare_integer_used_when_no_span_word(self):
        # No "span" token at all; exactly one integer in the whole answer -
        # fallback path should treat it as the count.
        expected = {"span_count": 7, "services": ["driver", "redis-manual"]}
        g = gt("23_trace_shape", expected)
        verdict, expl = scorer.score(
            "23_trace_shape", "7. driver, redis-manual.", g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_fallback_does_not_apply_with_multiple_bare_integers(self):
        # No "span" token, and MORE than one bare integer - too ambiguous
        # for the single-integer fallback, so the wrong count (9) must not
        # be accidentally accepted as correct via fallback over-triggering.
        expected = {"span_count": 7, "services": ["driver", "redis-manual"]}
        g = gt("23_trace_shape", expected)
        verdict, _ = scorer.score(
            "23_trace_shape", "somewhere between 7 and 9, driver, redis-manual.", g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)


class Task24AttributeHuntTest(unittest.TestCase):
    expected = "T7g8f9AbC"

    def test_correct(self):
        g = gt("24_attribute_hunt", self.expected)
        verdict, _ = scorer.score(
            "24_attribute_hunt",
            "The param.driverID attribute on the failing span is T7g8f9AbC.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_wrong_value(self):
        g = gt("24_attribute_hunt", self.expected)
        verdict, _ = scorer.score(
            "24_attribute_hunt",
            "The param.driverID attribute on the failing span is T1x2y3ZzZ.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_incorrect_case_mismatch(self):
        """Case-sensitive by design: this is a verbatim ID transcription,
        not a natural-language claim - see scorer.py's module note."""
        g = gt("24_attribute_hunt", self.expected)
        verdict, _ = scorer.score("24_attribute_hunt", "The value is t7g8f9abc.", g)
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("24_attribute_hunt", self.expected)
        verdict, _ = scorer.score(
            "24_attribute_hunt",
            "I am unable to retrieve the attribute value from the trace data.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)


class Task25DependencyTest(unittest.TestCase):
    expected = {"callers": ["customer"], "target_operation": "SQL SELECT"}

    def test_correct(self):
        g = gt("25_dependency", self.expected)
        verdict, _ = scorer.score(
            "25_dependency",
            "Only the customer service directly calls mysql, via a SQL SELECT span.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_correct_without_operation_name(self):
        """Operation name is secondary color and does not gate correctness -
        same 'primary signal wins' precedent as arm 1's _score_02/_score_04."""
        g = gt("25_dependency", self.expected)
        verdict, _ = scorer.score("25_dependency", "customer calls mysql directly.", g)
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_names_a_different_caller(self):
        g = gt("25_dependency", self.expected)
        verdict, _ = scorer.score(
            "25_dependency", "The frontend service calls mysql directly.", g
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("25_dependency", self.expected)
        verdict, _ = scorer.score(
            "25_dependency",
            "I cannot determine which service calls mysql from the data available.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)

    def test_hedged_answer_naming_an_extra_service_is_incorrect(self):
        """FIX 5: strict list. The task prompt demands 'exactly the direct
        callers, no others', so hedging by also asserting frontend (not an
        expected caller, no negation context) fails even though the true
        caller (customer) is also present."""
        g = gt("25_dependency", self.expected)
        verdict, expl = scorer.score(
            "25_dependency",
            "The direct callers of mysql are customer and possibly frontend.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT, expl)

    def test_both_hedge_from_smoke_run_is_incorrect(self):
        """The reviewer's original hedge example: positive assertion of a
        non-caller with no negation cue anywhere near it."""
        g = gt("25_dependency", self.expected)
        verdict, expl = scorer.score(
            "25_dependency",
            "Both frontend and customer call mysql directly, via SQL SELECT.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT, expl)

    def test_negated_mentions_are_not_extras_regression_d78bb27b(self):
        """Regression from run d78bb27b: exemplary answers that name
        non-callers only to EXCLUDE them were scored incorrect by the
        any-mention rule. All three real answers below must score correct."""
        g = gt("25_dependency", self.expected)
        real_answers = [
            # claude/tiered trial_0
            "**Direct caller of `mysql`:** `customer` - and it is the *only* "
            "direct caller. Confirmed two ways: the only edge into `mysql` is "
            "`customer -> mysql` (52 calls). `driver` calls `redis-manual`, "
            "not mysql. Operation on the mysql span: `SQL SELECT`.",
            # claude/flat trial_0
            "**Direct caller of mysql:** `customer` - and only `customer`. No "
            "other hotrod service (`frontend`, `route`, `driver`) calls mysql "
            "directly. Operation name: SQL SELECT.",
            # claude/flat trial_2 - includes 'route' as a verb
            "**Direct caller of mysql:** `customer` (no other hotrod service "
            "- frontend, driver, route - calls mysql directly; they route "
            "through customer). **Operation name on mysql spans:** `SQL SELECT`",
        ]
        for ans in real_answers:
            verdict, expl = scorer.score("25_dependency", ans, g)
            self.assertEqual(verdict, scorer.VERDICT_CORRECT, f"{expl}\n{ans[:80]}")

    def test_chain_narration_is_not_an_assertion_regression_c429fafa(self):
        """Regression from run c429fafa claude/tiered/25: a correct answer
        narrates the call chain (frontend -> customer -> mysql) and names
        frontend while establishing context; only the token immediately
        before mysql in a chain is a direct-caller assertion."""
        g = gt("25_dependency", self.expected)
        ans = (
            "The trace is rooted at frontend. Walking the chain frontend -> "
            "customer -> mysql: this confirms the pattern - `customer` calls "
            "`mysql` directly (span `f961c8a31ac69f04`, child of the "
            "`/customer` span), with span name `SQL SELECT`.\n\n**Answer:**\n"
            "- **Direct caller of mysql:** `customer` (only service that "
            "calls mysql directly)\n- **Operation:** `SQL SELECT`"
        )
        verdict, expl = scorer.score("25_dependency", ans, g)
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_long_exclusion_list_with_alias_regression_c429fafa_flat(self):
        """Regression from run c429fafa claude/flat/25 trial_1: the negation
        cue heads a long parenthetical exclusion list, so it sits further
        from the aliased 'redis' hit than any fixed lookback window - the
        guard must be sentence-scoped."""
        g = gt("25_dependency", self.expected)
        ans = (
            "**Direct caller of `mysql`:** only the **`customer`** service - "
            "every mysql span's parent is a span in `customer`. No other "
            "hotrod service (`frontend`, `driver`, `route`, `mongodb`, "
            "`redis`) calls mysql directly.\n\n**Operation name:** `SQL SELECT`"
        )
        verdict, expl = scorer.score("25_dependency", ans, g)
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_arrow_chain_direct_link_is_an_assertion(self):
        """The inverse of chain narration: an arrow pointing straight at
        mysql from a non-caller IS an assertion and must fail."""
        g = gt("25_dependency", self.expected)
        verdict, expl = scorer.score(
            "25_dependency", "Call graph: frontend -> mysql, customer -> mysql.", g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT, expl)

    def test_exact_bare_list_is_correct(self):
        g = gt("25_dependency", self.expected)
        verdict, expl = scorer.score("25_dependency", "customer.", g)
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_redis_alias_accepted_as_caller(self):
        """FIX 6: bare 'redis' is accepted for 'redis-manual' (unambiguous
        in the hotrod service universe)."""
        g = gt("25_dependency", {"callers": ["redis-manual"], "target_operation": "SQL SELECT"})
        verdict, expl = scorer.score(
            "25_dependency", "Only redis directly calls mysql.", g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT, expl)

    def test_redis_alias_still_counts_as_an_extra_when_not_expected(self):
        # Same alias, but here redis-manual is NOT an expected caller, so
        # naming it (even as "redis") must still trip the strict-extras
        # check.
        g = gt("25_dependency", self.expected)
        verdict, expl = scorer.score(
            "25_dependency", "customer and redis both call mysql.", g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT, expl)


class Task26CompareTracesTest(unittest.TestCase):
    expected = {"service": "route", "operation": "GET /route"}

    def test_correct(self):
        g = gt("26_compare_traces", self.expected)
        verdict, _ = scorer.score(
            "26_compare_traces",
            "Most of the extra latency comes from route's GET /route operation.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_CORRECT)

    def test_incorrect_names_a_different_operation(self):
        g = gt("26_compare_traces", self.expected)
        verdict, _ = scorer.score(
            "26_compare_traces",
            "Most of the extra latency comes from the customer service's GET /customer call.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_INCORRECT)

    def test_non_answer(self):
        g = gt("26_compare_traces", self.expected)
        verdict, _ = scorer.score(
            "26_compare_traces",
            "I can't compare the two traces with the information returned.",
            g,
        )
        self.assertEqual(verdict, scorer.VERDICT_NON_ANSWER)


if __name__ == "__main__":
    unittest.main()
