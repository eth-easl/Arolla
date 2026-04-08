#!/usr/bin/env python3
"""
Evaluate a trained RL agent against static and no-budget baselines on any
single-service YAML scenario.

Runs three variants of the same scenario and compares them on:
  1. Success rate during failure phase (aggregated + per-client)
  2. Load amplification
  3. Retry efficiency (% of admitted retries that succeed)
  4. Fairness: retry share between clients
  5. Time to recover
  6. Latencies (p50, p95, p99)

Usage:
    python simulator/bin/eval_rl_scenario.py \
        --model ppo_random_agent \
        --yaml  simulator/experiments/yaml/load_spike_metastable_failure/ep3.yaml
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO

from simulator.config.loader import ConfigLoader
from simulator.config.schema import ExperimentConfig
from simulator.metrics.collector import Metrics
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.rl.random_scenario_env import token_bucket_indices_from_physical
from simulator.utils.time import s_to_ns

REFILL_RATE_MAP = [5, 15, 30, 60, 90]
BUCKET_CAPACITY_MAP = [5, 10, 20, 50, 80]


# Scenario runners
def _build_and_drive(config: ExperimentConfig):
    """Build simulation from config, wire workloads, return components."""
    sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
    service = list(services.values())[0]
    service.enable_live_buffer()

    for wl, client in zip(workloads, clients):
        wl.drive(sim, lambda s, c=client: c.start_request(s))

    episode_end = s_to_ns(workloads[0].duration_s)
    return sim, clients, service, workloads, episode_end


def run_no_budget(yaml_path: str, seed=42):
    """Run the scenario WITHOUT any global_retry_budget."""
    config = ConfigLoader.load_from_file(yaml_path)
    stripped = [
        svc.model_copy(update={"global_retry_budget": None,
                                "aimd_global_retry_budget": None})
        for svc in config.services
    ]
    config = config.model_copy(update={"services": stripped, "seed": seed})

    sim, clients, service, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def run_static_budget(yaml_path: str, seed=42):
    """Run the scenario with the budget exactly as defined in the YAML."""
    config = ConfigLoader.load_from_file(yaml_path)
    config = config.model_copy(update={"seed": seed})

    sim, clients, service, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def run_rl_agent(yaml_path: str, model_path: str,
                 decision_interval_s: float = 2.0, seed=42):
    """Run with the RL agent dynamically tuning the token bucket."""
    config = ConfigLoader.load_from_file(yaml_path)
    config = config.model_copy(update={"seed": seed})

    sim, clients, service, workloads, episode_end = _build_and_drive(config)
    decision_interval_ns = s_to_ns(decision_interval_s)

    model = PPO.load(model_path)

    actions_history = []

    prev_success_rate = 1.0
    prev_error_rate = 0.0
    prev_retry_ratio = 0.0

    next_time = decision_interval_ns
    sim.run(until=next_time)

    limiter = service.cfg.load_limiter
    if isinstance(limiter, GlobalRetryBudget):
        current_refill_idx, current_capacity_idx = token_bucket_indices_from_physical(
            int(limiter.refill_rate),
            int(limiter.max_tokens),
            REFILL_RATE_MAP,
            BUCKET_CAPACITY_MAP,
        )
    else:
        current_refill_idx = 0
        current_capacity_idx = 0

    while sim.timestep < episode_end:
        obs_dict = service.live_buffer.get_observation(
            sim.timestep, decision_interval_ns
        )

        delta_success = obs_dict["success_rate"] - prev_success_rate
        delta_error = obs_dict["error_rate"] - prev_error_rate
        delta_retry = obs_dict["retry_ratio"] - prev_retry_ratio

        prev_success_rate = obs_dict["success_rate"]
        prev_error_rate = obs_dict["error_rate"]
        prev_retry_ratio = obs_dict["retry_ratio"]

        obs = np.array([
            obs_dict["success_rate"],
            obs_dict["error_rate"],
            obs_dict["retry_ratio"],
            obs_dict["p50"],
            obs_dict["p99"],
            obs_dict["queue_avg"],
            obs_dict["total_requests"],
            obs_dict["success"],
            obs_dict["failure"],
            obs_dict["retries"],
            obs_dict["fail_queue_full"],
            obs_dict["fail_deadline"],
            obs_dict["fail_server"],
            delta_success,
            delta_error,
            delta_retry,
            float(current_refill_idx) / 4.0,
            float(current_capacity_idx) / 4.0,
        ], dtype=np.float32)

        action, _ = model.predict(obs, deterministic=True)

        current_refill_idx = int(action[0])
        current_capacity_idx = int(action[1])
        refill_rate = REFILL_RATE_MAP[current_refill_idx]
        bucket_capacity = BUCKET_CAPACITY_MAP[current_capacity_idx]

        service.update_token_bucket(
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
        )

        actions_history.append({
            "time_s": sim.timestep / 1e9,
            "refill_rate": refill_rate,
            "bucket_capacity": bucket_capacity,
        })

        next_time += decision_interval_ns
        sim.run(until=min(next_time, episode_end))

    sim.run()
    return clients, pd.DataFrame(actions_history)

# Fault-window helpers
def detect_fault_windows(yaml_path: str):
    """Extract fault/spike windows from the YAML for metric slicing."""
    config = ConfigLoader.load_from_file(yaml_path)
    windows = []
    for svc in config.services:
        for pf in svc.partial_failures:
            windows.append(("Partial Failure", pf.start_s, pf.end_s, "red"))
    if config.workload:
        for ls in config.workload.load_spikes:
            windows.append(("Load Spike", ls.start_s, ls.end_s, "orange"))
    if config.clients:
        for cl in config.clients:
            for ls in cl.workload.load_spikes:
                windows.append(("Load Spike", ls.start_s, ls.end_s, "orange"))
    return windows

# Metric computation
def compute_metrics(clients, fault_windows, label):
    """Compute the six evaluation metrics from completed client data."""

    per_client_roots = {c.cfg.name: c.roots for c in clients}
    all_roots = [r for c in clients for r in c.roots]
    total_attempts = sum(c.attempts_total for c in clients)
    total_roots = len(all_roots)

    # -- 1. Success rate during failure phase --------------------------------
    def _sr_in_faults(roots):
        in_fault = []
        for root in roots:
            if not root.attempts:
                continue
            t_s = root.attempts[0].interval.begin / 1e9
            for _, start, end, _ in fault_windows:
                if start <= t_s <= end:
                    in_fault.append(root)
                    break
        if not in_fault:
            return float("nan")
        ok = sum(1 for r in in_fault if any(a.success for a in r.attempts))
        return ok / len(in_fault)

    sr_fault_agg = _sr_in_faults(all_roots)
    sr_fault_per_client = {
        name: _sr_in_faults(roots) for name, roots in per_client_roots.items()
    }

    # -- 2. Load amplification -----------------------------------------------
    load_amp = total_attempts / total_roots if total_roots > 0 else float("nan")

    # -- 3. Retry efficiency --------------------------------------------------
    retry_total = 0
    retry_ok = 0
    for root in all_roots:
        for attempt in root.attempts[1:]:
            retry_total += 1
            if attempt.success:
                retry_ok += 1
    retry_eff = retry_ok / retry_total if retry_total > 0 else float("nan")

    # -- 4. Fairness: retry share per client ----------------------------------
    per_client_retries = {}
    total_retries = 0
    for name, roots in per_client_roots.items():
        retries = sum(r.attempt_count() - 1 for r in roots)
        per_client_retries[name] = retries
        total_retries += retries
    retry_share = {
        name: count / total_retries if total_retries > 0 else 0.0
        for name, count in per_client_retries.items()
    }

    # -- 5. Time to recover ---------------------------------------------------
    metrics_obj = Metrics(roots=all_roots, attempts_total=total_attempts)
    ts_df = metrics_obj.to_dataframe(granularity_s=1.0)

    recovery_times = []
    for _, _, end_s, _ in fault_windows:
        if ts_df.empty:
            recovery_times.append(float("inf"))
            continue
        post = ts_df[ts_df["timepoint"] >= end_s]
        recovered = False
        for _, row in post.iterrows():
            completed = row["success_root"] + row["failure_root"]
            if completed > 0 and row["success_root"] / completed >= 0.95:
                recovery_times.append(row["timepoint"] - end_s)
                recovered = True
                break
        if not recovered:
            recovery_times.append(float("inf"))
    avg_recovery = float(np.mean(recovery_times)) if recovery_times else float("nan")

    # -- 6. Latencies ---------------------------------------------------------
    summary = metrics_obj.summary()

    return {
        "label": label,
        "sr_fault_agg": sr_fault_agg,
        "sr_fault_per_client": sr_fault_per_client,
        "load_amp": load_amp,
        "retry_eff": retry_eff,
        "retry_share": retry_share,
        "recovery_times": recovery_times,
        "avg_recovery": avg_recovery,
        "p50": summary.p50,
        "p95": summary.p95,
        "p99": summary.p99,
        "ts_df": ts_df,
    }


# Display
def print_comparison(results, fault_windows):
    """Print a formatted comparison table."""
    labels = [r["label"] for r in results]

    print("\n" + "=" * 80)
    print("EVALUATION RESULTS")
    print("=" * 80)

    # Fault windows summary
    if fault_windows:
        print("\nFault windows:")
        for name, start, end, _ in fault_windows:
            print(f"  {name}: {start:.0f}s – {end:.0f}s")

    header = f"{'Metric':<35}" + "".join(f"{l:>15}" for l in labels)
    print("\n" + header)
    print("-" * len(header))

    def _row(name, values, fmt=".4f", best_fn=None):
        cells = []
        for v in values:
            if np.isnan(v) or np.isinf(v):
                cells.append(f"{'N/A':>15}")
            else:
                cells.append(f"{v:>15{fmt}}")
        line = f"{name:<35}" + "".join(cells)
        if best_fn is not None:
            finite = [v for v in values if np.isfinite(v)]
            if finite:
                best = best_fn(finite)
                markers = [" *" if np.isfinite(v) and abs(v - best) < 1e-9 else "" for v in values]
                line += "".join(markers)
        print(line)

    vals = lambda key: [r[key] for r in results]

    _row("Success Rate (fault phase)", vals("sr_fault_agg"), best_fn=max)
    _row("Load Amplification", vals("load_amp"), best_fn=min)
    _row("Retry Efficiency (%)", [r["retry_eff"] * 100 for r in results], fmt=".1f", best_fn=max)
    _row("Avg Recovery Time (s)", vals("avg_recovery"), fmt=".1f", best_fn=min)
    _row("P50 Latency (ms)", vals("p50"), fmt=".2f", best_fn=min)
    _row("P95 Latency (ms)", vals("p95"), fmt=".2f", best_fn=min)
    _row("P99 Latency (ms)", vals("p99"), fmt=".2f", best_fn=min)

    # Per-client success rate during faults (only useful with multiple clients)
    all_client_names = set()
    for r in results:
        all_client_names.update(r["sr_fault_per_client"].keys())
    if len(all_client_names) > 1:
        print(f"\n{'--- Per-Client Success Rate (fault phase) ---':^{len(header)}}")
        for cname in sorted(all_client_names):
            _row(f"  {cname}",
                 [r["sr_fault_per_client"].get(cname, float("nan")) for r in results],
                 best_fn=max)

    # Retry share (only useful with multiple clients)
    if len(all_client_names) > 1:
        print(f"\n{'--- Retry Share (%) ---':^{len(header)}}")
        for cname in sorted(all_client_names):
            _row(f"  {cname}",
                 [r["retry_share"].get(cname, 0.0) * 100 for r in results],
                 fmt=".1f")

    print("=" * 80)


def plot_comparison(results, fault_windows, rl_actions_df=None,
                    save_path="eval_comparison.png"):
    """Time-series comparison plot with 4 panels."""

    n_panels = 5 if rl_actions_df is not None and not rl_actions_df.empty else 4
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 4 * n_panels), sharex=True)

    styles = {
        "No Budget":     ("#d32f2f", "--", 1.8),
        "Static Budget": ("gray",    "-",  2.0),
        "RL Agent":      ("#2e7d32", "-",  2.5),
    }

    # -- Panel 1: Success Rate -----------------------------------------------
    for r in results:
        color, ls, lw = styles[r["label"]]
        df = r["ts_df"]
        if df.empty:
            continue
        completed = df["success_root"] + df["failure_root"]
        sr = df["success_root"] / completed.replace(0, np.nan)
        axes[0].plot(df["timepoint"], sr,
                     label=r["label"], color=color, linestyle=ls, linewidth=lw)
    axes[0].set_ylabel("Success Rate")
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_title("Success Rate Over Time")
    axes[0].legend(loc="lower left")

    # -- Panel 2: Load Amplification -----------------------------------------
    for r in results:
        color, ls, lw = styles[r["label"]]
        df = r["ts_df"]
        if df.empty:
            continue
        total_attempts = df["root_requests"] + df["retries"]
        amp = total_attempts / df["root_requests"].replace(0, np.nan)
        axes[1].plot(df["timepoint"], amp,
                     label=r["label"], color=color, linestyle=ls, linewidth=lw)
    axes[1].set_ylabel("Load Amplification")
    axes[1].set_title("Load Amplification (total attempts / root requests)")
    axes[1].legend()

    # -- Panel 3: P99 Latency ------------------------------------------------
    for r in results:
        color, ls, lw = styles[r["label"]]
        df = r["ts_df"]
        if df.empty:
            continue
        axes[2].plot(df["timepoint"], df["p99"],
                     label=r["label"], color=color, linestyle=ls, linewidth=lw)
    axes[2].set_ylabel("P99 Latency (ms)")
    axes[2].set_title("Tail Latency (P99)")
    axes[2].legend()

    # -- Panel 4: Retry Efficiency -------------------------------------------
    for r in results:
        color, ls, lw = styles[r["label"]]
        df = r["ts_df"]
        if df.empty:
            continue
        eff = (df["retries"] - df["failure_retry"]) / df["retries"].replace(0, np.nan) * 100
        axes[3].plot(df["timepoint"], eff,
                     label=r["label"], color=color, linestyle=ls, linewidth=lw)
    axes[3].set_ylabel("Retry Efficiency (%)")
    axes[3].set_title("Retry Efficiency (successful retries / total retries)")
    axes[3].legend()

    # -- Panel 5 (optional): RL Agent Actions --------------------------------
    if n_panels == 5:
        ax_left = axes[4]
        ax_right = ax_left.twinx()
        ax_left.step(rl_actions_df["time_s"], rl_actions_df["refill_rate"],
                     where="post", color="#2196F3", linewidth=2, label="Refill Rate")
        ax_right.step(rl_actions_df["time_s"], rl_actions_df["bucket_capacity"],
                      where="post", color="#9C27B0", linewidth=2, label="Bucket Cap.")
        ax_left.set_ylabel("Refill Rate (rps)", color="#2196F3")
        ax_right.set_ylabel("Bucket Capacity", color="#9C27B0")
        ax_left.set_yticks(REFILL_RATE_MAP)
        ax_right.set_yticks(BUCKET_CAPACITY_MAP)
        lines_l, labels_l = ax_left.get_legend_handles_labels()
        lines_r, labels_r = ax_right.get_legend_handles_labels()
        ax_left.legend(lines_l + lines_r, labels_l + labels_r, loc="upper right")
        axes[4].set_title("RL Agent Actions")

    axes[-1].set_xlabel("Time (s)")

    for name, start, end, color in fault_windows:
        for ax in axes:
            ax.axvspan(start, end, alpha=0.12, color=color)

    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"\nSaved plot to {save_path}")
    plt.show()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate RL agent vs static/no-budget baselines"
    )
    parser.add_argument("--model", required=True,
                        help="Path to saved PPO model (without .zip)")
    parser.add_argument("--yaml", required=True,
                        help="Path to scenario YAML")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="eval_comparison.png",
                        help="Output plot filename")
    args = parser.parse_args()

    yaml_path = args.yaml
    fault_windows = detect_fault_windows(yaml_path)

    print(f"Scenario : {yaml_path}")
    print(f"Model    : {args.model}")
    print(f"Seed     : {args.seed}")
    print()

    # --- Run 1: No budget ---
    print("[1/3] Running scenario WITHOUT budget …")
    nb_clients = run_no_budget(yaml_path, seed=args.seed)

    # --- Run 2: Static budget ---
    print("[2/3] Running scenario with STATIC budget …")
    st_clients = run_static_budget(yaml_path, seed=args.seed)

    # --- Run 3: RL agent ---
    print("[3/3] Running scenario with RL AGENT …")
    rl_clients, rl_actions = run_rl_agent(yaml_path, args.model, seed=args.seed)

    # --- Compute metrics ---
    results = [
        compute_metrics(nb_clients, fault_windows, "No Budget"),
        compute_metrics(st_clients, fault_windows, "Static Budget"),
        compute_metrics(rl_clients, fault_windows, "RL Agent"),
    ]

    # --- Output ---
    print_comparison(results, fault_windows)
    plot_comparison(results, fault_windows,
                    rl_actions_df=rl_actions,
                    save_path=args.output)
