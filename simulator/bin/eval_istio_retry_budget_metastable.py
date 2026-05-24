#!/usr/bin/env python3
"""Evaluate RL that tunes Istio retryBudget.percent and minRetryConcurrency."""

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
    _build_and_drive,
    compute_metrics,
    detect_fault_windows,
    plot_comparison,
    print_comparison,
    save_metrics_artifacts,
)
from simulator.config.loader import ConfigLoader
from simulator.rl.istio_retry_budget_env import IstioRetryBudgetMetastableEnv


def _load_obs_normalizer(
    model_path: str,
    yaml_path: str,
    decision_interval_s: float,
    observation_window_s: float | None,
    delta_window_s: float | None,
) -> VecNormalize:
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(f"Missing VecNormalize stats at {vecnorm_path}")

    def make_env():
        return IstioRetryBudgetMetastableEnv(
            yaml_path=yaml_path,
            decision_interval_s=decision_interval_s,
            observation_window_s=observation_window_s,
            delta_window_s=delta_window_s,
            randomize_scenarios=False,
        )

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


def run_no_budget(yaml_path: str, seed: int = 42):
    """Run the scenario with all server-side retry budgets removed."""
    config = ConfigLoader.load_from_file(yaml_path)
    stripped = [
        svc.model_copy(update={
            "global_retry_budget": None,
            "aimd_global_retry_budget": None,
            "istio_retry_budget": None,
        })
        for svc in config.services
    ]
    config = config.model_copy(update={"services": stripped, "seed": seed})
    sim, clients, _, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def run_static_budget(yaml_path: str, seed: int = 42):
    """Run the fixed YAML Istio retry budget unchanged for the whole scenario."""
    config = ConfigLoader.load_from_file(yaml_path)
    config = config.model_copy(update={"seed": seed})
    sim, clients, _, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def run_rl_agent(
    yaml_path: str,
    model_path: str,
    decision_interval_s: float = 2.0,
    observation_window_s: float | None = None,
    delta_window_s: float | None = None,
    seed: int = 42,
):
    env = IstioRetryBudgetMetastableEnv(
        yaml_path=yaml_path,
        decision_interval_s=decision_interval_s,
        observation_window_s=observation_window_s,
        delta_window_s=delta_window_s,
        randomize_scenarios=False,
    )
    obs_normalizer = _load_obs_normalizer(
        model_path,
        yaml_path,
        decision_interval_s,
        observation_window_s,
        delta_window_s,
    )
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
                "percent": row["action_percent"],
                "min_retry_concurrency": row["action_min_retry_concurrency"],
                "retry_concurrency_limit": row["retry_concurrency_limit"],
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
    decision_interval_s: float = 2.0,
    observation_window_s: float | None = None,
    delta_window_s: float | None = None,
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

    print("[1/3] Running scenario WITHOUT budget ...")
    nb_clients = run_no_budget(yaml_path, seed=seed)

    print("[2/3] Running scenario with FIXED ISTIO RETRY BUDGET ...")
    st_clients = run_static_budget(yaml_path, seed=seed)

    print("[3/3] Running scenario with ISTIO RETRY-BUDGET RL AGENT ...")
    rl_clients, rl_actions = run_rl_agent(
        yaml_path,
        model_path,
        decision_interval_s=decision_interval_s,
        observation_window_s=observation_window_s,
        delta_window_s=delta_window_s,
        seed=seed,
    )

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
    parser = argparse.ArgumentParser(description="Evaluate Istio retry-budget RL agent")
    parser.add_argument("--model", required=True, help="Path to saved PPO model (without .zip)")
    parser.add_argument("--yaml", required=True, help="Path to scenario YAML")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--decision-interval-s",
        type=float,
        default=2.0,
        help="Seconds between retry-budget decisions/actions.",
    )
    parser.add_argument(
        "--observation-window-s",
        type=float,
        default=None,
        help="Metrics window in seconds for the RL observation/reward. Defaults to the decision interval.",
    )
    parser.add_argument(
        "--delta-window-s",
        type=float,
        default=None,
        help="Metrics window in seconds for delta features. Defaults to the observation metrics window.",
    )
    parser.add_argument("--output", default="istio_retry_budget_eval.png", help="Output plot filename")
    args = parser.parse_args()

    evaluate_scenario(
        model_path=args.model,
        yaml_path=args.yaml,
        seed=args.seed,
        decision_interval_s=args.decision_interval_s,
        observation_window_s=args.observation_window_s,
        delta_window_s=args.delta_window_s,
        plot_path=args.output,
        print_table=True,
        show_plot=True,
    )
