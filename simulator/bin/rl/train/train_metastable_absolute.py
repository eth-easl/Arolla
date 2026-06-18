#!/usr/bin/env python3
"""Train an absolute-action RL agent specialized for metastable failures."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, EvalCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize, sync_envs_normalization

from run_metastable_benchmarks_absolute import run_benchmark_suite
from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv


YAML_PATH = str(
    Path(__file__).resolve().parents[3] / "experiments" / "yaml" / "rl" / "metastable_token_bucket_fairness.yaml"
)
TRAINED_MODELS_DIR = Path(__file__).resolve().parents[3] / "trained_models" / "metastable_absolute_action"
MODEL_FAMILY = "metastable_absolute_action"


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


def make_env(rank: int):
    def _init():
        return MetastableFairnessSimEnv(YAML_PATH, decision_interval_s=2.0, randomize_scenarios=True)

    return _init


def make_eval_vec_env(vecnorm_path: str):
    env = DummyVecEnv([make_env(999)])
    env = VecNormalize.load(vecnorm_path, env)
    env.training = False
    env.norm_reward = False
    return env


def write_training_spec(run_dir: Path) -> None:
    spec_path = run_dir / "training_spec.txt"
    model_info_path = run_dir / "model_variant.txt"
    lines = [
        "MetastableFairnessSimEnv training spec",
        "",
        f"Model family: {MODEL_FAMILY}",
        f"YAML template: {YAML_PATH}",
        "Decision interval: 2.0s",
        "",
        MetastableFairnessSimEnv.observation_space_description(),
        "",
        MetastableFairnessSimEnv.action_space_description(),
        "",
        MetastableFairnessSimEnv.reward_function_description(),
        "",
    ]
    spec_path.write_text("\n".join(lines))
    model_info_path.write_text(
        "\n".join(
            [
                f"model_family={MODEL_FAMILY}",
                "env_class=MetastableFairnessSimEnv",
                "action_type=absolute_discrete_selection",
                "scenario_profile=metastable_fairness",
            ]
        ) + "\n"
    )


def plot_episode(history: list, save_path: str = "metastable_absolute_episode.png"):
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

    axes[4].step(df["time_s"], df["action_refill_rate"], where="post", color="#2196F3", linewidth=2)
    axes[4].set_ylabel("Refill")
    axes[4].set_yticks([5, 15, 30, 60, 90])
    axes[4].set_title("Agent Action: Refill Rate")

    axes[5].step(df["time_s"], df["action_bucket_capacity"], where="post", color="#9C27B0", linewidth=2)
    axes[5].set_ylabel("Capacity")
    axes[5].set_yticks([5, 10, 20, 50, 80])
    axes[5].set_xlabel("Time (s)")
    axes[5].set_title("Agent Action: Bucket Capacity")

    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved episode plot to {save_path}")
    plt.show()


def plot_eval_results(log_dir: str, save_path: str = "eval_reward.png"):
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
    ax.set_title("Metastable Evaluation Reward During Training")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved eval reward plot to {save_path}")
    plt.show()


def train(total_timesteps: int) -> Path:
    run_dir = _make_run_dir(total_timesteps)
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    best_model_dir = str(run_dir / "best_model")
    eval_log_dir = str(run_dir / "eval_logs")
    tb_log_dir = str(run_dir / "tb_logs")

    print(f"Run directory: {run_dir}")

    train_env = SubprocVecEnv([make_env(i) for i in range(4)])
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    eval_env = SubprocVecEnv([make_env(99)])
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

    print(f"Training PPO for {total_timesteps:,} timesteps on metastable fairness scenarios …")
    model.learn(total_timesteps=total_timesteps, callback=eval_callback)

    model.save(model_path)
    train_env.save(vecnorm_path)
    write_training_spec(run_dir)
    print(f"\nModel saved to {model_path}.zip")
    print(f"Normalisation stats saved to {vecnorm_path}")

    train_env.close()
    eval_env.close()
    return run_dir


def evaluate_and_plot(run_dir: Path):
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    eval_log_dir = str(run_dir / "eval_logs")
    plot_dir = run_dir / "plots"
    plot_dir.mkdir(exist_ok=True)

    print(f"\nLoading model from: {run_dir.name}")
    env = MetastableFairnessSimEnv(YAML_PATH, decision_interval_s=2.0, randomize_scenarios=True)
    obs_normalizer = make_eval_vec_env(vecnorm_path)
    model = PPO.load(model_path, env=obs_normalizer)

    print("Running evaluation episode …")
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

    print(f"Episode: {steps} steps, total reward: {total_reward:.2f}")
    history = env.history
    df = pd.DataFrame(history)
    print("\n" + "=" * 50)
    print("EPISODE SUMMARY")
    print("=" * 50)
    print(f"  Mean success rate      : {df['success_rate'].mean():.3f}")
    print(f"  Mean min-client success: {df['window_min_client_success'].mean():.3f}")
    print(f"  Mean load amplification: {df['window_load_amp'].mean():.3f}")
    print(f"  Mean retry efficiency  : {df['window_retry_eff'].mean():.3f}")
    print(f"  Mean fairness gap      : {df['window_fairness_gap'].mean():.3f}")
    print(f"  Mean P99 latency       : {df['p99'].mean():.1f} ms")
    print(f"  Total reward           : {total_reward:.2f}")

    plot_episode(history, save_path=str(plot_dir / "episode.png"))
    plot_eval_results(log_dir=eval_log_dir, save_path=str(plot_dir / "eval_reward.png"))
    env.close()
    obs_normalizer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train metastable-focused absolute-action RL agent")
    parser.add_argument("--timesteps", type=int, default=500_000, help="Total training timesteps")
    parser.add_argument("--skip-training", action="store_true", help="Skip training, only evaluate")
    parser.add_argument("--run-dir", type=str, default=None, help="Path to a specific run directory for evaluation")
    args = parser.parse_args()

    if not args.skip_training:
        run_dir = train(args.timesteps)
    elif args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        run_dir = _latest_run_dir()
        print(f"Using latest run: {run_dir.name}")

    evaluate_and_plot(run_dir)
    benchmark_dir = run_benchmark_suite(str(run_dir / "model"))
    print(f"Saved metastable benchmark suite to: {benchmark_dir}")
