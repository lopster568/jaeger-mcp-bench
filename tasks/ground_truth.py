"""Ground-truth resolution and scoring for benchmark tasks.

The `expected` field in each task YAML is a placeholder until the fixture
is snapshotted. After snapshot, run `python ground_truth.py compute --snapshot=...`
to populate the expected values per task; the answers come from a deterministic
SDK query against the snapshotted Prometheus + Jaeger metrics API.

Scoring:
    score(task, agent_answer) -> bool

Each scorer takes the parsed agent text answer and the task's expected value,
and returns True/False. For numeric tasks, tolerance windows are honored.
"""

from __future__ import annotations

import re
from typing import Any


def score(task: dict[str, Any], agent_answer: str) -> tuple[bool, str]:
    """Return (correct, explanation)."""
    gt = task["ground_truth"]
    gt_type = gt["type"]
    if gt_type == "numeric":
        return _score_numeric(gt, agent_answer)
    if gt_type == "boolean":
        return _score_boolean(gt, agent_answer)
    if gt_type == "ordered_list":
        return _score_ordered_list(gt, agent_answer)
    if gt_type == "classification":
        return _score_classification(gt, agent_answer)
    if gt_type == "structured":
        return _score_structured(gt, agent_answer)
    return False, f"unknown ground_truth.type={gt_type}"


def _score_numeric(gt: dict, ans: str) -> tuple[bool, str]:
    expected = float(gt["expected"]["value"])
    tol_pct = gt.get("tolerance_pct", 5) / 100.0
    candidates = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", ans)]
    if not candidates:
        return False, "no numeric value in answer"
    # Pick the candidate closest to expected; agent might have said multiple numbers.
    best = min(candidates, key=lambda v: abs(v - expected))
    delta = abs(best - expected)
    ok = delta <= abs(expected) * tol_pct
    return ok, f"got={best} expected={expected} tol={tol_pct*100:.1f}%"


def _score_boolean(gt: dict, ans: str) -> tuple[bool, str]:
    expected = bool(gt["expected"]["value"])
    a = ans.lower()
    if "yes" in a or "true" in a or "above" in a:
        got = True
    elif "no" in a or "false" in a or "below" in a:
        got = False
    else:
        return False, "indeterminate yes/no"
    return got == expected, f"got={got} expected={expected}"


def _score_ordered_list(gt: dict, ans: str) -> tuple[bool, str]:
    expected = [s.lower() for s in gt["expected"]["value"]]
    a = ans.lower()
    found_idx = []
    for s in expected:
        m = re.search(rf"\b{re.escape(s)}\b", a)
        if m:
            found_idx.append((s, m.start()))
    found_idx.sort(key=lambda x: x[1])
    got_order = [s for s, _ in found_idx]
    return got_order == expected, f"got={got_order} expected={expected}"


def _score_classification(gt: dict, ans: str) -> tuple[bool, str]:
    expected = gt["expected"]["value"].lower()
    a = ans.lower()
    for c in gt["classes"]:
        if c.lower() in a:
            return c.lower() == expected, f"got={c} expected={expected}"
    return False, "no class found in answer"


def _score_structured(gt: dict, ans: str) -> tuple[bool, str]:
    # MVP: structured tasks fall back to LLM-judge until a parser is plugged in.
    return False, "structured scorer not yet implemented; route to LLM-judge"
