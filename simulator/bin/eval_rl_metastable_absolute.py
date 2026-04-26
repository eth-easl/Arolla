#!/usr/bin/env python3
"""Evaluate the metastable-focused absolute-action RL agent."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from eval_rl_scenario import (
    compute_metrics,
    detect_fault_windows,
    plot_comparison,
    print_comparison,
    run_no_budget,
    run_static_budget,
    save_metrics_artifacts,
)
from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv


def _load_obs_normalizer(model_path: str, yaml_path: str, decision_interval_s: float) -> VecNormalize:
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(f"Missing VecNormalize stats at {vecnorm_path}")

    def make_env():
        return MetastableFairnessSimEnv(
            yaml_path=yaml_path,
            decision_interval_s=decision_interval_s,
            randomize_scenarios=False,
        )

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


def run_rl_agent(yaml_path: str, model_path: str, decision_interval_s: float = 2.0, seed: int = 42):
    env = MetastableFairnessSimEnv(
        yaml_path=yaml_path,
        decision_interval_s=decision_interval_s,
        randomize_scenarios=False,
    )
    obs_normalizer = _load_obs_normalizer(model_path, yaml_path, decision_interval_s)
    model = PPO.load(model_path, env=obs_normalizer)

    obs, _ = env.reset(seed=seed)
    while True:
        normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
        action, _ = model.predict(normalized_obs, deterministic=True)
        obs, _, done, _, _ = env.step(action)
        if done:
            break

    actions_df = pd.DataFrame(
        [
            {
                "time_s": row["time_s"],
                "refill_rate": row["action_refill_rate"],
                "bucket_capacity": row["action_bucket_capacity"],
            }
            for row in env.history
        ]
    )
    clients = env.clients
    env.close()
    obs_normalizer.close()
    return clients, actions_df


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

    print("[3/3] Running scenario with METASTABLE ABSOLUTE RL AGENT …")
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
    parser = argparse.ArgumentParser(description="Evaluate metastable absolute-action RL agent")
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
