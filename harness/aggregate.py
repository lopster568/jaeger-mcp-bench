"""Aggregate trial dumps into per-cell statistics + a comparison table.

Inputs:  results/runs/<run_id>/<model>/<format>/<task>/trial_*.json
Outputs: results/tables/<run_id>.csv  (one row per cell)
         results/tables/<run_id>.md   (markdown comparison ready to paste)

Per-cell metrics:
    pass_at_n          # any-trial correctness (boolean OR across trials)
    pass_pow_n         # mean correctness across trials
    mean_input_tokens
    mean_output_tokens
    mean_total_tokens
    mean_tool_calls
    mean_duration_ms
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
sys.path.insert(0, str(Path(__file__).parent.parent / "tasks"))
import ground_truth as gt_mod  # noqa


HERE = Path(__file__).parent
ROOT = HERE.parent


def load_trial(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def load_tasks() -> dict[str, dict]:
    import yaml
    tasks = {}
    for p in (ROOT / "tasks").glob("*.yaml"):
        with open(p) as f:
            t = yaml.safe_load(f)
            if t and "id" in t:
                tasks[t["id"]] = t
    return tasks


def aggregate(run_dir: Path) -> list[dict]:
    """Walk run_dir and produce one row per (model, format, task) cell."""
    tasks_by_id = load_tasks()
    cells = defaultdict(list)

    for trial_path in run_dir.rglob("trial_*.json"):
        rec = load_trial(trial_path)
        key = (rec["model"], rec["format"], rec["task_id"])
        cells[key].append(rec)

    rows = []
    for (model, fmt, tid), trials in sorted(cells.items()):
        task = tasks_by_id.get(tid, {})
        scored = []
        for t in trials:
            r = t.get("result", {}) or {}
            answer = r.get("answer", "") or ""
            ok, _expl = gt_mod.score(task, answer) if task else (False, "no task")
            scored.append({
                "ok": ok,
                "input_tokens": r.get("input_tokens") or 0,
                "output_tokens": r.get("output_tokens") or 0,
                "tool_calls": r.get("tool_calls") or 0,
                "duration_ms": r.get("duration_ms") or 0,
            })

        n = len(scored)
        rows.append({
            "model": model,
            "format": fmt,
            "task_id": tid,
            "n_trials": n,
            "pass_at_n": int(any(s["ok"] for s in scored)),
            "pass_pow_n": round(sum(1 for s in scored if s["ok"]) / n, 3) if n else 0,
            "mean_input_tokens": round(statistics.mean(s["input_tokens"] for s in scored), 1) if n else 0,
            "mean_output_tokens": round(statistics.mean(s["output_tokens"] for s in scored), 1) if n else 0,
            "mean_tool_calls": round(statistics.mean(s["tool_calls"] for s in scored), 2) if n else 0,
            "mean_duration_ms": round(statistics.mean(s["duration_ms"] for s in scored)) if n else 0,
        })
    return rows


def write_csv(rows: list[dict], out: Path):
    if not rows:
        out.write_text("")
        return
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(rows: list[dict], out: Path):
    if not rows:
        out.write_text("(no data)\n")
        return
    cols = list(rows[0].keys())
    lines = []
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * len(cols))
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    out.write_text("\n".join(lines) + "\n")


@click.command()
@click.option("--run-id", required=True, help="Run id under results/runs/")
def main(run_id):
    run_dir = ROOT / "results" / "runs" / run_id
    if not run_dir.exists():
        click.echo(f"run dir missing: {run_dir}", err=True)
        sys.exit(2)

    out_dir = ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = aggregate(run_dir)
    write_csv(rows, out_dir / f"{run_id}.csv")
    write_markdown(rows, out_dir / f"{run_id}.md")
    click.echo(f"DONE. {len(rows)} cells -> {out_dir}/{run_id}.{{csv,md}}")


if __name__ == "__main__":
    main()
