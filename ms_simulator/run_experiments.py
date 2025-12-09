#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable or "python"

PLOT_MODULES = {
    "qps_over_time": "plotting.qps_over_time",
    "latencies_over_time": "plotting.latencies_over_time",
    "failures_over_time": "plotting.failures_over_time",
    "queue_size_over_time": "plotting.queue_size_over_time",
}


@dataclass
class Experiment:
    key: str
    module: str
    csv: str
    fault_json: Optional[str] = None
    final_csv: Optional[str] = None
    final_fault_json: Optional[str] = None

    def normalized_names(self):
        final_csv = self.final_csv or self.csv
        final_fault_json = self.final_fault_json or self.fault_json
        return final_csv, final_fault_json


EXPERIMENTS: Dict[str, Experiment] = {
    "default": Experiment(
        key="default",
        module="experiments.default",
        csv="output.csv",
        fault_json="fault_events.json",
        final_csv="default_output.csv",
        final_fault_json="default_fault_events.json",
    ),
    "nofaults": Experiment(
        key="nofaults",
        module="experiments.nofaults",
        csv="output.csv",
        final_csv="nofaults_output.csv",
    ),
    "bursty_limiter": Experiment(
        key="bursty_limiter",
        module="experiments.single.bursty_limiter",
        csv="bursty_limiter_output.csv",
    ),
    "circuit_breaker_count": Experiment(
        key="circuit_breaker_count",
        module="experiments.single.circuit_breaker_count",
        csv="circuit_breaker_count_output.csv",
    ),
    "circuit_breaker_time": Experiment(
        key="circuit_breaker_time",
        module="experiments.single.circuit_breaker_time",
        csv="circuit_breaker_time_output.csv",
    ),
    "exponential_backoff": Experiment(
        key="exponential_backoff",
        module="experiments.single.exponential_backoff",
        csv="exponential_backoff_output.csv",
    ),
    "fixed_window_limiter": Experiment(
        key="fixed_window_limiter",
        module="experiments.single.fixed_window_limiter",
        csv="fixed_window_limiter_output.csv",
    ),
    "jittered_backoff": Experiment(
        key="jittered_backoff",
        module="experiments.single.jittered_backoff",
        csv="jittered_backoff_output.csv",
    ),
    "rate_limiter_leaky": Experiment(
        key="rate_limiter_leaky",
        module="experiments.single.rate_limiter_leaky",
        csv="rate_limiter_leaky_output.csv",
    ),
    "retry_budget": Experiment(
        key="retry_budget",
        module="experiments.single.retry_budget",
        csv="retry_budget_output.csv",
    ),
    "2_chain": Experiment(
        key="2_chain",
        module="experiments.2_chain",
        csv="2_chain_output.csv",
    ),
}


def run(cmd: List[str]) -> int:
    print(f"[RUN] {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=REPO_ROOT)


def ensure_unique_outputs(exp: Experiment) -> Tuple[str, Optional[str]]:
    csv_final, fault_final = exp.normalized_names()

    src_csv = os.path.join(REPO_ROOT, exp.csv)
    dst_csv = os.path.join(REPO_ROOT, csv_final)

    src_fault = (
        os.path.join(REPO_ROOT, exp.fault_json) if exp.fault_json else None
    )
    dst_fault = (
        os.path.join(REPO_ROOT, fault_final) if fault_final else None
    )

    if os.path.exists(src_csv) and os.path.abspath(src_csv) != os.path.abspath(dst_csv):
        safe_move(src_csv, dst_csv)
    elif not os.path.exists(dst_csv) and os.path.exists(src_csv):
        dst_csv = src_csv

    if src_fault and os.path.exists(src_fault):
        if dst_fault and os.path.abspath(src_fault) != os.path.abspath(dst_fault):
            safe_move(src_fault, dst_fault)
        else:
            dst_fault = src_fault

    return dst_csv, dst_fault


def safe_move(src: str, dst: str):
    os.makedirs(os.path.dirname(dst) or REPO_ROOT, exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    print(f"[MOVE] {os.path.relpath(src, REPO_ROOT)} -> {os.path.relpath(dst, REPO_ROOT)}")
    shutil.move(src, dst)


def move_outputs_to_dir(csv_path: str, fault_events_json: Optional[str], outputs_dir: str) -> Tuple[str, Optional[str]]:
    os.makedirs(outputs_dir, exist_ok=True)

    new_csv = os.path.join(outputs_dir, os.path.basename(csv_path))
    if os.path.abspath(new_csv) != os.path.abspath(csv_path):
        safe_move(csv_path, new_csv)

    new_fault = None
    if fault_events_json:
        new_fault = os.path.join(outputs_dir, os.path.basename(fault_events_json))
        if os.path.abspath(new_fault) != os.path.abspath(fault_events_json):
            safe_move(fault_events_json, new_fault)

    return new_csv, new_fault


def remove_if_exists(path: Optional[str]):
    if path and os.path.exists(path):
        print(f"[CLEAN] Removing stale file: {os.path.relpath(path, REPO_ROOT)}")
        os.remove(path)


def run_experiment(exp: Experiment) -> Tuple[str, Optional[str]]:
    src_csv = os.path.join(REPO_ROOT, exp.csv)
    src_fault = os.path.join(REPO_ROOT, exp.fault_json) if exp.fault_json else None
    final_csv, final_fault = exp.normalized_names()
    dst_csv = os.path.join(REPO_ROOT, final_csv) if final_csv else None
    dst_fault = os.path.join(REPO_ROOT, final_fault) if final_fault else None

    for p in (src_csv, src_fault, dst_csv, dst_fault):
        remove_if_exists(p)

    code = run([PYTHON, "-m", exp.module])
    if code != 0:
        raise SystemExit(f"Experiment {exp.key} failed with exit code {code}")
    return ensure_unique_outputs(exp)


def generate_plots(csv_path: str, fault_events_json: Optional[str], outdir: str, exp_key: str, plots: List[str]):
    os.makedirs(outdir, exist_ok=True)
    for plot_key in plots:
        mod = PLOT_MODULES[plot_key]
        output_png = os.path.join(outdir, f"{exp_key}_{plot_key}.png")
        cmd = [PYTHON, "-m", mod, csv_path, "--output", output_png]
        if fault_events_json and os.path.exists(fault_events_json):
            cmd += ["--fault-events", fault_events_json]
        code = run(cmd)
        if code != 0:
            print(f"[WARN] Plot {plot_key} failed for {exp_key} (exit {code})")


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="Run experiments and generate plots.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--all", action="store_true", help="Run all experiments")
    group.add_argument("--experiment", "-e", help="Run a single experiment by name")

    parser.add_argument(
        "--plots",
        nargs="*",
        choices=list(PLOT_MODULES.keys()),
        default=list(PLOT_MODULES.keys()),
        help="Which plot types to generate (default: all)",
    )
    parser.add_argument(
        "--outdir",
        default=os.path.join(REPO_ROOT, "results"),
        help="Base results directory where artifacts will be saved (default: ./results)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available experiments and plots, then exit",
    )
    return parser.parse_args(argv)


def list_items():
    print("Available experiments:")
    for k in sorted(EXPERIMENTS.keys()):
        exp = EXPERIMENTS[k]
        final_csv, _ = exp.normalized_names()
        print(f"  - {k}: module={exp.module}, csv={final_csv}")
    print("\nAvailable plots:")
    for k in PLOT_MODULES.keys():
        print(f"  - {k}")


def main(argv: Optional[List[str]] = None):
    args = parse_args(argv)

    if args.list:
        list_items()
        if not (args.all or args.experiment):
            return 0

    plots = args.plots

    if args.all:
        for key, exp in EXPERIMENTS.items():
            print(f"\n=== Running experiment: {key} ({exp.module}) ===")
            csv_path, fault_json = run_experiment(exp)
            outputs_dir = os.path.join(args.outdir, key, "outputs")
            csv_path, fault_json = move_outputs_to_dir(csv_path, fault_json, outputs_dir)
            print(f"[OK] CSV: {os.path.relpath(csv_path, REPO_ROOT)}")
            if fault_json:
                print(f"[OK] Fault events: {os.path.relpath(fault_json, REPO_ROOT)}")
            plots_dir = os.path.join(args.outdir, key, "plots")
            generate_plots(csv_path, fault_json, plots_dir, key, plots)
        print("\nAll experiments and plots finished.")
        return 0

    key = args.experiment
    if key not in EXPERIMENTS:
        print(f"Unknown experiment '{key}'. Use --list to see options.")
        return 2
    exp = EXPERIMENTS[key]
    print(f"\n=== Running experiment: {key} ({exp.module}) ===")
    csv_path, fault_json = run_experiment(exp)
    outputs_dir = os.path.join(args.outdir, key, "outputs")
    csv_path, fault_json = move_outputs_to_dir(csv_path, fault_json, outputs_dir)
    print(f"[OK] CSV: {os.path.relpath(csv_path, REPO_ROOT)}")
    if fault_json:
        print(f"[OK] Fault events: {os.path.relpath(fault_json, REPO_ROOT)}")
    plots_dir = os.path.join(args.outdir, key, "plots")
    generate_plots(csv_path, fault_json, plots_dir, key, plots)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
