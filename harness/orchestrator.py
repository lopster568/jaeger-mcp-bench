"""Orchestrator: run the (model × format × task × trial) matrix and dump per-trial JSON.

Each trial dump goes to results/runs/<run_id>/<model>/<format>/<task_id>/trial_<n>.json.
Aggregation lives in aggregate_v2.py; this script is just the runner.

Bug fix: previous version iterated nested model→format→task→trial, which
created a temporal confound - all summary trials run on a temporally-earlier
fixture state than all series trials. Fixed by shuffling cells with a seeded
RNG so format/task/trial ordering is decorrelated from fixture drift. The
seed is recorded in the run manifest for reproducibility.
"""

from __future__ import annotations

import json
import random
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import click
import yaml

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from mcp_config import write_claude_config, write_gemini_config
import claude_runner
import gemini_runner


HERE = Path(__file__).parent
ROOT = HERE.parent
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results" / "runs"


def load_tasks(task_dir: Path) -> list[dict]:
    tasks = []
    for p in sorted(task_dir.glob("*.yaml")):
        if p.name == "ground_truth.py":
            continue
        with open(p) as f:
            t = yaml.safe_load(f)
            if t and "id" in t:
                t["_path"] = str(p)
                tasks.append(t)
    return tasks


def run_one(
    *,
    model: str,
    format_: str,
    task: dict,
    trial_idx: int,
    jaeger_url: str,
) -> dict:
    if model == "claude":
        cfg = write_claude_config(format_, jaeger_url=jaeger_url)
        result = claude_runner.run(
            prompt=task["prompt"],
            mcp_config_path=cfg,
        )
        return asdict(result)
    if model == "gemini":
        cfg = write_gemini_config(format_, jaeger_url=jaeger_url)
        result = gemini_runner.run(
            prompt=task["prompt"],
            mcp_config_path=cfg,
        )
        return asdict(result)
    raise ValueError(f"unknown model: {model}")


def matrix(
    *,
    models: Iterable[str],
    formats: Iterable[str],
    tasks: list[dict],
    trials: int,
):
    for model in models:
        for format_ in formats:
            for task in tasks:
                for n in range(trials):
                    yield model, format_, task, n


@click.command()
@click.option("--models", default="claude,gemini", help="Comma-separated model list")
@click.option("--formats", default="summary,series", help="Comma-separated format list")
@click.option("--trials", default=3, type=int)
@click.option("--jaeger-url", default="http://localhost:16686")
@click.option("--task-glob", default="*", help="Filter tasks by id (substring match)")
@click.option("--seed", default=42, type=int, help="RNG seed for cell-order shuffle")
@click.option("--no-shuffle", is_flag=True, help="Run cells in deterministic order (legacy / debugging)")
@click.option("--dry-run", is_flag=True, help="Print the matrix without running")
def main(models, formats, trials, jaeger_url, task_glob, seed, no_shuffle, dry_run):
    models_list = [m.strip() for m in models.split(",") if m.strip()]
    formats_list = [f.strip() for f in formats.split(",") if f.strip()]
    all_tasks = load_tasks(TASKS_DIR)
    if task_glob != "*":
        all_tasks = [t for t in all_tasks if task_glob in t["id"]]
    if not all_tasks:
        click.echo("no tasks loaded", err=True)
        sys.exit(2)

    run_id = uuid.uuid4().hex[:8]
    run_root = RESULTS_DIR / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    cells = list(matrix(
        models=models_list, formats=formats_list, tasks=all_tasks, trials=trials,
    ))
    if not no_shuffle:
        rng = random.Random(seed)
        rng.shuffle(cells)
    click.echo(
        f"run_id={run_id} cells={len(cells)} seed={seed} shuffled={not no_shuffle} "
        f"models={models_list} formats={formats_list} tasks={[t['id'] for t in all_tasks]}"
    )

    # Persist run manifest for reproducibility
    manifest = {
        "run_id": run_id,
        "started_at_unix": int(time.time()),
        "seed": seed,
        "shuffled": not no_shuffle,
        "models": models_list,
        "formats": formats_list,
        "trials_per_cell": trials,
        "task_ids": [t["id"] for t in all_tasks],
        "cell_order": [
            {"model": m, "format": f, "task_id": t["id"], "trial": n}
            for (m, f, t, n) in cells
        ],
    }
    with open(run_root / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    if dry_run:
        return

    for i, (model, format_, task, n) in enumerate(cells, 1):
        click.echo(f"[{i}/{len(cells)}] model={model} format={format_} task={task['id']} trial={n}")
        out_dir = run_root / model / format_ / task["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"trial_{n}.json"

        try:
            result = run_one(
                model=model, format_=format_, task=task,
                trial_idx=n, jaeger_url=jaeger_url,
            )
        except Exception as e:
            result = {"success": False, "error": str(e), "exception_type": type(e).__name__}

        record = {
            "run_id": run_id,
            "model": model,
            "format": format_,
            "task_id": task["id"],
            "trial": n,
            "ts_unix": int(time.time()),
            "result": result,
        }
        with open(out_path, "w") as f:
            json.dump(record, f, indent=2)

    click.echo(f"DONE. results at {run_root}")


if __name__ == "__main__":
    main()
