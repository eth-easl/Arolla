#!/usr/bin/env python3
"""Run the metastable benchmark suite for a hybrid (3-knob) RL agent.

This is the hybrid counterpart of ``run_metastable_benchmarks_relative.py`` /
``run_metastable_benchmarks_absolute.py``. It iterates over the benchmark
scenarios defined in ``metastable_benchmark_suite.py``, evaluates the hybrid
agent against the No-Budget and Static-Budget baselines on each, and writes
per-scenario plots/JSON plus two aggregate CSVs.

The hybrid variant (absolute vs. relative) is auto-detected from the
checkpoint's ``model_variant.txt`` unless overridden with ``--variant``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from eval_rl_metastable_hybrid import evaluate_scenario
from metastable_benchmark_suite import resolve_benchmark_yaml_paths
from rl_paths import default_output_dir


def _default_output_dir(model_path: str) -> Path:
    # Artifacts land next to the model so each checkpoint keeps its own benchmark
    # folder (easy diffing); for the committed models/ tree they are redirected to
    # the git-ignored outputs/ dir so the reference model stays clean.
    return default_output_dir(model_path, "metastable_benchmarks")


# Known baseline labels, in column order. "Best Static" is optional; we detect
# whether it was produced by checking evaluate_scenario's returned results.
_BASELINE_COLUMN_SPEC = [
    ("No Budget", "no_budget"),
    ("Static Budget", "static_budget"),
    ("Best Static", "best_static"),
    ("RL Agent", "rl_agent"),
]


def run_benchmark_suite(
    model_path: str,
    output_dir: str | None = None,
    seed: int = 42,
    scenario_names: list[str] | None = None,
    variant: str | None = None,
    include_best_static: bool = True,
    fixed_static_refill_rate: int | None = None,
    fixed_static_bucket_capacity: int | None = None,
) -> Path:
    model_path = str(Path(model_path).resolve())
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir(model_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    if (fixed_static_refill_rate is None) != (fixed_static_bucket_capacity is None):
        raise ValueError(
            "fixed_static_refill_rate and fixed_static_bucket_capacity must be "
            "provided together."
        )

    yaml_paths = resolve_benchmark_yaml_paths(scenario_names)

    summary_rows = []
    per_client_rows = []

    for yaml_path in yaml_paths:
        scenario_name = yaml_path.stem
        scenario_dir = out_dir / scenario_name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        plot_path = scenario_dir / "comparison.png"

        results, _, fault_windows = evaluate_scenario(
            model_path=model_path,
            yaml_path=str(yaml_path),
            seed=seed,
            plot_path=str(plot_path),
            artifacts_dir=str(scenario_dir),
            print_table=False,
            show_plot=False,
            variant=variant,
            include_best_static=include_best_static,
            fixed_static_refill_rate=fixed_static_refill_rate,
            fixed_static_bucket_capacity=fixed_static_bucket_capacity,
        )

        by_label = {result["label"]: result for result in results}
        summary_row = {
            "scenario": scenario_name,
            "yaml": str(yaml_path),
            "fault_windows": "; ".join(
                f"{name} {start:.0f}-{end:.0f}s" for name, start, end, _ in fault_windows
            ),
        }
        # Record which (refill, capacity) the Best Static sweep selected, so
        # downstream analysis can answer "did the RL pick a static point at
        # all, or did it genuinely time-vary?".
        bs_choice = by_label.get("Best Static", {}).get("best_static_choice")
        if bs_choice is not None:
            summary_row["best_static_refill_rate"] = bs_choice["refill_rate"]
            summary_row["best_static_bucket_capacity"] = bs_choice["bucket_capacity"]
        static_choice = by_label.get("Static Budget", {}).get("static_budget_choice")
        if static_choice is not None:
            summary_row["static_budget_refill_rate"] = static_choice["refill_rate"]
            summary_row["static_budget_bucket_capacity"] = static_choice["bucket_capacity"]

        for label, prefix in _BASELINE_COLUMN_SPEC:
            if label not in by_label:
                continue
            result = by_label[label]
            summary_row[f"{prefix}_sr_fault_agg"] = result["sr_fault_agg"]
            summary_row[f"{prefix}_load_amp"] = result["load_amp"]
            summary_row[f"{prefix}_retry_eff"] = result["retry_eff"]
            summary_row[f"{prefix}_avg_recovery"] = result["avg_recovery"]
            summary_row[f"{prefix}_p50"] = result["p50"]
            summary_row[f"{prefix}_p95"] = result["p95"]
            summary_row[f"{prefix}_p99"] = result["p99"]
        summary_rows.append(summary_row)

        all_clients = sorted(
            {
                client_name
                for result in results
                for client_name in result["sr_fault_per_client"].keys()
            }
        )
        for client_name in all_clients:
            row = {"scenario": scenario_name, "client": client_name}
            for label, prefix in _BASELINE_COLUMN_SPEC:
                if label not in by_label:
                    continue
                result = by_label[label]
                row[f"{prefix}_sr_fault"] = result["sr_fault_per_client"].get(client_name)
                row[f"{prefix}_retry_share"] = result["retry_share"].get(client_name, 0.0)
            per_client_rows.append(row)

    pd.DataFrame(summary_rows).to_csv(out_dir / "benchmark_summary.csv", index=False)
    pd.DataFrame(per_client_rows).to_csv(out_dir / "benchmark_per_client.csv", index=False)
    return out_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run metastable benchmark suite for a hybrid (3-knob) RL agent"
    )
    parser.add_argument("--model", required=True, help="Path to saved PPO model (without .zip)")
    parser.add_argument("--output-dir", default=None, help="Directory to save benchmark outputs")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        default=None,
        help="Benchmark scenario stem to run (repeat to select a subset)",
    )
    parser.add_argument(
        "--variant",
        choices=["auto", "relative", "absolute"],
        default="auto",
        help="Hybrid variant (default: auto-detect from model_variant.txt)",
    )
    parser.add_argument(
        "--no-best-static",
        dest="include_best_static",
        action="store_false",
        help=(
            "Skip the Best-Static grid sweep baseline per scenario "
            "(adds ~25 episodes per scenario, ~20s wall time each)."
        ),
    )
    parser.add_argument(
        "--static-refill-rate",
        type=int,
        default=None,
        help="Use this same static refill rate for every benchmark scenario.",
    )
    parser.add_argument(
        "--static-bucket-capacity",
        type=int,
        default=None,
        help="Use this same static bucket capacity for every benchmark scenario.",
    )
    parser.set_defaults(include_best_static=True)
    args = parser.parse_args()

    output_dir = run_benchmark_suite(
        args.model,
        output_dir=args.output_dir,
        seed=args.seed,
        scenario_names=args.scenarios,
        variant=args.variant,
        include_best_static=args.include_best_static,
        fixed_static_refill_rate=args.static_refill_rate,
        fixed_static_bucket_capacity=args.static_bucket_capacity,
    )
    print(f"\nSaved benchmark suite to {output_dir}")
