#!/usr/bin/env python3
"""Run the Istio retry-budget metastable benchmark suite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from eval_istio_retry_budget_metastable import evaluate_scenario


BENCHMARK_DIR = Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "istio_retry_budget_benchmarks"
DEFAULT_SCENARIOS = [
    "istio_partial_failure",
    "istio_load_spike",
    "istio_compound_failure",
    "istio_switchback_adversarial",
]


def resolve_benchmark_yaml_paths(scenario_names: list[str] | None = None) -> list[Path]:
    names = scenario_names if scenario_names is not None else DEFAULT_SCENARIOS
    paths = []
    for name in names:
        path = BENCHMARK_DIR / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Benchmark scenario not found: {path}")
        paths.append(path)
    return paths


def _default_output_dir(model_path: str) -> Path:
    return Path(model_path).resolve().parent / "istio_retry_budget_benchmarks"


def run_benchmark_suite(
    model_path: str,
    output_dir: str | None = None,
    seed: int = 42,
    scenario_names: list[str] | None = None,
) -> Path:
    model_path = str(Path(model_path).resolve())
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir(model_path)
    out_dir.mkdir(parents=True, exist_ok=True)

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
        )

        by_label = {result["label"]: result for result in results}
        summary_row = {
            "scenario": scenario_name,
            "yaml": str(yaml_path),
            "static_percent": 20.0,
            "static_min_retry_concurrency": 3,
            "fault_windows": "; ".join(f"{name} {start:.0f}-{end:.0f}s" for name, start, end, _ in fault_windows),
        }
        for label, prefix in [("No Budget", "no_budget"), ("Static Budget", "static_budget"), ("RL Agent", "rl_agent")]:
            result = by_label[label]
            summary_row[f"{prefix}_sr_fault_agg"] = result["sr_fault_agg"]
            summary_row[f"{prefix}_load_amp"] = result["load_amp"]
            summary_row[f"{prefix}_retry_eff"] = result["retry_eff"]
            summary_row[f"{prefix}_avg_recovery"] = result["avg_recovery"]
            summary_row[f"{prefix}_p50"] = result["p50"]
            summary_row[f"{prefix}_p95"] = result["p95"]
            summary_row[f"{prefix}_p99"] = result["p99"]
        summary_rows.append(summary_row)

        all_clients = sorted({
            client_name
            for result in results
            for client_name in result["sr_fault_per_client"].keys()
        })
        for client_name in all_clients:
            row = {"scenario": scenario_name, "client": client_name}
            for label, prefix in [("No Budget", "no_budget"), ("Static Budget", "static_budget"), ("RL Agent", "rl_agent")]:
                result = by_label[label]
                row[f"{prefix}_sr_fault"] = result["sr_fault_per_client"].get(client_name)
                row[f"{prefix}_retry_share"] = result["retry_share"].get(client_name, 0.0)
            per_client_rows.append(row)

    pd.DataFrame(summary_rows).to_csv(out_dir / "benchmark_summary.csv", index=False)
    pd.DataFrame(per_client_rows).to_csv(out_dir / "benchmark_per_client.csv", index=False)
    return out_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Istio retry-budget benchmark suite")
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
    args = parser.parse_args()

    output_dir = run_benchmark_suite(
        args.model,
        output_dir=args.output_dir,
        seed=args.seed,
        scenario_names=args.scenarios,
    )
    print(f"\nSaved benchmark suite to {output_dir}")
