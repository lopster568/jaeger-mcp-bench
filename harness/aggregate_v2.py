"""Aggregate trial dumps into per-cell statistics + comparison tables.

v2: uses scorer.py + ground_truth_values.json (from ground_truth_resolver.py).

Inputs:
    results/runs/<run_id>/<model>/<format>/<task>/trial_*.json
    --ground-truth path/to/gt.json

Outputs:
    results/tables/<run_id>.csv
    results/tables/<run_id>.md

Per-cell metrics:
    n_trials, correct, incorrect, non_answer
    pass_pow_n     (correct / scorable trials, mean correctness)
    pass_at_n      (any-trial correctness)
    error_pattern  (FP-rate vs FN-rate, descriptive)
    mean_input_tokens, mean_output_tokens, mean_cache_creation, mean_cache_read
    mean_tool_calls, mean_duration_ms
    sample_answer  (first correct answer; for human inspection)

Arm 2 (docs/arm2-design.md) additive columns:
    n_unscorable        trials whose ground truth was unavailable (scorer.py's
                         "unscorable" verdict - a resolver-side gap, not an
                         agent mistake). EXCLUDED from the pass_pow_n /
                         pass_at_n denominators and reported only here, never
                         folded into incorrect.
    n_budget_exhausted   trials where the runner's TrialResult.budget_exhausted
                         was True. These keep whatever verdict they scored
                         (typically incorrect/non_answer, since the trial
                         didn't finish) and are counted in both that verdict
                         column AND here - it's an outcome tag, not a
                         replacement verdict.
    n_timeout            trials whose result "error" field is/contains
                         "timeout". Same double-counting rule as
                         n_budget_exhausted.
Existing columns keep their names and semantics unchanged (additive only -
arm-1 runs re-aggregate identically modulo the rare case where arm-1 itself
now surfaces a genuinely missing ground truth as unscorable instead of
incorrect, which is the crash fix scorer.py FIX 1 exists for).
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import click

# Local imports
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scorer


ROOT = HERE.parent


def load_trial(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def aggregate(run_dir: Path, ground_truth: dict) -> list[dict]:
    cells = defaultdict(list)
    for trial_path in run_dir.rglob("trial_*.json"):
        rec = load_trial(trial_path)
        key = (rec["model"], rec["format"], rec["task_id"])
        cells[key].append(rec)

    rows = []
    for (model, fmt, tid), trials in sorted(cells.items()):
        scored_per_trial = []
        for t in trials:
            r = t.get("result") or {}
            ans = r.get("answer", "") or ""
            if str(r.get("error") or "") == "api_connection_error":
                # Transport failure (claude_runner.INFRA_ERROR): the model
                # never got to attempt the task, so this is neither an
                # outcome nor an answer. Counted in n_infra_error, excluded
                # from verdicts and every denominator. With retries in the
                # runner these should be rare.
                scored_per_trial.append({
                    "verdict": "infra_error",
                    "explanation": "api_connection_error",
                    "answer": "",
                    "input_tokens": r.get("input_tokens") or 0,
                    "output_tokens": r.get("output_tokens") or 0,
                    "cache_creation_tokens": r.get("cache_creation_tokens") or 0,
                    "cache_read_tokens": r.get("cache_read_tokens") or 0,
                    "tool_calls": r.get("tool_calls") or 0,
                    "duration_ms": r.get("duration_ms") or 0,
                    "budget_exhausted": False,
                    "is_timeout": False,
                })
                continue
            if not ans:
                # Arm-1 semantics preserved: empty answer text is always
                # incorrect, UNLESS ground truth itself is unscorable
                # (mirrors scorer.score()'s own gt check before dispatch) -
                # a resolver failure isn't the agent's fault regardless of
                # what it did or didn't say.
                gt_entry = ground_truth.get(tid)
                if not gt_entry or "value" not in gt_entry or gt_entry.get("value") is None:
                    verdict, expl = scorer.score(tid, ans, ground_truth)
                else:
                    verdict = scorer.VERDICT_INCORRECT
                    expl = "no answer text"
            else:
                verdict, expl = scorer.score(tid, ans, ground_truth)

            error_field = str(r.get("error") or "")
            scored_per_trial.append({
                "verdict": verdict,
                "explanation": expl,
                "answer": ans,
                "input_tokens": r.get("input_tokens") or 0,
                "output_tokens": r.get("output_tokens") or 0,
                "cache_creation_tokens": r.get("cache_creation_tokens") or 0,
                "cache_read_tokens": r.get("cache_read_tokens") or 0,
                "tool_calls": r.get("tool_calls") or 0,
                "duration_ms": r.get("duration_ms") or 0,
                "budget_exhausted": bool(r.get("budget_exhausted", False)),
                "is_timeout": "timeout" in error_field.lower(),
            })

        n = len(scored_per_trial)
        correct = sum(1 for s in scored_per_trial if s["verdict"] == scorer.VERDICT_CORRECT)
        incorrect = sum(1 for s in scored_per_trial if s["verdict"] == scorer.VERDICT_INCORRECT)
        non_answer = sum(1 for s in scored_per_trial if s["verdict"] == scorer.VERDICT_NON_ANSWER)
        unscorable = sum(1 for s in scored_per_trial if s["verdict"] == scorer.VERDICT_UNSCORABLE)
        budget_exhausted = sum(1 for s in scored_per_trial if s["budget_exhausted"])
        timeout = sum(1 for s in scored_per_trial if s["is_timeout"])

        infra_error = sum(1 for s in scored_per_trial if s["verdict"] == "infra_error")

        # Unscorable and infra-error trials are excluded from the pass-rate
        # denominator - no ground truth to fail against / no model attempt
        # at all - but n_trials itself stays the raw trial count (arm-1
        # semantics unchanged).
        scorable_n = n - unscorable - infra_error

        sample_correct = next((s["answer"][:200] for s in scored_per_trial if s["verdict"] == scorer.VERDICT_CORRECT), "")
        sample_incorrect = next((f"{s['explanation']} :: {s['answer'][:120]}" for s in scored_per_trial if s["verdict"] != scorer.VERDICT_CORRECT), "")

        rows.append({
            "model": model,
            "format": fmt,
            "task_id": tid,
            "n_trials": n,
            "correct": correct,
            "incorrect": incorrect,
            "non_answer": non_answer,
            "pass_pow_n": round(correct / scorable_n, 3) if scorable_n else 0,
            "pass_at_n": int(correct >= 1),
            "mean_input_tokens": round(statistics.mean(s["input_tokens"] for s in scored_per_trial), 1) if n else 0,
            "mean_output_tokens": round(statistics.mean(s["output_tokens"] for s in scored_per_trial), 1) if n else 0,
            "mean_cache_creation": round(statistics.mean(s["cache_creation_tokens"] for s in scored_per_trial), 1) if n else 0,
            "mean_cache_read": round(statistics.mean(s["cache_read_tokens"] for s in scored_per_trial), 1) if n else 0,
            "mean_tool_calls": round(statistics.mean(s["tool_calls"] for s in scored_per_trial), 2) if n else 0,
            "mean_duration_ms": round(statistics.mean(s["duration_ms"] for s in scored_per_trial)) if n else 0,
            "sample_correct": sample_correct,
            "sample_incorrect": sample_incorrect,
            "n_unscorable": unscorable,
            "n_budget_exhausted": budget_exhausted,
            "n_timeout": timeout,
            "n_infra_error": infra_error,
        })
    return rows


def write_csv(rows: list[dict], out: Path) -> None:
    if not rows:
        out.write_text("")
        return
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(rows: list[dict], out: Path) -> None:
    if not rows:
        out.write_text("(no data)\n")
        return
    cols = ["model", "format", "task_id", "n_trials",
            "pass_pow_n", "pass_at_n",
            "correct", "incorrect", "non_answer",
            "n_unscorable", "n_budget_exhausted", "n_timeout",
            "mean_cache_creation", "mean_output_tokens",
            "mean_tool_calls", "mean_duration_ms"]
    lines = []
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * len(cols))
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    out.write_text("\n".join(lines) + "\n")


@click.command()
@click.option("--run-id", required=True)
@click.option("--ground-truth", required=True, type=click.Path(exists=True))
def main(run_id: str, ground_truth: str):
    run_dir = ROOT / "results" / "runs" / run_id
    if not run_dir.exists():
        click.echo(f"run dir missing: {run_dir}", err=True)
        sys.exit(2)

    with open(ground_truth) as f:
        gt = json.load(f).get("ground_truth", {})

    rows = aggregate(run_dir, gt)
    out_dir = ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)

    write_csv(rows, out_dir / f"{run_id}.csv")
    write_markdown(rows, out_dir / f"{run_id}.md")

    click.echo(f"DONE. {len(rows)} cells")
    click.echo(f"  CSV: {out_dir}/{run_id}.csv")
    click.echo(f"  MD : {out_dir}/{run_id}.md")
    click.echo()
    click.echo("=== summary ===")
    for r in rows:
        click.echo(f"  {r['model']:<7} {r['format']:<8} {r['task_id']:<22} pass={r['pass_pow_n']} (ok={r['correct']}/{r['n_trials']})")


if __name__ == "__main__":
    main()
