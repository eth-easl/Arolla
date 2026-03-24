#!/usr/bin/env python3
"""
Evaluate a trained RL agent against static and no-budget baselines on any
single-service YAML scenario.

Runs three variants of the same scenario and plots them side by side:
  1. No Budget      – global_retry_budget removed, retries unchecked
  2. Static Budget  – global_retry_budget as defined in the YAML (fixed)
  3. RL Agent       – trained model dynamically tuning the token bucket

Usage:
    python simulator/bin/eval_rl_scenario.py \
        --model ppo_random_agent \
        --yaml  simulator/experiments/yaml/load_spike_metastable_failure/ep3.yaml

    python simulator/bin/eval_rl_scenario.py \
        --model ppo_random_agent \
        --yaml  simulator/experiments/yaml/partial_failure_metastable_failure/ep5.yaml
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
from simulator.rl.env import RetrySimEnv
from simulator.utils.time import s_to_ns


# Helpers

def run_with_env(yaml_path: str, model=None, fixed_action=None, seed=42):
    """
    Run one episode through RetrySimEnv.

    - model != None       → RL agent picks actions
    - fixed_action != None → same action every step (static baseline)
    - both None           → not valid, caller must supply one
    """
    env = RetrySimEnv(yaml_path, decision_interval_s=2.0)
    obs, _ = env.reset(seed=seed)

    while True:
        if model is not None:
            action, _ = model.predict(obs, deterministic=True)
        else:
            action = fixed_action
        obs, reward, done, _, info = env.step(action)
        if done:
            break

    return pd.DataFrame(env.history)


def run_no_budget(yaml_path: str, decision_interval_s: float = 2.0, seed=42):
    """
    Run the scenario WITHOUT any global_retry_budget.
    Uses ConfigLoader directly so we can strip the budget from the config.
    Collects metrics at the same cadence as the RL env for fair comparison.
    """
    config = ConfigLoader.load_from_file(yaml_path)

    # Strip global_retry_budget from every service
    stripped_services = []
    for svc in config.services:
        stripped_services.append(
            svc.model_copy(update={"global_retry_budget": None,
                                   "aimd_global_retry_budget": None})
        )
    config = config.model_copy(update={"services": stripped_services,
                                       "seed": seed})

    sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
    client = clients[0]
    service = list(services.values())[0]
    service.enable_live_buffer()

    workload = workloads[0]
    episode_end = s_to_ns(workload.duration_s)
    decision_interval_ns = s_to_ns(decision_interval_s)

    workload.drive(sim, lambda s, c=client: c.start_request(s))

    # Step through at the same cadence as the RL env
    history = []
    next_time = decision_interval_ns
    sim.run(until=next_time)

    while sim.timestep < episode_end:
        next_time += decision_interval_ns
        sim.run(until=min(next_time, episode_end))

        obs_dict = service.live_buffer.get_observation(
            sim.timestep, decision_interval_ns
        )
        history.append({
            "time_s": sim.timestep / 1e9,
            "success_rate": obs_dict["success_rate"],
            "error_rate": obs_dict["error_rate"],
            "retry_ratio": obs_dict["retry_ratio"],
            "p50": obs_dict["p50"],
            "p99": obs_dict["p99"],
            "queue_avg": obs_dict["queue_avg"],
            "fail_server": obs_dict["fail_server"],
            "fail_deadline": obs_dict["fail_deadline"],
            "fail_queue_full": obs_dict["fail_queue_full"],
        })

    sim.run()  # drain remaining
    return pd.DataFrame(history)


def detect_fault_windows(yaml_path: str):
    """Read the YAML and extract fault/spike windows for shading on the plot."""
    config = ConfigLoader.load_from_file(yaml_path)
    faults = []
    for svc in config.services:
        for pf in svc.partial_failures:
            faults.append(("Partial Failure", pf.start_s, pf.end_s, "red"))
    if config.workload:
        for ls in config.workload.load_spikes:
            faults.append(("Load Spike", ls.start_s, ls.end_s, "orange"))
    return faults


def find_static_action(yaml_path: str):
    """
    Determine the fixed action indices that match the YAML's
    global_retry_budget settings.
    """
    refill_map = [5, 15, 30, 60, 120]
    capacity_map = [5, 10, 20, 50, 100]

    config = ConfigLoader.load_from_file(yaml_path)
    svc = config.services[0]

    if svc.global_retry_budget is None:
        return [2, 2] # Default

    target_rps = svc.global_retry_budget.target_rps
    max_burst = svc.global_retry_budget.max_burst

    # Find closest index in the action maps
    ri = int(np.argmin([abs(v - target_rps) for v in refill_map]))
    ci = int(np.argmin([abs(v - max_burst) for v in capacity_map]))
    return [ri, ci]


# ========================================================================
# Plotting
# ========================================================================

def plot_comparison(no_budget_df, static_df, rl_df, fault_windows,
                    save_path="eval_comparison.png"):
    """Three-way comparison plot."""

    fig, axes = plt.subplots(4, 1, figsize=(14, 16), sharex=True)

    styles = [
        ("No Budget",      no_budget_df, "#d32f2f", "--",  1.8),
        ("Static Budget",  static_df,    "gray",    "-",   2.0),
        ("RL Agent",       rl_df,        "#2e7d32", "-",   2.5),
    ]

    # --- Panel 1: Success Rate ---
    for label, df, color, ls, lw in styles:
        axes[0].plot(df["time_s"], df["success_rate"],
                     label=label, color=color, linestyle=ls, linewidth=lw)
    axes[0].set_ylabel("Success Rate")
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_title("Success Rate")
    axes[0].legend(loc="lower left")

    # --- Panel 2: Retry Ratio ---
    for label, df, color, ls, lw in styles:
        axes[1].plot(df["time_s"], df["retry_ratio"],
                     label=label, color=color, linestyle=ls, linewidth=lw)
    axes[1].set_ylabel("Retry Ratio")
    axes[1].set_title("Retry Amplification")
    axes[1].legend()

    # --- Panel 3: P99 Latency ---
    for label, df, color, ls, lw in styles:
        axes[2].plot(df["time_s"], df["p99"],
                     label=label, color=color, linestyle=ls, linewidth=lw)
    axes[2].set_ylabel("P99 Latency (ns)")
    axes[2].set_title("Tail Latency")
    axes[2].legend()

    # --- Panel 4: RL Agent Actions ---
    if "action_refill_rate" in rl_df.columns:
        ax_left = axes[3]
        ax_right = ax_left.twinx()
        ax_left.step(rl_df["time_s"], rl_df["action_refill_rate"],
                     where="post", color="#2196F3", linewidth=2, label="Refill Rate")
        ax_right.step(rl_df["time_s"], rl_df["action_bucket_capacity"],
                      where="post", color="#9C27B0", linewidth=2, label="Bucket Capacity")
        ax_left.set_ylabel("Refill Rate (rps)", color="#2196F3")
        ax_right.set_ylabel("Bucket Capacity", color="#9C27B0")
        ax_left.set_yticks([5, 15, 30, 60, 120])
        ax_right.set_yticks([5, 10, 20, 50, 100])
        lines_l, labels_l = ax_left.get_legend_handles_labels()
        lines_r, labels_r = ax_right.get_legend_handles_labels()
        ax_left.legend(lines_l + lines_r, labels_l + labels_r, loc="upper right")
        axes[3].set_title("RL Agent Actions")

    axes[3].set_xlabel("Time (s)")

    # Shade fault windows on all panels
    for name, start, end, color in fault_windows:
        for ax in axes:
            ax.axvspan(start, end, alpha=0.12, color=color)

    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"\nSaved plot to {save_path}")
    plt.show()


def print_summary(no_budget_df, static_df, rl_df):
    """Print a comparison table of mean metrics."""
    print("\n" + "=" * 70)
    print("SUMMARY (mean over episode)")
    print("=" * 70)
    print(f"{'Metric':<22} {'No Budget':>12} {'Static':>12} {'RL Agent':>12}")
    print("-" * 70)

    for col in ["success_rate", "error_rate", "retry_ratio", "p99"]:
        nb = no_budget_df[col].mean()
        st = static_df[col].mean()
        rl = rl_df[col].mean()

        # Mark best value
        if col == "success_rate":
            best = max(nb, st, rl)
        else:
            best = min(nb, st, rl)

        def marker(v):
            return " <-- best" if abs(v - best) < 1e-6 else ""

        print(f"{col:<22} {nb:>12.4f} {st:>12.4f} {rl:>12.4f}{marker(rl)}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate RL agent vs static/no-budget baselines on a YAML scenario"
    )
    parser.add_argument("--model", required=True,
                        help="Path to saved PPO model (without .zip)")
    parser.add_argument("--yaml", required=True,
                        help="Path to scenario YAML (must have global_retry_budget)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for reproducibility")
    parser.add_argument("--output", default="eval_comparison.png",
                        help="Output plot filename")
    args = parser.parse_args()

    yaml_path = args.yaml
    fault_windows = detect_fault_windows(yaml_path)
    static_action = find_static_action(yaml_path)

    print(f"Scenario : {yaml_path}")
    print(f"Model    : {args.model}")
    print(f"Seed     : {args.seed}")
    print(f"Static action (closest to YAML budget): {static_action}")
    print()

    # --- Run 1: No budget ---
    print("[1/3] Running scenario WITHOUT budget …")
    no_budget_df = run_no_budget(yaml_path, seed=args.seed)

    # --- Run 2: Static budget ---
    print("[2/3] Running scenario with STATIC budget …")
    static_df = run_with_env(yaml_path, fixed_action=static_action, seed=args.seed)

    # --- Run 3: RL agent ---
    print("[3/3] Running scenario with RL AGENT …")
    env_for_model = RetrySimEnv(yaml_path, decision_interval_s=2.0)
    model = PPO.load(args.model, env=env_for_model)
    rl_df = run_with_env(yaml_path, model=model, seed=args.seed)

    # --- Results ---
    print_summary(no_budget_df, static_df, rl_df)
    plot_comparison(no_budget_df, static_df, rl_df, fault_windows,
                    save_path=args.output)
