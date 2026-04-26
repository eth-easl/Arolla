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
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from simulator.config.loader import ConfigLoader
from simulator.config.schema import ExperimentConfig
from simulator.metrics.collector import Metrics
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.rl.random_scenario_env import (
    RandomScenarioSimEnv,
    build_observation_vector,
    stabilize_window_observation,
    token_bucket_indices_from_physical,
)
from simulator.utils.time import s_to_ns

REFILL_RATE_MAP = [5, 15, 30, 60, 90]
BUCKET_CAPACITY_MAP = [5, 10, 20, 50, 80]
# Tokens added per successful attempt for the hybrid (3-knob) env. Kept in sync
# with EVENT_REWARD_MAP in simulator.rl.hybrid_metastable_env; duplicated here
# so this plotting module stays independent of the RL env package.
EVENT_REWARD_MAP = [0.0, 0.05, 0.15, 0.30, 0.60]


def _load_obs_normalizer(model_path: str, yaml_path: str, decision_interval_s: float) -> VecNormalize:
    """Load the saved VecNormalize stats so inference uses normalized observations."""
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(
            f"Missing VecNormalize stats at {vecnorm_path}. "
            "This model was trained with normalized observations, so evaluation "
            "must load the matching stats."
        )

    def make_env():
        return RandomScenarioSimEnv(yaml_path, decision_interval_s=decision_interval_s)

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


# Scenario runners
def _build_and_drive(config: ExperimentConfig):
    """Build simulation from config, wire workloads, return components."""
    sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
    service = list(services.values())[0]
    service.enable_live_buffer()

    for wl, client in zip(workloads, clients):
        wl.drive(sim, lambda s, c=client: c.start_request(s))

    episode_end = max(s_to_ns(workload.duration_s) for workload in workloads)
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


def run_static_budget_with_override(
    yaml_path: str, refill_rate: int, bucket_capacity: int, seed: int = 42
):
    """Run the scenario with the bucket overridden to ``(refill_rate, bucket_capacity)``.

    Used by ``run_best_static_budget`` to sweep the RL's action grid and find
    the best single static setting a deployer could have picked. We deep-copy
    the YAML config and replace ``global_retry_budget`` before building so the
    override is active from the very first request (unlike a mid-episode call
    to ``service.update_token_bucket``, which is what the RL agent does).
    """
    config = ConfigLoader.load_from_file(yaml_path)
    service_template = config.services[0]
    if service_template.global_retry_budget is None:
        raise ValueError(
            "Cannot sweep static budget for a scenario with no global_retry_budget "
            "defined. Add a placeholder budget to the YAML first."
        )
    budget_cfg = service_template.global_retry_budget.model_copy(
        update={"target_rps": int(refill_rate), "max_burst": int(bucket_capacity)}
    )
    new_service = service_template.model_copy(update={"global_retry_budget": budget_cfg})
    config = config.model_copy(update={"services": [new_service], "seed": seed})

    sim, clients, service, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def run_best_static_budget(
    yaml_path: str,
    fault_windows,
    seed: int = 42,
    refill_grid=None,
    capacity_grid=None,
    verbose: bool = False,
):
    """Sweep the RL's action grid and return clients for the best static setting.

    Returns ``(best_clients, best_choice, all_results)`` where:

    - ``best_clients`` is the list of simulated clients for the chosen setting,
      ready to feed into ``compute_metrics``.
    - ``best_choice`` is ``{"refill_rate": int, "bucket_capacity": int}``.
    - ``all_results`` is a list of per-cell dicts for diagnostics/plotting.

    The "best" cell is picked by success rate during the fault phase (primary),
    with load amplification as a tiebreaker. Using the RL's *own* grid means
    this is the tightest apples-to-apples static upper bound: any edge the RL
    shows over this curve is attributable to being non-stationary, not to
    picking a finer static value than the RL could have picked itself.
    """
    refill_grid = list(refill_grid if refill_grid is not None else REFILL_RATE_MAP)
    capacity_grid = list(capacity_grid if capacity_grid is not None else BUCKET_CAPACITY_MAP)

    all_results = []
    best = None
    for refill in refill_grid:
        for capacity in capacity_grid:
            clients = run_static_budget_with_override(
                yaml_path, refill_rate=refill, bucket_capacity=capacity, seed=seed
            )
            metrics = compute_metrics(clients, fault_windows, label="__sweep__")
            cell = {
                "refill_rate": int(refill),
                "bucket_capacity": int(capacity),
                "sr_fault_agg": metrics["sr_fault_agg"],
                "load_amp": metrics["load_amp"],
                "retry_eff": metrics["retry_eff"],
                "avg_recovery": metrics["avg_recovery"],
                "p99": metrics["p99"],
            }
            all_results.append(cell)
            if verbose:
                sr = cell["sr_fault_agg"]
                amp = cell["load_amp"]
                print(
                    f"  sweep (refill={refill:>2}, cap={capacity:>2}): "
                    f"sr_fault={sr:.4f}, load_amp={amp:.3f}"
                )

            # Rank by (sr_fault_agg desc, load_amp asc). NaNs sort to the bottom.
            sr_key = cell["sr_fault_agg"]
            if np.isnan(sr_key):
                sr_key = -np.inf
            amp_key = cell["load_amp"]
            if np.isnan(amp_key):
                amp_key = np.inf
            rank_key = (-sr_key, amp_key)

            if best is None or rank_key < best["rank_key"]:
                best = {
                    "rank_key": rank_key,
                    "clients": clients,
                    "choice": {
                        "refill_rate": int(refill),
                        "bucket_capacity": int(capacity),
                    },
                }

    assert best is not None, "Best static sweep produced no cells (empty grid?)"
    return best["clients"], best["choice"], all_results


def run_rl_agent(yaml_path: str, model_path: str,
                 decision_interval_s: float = 2.0, seed=42):
    """Run with the RL agent dynamically tuning the token bucket."""
    config = ConfigLoader.load_from_file(yaml_path)
    config = config.model_copy(update={"seed": seed})

    sim, clients, service, workloads, episode_end = _build_and_drive(config)
    decision_interval_ns = s_to_ns(decision_interval_s)
    queue_capacity = service.cfg.queue_capacity or 1

    model = PPO.load(model_path)
    obs_normalizer = _load_obs_normalizer(model_path, yaml_path, decision_interval_s)

    actions_history = []

    prev_success_rate = 1.0
    prev_retry_ratio = 0.0
    prev_queue_util = 0.0
    timeout_cfg = config.services[0].timeout
    attempt_timeout_ms = float(timeout_cfg.attempt_ms) if timeout_cfg and timeout_cfg.attempt_ms else 50.0
    next_time = 0

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
        obs_dict = stabilize_window_observation(
            obs_dict,
            fallback_success_rate=prev_success_rate,
            fallback_retry_ratio=prev_retry_ratio,
            fallback_queue_util=prev_queue_util,
            queue_capacity=queue_capacity,
        )

        obs = build_observation_vector(
            obs_dict=obs_dict,
            queue_capacity=queue_capacity,
            attempt_timeout_ms=attempt_timeout_ms,
            decision_interval_ns=decision_interval_ns,
            prev_success_rate=prev_success_rate,
            prev_retry_ratio=prev_retry_ratio,
            prev_queue_util=prev_queue_util,
            bucket_balance=float(limiter.balance) if isinstance(limiter, GlobalRetryBudget) else 0.0,
            refill_rate=int(limiter.refill_rate) if isinstance(limiter, GlobalRetryBudget) else 1,
            bucket_capacity=int(limiter.max_tokens) if isinstance(limiter, GlobalRetryBudget) else 1,
            current_refill_idx=current_refill_idx,
            current_capacity_idx=current_capacity_idx,
        )
        prev_success_rate = obs_dict["success_rate"]
        prev_retry_ratio = obs_dict["retry_ratio"]
        prev_queue_util = obs_dict["queue_avg"] / queue_capacity

        normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
        action, _ = model.predict(normalized_obs, deterministic=True)

        current_refill_idx = int(action[0])
        current_capacity_idx = int(action[1])
        refill_rate = REFILL_RATE_MAP[current_refill_idx]
        bucket_capacity = BUCKET_CAPACITY_MAP[current_capacity_idx]

        service.update_token_bucket(
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
        )
        limiter = service.cfg.load_limiter

        actions_history.append({
            "time_s": sim.timestep / 1e9,
            "refill_rate": refill_rate,
            "bucket_capacity": bucket_capacity,
        })

        next_time = min(next_time + decision_interval_ns, episode_end)
        sim.run(until=next_time)

    sim.run()
    obs_normalizer.close()
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
                    save_path="eval_comparison.png", show_plot=True):
    """Time-series comparison plot with 4 panels."""

    has_rl_actions = rl_actions_df is not None and not rl_actions_df.empty
    # Hybrid (3-knob) agents emit an extra event_reward column; when present,
    # we add a dedicated 6th panel for it instead of cramming a third axis
    # onto panel 5 (which already twin-axes refill rate and bucket capacity).
    has_event_reward = has_rl_actions and "event_reward" in rl_actions_df.columns
    if has_event_reward:
        n_panels = 6
    elif has_rl_actions:
        n_panels = 5
    else:
        n_panels = 4
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 4 * n_panels), sharex=True)

    styles = {
        "No Budget":     ("#d32f2f", "--", 1.8),
        "Static Budget": ("gray",    "-",  2.0),
        # "Best Static" is the upper bound over the RL's own action grid.
        # Drawn dotted to visually distinguish it from the YAML-static line
        # while keeping it close in hue so the eye groups the two statics.
        "Best Static":   ("#455A64", ":",  2.0),
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

    # -- Panel 5 (optional): RL Agent Actions (refill rate + bucket capacity) --
    if has_rl_actions:
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

    # -- Panel 6 (hybrid only): event-reward knob ----------------------------
    # The hybrid (3-knob) agent also controls how many tokens are added to the
    # retry bucket per successful attempt. Plot it on its own axis so the y-scale
    # (0.0–0.60) doesn't get compressed next to the ~90-rps refill rate.
    if has_event_reward:
        ax_event = axes[5]
        ax_event.step(
            rl_actions_df["time_s"], rl_actions_df["event_reward"],
            where="post", color="#FF9800", linewidth=2, label="Event Reward",
        )
        ax_event.set_ylabel("Event Reward (tokens/success)", color="#FF9800")
        ax_event.set_yticks(EVENT_REWARD_MAP)
        ax_event.set_title("RL Agent Actions — Event-Based Refill")
        ax_event.legend(loc="upper right")

    axes[-1].set_xlabel("Time (s)")

    for name, start, end, color in fault_windows:
        for ax in axes:
            ax.axvspan(start, end, alpha=0.12, color=color)

    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"\nSaved plot to {save_path}")
    if show_plot:
        plt.show()
    else:
        plt.close(fig)


def _json_ready(value):
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    return value


def save_metrics_artifacts(results, rl_actions_df, fault_windows, output_dir: Path) -> None:
    """Save scenario metrics and time series under output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    per_client_rows = []
    details = {
        "fault_windows": _json_ready(fault_windows),
        "variants": [],
    }

    for result in results:
        summary_rows.append({
            "label": result["label"],
            "sr_fault_agg": result["sr_fault_agg"],
            "load_amp": result["load_amp"],
            "retry_eff": result["retry_eff"],
            "avg_recovery": result["avg_recovery"],
            "p50": result["p50"],
            "p95": result["p95"],
            "p99": result["p99"],
        })

        for client_name, sr in result["sr_fault_per_client"].items():
            per_client_rows.append({
                "label": result["label"],
                "client": client_name,
                "sr_fault": sr,
                "retry_share": result["retry_share"].get(client_name, 0.0),
            })

        details["variants"].append({
            "label": result["label"],
            "metrics": _json_ready({
                "sr_fault_agg": result["sr_fault_agg"],
                "sr_fault_per_client": result["sr_fault_per_client"],
                "load_amp": result["load_amp"],
                "retry_eff": result["retry_eff"],
                "retry_share": result["retry_share"],
                "recovery_times": result["recovery_times"],
                "avg_recovery": result["avg_recovery"],
                "p50": result["p50"],
                "p95": result["p95"],
                "p99": result["p99"],
            }),
        })

        ts_path = output_dir / f"{result['label'].lower().replace(' ', '_')}_timeseries.csv"
        result["ts_df"].to_csv(ts_path, index=False)

    pd.DataFrame(summary_rows).to_csv(output_dir / "summary.csv", index=False)
    pd.DataFrame(per_client_rows).to_csv(output_dir / "per_client_metrics.csv", index=False)
    rl_actions_df.to_csv(output_dir / "rl_actions.csv", index=False)
    (output_dir / "details.json").write_text(json.dumps(_json_ready(details), indent=2))


def evaluate_scenario(
    model_path: str,
    yaml_path: str,
    seed: int = 42,
    plot_path: str | None = None,
    artifacts_dir: str | None = None,
    print_table: bool = True,
    show_plot: bool = True,
):
    """Run no-budget/static/RL variants and optionally save outputs."""
    fault_windows = detect_fault_windows(yaml_path)

    print(f"Scenario : {yaml_path}")
    print(f"Model    : {model_path}")
    print(f"Seed     : {seed}")
    print()

    print("[1/3] Running scenario WITHOUT budget …")
    nb_clients = run_no_budget(yaml_path, seed=seed)

    print("[2/3] Running scenario with STATIC budget …")
    st_clients = run_static_budget(yaml_path, seed=seed)

    print("[3/3] Running scenario with RL AGENT …")
    rl_clients, rl_actions = run_rl_agent(yaml_path, model_path, seed=seed)

    results = [
        compute_metrics(nb_clients, fault_windows, "No Budget"),
        compute_metrics(st_clients, fault_windows, "Static Budget"),
        compute_metrics(rl_clients, fault_windows, "RL Agent"),
    ]

    if print_table:
        print_comparison(results, fault_windows)

    if plot_path is not None:
        plot_comparison(
            results,
            fault_windows,
            rl_actions_df=rl_actions,
            save_path=plot_path,
            show_plot=show_plot,
        )

    if artifacts_dir is not None:
        save_metrics_artifacts(results, rl_actions, fault_windows, Path(artifacts_dir))

    return results, rl_actions, fault_windows

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

    evaluate_scenario(
        model_path=args.model,
        yaml_path=args.yaml,
        seed=args.seed,
        plot_path=args.output,
        print_table=True,
        show_plot=True,
    )
