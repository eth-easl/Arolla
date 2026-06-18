#!/usr/bin/env python3
"""Evaluate the relative-action RL agent against static and no-budget baselines."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from eval_rl_scenario import (
    _build_and_drive,
    compute_metrics,
    detect_fault_windows,
    plot_comparison,
    print_comparison,
    run_no_budget,
    run_static_budget,
    save_metrics_artifacts,
)
from simulator.config.loader import ConfigLoader
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.rl.random_scenario_env import (
    build_observation_vector,
    stabilize_window_observation,
    token_bucket_indices_from_physical,
)
from simulator.rl.relative_action_env import RelativeActionRandomScenarioSimEnv, relative_action_to_indices
from simulator.utils.time import s_to_ns


REFILL_RATE_MAP = [5, 15, 30, 60, 90]
BUCKET_CAPACITY_MAP = [5, 10, 20, 50, 80]


def _load_obs_normalizer(model_path: str, yaml_path: str, decision_interval_s: float) -> VecNormalize:
    """Load the saved VecNormalize stats for the relative-action env."""
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(
            f"Missing VecNormalize stats at {vecnorm_path}. "
            "This model was trained with normalized observations, so evaluation "
            "must load the matching stats."
        )

    def make_env():
        return RelativeActionRandomScenarioSimEnv(yaml_path, decision_interval_s=decision_interval_s)

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


def run_rl_agent(yaml_path: str, model_path: str, decision_interval_s: float = 2.0, seed=42):
    """Run the scenario with the relative-action RL agent."""
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
        obs_dict = stabilize_window_observation(
            service.live_buffer.get_observation(sim.timestep, decision_interval_ns),
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

        current_refill_idx, current_capacity_idx = relative_action_to_indices(
            current_refill_idx=current_refill_idx,
            current_capacity_idx=current_capacity_idx,
            action=action,
            refill_choices=len(REFILL_RATE_MAP),
            capacity_choices=len(BUCKET_CAPACITY_MAP),
        )
        refill_rate = REFILL_RATE_MAP[current_refill_idx]
        bucket_capacity = BUCKET_CAPACITY_MAP[current_capacity_idx]

        service.update_token_bucket(refill_rate=refill_rate, bucket_capacity=bucket_capacity)
        limiter = service.cfg.load_limiter
        actions_history.append(
            {
                "time_s": sim.timestep / 1e9,
                "refill_rate": refill_rate,
                "bucket_capacity": bucket_capacity,
            }
        )

        next_time = min(next_time + decision_interval_ns, episode_end)
        sim.run(until=next_time)

    sim.run()
    obs_normalizer.close()
    return clients, pd.DataFrame(actions_history)


def evaluate_scenario(
    model_path: str,
    yaml_path: str,
    seed: int = 42,
    plot_path: str | None = None,
    artifacts_dir: str | None = None,
    print_table: bool = True,
    show_plot: bool = True,
):
    fault_windows = detect_fault_windows(yaml_path)

    print(f"Scenario : {yaml_path}")
    print(f"Model    : {model_path}")
    print(f"Seed     : {seed}")
    print()

    print("[1/3] Running scenario WITHOUT budget …")
    nb_clients = run_no_budget(yaml_path, seed=seed)

    print("[2/3] Running scenario with STATIC budget …")
    st_clients = run_static_budget(yaml_path, seed=seed)

    print("[3/3] Running scenario with RELATIVE RL AGENT …")
    rl_clients, rl_actions = run_rl_agent(yaml_path, model_path, seed=seed)

    results = [
        compute_metrics(nb_clients, fault_windows, "No Budget"),
        compute_metrics(st_clients, fault_windows, "Static Budget"),
        compute_metrics(rl_clients, fault_windows, "RL Agent"),
    ]

    if print_table:
        print_comparison(results, fault_windows)

    if plot_path is not None:
        plot_comparison(results, fault_windows, rl_actions_df=rl_actions, save_path=plot_path, show_plot=show_plot)

    if artifacts_dir is not None:
        save_metrics_artifacts(results, rl_actions, fault_windows, Path(artifacts_dir))

    return results, rl_actions, fault_windows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate relative-action RL agent vs static/no-budget baselines")
    parser.add_argument("--model", required=True, help="Path to saved PPO model (without .zip)")
    parser.add_argument("--yaml", required=True, help="Path to scenario YAML")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="eval_comparison.png", help="Output plot filename")
    args = parser.parse_args()

    evaluate_scenario(
        model_path=args.model,
        yaml_path=args.yaml,
        seed=args.seed,
        plot_path=args.output,
        print_table=True,
        show_plot=True,
    )
