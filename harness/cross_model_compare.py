"""Build cross-model comparison from Trial 4 (Claude) + Trial 5 (Gemini).

Token semantics differ:
- Claude exposes: input_tokens, output_tokens, cache_creation, cache_read
- Gemini exposes: prompt, candidates, cached, thoughts, total (plus stats.tools.totalCalls)

For cross-model comparison we use a normalized "format-specific tokens" metric:
- Claude: cache_creation_input_tokens
- Gemini: prompt - cached (fresh input tokens not from cache)

Both reflect "tokens added to context that depend on format choice" since the
baseline (system prompt + tool descriptions) is cached.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import click

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scorer

ROOT = HERE.parent


def _two_prop_z(a_correct: int, a_n: int, b_correct: int, b_n: int) -> tuple[float, float]:
    """Two-proportion z-test, two-sided p-value. Pooled variance.

    Bug fix: previous post-draft cited z-test results that no code in
    repo computed. This makes them reproducible.
    """
    if a_n == 0 or b_n == 0:
        return (0.0, 1.0)
    p1 = a_correct / a_n
    p2 = b_correct / b_n
    p_pool = (a_correct + b_correct) / (a_n + b_n)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / a_n + 1 / b_n))
    if se == 0:
        return (0.0, 1.0)
    z = (p1 - p2) / se
    p = math.erfc(abs(z) / math.sqrt(2))  # 2-sided
    return (z, p)


def load_trial(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def normalize_tokens(model: str, trial_record: dict) -> dict:
    """Return a per-trial dict with model-agnostic field names.

    Fields:
        input_format_tokens   - tokens added to context that depend on tool result shape
        output_tokens         - final response tokens (Claude: output_tokens; Gemini: candidates)
        cache_baseline        - cached tokens (constant baseline; Claude: cache_read; Gemini: cached)
        thinking_tokens       - extended-reasoning tokens (Gemini "thoughts"; Claude: 0 by default)
        tool_calls            - number of tool invocations
        duration_ms           - wall time
    """
    r = trial_record.get("result", {}) or {}
    raw = r.get("raw_output", {}) or {}

    if model == "claude":
        usage = raw.get("usage", {}) or {}
        return {
            "input_format_tokens": usage.get("cache_creation_input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cache_baseline": usage.get("cache_read_input_tokens") or 0,
            "thinking_tokens": 0,
            "tool_calls": r.get("tool_calls", 0) or 0,
            "duration_ms": r.get("duration_ms", 0) or 0,
        }
    elif model == "gemini":
        models_block = (raw.get("stats", {}) or {}).get("models", {}) or {}
        first_model = next(iter(models_block.values()), {}) or {}
        toks = first_model.get("tokens", {}) or {}
        prompt = toks.get("prompt") or 0
        cached = toks.get("cached") or 0
        return {
            "input_format_tokens": max(0, prompt - cached),
            "output_tokens": toks.get("candidates") or 0,
            "cache_baseline": cached,
            "thinking_tokens": toks.get("thoughts") or 0,
            "tool_calls": r.get("tool_calls", 0) or 0,
            "duration_ms": r.get("duration_ms", 0) or 0,
        }
    else:
        return {
            "input_format_tokens": 0, "output_tokens": 0,
            "cache_baseline": 0, "thinking_tokens": 0,
            "tool_calls": 0, "duration_ms": 0,
        }


def aggregate_run(run_dir: Path, model: str, ground_truth: dict) -> list[dict]:
    cells = defaultdict(list)
    for trial_path in run_dir.rglob("trial_*.json"):
        rec = load_trial(trial_path)
        if rec.get("model") != model:
            continue
        key = (rec["format"], rec["task_id"])
        cells[key].append(rec)

    rows = []
    for (fmt, tid), trials in sorted(cells.items()):
        scored = []
        for t in trials:
            r = t.get("result") or {}
            ans = r.get("answer", "") or ""
            verdict, _ = scorer.score(tid, ans, ground_truth) if ans else (scorer.VERDICT_INCORRECT, "no answer")
            tk = normalize_tokens(model, t)
            scored.append({"verdict": verdict, **tk})

        n = len(scored)
        if n == 0:
            continue

        rows.append({
            "model": model,
            "format": fmt,
            "task_id": tid,
            "n_trials": n,
            "correct": sum(1 for s in scored if s["verdict"] == scorer.VERDICT_CORRECT),
            "incorrect": sum(1 for s in scored if s["verdict"] == scorer.VERDICT_INCORRECT),
            "non_answer": sum(1 for s in scored if s["verdict"] == scorer.VERDICT_NON_ANSWER),
            "pass_pow_n": round(sum(1 for s in scored if s["verdict"] == scorer.VERDICT_CORRECT) / n, 3),
            "mean_input_format_tokens": round(statistics.mean(s["input_format_tokens"] for s in scored), 1),
            "mean_output_tokens": round(statistics.mean(s["output_tokens"] for s in scored), 1),
            "mean_thinking_tokens": round(statistics.mean(s["thinking_tokens"] for s in scored), 1),
            "mean_tool_calls": round(statistics.mean(s["tool_calls"] for s in scored), 2),
            "mean_duration_ms": round(statistics.mean(s["duration_ms"] for s in scored)),
        })
    return rows


@click.command()
@click.option("--claude-run", required=True, help="Run id for Claude matrix")
@click.option("--gemini-run", required=True, help="Run id for Gemini matrix")
@click.option("--ground-truth", required=True, type=click.Path(exists=True))
@click.option("--out-prefix", default="cross_model")
def main(claude_run: str, gemini_run: str, ground_truth: str, out_prefix: str):
    runs_dir = ROOT / "results" / "runs"
    with open(ground_truth) as f:
        gt = json.load(f).get("ground_truth", {})

    rows = []
    rows += aggregate_run(runs_dir / claude_run, "claude", gt)
    rows += aggregate_run(runs_dir / gemini_run, "gemini", gt)

    out_dir = ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Master CSV
    csv_path = out_dir / f"{out_prefix}.csv"
    with open(csv_path, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    # Side-by-side per-task table
    by_task = defaultdict(dict)
    for r in rows:
        by_task[r["task_id"]][f"{r['model']}/{r['format']}"] = r

    # Summary view: compact comparison
    md_path = out_dir / f"{out_prefix}.md"
    lines = ["# Cross-model format comparison\n"]
    lines.append(f"Run sources: claude={claude_run}, gemini={gemini_run}\n")

    lines.append("\n## Pass^3 by (model, format, task)\n")
    lines.append("| task | claude/summary | claude/series | gemini/summary | gemini/series |")
    lines.append("|---|---|---|---|---|")
    for tid in sorted(by_task):
        cs = by_task[tid].get("claude/summary", {})
        cr = by_task[tid].get("claude/series", {})
        gs = by_task[tid].get("gemini/summary", {})
        gr = by_task[tid].get("gemini/series", {})
        lines.append(f"| {tid} | {cs.get('pass_pow_n','-')} | {cr.get('pass_pow_n','-')} | {gs.get('pass_pow_n','-')} | {gr.get('pass_pow_n','-')} |")

    lines.append("\n## Format-specific input tokens (mean per trial; lower = cheaper)\n")
    lines.append("| task | claude/summary | claude/series | gemini/summary | gemini/series |")
    lines.append("|---|---|---|---|---|")
    for tid in sorted(by_task):
        cs = by_task[tid].get("claude/summary", {})
        cr = by_task[tid].get("claude/series", {})
        gs = by_task[tid].get("gemini/summary", {})
        gr = by_task[tid].get("gemini/series", {})
        lines.append(f"| {tid} | {cs.get('mean_input_format_tokens','-')} | {cr.get('mean_input_format_tokens','-')} | {gs.get('mean_input_format_tokens','-')} | {gr.get('mean_input_format_tokens','-')} |")

    lines.append("\n## Output tokens (mean per trial)\n")
    lines.append("| task | claude/summary | claude/series | gemini/summary | gemini/series |")
    lines.append("|---|---|---|---|---|")
    for tid in sorted(by_task):
        cs = by_task[tid].get("claude/summary", {})
        cr = by_task[tid].get("claude/series", {})
        gs = by_task[tid].get("gemini/summary", {})
        gr = by_task[tid].get("gemini/series", {})
        lines.append(f"| {tid} | {cs.get('mean_output_tokens','-')} | {cr.get('mean_output_tokens','-')} | {gs.get('mean_output_tokens','-')} | {gr.get('mean_output_tokens','-')} |")

    lines.append("\n## Tool calls (mean per trial)\n")
    lines.append("| task | claude/summary | claude/series | gemini/summary | gemini/series |")
    lines.append("|---|---|---|---|---|")
    for tid in sorted(by_task):
        cs = by_task[tid].get("claude/summary", {})
        cr = by_task[tid].get("claude/series", {})
        gs = by_task[tid].get("gemini/summary", {})
        gr = by_task[tid].get("gemini/series", {})
        lines.append(f"| {tid} | {cs.get('mean_tool_calls','-')} | {cr.get('mean_tool_calls','-')} | {gs.get('mean_tool_calls','-')} | {gr.get('mean_tool_calls','-')} |")

    # Aggregate correct rate across all tasks
    lines.append("\n## Aggregate correct rate across all 6 tasks\n")
    by_arm = defaultdict(list)
    arm_counts = defaultdict(lambda: {"correct": 0, "incorrect": 0, "non_answer": 0, "n": 0})
    for r in rows:
        by_arm[(r['model'], r['format'])].append(r['pass_pow_n'])
        arm_counts[(r['model'], r['format'])]["correct"] += r['correct']
        arm_counts[(r['model'], r['format'])]["incorrect"] += r['incorrect']
        arm_counts[(r['model'], r['format'])]["non_answer"] += r['non_answer']
        arm_counts[(r['model'], r['format'])]["n"] += r['n_trials']
    for arm, pp in sorted(by_arm.items()):
        mean_pass = statistics.mean(pp)
        c = arm_counts[arm]
        lines.append(
            f"- **{arm[0]}/{arm[1]}**: correct={c['correct']}/{c['n']} "
            f"({mean_pass:.3f} mean across 6 tasks); "
            f"incorrect={c['incorrect']}; non_answer={c['non_answer']}"
        )

    # Two-proportion z-tests (Bug fix: results were cited but never computed in repo)
    lines.append("\n## Two-proportion z-tests on correct/total\n")
    lines.append("| comparison | a_correct/n | b_correct/n | z | p (2-sided) | sig α=0.05 |")
    lines.append("|---|---|---|---|---|---|")
    pairs = [
        (("claude", "summary"), ("claude", "series")),
        (("gemini", "summary"), ("gemini", "series")),
        (("claude", "summary"), ("gemini", "summary")),
        (("claude", "series"), ("gemini", "series")),
    ]
    for a, b in pairs:
        ac = arm_counts[a]
        bc = arm_counts[b]
        z, p = _two_prop_z(ac["correct"], ac["n"], bc["correct"], bc["n"])
        sig = "yes" if p < 0.05 else "no"
        lines.append(
            f"| {a[0]}/{a[1]} vs {b[0]}/{b[1]} | "
            f"{ac['correct']}/{ac['n']} | {bc['correct']}/{bc['n']} | "
            f"{z:.2f} | {p:.3f} | {sig} |"
        )

    md_path.write_text("\n".join(lines) + "\n")

    click.echo(f"Wrote: {csv_path}")
    click.echo(f"Wrote: {md_path}")
    click.echo()
    click.echo("=== aggregate Pass^3 ===")
    for arm, pp in sorted(by_arm.items()):
        click.echo(f"  {arm[0]}/{arm[1]}: {statistics.mean(pp):.3f}")


if __name__ == "__main__":
    main()
