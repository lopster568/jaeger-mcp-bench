"""Score agent answers against per-task ground truth.

Each scorer parses the agent's free-text answer against the canonical answer
captured by ground_truth_resolver.py, returning:
    (verdict: "correct" | "incorrect" | "non_answer", explanation: str)

"non_answer" is reserved for cases where the agent explicitly declines to
answer (e.g., "I can't determine from this data"). It's distinct from
"incorrect" so we can analyze the format-driven error pattern (false negatives
via non-answer vs. false positives via overinterpretation).
"""

from __future__ import annotations

import re
from typing import Any


VERDICT_CORRECT = "correct"
VERDICT_INCORRECT = "incorrect"
VERDICT_NON_ANSWER = "non_answer"


# Phrases that indicate the agent explicitly declines to answer.
# Bug fix: the original regex missed common decline phrasings like
# "can't break the hour into buckets" / "can't identify a spike" / "no
# per-bucket time series" / "is insufficient" - letting the keyword
# detectors below mis-fire on subsequent words. Expanded coverage.
_NON_ANSWER_RE = re.compile(
    r"\b(cannot answer|cannot determine|can't determine|"
    r"can't tell|can't break|can't identify|can't say|can't compute|"
    r"can't compare|can't decide|cannot identify|cannot compare|"
    r"insufficient (data|information)|is insufficient|"
    r"not enough (data|information)|"
    r"unable to (answer|determine|tell|fetch|retrieve|compute|identify|compare)|"
    r"no per[- ]bucket|no time[- ]series|no time series|"
    r"no .* (numeric|usable) value|"
    r"the tool (returned|returns) no|"
    r"there (is no|are no) (numeric|usable|per[- ]bucket|time[- ]series)|"
    r"both responses contain only metadata|"
    r"i (cannot|can't) (determine|confirm|answer|identify|tell|compute|compare|break|find))\b",
    re.IGNORECASE,
)


def is_non_answer(text: str) -> bool:
    return bool(_NON_ANSWER_RE.search(text))


def score(task_id: str, agent_answer: str, ground_truth: dict) -> tuple[str, str]:
    """Dispatch to per-task scorer."""
    gt = ground_truth.get(task_id)
    if not gt or "value" not in gt:
        return (VERDICT_INCORRECT, "no ground truth available")

    expected = gt["value"]
    handler = _HANDLERS.get(task_id)
    if not handler:
        return (VERDICT_INCORRECT, f"no scorer for {task_id}")
    return handler(agent_answer, expected, gt)


# ─── per-task scorers ─────────────────────────────────────────────────────

def _score_01_p99_now(ans: str, expected: float, _gt) -> tuple[str, str]:
    """Numeric: extract any number, accept if within an acceptance range.

    DESIGN NOTE: Task 01 ground truth is intrinsically format-divergent. Summary
    mode reports the bucket-max P99 (e.g. 377.5 ms), while a series-reading
    agent typically reports the most-recent bucket P99 (e.g. 249.0 ms). Both
    are defensible answers to "what's the P99 latency for driver over the
    last 60 minutes" given different (but valid) reductions. Additionally,
    the rolling-window aggregate drifts on a minute-scale as buckets enter
    and exit the window.

    For these reasons the tolerance is wide: any value in [50, 1000] ms
    counts as correct, IF the agent commits to a number. This accepts the
    natural format-driven divergence as still "correct in context" and
    moves the discrimination to Task 01's TOKEN delta + completion rate
    rather than its numeric Pass^N.
    """
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    nums = _extract_numbers_with_units(ans)
    if not nums:
        return (VERDICT_INCORRECT, "no numeric value in answer")
    # Accept any number in a generous range that covers both bucket-max
    # and most-recent-bucket interpretations as well as natural drift.
    # For driver over a load with mixed throughput, P99 stays in [50, 1000] ms.
    in_range = [n for n in nums if 50 <= n <= 1000]
    if in_range:
        # Pick the number closest to the expected value for reporting.
        best = min(in_range, key=lambda n: abs(n - expected) if expected else 0)
        delta = (abs(best - expected) / abs(expected) * 100) if expected else 0
        return (VERDICT_CORRECT, f"got={best:.1f}ms (in plausible range; Δ={delta:.1f}% from expected={expected:.1f}ms)")
    return (VERDICT_INCORRECT, f"no plausible P99 value in answer (got={nums[:3]}, range=[50,1000]ms)")


def _score_02_spike_detection(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Spike presence is the primary signal. Magnitude is secondary.

    Tolerant of both verbose agents (Claude) and terse agents (Gemini).
    A bare 'No.' in response to a yes/no question is treated as a committed
    'no spike' answer, not a non-answer.
    """
    expected_spike = bool(expected.get("spike_detected"))
    a = ans.lower().strip()
    # First check: is this an honest "I can't answer" non-answer?
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    # Bare "No" / "Yes" first (Bug fix: previous regex had dead ^/$ anchors inside \b...\b alternation)
    if re.match(r"^\s*no\b", a):
        agent_says_spike = False
    elif re.match(r"^\s*yes\b", a):
        agent_says_spike = True
    else:
        negative = bool(re.search(
            r"\b(no spike|no, |stable|flat|no significant|"
            r"did not spike|no measurable spike|no notable spike|"
            r"latency was (stable|flat)|did not show)\b", a))
        affirmative = bool(re.search(
            r"\b(spike|spiked|elevated|surge|peak)\b", a))
        if negative:
            agent_says_spike = False
        elif affirmative:
            agent_says_spike = True
        else:
            return (VERDICT_NON_ANSWER, "no clear yes/no on spike")
    if agent_says_spike == expected_spike:
        return (VERDICT_CORRECT, f"agent={agent_says_spike} expected={expected_spike}")
    return (VERDICT_INCORRECT, f"agent={agent_says_spike} expected={expected_spike}")


def _score_03_rank_error_rate(ans: str, expected: list[str], _gt) -> tuple[str, str]:
    """Top-3 ordered list. With ties (e.g. all zero) the order is unstable;
    accept any permutation of the expected set as correct."""
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    a = ans.lower()
    found = []
    for s in ["frontend", "customer", "driver", "route"]:
        m = re.search(rf"\b{re.escape(s)}\b", a)
        if m:
            found.append((s, m.start()))
    found.sort(key=lambda x: x[1])
    got_top3 = [s for s, _ in found[:3]]
    if not got_top3:
        return (VERDICT_NON_ANSWER, "no service names in answer")
    # When all rates are tied (e.g. all zero) any 3-of-4 is acceptable.
    rates_full = _gt.get("rates_full", {})
    if rates_full and len(set(rates_full.values())) == 1:
        # Tied: accept if at least 3 of the 4 services appear in answer
        ok = len(set(got_top3) & {"frontend", "customer", "driver", "route"}) == 3
        return (
            VERDICT_CORRECT if ok else VERDICT_INCORRECT,
            f"got={got_top3} (ties: any 3 of 4 accepted)"
        )
    # Non-tied: order matters.
    ok = got_top3 == [s.lower() for s in expected]
    return (
        VERDICT_CORRECT if ok else VERDICT_INCORRECT,
        f"got={got_top3} expected={expected}"
    )


def _score_04_correlation(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Correlated yes/no is the primary signal.

    Tolerant of terse answers like 'No.' which is a committed correlated=False
    response."""
    expected_correlated = bool(expected.get("correlated"))
    a = ans.lower().strip()
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    # Bare-form: handles "No", "No.", "No,", "No correlation observed."
    if re.match(r"^\s*no\b", a):
        agent_says = False
    elif re.match(r"^\s*yes\b", a):
        agent_says = True
    elif re.search(r"\bno\b.*correlat|did not correlat|did not coincide|no correlation|"
                   r"no error[- ]rate spike|no error spike|"
                   r"flat error rate|error rate (was |of )?(0|zero|flat)|"
                   r"no spike to compare", a):
        agent_says = False
    elif re.search(r"\bcorrelated\b|coincid|spike(s)? (was |were )?correlat", a):
        agent_says = True
    else:
        return (VERDICT_NON_ANSWER, "no clear yes/no on correlation")
    if agent_says == expected_correlated:
        return (VERDICT_CORRECT, f"agent={agent_says} expected={expected_correlated}")
    return (VERDICT_INCORRECT, f"agent={agent_says} expected={expected_correlated}")


def _score_05_threshold(ans: str, expected: bool, _gt) -> tuple[str, str]:
    """Boolean: above 5% or not. Tolerant of bare 'No' / 'Yes' answers."""
    a = ans.lower().strip()
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    # Bare-form: handles "No", "No.", "No,", "No more", etc. via word boundary.
    if re.match(r"^\s*no\b", a):
        agent_says = False
    elif re.match(r"^\s*yes\b", a):
        agent_says = True
    elif re.search(r"\bno\b|below|under|less than|0%|0 ?% |not above", a):
        agent_says = False
    elif re.search(r"\byes\b|above|over|exceeds|greater than", a):
        agent_says = True
    else:
        return (VERDICT_NON_ANSWER, "no clear yes/no on threshold")
    if agent_says == expected:
        return (VERDICT_CORRECT, f"agent={agent_says} expected={expected}")
    return (VERDICT_INCORRECT, f"agent={agent_says} expected={expected}")


def _score_06_trend(ans: str, expected: str, _gt) -> tuple[str, str]:
    """Classification: worse | stable | improving.

    Bug fix: the order of branches mattered ("increasing" matched
    `\\bincreas\\b` even when the agent said "no clear trend; rates have been
    increasing slightly recently"). Fixed by:
      1. Match 'stable' phrases FIRST (stable / no clear trend / etc.)
      2. Require word-final variants for trend verbs to avoid stem partials
      3. Reject hedged trends ("slightly increasing" alone insufficient)
    """
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    a = ans.lower()
    # Stable phrases checked first - "no clear trend" should win over an
    # incidental 'increase' word elsewhere in the same answer.
    if re.search(r"\b(stable|flat|unchanged|steady|consistent|constant|"
                 r"no clear trend|no significant trend|no .* trend|"
                 r"no notable trend|no meaningful trend|"
                 r"no errors|0%|zero error)\b", a):
        agent_says = "stable"
    elif re.search(r"\b(getting worse|deteriorat|worsen|worse|"
                   r"increasing|increased|trending up|rising)\b", a):
        agent_says = "worse"
    elif re.search(r"\b(improving|getting better|decreasing|decreased|"
                   r"trending down|falling)\b", a):
        agent_says = "improving"
    else:
        return (VERDICT_NON_ANSWER, "no clear trend classification")
    if agent_says == expected:
        return (VERDICT_CORRECT, f"agent={agent_says} expected={expected}")
    return (VERDICT_INCORRECT, f"agent={agent_says} expected={expected}")


_HANDLERS = {
    "01_p99_now": _score_01_p99_now,
    "02_spike_detection": _score_02_spike_detection,
    "03_rank_error_rate": _score_03_rank_error_rate,
    "04_correlation": _score_04_correlation,
    "05_threshold": _score_05_threshold,
    "06_trend": _score_06_trend,
}


# ─── helpers ──────────────────────────────────────────────────────────────

def _extract_numbers_with_units(text: str) -> list[float]:
    """Extract LATENCY numeric values, normalizing time units to ms.
    Returns only values that plausibly represent latency: ms or seconds (≤60s).

    Bug fix: previous version included ANY number with an optional unit,
    which matched "60 minutes" from prompt-echo and converted to 3.6M ms. The
    [50, 1000] filter on the caller side rescued single cases but obscured
    intent. Fixed by:
      1. Requiring an explicit ms / millisecond / s / sec / second unit
         (no implicit "ms" default - bare numbers without unit are skipped).
      2. Capping seconds at 60 (latency >60s is implausible for these tasks).
      3. Excluding "minute"/"min"/"hour" tokens entirely (those are window
         specs, not latency values).
    """
    out = []
    # Strict: number must have a latency-y unit attached.
    # Match: NUM (ms | millisecond[s] | s | sec | second[s])
    pat = re.compile(
        r"(?<![a-zA-Z])(\d+\.?\d*)\s*(ms|milliseconds?|millisecs?|seconds?|secs?|s)\b",
        re.IGNORECASE,
    )
    for m in pat.finditer(text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit.startswith("ms") or unit.startswith("milli"):
            out.append(v)
        elif unit.startswith("s") or unit.startswith("sec"):
            # Sanity-cap: seconds-units > 60s aren't latency
            if v <= 60:
                out.append(v * 1000)
    return out
