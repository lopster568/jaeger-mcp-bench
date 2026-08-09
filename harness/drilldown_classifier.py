"""Drill-down conformance classifier (docs/arm2-design.md, H3).

H3: "given the tiered inventory, agents follow the drill-down order the
server instructions describe (broad discovery or topology before span
details) in the majority of trials."

Classifies a single captured Claude trajectory (a sequence of tool calls, in
order) as one of:

    conforms         - a structural/discovery call preceded the first
                        verbose call
    violates          - the first verbose call happened with no preceding
                        structural/discovery call
    no_verbose_call   - the trajectory never made a verbose call at all
                        (nothing to violate; excluded from the conformance
                        proportion, counted separately)

Input is any sequence of objects exposing a `.tool` attribute holding the
raw (possibly MCP-prefixed) tool name, in call order - e.g.
`trajectory_capture.ToolStep`, so a `Trajectory.steps` list from
trajectory_capture.py's `capture`/`parse` commands can be passed straight in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Protocol

# Tiered-arm tool inventory (docs/arm2-design.md): nine tools shipped in
# Jaeger v2.20.0's MCP server. Split per H3's registered classification:
# structural/discovery calls vs. verbose (full-detail) calls. Tools outside
# both sets (get_critical_path, get_service_dependencies) are neutral for
# this classifier - present in the inventory but not part of H3's registered
# claim, so they neither count as discovery nor break conformance.
DISCOVERY_TOOLS = frozenset({
    "get_services",
    "search_traces",
    "get_trace_topology",
    "get_span_names",
    "read_skill",
})
VERBOSE_TOOLS = frozenset({
    "get_span_details",
    "get_trace_errors",
})

CONFORMS = "conforms"
VIOLATES = "violates"
NO_VERBOSE_CALL = "no_verbose_call"

# MCP tool names as they appear in captured trajectories carry a
# `mcp__<server-alias>__<tool-name>` prefix (trajectory_capture.py parses
# these straight from the Claude Code stream-json `tool_use` blocks). The
# server alias is "jaeger-bench" for every format, arm 1 and arm 2 alike
# (mcp_config.SERVER_ALIAS) - kept identical on purpose so this prefix
# convention doesn't have to special-case which physical MCP server
# answered the call.
_MCP_PREFIX = "mcp__"


def bare_tool_name(tool: str) -> str:
    """Strip the `mcp__<server-alias>__` prefix from a trajectory tool name.

    Handles prefixing robustly: server aliases without their own "__" (the
    only kind this harness ever configures - see mcp_config.SERVER_ALIAS)
    split cleanly on the first "__" after the "mcp__" prefix. Non-MCP tool
    names (e.g. a bare `Bash` call, if one ever appears) pass through
    unchanged, since they can't match DISCOVERY_TOOLS/VERBOSE_TOOLS anyway.
    """
    if tool.startswith(_MCP_PREFIX):
        rest = tool[len(_MCP_PREFIX):]
        _, _, bare = rest.partition("__")
        return bare or rest
    return tool


class _HasTool(Protocol):
    tool: str


@dataclass
class TrialClassification:
    trial_id: str
    classification: str  # conforms | violates | no_verbose_call
    first_verbose_index: int | None
    first_verbose_tool: str | None
    first_discovery_index: int | None
    first_discovery_tool: str | None


def classify_trial(steps: Iterable[_HasTool], trial_id: str = "") -> TrialClassification:
    """Classify one trajectory's tool-call sequence for H3 conformance.

    Only the *first* verbose call matters (per the registered question: did
    discovery happen before drilling into detail, the first time the agent
    drilled in). Discovery calls found before that point in the sequence
    count as preceding it; discovery calls after are irrelevant to this
    classification (the violation, if any, already happened).
    """
    first_verbose_idx: int | None = None
    first_verbose_tool: str | None = None
    first_discovery_idx: int | None = None
    first_discovery_tool: str | None = None

    for i, step in enumerate(steps):
        bare = bare_tool_name(step.tool)
        if bare in VERBOSE_TOOLS:
            first_verbose_idx = i
            first_verbose_tool = bare
            break
        if first_discovery_idx is None and bare in DISCOVERY_TOOLS:
            first_discovery_idx = i
            first_discovery_tool = bare

    if first_verbose_idx is None:
        return TrialClassification(
            trial_id=trial_id,
            classification=NO_VERBOSE_CALL,
            first_verbose_index=None,
            first_verbose_tool=None,
            first_discovery_index=first_discovery_idx,
            first_discovery_tool=first_discovery_tool,
        )

    conforms = first_discovery_idx is not None and first_discovery_idx < first_verbose_idx
    return TrialClassification(
        trial_id=trial_id,
        classification=CONFORMS if conforms else VIOLATES,
        first_verbose_index=first_verbose_idx,
        first_verbose_tool=first_verbose_tool,
        first_discovery_index=first_discovery_idx,
        first_discovery_tool=first_discovery_tool,
    )


def classify_trajectory_file(path: str | Path, trial_id: str | None = None) -> TrialClassification:
    """Load a `trajectory.json` (trajectory_capture.py output) and classify it."""
    path = Path(path)
    data = json.loads(path.read_text())
    steps = [SimpleNamespace(tool=s["tool"]) for s in data.get("steps", [])]
    return classify_trial(steps, trial_id=trial_id if trial_id is not None else path.stem)


@dataclass
class ConformanceAggregate:
    n: int
    conforms: int
    violates: int
    no_verbose_call: int
    # Proportion of trials-with-a-verbose-call that conformed. Trials with
    # no verbose call are excluded from the denominator (nothing to conform
    # to or violate) and reported separately via no_verbose_call/n.
    proportion_conforms: float


def aggregate(classifications: Iterable[TrialClassification]) -> ConformanceAggregate:
    classifications = list(classifications)
    n = len(classifications)
    conforms = sum(1 for c in classifications if c.classification == CONFORMS)
    violates = sum(1 for c in classifications if c.classification == VIOLATES)
    no_verbose = sum(1 for c in classifications if c.classification == NO_VERBOSE_CALL)
    denom = conforms + violates
    proportion = round(conforms / denom, 4) if denom else 0.0
    return ConformanceAggregate(
        n=n,
        conforms=conforms,
        violates=violates,
        no_verbose_call=no_verbose,
        proportion_conforms=proportion,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python drilldown_classifier.py <trajectory.json>...", file=sys.stderr)
        raise SystemExit(2)

    results = [classify_trajectory_file(p) for p in sys.argv[1:]]
    for r in results:
        print(f"{r.trial_id}: {r.classification} "
              f"(first_discovery={r.first_discovery_tool}@{r.first_discovery_index}, "
              f"first_verbose={r.first_verbose_tool}@{r.first_verbose_index})")
    agg = aggregate(results)
    print(json.dumps(agg.__dict__, indent=2))
