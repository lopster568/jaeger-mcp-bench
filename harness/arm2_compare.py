"""Arm 2 (docs/arm2-design.md) registered statistical comparisons for one run.

Implements the four PRE-REGISTERED two-proportion z-tests on correctness
(design doc, "Statistics (registered comparisons)"):

    1. claude/tiered vs claude/flat
    2. gemini/tiered vs gemini/flat
    3. tiered/claude vs tiered/gemini
    4. flat/claude   vs flat/gemini

Bonferroni-corrected alpha = 0.05 / 4 = 0.0125 (four comparisons, one
pre-registered family; any comparison not in this list is exploratory per
the design doc and does not belong here).

Unlike cross_model_compare.py (arm 1, hardcoded to summary/series and reads
TWO separate run ids - one per model, since arm 1 ran each model as its own
invocation), arm 2 runs both models and both formats (tiered/flat) in a
SINGLE run_id (orchestrator.py's `--models claude,gemini --formats
tiered,flat`), so this aggregator takes one `--run-id`.

`_two_prop_z` and `normalize_tokens` are reused unchanged from
cross_model_compare.py - same z-test math, and the same per-model token
normalization applies identically here since claude_runner.py's /
gemini_runner.py's TrialResult shape is unchanged by arm 2 - rather than
duplicated (attribution: adapted from harness/cross_model_compare.py).

Unscorable trials (scorer.py's "unscorable" verdict - ground truth the
resolver could not produce, e.g. Arm2VolumeError or an empty candidate set)
are excluded from every correctness proportion (per-cell pass_pow_n and the
z-test numerators/denominators) and reported in their own n_unscorable
column instead, per docs/arm2-design.md's Tasks section and scorer.py's
FIX 1 module note.

CLI:
    python arm2_compare.py --run-id <id> --ground-truth <snapshot.json> \\
        [--out-prefix arm2_compare]

Writes:
    results/tables/<prefix>.csv   one row per (model, format, task) cell
    results/tables/<prefix>.md    per-cell means (tokens, tool_calls,
                                   duration) plus the four registered
                                   z-tests and a Bonferroni note
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import click

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scorer  # noqa: E402
from cross_model_compare import _two_prop_z, normalize_tokens  # noqa: E402

ROOT = HERE.parent

# Arm 2's two tool-inventory-shape formats (docs/arm2-design.md, "The two
# arms"). Any other format present in the run dir (e.g. an arm-1
# summary/series cell accidentally mixed into the same run_id) is ignored -
# this aggregator is arm-2-specific by design, mirroring how
# cross_model_compare.py stays arm-1-only.
ARM2_FORMATS = ("tiered", "flat")

BONFERRONI_ALPHA = 0.05 / 4  # 0.0125, four pre-registered comparisons

# The four REGISTERED comparisons, in the order the design doc lists them.
_REGISTERED_PAIRS = [
    (("claude", "tiered"), ("claude", "flat"), "claude tiered vs flat"),
    (("gemini", "tiered"), ("gemini", "flat"), "gemini tiered vs flat"),
    (("claude", "tiered"), ("gemini", "tiered"), "tiered claude vs gemini"),
    (("claude", "flat"), ("gemini", "flat"), "flat claude vs gemini"),
]


def load_trial(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _score_verdict(task_id: str, ans: str, ground_truth: dict) -> str:
    """Verdict for one trial's answer text, mirroring aggregate_v2.py's
    empty-answer handling: an empty answer is always incorrect UNLESS
    ground truth itself is unscorable (checked exactly as scorer.score()
    does, before dispatch) - a resolver failure isn't the agent's fault
    regardless of what it did or didn't say.
    """
    if not ans:
        gt_entry = ground_truth.get(task_id)
        if not gt_entry or "value" not in gt_entry or gt_entry.get("value") is None:
            verdict, _ = scorer.score(task_id, ans, ground_truth)
            return verdict
        return scorer.VERDICT_INCORRECT
    verdict, _ = scorer.score(task_id, ans, ground_truth)
    return verdict


def aggregate_run(run_dir: Path, ground_truth: dict) -> list[dict]:
    """Per (model, format, task) cell stats for one arm-2 run dir.

    Unscorable trials are excluded from `correct`/`incorrect`/`non_answer`
    and from `pass_pow_n`'s denominator, but counted in `n_trials` and
    reported separately in `n_unscorable` - see module note above.
    """
    cells = defaultdict(list)
    for trial_path in run_dir.rglob("trial_*.json"):
        rec = load_trial(trial_path)
        if rec.get("format") not in ARM2_FORMATS:
            continue
        key = (rec["model"], rec["format"], rec["task_id"])
        cells[key].append(rec)

    rows = []
    for (model, fmt, tid), trials in sorted(cells.items()):
        scored = []
        for t in trials:
            r = t.get("result") or {}
            ans = r.get("answer", "") or ""
            verdict = _score_verdict(tid, ans, ground_truth)
            tk = normalize_tokens(model, t)
            scored.append({"verdict": verdict, **tk})

        n = len(scored)
        if n == 0:
            continue
        unscorable = sum(1 for s in scored if s["verdict"] == scorer.VERDICT_UNSCORABLE)
        scorable = [s for s in scored if s["verdict"] != scorer.VERDICT_UNSCORABLE]
        scorable_n = len(scorable)
        correct = sum(1 for s in scorable if s["verdict"] == scorer.VERDICT_CORRECT)

        rows.append({
            "model": model,
            "format": fmt,
            "task_id": tid,
            "n_trials": n,
            "n_unscorable": unscorable,
            "n_scorable": scorable_n,
            "correct": correct,
            "incorrect": sum(1 for s in scorable if s["verdict"] == scorer.VERDICT_INCORRECT),
            "non_answer": sum(1 for s in scorable if s["verdict"] == scorer.VERDICT_NON_ANSWER),
            "pass_pow_n": round(correct / scorable_n, 3) if scorable_n else 0,
            "mean_input_format_tokens": round(statistics.mean(s["input_format_tokens"] for s in scored), 1),
            "mean_output_tokens": round(statistics.mean(s["output_tokens"] for s in scored), 1),
            "mean_thinking_tokens": round(statistics.mean(s["thinking_tokens"] for s in scored), 1),
            "mean_tool_calls": round(statistics.mean(s["tool_calls"] for s in scored), 2),
            "mean_duration_ms": round(statistics.mean(s["duration_ms"] for s in scored)),
        })
    return rows


def arm_counts_from_rows(rows: list[dict]) -> dict[tuple[str, str], dict[str, int]]:
    """Sum correct/scorable-n across all tasks for each (model, format) arm -
    the z-tests operate on this pooled proportion, not per-task."""
    counts = defaultdict(lambda: {"correct": 0, "n": 0})
    for r in rows:
        key = (r["model"], r["format"])
        counts[key]["correct"] += r["correct"]
        counts[key]["n"] += r["n_scorable"]
    return counts


def run_registered_z_tests(arm_counts: dict[tuple[str, str], dict[str, int]]) -> list[dict]:
    results = []
    for a, b, label in _REGISTERED_PAIRS:
        ac = arm_counts[a]
        bc = arm_counts[b]
        z, p = _two_prop_z(ac["correct"], ac["n"], bc["correct"], bc["n"])
        results.append({
            "comparison": label,
            "a": f"{a[0]}/{a[1]}",
            "b": f"{b[0]}/{b[1]}",
            "a_correct": ac["correct"], "a_n": ac["n"],
            "b_correct": bc["correct"], "b_n": bc["n"],
            "z": z, "p": p,
            "sig_bonferroni": p < BONFERRONI_ALPHA,
        })
    return results


def write_csv(rows: list[dict], out: Path) -> None:
    if not rows:
        out.write_text("")
        return
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(run_id: str, rows: list[dict], z_results: list[dict], out: Path) -> None:
    lines = ["# Arm 2 comparison: tiered vs flat, claude vs gemini\n"]
    lines.append(f"Run source: {run_id}\n")

    if not rows:
        lines.append("(no data)\n")
        out.write_text("\n".join(lines) + "\n")
        return

    by_task = defaultdict(dict)
    for r in rows:
        by_task[r["task_id"]][f"{r['model']}/{r['format']}"] = r
    arm_cols = ["claude/tiered", "claude/flat", "gemini/tiered", "gemini/flat"]

    lines.append("\n## Pass rate (correct / scorable trials) by (model, format, task)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("pass_pow_n", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append("\n## Unscorable trials by (model, format, task)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("n_unscorable", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append("\n## Format-specific input tokens (mean per trial; H1 cost signal)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("mean_input_format_tokens", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append("\n## Output tokens (mean per trial)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("mean_output_tokens", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append("\n## Tool calls (mean per trial)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("mean_tool_calls", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append("\n## Duration ms (mean per trial)\n")
    lines.append("| task | " + " | ".join(arm_cols) + " |")
    lines.append("|---|" + "---|" * len(arm_cols))
    for tid in sorted(by_task):
        cells = [str(by_task[tid].get(c, {}).get("mean_duration_ms", "-")) for c in arm_cols]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")

    lines.append(
        "\n## Registered two-proportion z-tests on correctness "
        f"(Bonferroni alpha = 0.05/4 = {BONFERRONI_ALPHA})\n"
    )
    lines.append("| comparison | a_correct/n | b_correct/n | z | p (2-sided) | sig (Bonferroni) |")
    lines.append("|---|---|---|---|---|---|")
    for zr in z_results:
        lines.append(
            f"| {zr['comparison']} | {zr['a_correct']}/{zr['a_n']} | {zr['b_correct']}/{zr['b_n']} | "
            f"{zr['z']:.2f} | {zr['p']:.4f} | {'yes' if zr['sig_bonferroni'] else 'no'} |"
        )
    lines.append(
        "\nNote: these are the four PRE-REGISTERED comparisons "
        "(docs/arm2-design.md, Statistics section) - not a search over all "
        "possible pairs. Any other comparison surfaced elsewhere in the "
        "writeup is exploratory and not tested here. Unscorable trials are "
        "excluded from every correct/n figure above.\n"
    )

    out.write_text("\n".join(lines) + "\n")


@click.command()
@click.option("--run-id", required=True, help="Single run id containing both models and both arm-2 formats")
@click.option("--ground-truth", required=True, type=click.Path(exists=True))
@click.option("--out-prefix", default="arm2_compare")
def main(run_id: str, ground_truth: str, out_prefix: str):
    run_dir = ROOT / "results" / "runs" / run_id
    if not run_dir.exists():
        click.echo(f"run dir missing: {run_dir}", err=True)
        sys.exit(2)

    with open(ground_truth) as f:
        gt = json.load(f).get("ground_truth", {})

    rows = aggregate_run(run_dir, gt)
    arm_counts = arm_counts_from_rows(rows)
    z_results = run_registered_z_tests(arm_counts)

    out_dir = ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{out_prefix}.csv"
    md_path = out_dir / f"{out_prefix}.md"

    write_csv(rows, csv_path)
    write_markdown(run_id, rows, z_results, md_path)

    click.echo(f"Wrote: {csv_path}")
    click.echo(f"Wrote: {md_path}")
    click.echo()
    click.echo(f"=== registered z-tests (Bonferroni alpha={BONFERRONI_ALPHA}) ===")
    for zr in z_results:
        sig = "SIG" if zr["sig_bonferroni"] else "ns"
        click.echo(
            f"  {zr['comparison']:<28} {zr['a_correct']}/{zr['a_n']} vs {zr['b_correct']}/{zr['b_n']}"
            f"  z={zr['z']:.2f} p={zr['p']:.4f} [{sig}]"
        )


if __name__ == "__main__":
    main()
