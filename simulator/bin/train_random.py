#!/usr/bin/env python3
"""
Full RL training pipeline with randomised scenarios.

Usage:
    python simulator/bin/train_random.py                  # train 500k steps
    python simulator/bin/train_random.py --timesteps 100000  # custom length
    python simulator/bin/train_random.py --skip-training   # plot only (needs saved model)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import EvalCallback
from stable_baselines3.common.vec_env import SubprocVecEnv, VecNormalize

from simulator.rl.random_scenario_env import RandomScenarioSimEnv

YAML_PATH = str(
    Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "token_bucket.yaml"
)

MODEL_PATH = "ppo_random_agent"
VECNORM_PATH = "vecnormalize_stats.pkl"
BEST_MODEL_DIR = "./best_model/"
EVAL_LOG_DIR = "./eval_logs/"


# ========================================================================
# Helpers
# ========================================================================

def make_env(rank: int):
    """Factory: each subprocess gets its own env instance."""
    def _init():
        return RandomScenarioSimEnv(YAML_PATH, decision_interval_s=2.0)
    return _init


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
    axes[3].set_yticks([5, 15, 30, 60, 120])
    axes[3].set_title("Agent Action: Token Bucket Refill Rate")

    # Panel 5: Agent action – bucket capacity
    axes[4].step(df["time_s"], df["action_bucket_capacity"], where="post", color="#9C27B0", linewidth=2)
    axes[4].set_ylabel("Bucket Capacity")
    axes[4].set_yticks([5, 10, 20, 50, 100])
    axes[4].set_xlabel("Time (s)")
    axes[4].set_title("Agent Action: Token Bucket Capacity")

    # Shade fault/failure regions based on queue-full or server-fail spikes
    for ax in axes:
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved episode plot to {save_path}")
    plt.show()


def plot_eval_results(log_dir: str = EVAL_LOG_DIR, save_path: str = "eval_reward.png"):
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


# ========================================================================
# Training
# ========================================================================

def train(total_timesteps: int):
    """Run full PPO training with parallel envs and normalisation."""

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
        tensorboard_log="./tb_logs/",
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
        best_model_save_path=BEST_MODEL_DIR,
        log_path=EVAL_LOG_DIR,
    )

    print(f"Training PPO for {total_timesteps:,} timesteps with randomised scenarios …")
    model.learn(total_timesteps=total_timesteps, callback=eval_callback)

    model.save(MODEL_PATH)
    train_env.save(VECNORM_PATH)
    print(f"\nModel saved to {MODEL_PATH}.zip")
    print(f"Normalisation stats saved to {VECNORM_PATH}")

    train_env.close()
    eval_env.close()


# ========================================================================
# Evaluation / Plotting
# ========================================================================

def evaluate_and_plot():
    """Load trained model, run one episode, and plot results."""

    # Single (non-vectorised) env for easy history access
    env = RandomScenarioSimEnv(YAML_PATH, decision_interval_s=2.0)
    model = PPO.load(MODEL_PATH, env=env)

    print("\nRunning evaluation episode …")
    obs, _ = env.reset(seed=12345)
    total_reward = 0.0
    steps = 0

    while True:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, _, info = env.step(action)
        total_reward += reward
        steps += 1
        if done:
            break

    print(f"Episode: {steps} steps, total reward: {total_reward:.2f}")

    # Summary table
    df = pd.DataFrame(env.history)
    print("\n" + "=" * 50)
    print("EPISODE SUMMARY")
    print("=" * 50)
    print(f"  Mean success rate : {df['success_rate'].mean():.3f}")
    print(f"  Mean error rate   : {df['error_rate'].mean():.3f}")
    print(f"  Mean retry ratio  : {df['retry_ratio'].mean():.3f}")
    print(f"  Mean P99 latency  : {df['p99'].mean():.1f} ns")
    print(f"  Total reward      : {total_reward:.2f}")

    plot_episode(env.history)
    plot_eval_results()


# ========================================================================
# CLI
# ========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train RL agent with randomised scenarios")
    parser.add_argument("--timesteps", type=int, default=500_000, help="Total training timesteps")
    parser.add_argument("--skip-training", action="store_true", help="Skip training, only plot")
    args = parser.parse_args()

    if not args.skip_training:
        train(args.timesteps)

    evaluate_and_plot()
