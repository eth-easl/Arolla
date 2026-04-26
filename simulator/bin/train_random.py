#!/usr/bin/env python3
"""
Full RL training pipeline with randomised scenarios.

Each training run creates a timestamped folder under simulator/trained_models/
containing the model weights, normalisation stats, eval logs, and plots.

Usage:
    python simulator/bin/train_random.py                        # train 500k steps
    python simulator/bin/train_random.py --timesteps 1000000    # train 1M steps
    python simulator/bin/train_random.py --skip-training        # evaluate latest run
    python simulator/bin/train_random.py --skip-training --run-dir simulator/trained_models/run_2026-03-28_14-30-00_500k
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import EvalCallback, BaseCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize, sync_envs_normalization

from run_benchmarks import run_benchmark_suite
from simulator.rl.random_scenario_env import RandomScenarioSimEnv


YAML_PATH = str(
    Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "token_bucket.yaml"
)

# All training artifacts live under this folder (git-ignored)
TRAINED_MODELS_DIR = Path(__file__).parent.parent / "trained_models"


def _make_run_dir(total_timesteps: int) -> Path:
    """Create a unique run directory like trained_models/run_2026-03-28_14-30-00_500k/"""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if total_timesteps >= 1_000_000:
        steps_label = f"{total_timesteps / 1_000_000:.1f}M"
    else:
        steps_label = f"{total_timesteps // 1_000}k"

    run_dir = TRAINED_MODELS_DIR / f"run_{ts}_{steps_label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _latest_run_dir() -> Path:
    """Return the most recent run directory (by name sort)."""
    if not TRAINED_MODELS_DIR.exists():
        raise FileNotFoundError(f"No training runs found in {TRAINED_MODELS_DIR}")
    runs = sorted(TRAINED_MODELS_DIR.glob("run_*"))
    if not runs:
        raise FileNotFoundError(f"No training runs found in {TRAINED_MODELS_DIR}")
    return runs[-1]


# Helpers
class NormSyncCallback(BaseCallback):
    """Sync VecNormalize stats from training env to eval env before each eval."""
    def __init__(self, eval_env):
        super().__init__()
        self.eval_env = eval_env
    def _on_step(self):
        sync_envs_normalization(self.training_env, self.eval_env)
        return True

def make_env(rank: int):
    """Factory: each subprocess gets its own env instance."""
    def _init():
        return RandomScenarioSimEnv(YAML_PATH, decision_interval_s=2.0)
    return _init


def make_eval_vec_env(vecnorm_path: str):
    """Load the saved normalization stats for deterministic evaluation."""
    env = DummyVecEnv([make_env(999)])
    env = VecNormalize.load(vecnorm_path, env)
    env.training = False
    env.norm_reward = False
    return env


def write_training_spec(run_dir: Path) -> None:
    """Save the action/observation/reward setup used for this training run."""
    spec_path = run_dir / "training_spec.txt"
    lines = [
        "RandomScenarioSimEnv training spec",
        "",
        f"YAML template: {YAML_PATH}",
        f"Decision interval: 2.0s",
        "",
        RandomScenarioSimEnv.observation_space_description(),
        "",
        RandomScenarioSimEnv.action_space_description(),
        "",
        RandomScenarioSimEnv.reward_function_description(),
        "",
    ]
    spec_path.write_text("\n".join(lines))


def plot_episode(history: list, save_path: str = "random_agent_episode.png"):
    """Plot metrics and actions from one episode of the trained agent."""
    df = pd.DataFrame(history)

    fig, axes = plt.subplots(5, 1, figsize=(14, 18), sharex=True)

    # Panel 1: Success & Error rates
    axes[0].plot(df["time_s"], df["success_rate"], label="Success Rate", color="green", linewidth=2)
    axes[0].plot(df["time_s"], df["error_rate"], label="Error Rate", color="red", linewidth=1.5, alpha=0.7)
    axes[0].set_ylabel("Rate")
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].legend(loc="lower left")
    axes[0].set_title("Success & Error Rate Over Time")

    # Panel 2: Retry Ratio
    axes[1].plot(df["time_s"], df["retry_ratio"], color="orange", linewidth=2)
    axes[1].set_ylabel("Retry Ratio")
    axes[1].set_title("Retry Amplification")

    # Panel 3: Tail Latency
    axes[2].plot(df["time_s"], df["p50"], label="P50", color="steelblue", linewidth=1.5)
    axes[2].plot(df["time_s"], df["p99"], label="P99", color="navy", linewidth=2)
    axes[2].set_ylabel("Latency (ns)")
    axes[2].legend()
    axes[2].set_title("Latency")

    # Panel 4: Agent action – refill rate
    axes[3].step(df["time_s"], df["action_refill_rate"], where="post", color="#2196F3", linewidth=2)
    axes[3].set_ylabel("Refill Rate (rps)")
    axes[3].set_yticks([5, 15, 30, 60, 90])
    axes[3].set_title("Agent Action: Token Bucket Refill Rate")

    # Panel 5: Agent action – bucket capacity
    axes[4].step(df["time_s"], df["action_bucket_capacity"], where="post", color="#9C27B0", linewidth=2)
    axes[4].set_ylabel("Bucket Capacity")
    axes[4].set_yticks([5, 10, 20, 50, 80])
    axes[4].set_xlabel("Time (s)")
    axes[4].set_title("Agent Action: Token Bucket Capacity")

    # Shade fault/failure regions based on queue-full or server-fail spikes
    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved episode plot to {save_path}")
    plt.show()


def plot_eval_results(log_dir: str, save_path: str = "eval_reward.png"):
    """Plot evaluation reward over training from EvalCallback logs."""
    eval_file = Path(log_dir) / "evaluations.npz"
    if not eval_file.exists():
        print(f"No eval log found at {eval_file}, skipping eval plot.")
        return

    data = np.load(str(eval_file))
    timesteps = data["timesteps"]
    # results shape: (n_evals, n_eval_episodes)
    mean_reward = data["results"].mean(axis=1)
    std_reward = data["results"].std(axis=1)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(timesteps, mean_reward, color="green", linewidth=2, label="Mean eval reward")
    ax.fill_between(
        timesteps,
        mean_reward - std_reward,
        mean_reward + std_reward,
        alpha=0.2, color="green",
    )
    ax.set_xlabel("Training Timesteps")
    ax.set_ylabel("Mean Episode Reward")
    ax.set_title("Evaluation Reward During Training")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved eval reward plot to {save_path}")
    plt.show()

# Training
def train(total_timesteps: int) -> Path:
    """Run full PPO training with parallel envs and normalisation.

    Returns the run directory where all artifacts are saved.
    """
    run_dir = _make_run_dir(total_timesteps)
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    best_model_dir = str(run_dir / "best_model")
    eval_log_dir = str(run_dir / "eval_logs")
    tb_log_dir = str(run_dir / "tb_logs")

    print(f"Run directory: {run_dir}")

    # 4 parallel training envs
    train_env = SubprocVecEnv([make_env(i) for i in range(4)])
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    # 1 separate eval env
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

    print(f"Training PPO for {total_timesteps:,} timesteps with randomised scenarios …")
    model.learn(total_timesteps=total_timesteps, callback=eval_callback)

    model.save(model_path)
    train_env.save(vecnorm_path)
    write_training_spec(run_dir)
    print(f"\nModel saved to {model_path}.zip")
    print(f"Normalisation stats saved to {vecnorm_path}")

    train_env.close()
    eval_env.close()

    return run_dir


# Evaluation / Plotting
def evaluate_and_plot(run_dir: Path):
    """Load trained model from a run directory, run one episode, and plot results."""

    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    eval_log_dir = str(run_dir / "eval_logs")
    plot_dir = run_dir / "plots"
    plot_dir.mkdir(exist_ok=True)

    print(f"\nLoading model from: {run_dir.name}")

    # Step a raw env so episode history is preserved, and use VecNormalize
    # only to transform observations before model.predict().
    env = RandomScenarioSimEnv(YAML_PATH, decision_interval_s=2.0)
    obs_normalizer = make_eval_vec_env(vecnorm_path)
    model = PPO.load(model_path, env=obs_normalizer)

    print("Running evaluation episode …")
    obs, _ = env.reset(seed=12345)
    total_reward = 0.0
    steps = 0

    while True:
        normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
        action, _ = model.predict(normalized_obs, deterministic=True)
        obs, reward, done, _, info = env.step(action)
        total_reward += float(reward)
        steps += 1
        if done:
            break

    print(f"Episode: {steps} steps, total reward: {total_reward:.2f}")

    # Summary table
    history = env.history
    df = pd.DataFrame(history)
    print("\n" + "=" * 50)
    print("EPISODE SUMMARY")
    print("=" * 50)
    print(f"  Mean success rate : {df['success_rate'].mean():.3f}")
    print(f"  Mean error rate   : {df['error_rate'].mean():.3f}")
    print(f"  Mean retry ratio  : {df['retry_ratio'].mean():.3f}")
    print(f"  Mean P99 latency  : {df['p99'].mean():.1f} ns")
    print(f"  Total reward      : {total_reward:.2f}")

    plot_episode(history, save_path=str(plot_dir / "episode.png"))
    plot_eval_results(log_dir=eval_log_dir, save_path=str(plot_dir / "eval_reward.png"))
    env.close()
    obs_normalizer.close()

# CLI
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train RL agent with randomised scenarios")
    parser.add_argument("--timesteps", type=int, default=500_000, help="Total training timesteps")
    parser.add_argument("--skip-training", action="store_true", help="Skip training, only evaluate")
    parser.add_argument("--run-dir", type=str, default=None,
                        help="Path to a specific run directory for evaluation (default: latest)")
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
    print(f"Saved benchmark suite to: {benchmark_dir}")
