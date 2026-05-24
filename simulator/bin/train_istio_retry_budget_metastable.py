#!/usr/bin/env python3
"""Train PPO to tune Istio retryBudget.percent and minRetryConcurrency."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, EvalCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize, sync_envs_normalization

from simulator.rl.istio_retry_budget_env import IstioRetryBudgetMetastableEnv


YAML_PATH = str(
    Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "istio_retry_budget_metastable.yaml"
)
TRAINED_MODELS_DIR = Path(__file__).parent.parent / "trained_models" / "istio_retry_budget_metastable"
MODEL_FAMILY = "istio_retry_budget_metastable"


def _make_run_dir(total_timesteps: int) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    steps_label = f"{total_timesteps / 1_000_000:.1f}M" if total_timesteps >= 1_000_000 else f"{total_timesteps // 1_000}k"
    run_dir = TRAINED_MODELS_DIR / f"run_{ts}_{steps_label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _latest_run_dir() -> Path:
    if not TRAINED_MODELS_DIR.exists():
        raise FileNotFoundError(f"No training runs found in {TRAINED_MODELS_DIR}")
    runs = sorted(TRAINED_MODELS_DIR.glob("run_*"))
    if not runs:
        raise FileNotFoundError(f"No training runs found in {TRAINED_MODELS_DIR}")
    return runs[-1]


class NormSyncCallback(BaseCallback):
    def __init__(self, eval_env):
        super().__init__()
        self.eval_env = eval_env

    def _on_step(self):
        sync_envs_normalization(self.training_env, self.eval_env)
        return True


def make_env(
    rank: int,
    scenario_profile: str,
    decision_interval_s: float,
    observation_window_s: float | None,
    delta_window_s: float | None,
):
    def _init():
        return IstioRetryBudgetMetastableEnv(
            YAML_PATH,
            decision_interval_s=decision_interval_s,
            observation_window_s=observation_window_s,
            delta_window_s=delta_window_s,
            randomize_scenarios=True,
            scenario_profile=scenario_profile,
        )

    return _init


def write_training_spec(
    run_dir: Path,
    scenario_profile: str,
    decision_interval_s: float,
    observation_window_s: float | None,
    delta_window_s: float | None,
) -> None:
    metrics_window_s = observation_window_s if observation_window_s is not None else decision_interval_s
    effective_delta_window_s = delta_window_s if delta_window_s is not None else metrics_window_s
    (run_dir / "training_spec.txt").write_text(
        "\n".join(
            [
                "IstioRetryBudgetMetastableEnv training spec",
                "",
                f"Model family: {MODEL_FAMILY}",
                f"YAML template: {YAML_PATH}",
                f"Scenario profile: {scenario_profile}",
                f"Decision interval: {decision_interval_s:.1f}s",
                f"Observation metrics window: {metrics_window_s:.1f}s",
                f"Delta metrics window: {effective_delta_window_s:.1f}s",
                "",
                IstioRetryBudgetMetastableEnv.observation_space_description(),
                "",
                IstioRetryBudgetMetastableEnv.action_space_description(),
                "",
                IstioRetryBudgetMetastableEnv.reward_function_description(),
                "",
            ]
        )
    )
    (run_dir / "model_variant.txt").write_text(
        "\n".join(
            [
                f"model_family={MODEL_FAMILY}",
                "env_class=IstioRetryBudgetMetastableEnv",
                "action_type=absolute_discrete_selection",
                f"scenario_profile={scenario_profile}",
                "server_side_budget=istio_retry_budget",
                "knobs=percent,minRetryConcurrency",
                f"decision_interval_s={decision_interval_s:.1f}",
                f"observation_window_s={metrics_window_s:.1f}",
                f"delta_window_s={effective_delta_window_s:.1f}",
            ]
        )
        + "\n"
    )


def plot_episode(history: list, save_path: str):
    df = pd.DataFrame(history)
    fig, axes = plt.subplots(6, 1, figsize=(14, 20), sharex=True)

    axes[0].plot(df["time_s"], df["success_rate"], color="green", linewidth=2, label="Service success")
    axes[0].plot(df["time_s"], df["window_min_client_success"], color="darkgreen", linewidth=1.5, label="Min client success")
    axes[0].set_ylabel("Success")
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_title("Success During Metastable Stress")
    axes[0].legend(loc="lower left")

    axes[1].plot(df["time_s"], df["window_load_amp"], color="orange", linewidth=2, label="Load amp")
    axes[1].plot(df["time_s"], df["window_retry_eff"], color="purple", linewidth=1.5, label="Retry eff")
    axes[1].set_ylabel("Window Metric")
    axes[1].set_title("Amplification and Retry Efficiency")
    axes[1].legend()

    axes[2].plot(df["time_s"], df["window_fairness_gap"], color="red", linewidth=2)
    axes[2].set_ylabel("Gap")
    axes[2].set_title("Retry Share Fairness Gap")

    axes[3].plot(df["time_s"], df["p95"], color="steelblue", linewidth=1.5, label="P95")
    axes[3].plot(df["time_s"], df["p99"], color="navy", linewidth=2, label="P99")
    axes[3].set_ylabel("Latency (ms)")
    axes[3].set_title("Tail Latency")
    axes[3].legend()

    axes[4].step(df["time_s"], df["action_percent"], where="post", color="#2196F3", linewidth=2)
    axes[4].set_ylabel("Percent")
    axes[4].set_yticks([5, 10, 20, 30, 50])
    axes[4].set_title("Agent Action: retryBudget.percent")

    axes[5].step(df["time_s"], df["action_min_retry_concurrency"], where="post", color="#9C27B0", linewidth=2)
    axes[5].set_ylabel("Min Retry")
    axes[5].set_yticks([1, 2, 3, 5, 8])
    axes[5].set_xlabel("Time (s)")
    axes[5].set_title("Agent Action: minRetryConcurrency")

    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved episode plot to {save_path}")
    plt.close(fig)


def plot_eval_results(log_dir: str, save_path: str):
    eval_file = Path(log_dir) / "evaluations.npz"
    if not eval_file.exists():
        print(f"No eval log found at {eval_file}, skipping eval plot.")
        return

    data = np.load(str(eval_file))
    timesteps = data["timesteps"]
    mean_reward = data["results"].mean(axis=1)
    std_reward = data["results"].std(axis=1)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(timesteps, mean_reward, color="green", linewidth=2, label="Mean eval reward")
    ax.fill_between(timesteps, mean_reward - std_reward, mean_reward + std_reward, alpha=0.2, color="green")
    ax.set_xlabel("Training Timesteps")
    ax.set_ylabel("Mean Episode Reward")
    ax.set_title("Istio Retry-Budget Evaluation Reward During Training")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved eval reward plot to {save_path}")
    plt.close(fig)


def train(
    total_timesteps: int,
    scenario_profile: str,
    decision_interval_s: float,
    observation_window_s: float | None,
    delta_window_s: float | None,
) -> Path:
    run_dir = _make_run_dir(total_timesteps)
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    best_model_dir = str(run_dir / "best_model")
    eval_log_dir = str(run_dir / "eval_logs")
    tb_log_dir = str(run_dir / "tb_logs")

    print(f"Run directory: {run_dir}")

    train_env = SubprocVecEnv([
        make_env(i, scenario_profile, decision_interval_s, observation_window_s, delta_window_s)
        for i in range(4)
    ])
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    eval_env = DummyVecEnv([
        make_env(99, scenario_profile, decision_interval_s, observation_window_s, delta_window_s)
    ])
    eval_env = VecNormalize(eval_env, norm_obs=True, norm_reward=True, training=False)

    model = PPO(
        "MlpPolicy",
        train_env,
        verbose=1,
        tensorboard_log=tb_log_dir,
        n_steps=512,
        batch_size=128,
        n_epochs=10,
        learning_rate=3e-4,
        ent_coef=0.01,
        gamma=0.99,
    )

    eval_callback = EvalCallback(
        eval_env,
        eval_freq=5000,
        n_eval_episodes=10,
        best_model_save_path=best_model_dir,
        log_path=eval_log_dir,
        callback_after_eval=NormSyncCallback(eval_env),
    )

    print(f"Training PPO for {total_timesteps:,} timesteps on Istio retry-budget scenarios ...")
    model.learn(total_timesteps=total_timesteps, callback=eval_callback)

    model.save(model_path)
    train_env.save(vecnorm_path)
    write_training_spec(
        run_dir,
        scenario_profile,
        decision_interval_s,
        observation_window_s,
        delta_window_s,
    )
    print(f"\nModel saved to {model_path}.zip")
    print(f"Normalisation stats saved to {vecnorm_path}")

    train_env.close()
    eval_env.close()
    return run_dir


def evaluate_and_plot(
    run_dir: Path,
    decision_interval_s: float,
    observation_window_s: float | None,
    delta_window_s: float | None,
):
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    eval_log_dir = str(run_dir / "eval_logs")
    plot_dir = run_dir / "plots"
    plot_dir.mkdir(exist_ok=True)

    env = IstioRetryBudgetMetastableEnv(
        YAML_PATH,
        decision_interval_s=decision_interval_s,
        observation_window_s=observation_window_s,
        delta_window_s=delta_window_s,
        randomize_scenarios=True,
    )
    obs_normalizer = DummyVecEnv([
        lambda: IstioRetryBudgetMetastableEnv(
            YAML_PATH,
            decision_interval_s=decision_interval_s,
            observation_window_s=observation_window_s,
            delta_window_s=delta_window_s,
        )
    ])
    obs_normalizer = VecNormalize.load(vecnorm_path, obs_normalizer)
    obs_normalizer.training = False
    obs_normalizer.norm_reward = False
    model = PPO.load(model_path, env=obs_normalizer)

    obs, _ = env.reset(seed=12345)
    total_reward = 0.0
    steps = 0
    while True:
        normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
        action, _ = model.predict(normalized_obs, deterministic=True)
        obs, reward, done, _, _ = env.step(action)
        total_reward += float(reward)
        steps += 1
        if done:
            break

    df = pd.DataFrame(env.history)
    print("\n" + "=" * 50)
    print("EPISODE SUMMARY")
    print("=" * 50)
    print(f"  Steps                  : {steps}")
    print(f"  Mean success rate      : {df['success_rate'].mean():.3f}")
    print(f"  Mean min-client success: {df['window_min_client_success'].mean():.3f}")
    print(f"  Mean load amplification: {df['window_load_amp'].mean():.3f}")
    print(f"  Mean retry efficiency  : {df['window_retry_eff'].mean():.3f}")
    print(f"  Mean P99 latency       : {df['p99'].mean():.1f} ms")
    print(f"  Total reward           : {total_reward:.2f}")

    plot_episode(env.history, save_path=str(plot_dir / "episode.png"))
    plot_eval_results(eval_log_dir, save_path=str(plot_dir / "eval_reward.png"))
    env.close()
    obs_normalizer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Istio retry-budget RL agent")
    parser.add_argument("--timesteps", type=int, default=500_000, help="Total PPO timesteps")
    parser.add_argument(
        "--scenario-profile",
        choices=IstioRetryBudgetMetastableEnv.SUPPORTED_SCENARIO_PROFILES,
        default="metastable_fairness",
        help="Randomized training profile",
    )
    parser.add_argument("--skip-training", action="store_true", help="Skip training, only evaluate")
    parser.add_argument("--run-dir", type=str, default=None, help="Path to a specific run directory for evaluation")
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
        help="Metrics window in seconds for observations/reward. Defaults to the decision interval.",
    )
    parser.add_argument(
        "--delta-window-s",
        type=float,
        default=None,
        help="Metrics window in seconds for delta features. Defaults to the observation metrics window.",
    )
    args = parser.parse_args()

    if args.skip_training:
        run_dir = Path(args.run_dir) if args.run_dir else _latest_run_dir()
    else:
        run_dir = train(
            args.timesteps,
            args.scenario_profile,
            args.decision_interval_s,
            args.observation_window_s,
            args.delta_window_s,
        )

    evaluate_and_plot(
        run_dir,
        args.decision_interval_s,
        args.observation_window_s,
        args.delta_window_s,
    )
