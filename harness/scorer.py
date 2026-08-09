"""Score agent answers against per-task ground truth.

Each scorer parses the agent's free-text answer against the canonical answer
captured by ground_truth_resolver.py, returning:
    (verdict: "correct" | "incorrect" | "non_answer" | "unscorable", explanation: str)

"non_answer" is reserved for cases where the agent explicitly declines to
answer (e.g., "I can't determine from this data"). It's distinct from
"incorrect" so we can analyze the format-driven error pattern (false negatives
via non-answer vs. false positives via overinterpretation).

"unscorable" is reserved for cases where the *resolver*, not the agent,
failed to produce ground truth for a task (missing entry, no "value" key, or
an explicit value=None with an "error" field - see
ground_truth_resolver.py's resolve_24/26 None-value returns for arm 2's
volume-cap and empty-candidate-set cases). This is a crash fix: score()
previously folded "no ground truth" into "incorrect" - a resolver failure
would then silently look like a wrong agent answer instead of a scoring gap.
docs/arm2-design.md's Tasks section registers unscorable as excluded from
pass rates and reported as its own count (see aggregate_v2.py).
"""

from __future__ import annotations

import re
from typing import Any


VERDICT_CORRECT = "correct"
VERDICT_INCORRECT = "incorrect"
VERDICT_NON_ANSWER = "non_answer"
VERDICT_UNSCORABLE = "unscorable"


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
    """Dispatch to per-task scorer.

    Ground truth unavailability is a resolver-side condition, not an agent
    mistake: missing entry, missing "value" key, or an explicit value=None
    (the shape resolve_24/26 etc. return when the fixture had no qualifying
    trace/span for a task) all score "unscorable" rather than "incorrect".
    """
    gt = ground_truth.get(task_id)
    if not gt or "value" not in gt or gt.get("value") is None:
        err = gt.get("error", "missing") if gt else "missing"
        return (VERDICT_UNSCORABLE, f"ground truth unavailable: {err}")

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


# ─── Arm 2 (docs/arm2-design.md): trace-based task scorers ────────────────
#
# Ground truth for these comes from harness/ground_truth_resolver.py's
# trace-based resolvers (resolve_21..26), each writing {"value": {...}} in
# the same snapshot shape arm 1 uses (harness/aggregate_v2.py loads
# `ground_truth[task_id]["value"]` and passes the whole gt dict here
# unchanged - this file never needed to know arm 1 vs arm 2). Same verdict
# discipline as arm 1: tolerant of phrasing, strict on substance. Identifiers
# (service names, operation/span names, attribute values) are matched
# case-insensitively except task 24's raw attribute value, which is an
# opaque ID read verbatim from a span tag and is scored case-sensitively -
# "exact value read" per the task's ground-truth rule, not a natural-language
# claim with normal-casing slack.

_INT_RE = re.compile(r"\b(\d+)\b")

# Task 23 span-count extraction (bug fix: the previous version matched ANY
# integer anywhere in the answer, so an unrelated number in the prose - "24"
# from a "last 24 hours" window mention, or the "57" inside a "1.57 s"
# latency figure - could satisfy `expected_count in ints` by pure
# coincidence. Anchored to the token "span"/"spans" instead, covering both
# orderings agents use:
#   number-then-word:  "57 spans", "57 total spans", "contains 57 spans"
#   word-then-number:  "span count: 57", "Spans: 57"
# A short window (<=20 chars, no sentence boundary) on the word-then-number
# side keeps it from reaching across into an unrelated clause.
_SPAN_COUNT_NUM_FIRST_RE = re.compile(r"(\d+)\s*(?:total\s+)?spans?\b", re.IGNORECASE)
_SPAN_COUNT_WORD_FIRST_RE = re.compile(r"spans?[^.\n]{0,20}?\b(\d+)\b", re.IGNORECASE)


def _extract_ints(text: str) -> list[int]:
    return [int(m) for m in _INT_RE.findall(text)]


def _is_decimal_adjacent(text: str, start: int, end: int) -> bool:
    """True if the digit run text[start:end] is one half of a decimal
    number - e.g. both "1" and "57" in "1.57 s" - and must not be mistaken
    for a whole-number count like a span count.

    Requires a digit on the OTHER side of the '.' too, not just any
    trailing/leading period - otherwise a sentence-ending period after a
    bare integer (e.g. "7." as a terse final answer) would be wrongly
    treated as decimal-adjacent and the number discarded.
    """
    if start > 1 and text[start - 1] == "." and text[start - 2].isdigit():
        return True
    if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
        return True
    return False


def _extract_ints_excluding_decimals(text: str) -> list[int]:
    out = []
    for m in _INT_RE.finditer(text):
        start, end = m.span(1)
        if _is_decimal_adjacent(text, start, end):
            continue
        out.append(int(m.group(1)))
    return out


def _extract_span_counts(text: str) -> list[int]:
    """Span-anchored integer extraction for task 23's span-count question.
    See module comment above _SPAN_COUNT_NUM_FIRST_RE for the bug this
    fixes. Excludes decimal-adjacent digits (see _is_decimal_adjacent) so a
    stray latency figure near the word "span" can't contribute a false
    count either.
    """
    out = []
    for pat in (_SPAN_COUNT_NUM_FIRST_RE, _SPAN_COUNT_WORD_FIRST_RE):
        for m in pat.finditer(text):
            start, end = m.span(1)
            if _is_decimal_adjacent(text, start, end):
                continue
            out.append(int(m.group(1)))
    return out


def _mentions(text: str, term: str, *, case_sensitive: bool = False) -> bool:
    """Containment check for an identifier or short phrase inside free text.

    Plain identifiers (letters/digits/underscore/hyphen only, e.g. service
    names like 'redis-manual' or operation names like 'GetDriver') are
    matched with \\b word boundaries so 'get' doesn't match inside a longer
    word. Terms containing spaces/slashes/dots (e.g. 'GET /dispatch',
    'driver.DriverService/FindNearest') fall back to plain substring search,
    since \\b behaves unpredictably at punctuation edges.
    """
    if not term:
        return False
    flags = 0 if case_sensitive else re.IGNORECASE
    if re.fullmatch(r"[A-Za-z0-9_-]+", term):
        return re.search(rf"\b{re.escape(term)}\b", text, flags) is not None
    haystack = text if case_sensitive else text.lower()
    needle = term if case_sensitive else term.lower()
    return needle in haystack


def _mentions_service(text: str, service: str) -> bool:
    """Service-name containment, with one bounded identifier alias: bare
    "redis" also counts as a hit for "redis-manual". The hotrod fixture's
    service universe (_HOTROD_SERVICE_NAMES) has no other "redis*" service,
    so the shortened form is unambiguous, and agents routinely drop the
    "-manual" suffix when naming it conversationally. Everything else stays
    exact (case-insensitive) via _mentions.
    """
    if _mentions(text, service):
        return True
    if service == "redis-manual" and _mentions(text, "redis"):
        return True
    return False


def _mentions_operation(text: str, operation: str) -> bool:
    """Operation-name containment, with one bounded alias: for
    "METHOD /path"-shaped operations (e.g. "GET /dispatch", "GET /route"),
    also accept the bare path token ("/dispatch") when the full "METHOD
    /path" string is absent - agents commonly refer to a route by its path
    alone, dropping the HTTP verb. Everything else stays exact
    (case-insensitive) via _mentions.
    """
    if _mentions(text, operation):
        return True
    parts = operation.split(" ", 1)
    if len(parts) == 2 and parts[1].startswith("/") and _mentions(text, parts[1]):
        return True
    return False


def _score_service_operation_pair(ans: str, expected: dict, *, method_desc: str) -> tuple[str, str]:
    """Shared scorer body for tasks whose ground truth is a single
    {"service": ..., "operation": ...} pair identified in free text: tasks
    21 (error root cause), 22 (critical path), and 26 (compare traces).
    Correct requires BOTH the expected service name AND the expected
    operation/span name to appear in the answer - naming only the service
    (e.g. 'driver' when the actual origin is redis-manual's GetDriver span)
    is a materially incomplete/wrong answer for these tasks, unlike arm 1's
    yes/no tasks where a secondary detail is genuinely optional color.
    """
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    exp_service = str(expected.get("service", ""))
    exp_operation = str(expected.get("operation", ""))
    got_service = _mentions_service(ans, exp_service)
    got_operation = _mentions_operation(ans, exp_operation)
    if got_service and got_operation:
        return (VERDICT_CORRECT, f"{method_desc}: found service={exp_service!r} and operation={exp_operation!r}")
    # Deliberately INCORRECT, not NON_ANSWER, when neither term is found: this
    # is an open-format identification question (not a small fixed
    # vocabulary), so a committed-but-entirely-wrong answer (e.g. naming a
    # different service/operation altogether) is a very plausible failure
    # mode and must not be miscounted as a decline. is_non_answer() above is
    # what actually catches declines.
    return (
        VERDICT_INCORRECT,
        f"{method_desc}: service_match={got_service} operation_match={got_operation}"
        f" (expected service={exp_service!r} operation={exp_operation!r})",
    )


def _score_21_error_root_cause(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Root-cause service+operation. Both parts required - see
    _score_service_operation_pair."""
    return _score_service_operation_pair(ans, expected, method_desc="root_cause")


def _score_22_critical_path(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Max-self-time service+operation on the slowest frontend trace."""
    return _score_service_operation_pair(ans, expected, method_desc="critical_path")


def _score_23_trace_shape(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Span count (exact) + full service set (all names must be mentioned).

    Both parts required for 'correct', matching the task's two explicit
    questions ("how many spans... and which services"). A committed but
    wrong count, or a partial service list, is 'incorrect' rather than
    'non_answer' - the agent did answer, just not accurately.

    Count extraction is span-anchored (_extract_span_counts), not "any
    integer in the answer" - see that function's docstring for the bug this
    fixes (window/latency numbers elsewhere in the prose false-matching the
    span count). Fallback: if no span-anchored integer is found but the
    answer contains exactly one integer overall (decimal-adjacent digits
    excluded), treat that as the intended count - covers terse answers that
    never say the word "span" at all (e.g. "7. driver, redis-manual.").
    """
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    expected_count = expected.get("span_count")
    expected_services = expected.get("services") or []
    ints = _extract_span_counts(ans)
    if not ints:
        fallback = _extract_ints_excluding_decimals(ans)
        if len(fallback) == 1:
            ints = fallback
    count_ok = expected_count is not None and expected_count in ints
    missing_services = [s for s in expected_services if not _mentions_service(ans, s)]
    services_ok = not missing_services
    if not ints and not any(_mentions_service(ans, s) for s in expected_services):
        return (VERDICT_NON_ANSWER, "no span count or service names found in answer")
    if count_ok and services_ok:
        return (VERDICT_CORRECT, f"got_count in {ints} (expected {expected_count}), all services mentioned")
    return (
        VERDICT_INCORRECT,
        f"count_ok={count_ok} (got={ints}, expected={expected_count});"
        f" missing_services={missing_services or 'none'}",
    )


def _score_24_attribute_hunt(ans: str, expected: str, _gt) -> tuple[str, str]:
    """Exact (case-sensitive) attribute value, read verbatim off the failing
    span. This is a literal ID transcription, not a natural-language claim,
    so no case-folding tolerance - see module note above."""
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    if not expected:
        return (VERDICT_INCORRECT, "no ground truth value available")
    if _mentions(ans, expected, case_sensitive=True):
        return (VERDICT_CORRECT, f"found exact value {expected!r} in answer")
    return (VERDICT_INCORRECT, f"expected value {expected!r} not found verbatim in answer")


# The fixed hotrod service universe (docs/research-log.md; verified against
# examples/hotrod source - see ground_truth_resolver.py's Arm 2 module note).
# Used only to detect WHICH service(s) the agent claims as caller(s), so a
# wrong-but-committed answer (e.g. naming frontend instead of customer) scores
# INCORRECT rather than being missed as a NON_ANSWER - same reasoning as
# _score_service_operation_pair above.
_HOTROD_SERVICE_NAMES = ["frontend", "customer", "driver", "route", "mysql", "redis-manual"]

# Task 25's query target (ground_truth_resolver.DEPENDENCY_TARGET_SERVICE;
# hardcoded here rather than imported since scorer.py deliberately stays
# decoupled from the resolver - see the Arm 2 module note above). Naming the
# target itself in the answer ("X calls mysql") is expected phrasing, not an
# over-list, so it's excluded from the strict-extras check below.
_TASK_25_TARGET_SERVICE = "mysql"


def _score_25_dependency(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Direct callers of the target service - a STRICT bare list.

    The task YAML prompt demands "exactly the direct callers, no others", so
    correct requires all three:
      (a) every expected caller is named (as before), AND
      (b) no other hotrod service (i.e. anything in _HOTROD_SERVICE_NAMES
          that isn't an expected caller or the query target itself) is named
          ANYWHERE in the answer - over-listing (hedging by naming every
          candidate service) now fails, where it previously passed as long
          as the true callers happened to be included, AND
      (c) the target_operation check, unchanged: the observed operation name
          is real, resolver-derived context but stays secondary color, not
          gating correctness - same 'primary signal wins' precedent as arm
          1's spike/correlation scorers (_score_02/_score_04).
    Negated mentions ("frontend does not call mysql") still count as
    "named" and still fail (b) - the prompt demands a bare list, not a
    list-with-caveats, so a hedge that names every candidate and narrows in
    prose afterward does not satisfy "exactly the direct callers, no
    others" either. Accepted as fair given how explicit the prompt is.
    """
    if is_non_answer(ans):
        return (VERDICT_NON_ANSWER, "agent declined to answer")
    expected_callers = expected.get("callers") or []
    if not expected_callers:
        return (VERDICT_INCORRECT, "no ground truth callers available")
    mentioned = [s for s in _HOTROD_SERVICE_NAMES if _mentions_service(ans, s)]
    if not mentioned:
        return (VERDICT_NON_ANSWER, "no hotrod service name found in answer")
    missing = [c for c in expected_callers if c not in mentioned]
    extras = [s for s in mentioned if s not in expected_callers and s != _TASK_25_TARGET_SERVICE]
    if not missing and not extras:
        return (
            VERDICT_CORRECT,
            f"exactly the expected callers {expected_callers} mentioned (answer named: {mentioned})",
        )
    return (
        VERDICT_INCORRECT,
        f"missing callers {missing}; extra services named {extras};"
        f" answer named: {mentioned} (expected {expected_callers})",
    )


def _score_26_compare_traces(ans: str, expected: dict, _gt) -> tuple[str, str]:
    """Service+operation with the largest critical-path self-time delta
    between the fast and slow trace. Both parts required - see
    _score_service_operation_pair."""
    return _score_service_operation_pair(ans, expected, method_desc="compare_traces")


_HANDLERS = {
    "01_p99_now": _score_01_p99_now,
    "02_spike_detection": _score_02_spike_detection,
    "03_rank_error_rate": _score_03_rank_error_rate,
    "04_correlation": _score_04_correlation,
    "05_threshold": _score_05_threshold,
    "06_trend": _score_06_trend,
    "21_error_root_cause": _score_21_error_root_cause,
    "22_critical_path": _score_22_critical_path,
    "23_trace_shape": _score_23_trace_shape,
    "24_attribute_hunt": _score_24_attribute_hunt,
    "25_dependency": _score_25_dependency,
    "26_compare_traces": _score_26_compare_traces,
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
