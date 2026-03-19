#!/usr/bin/env python3
"""Compare RL agent vs default static policy on the same scenario."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd
import matplotlib.pyplot as plt
from stable_baselines3 import PPO
from simulator.rl.env import RetrySimEnv
from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns

yaml_path = str(Path(__file__).parent.parent / "experiments" / "yaml" / "default.yaml")

# ─── Run 1: RL Agent ───
print("Running RL agent episode...")
env = RetrySimEnv(yaml_path, decision_interval_s=2.0)
model = PPO.load("ppo_test_retry_agent", env=env)

obs, _ = env.reset(seed=42)
while True:
    action, _ = model.predict(obs, deterministic=False)
    obs, reward, done, _, info = env.step(action)
    if done:
        break

rl_df = pd.DataFrame(env.history)

# ─── Run 2: Baseline (static default policy, no RL intervention) ───
print("Running baseline episode...")
env2 = RetrySimEnv(yaml_path, decision_interval_s=2.0)
obs, _ = env2.reset(seed=42)  # same seed = same scenario

while True:
    # Always pick the default config: max_attempts=2, delay=100ms, budget=0.10
    # That's index [1, 1, 2] in the action maps
    action = [1, 1, 2]
    obs, reward, done, _, info = env2.step(action)
    if done:
        break

baseline_df = pd.DataFrame(env2.history)

# ─── Plot comparison ───
fig, axes = plt.subplots(6, 1, figsize=(14, 22), sharex=True,
                         gridspec_kw={"height_ratios": [2, 2, 2, 1, 1, 1]})

# Panel 1: Success Rate
axes[0].plot(baseline_df["time_s"], baseline_df["success_rate"], label="Baseline (static)", color="gray", linewidth=2)
axes[0].plot(rl_df["time_s"], rl_df["success_rate"], label="RL Agent", color="green", linewidth=2)
axes[0].axvspan(50, 70, alpha=0.15, color="red", label="Fault Window")
axes[0].axvspan(20, 40, alpha=0.15, color="orange", label="Load Spike")
axes[0].set_ylabel("Success Rate")
axes[0].legend()
axes[0].set_title("Success Rate: RL Agent vs Baseline")
axes[0].set_ylim(0, 1.05)

# Panel 2: Retry Ratio
axes[1].plot(baseline_df["time_s"], baseline_df["retry_ratio"], label="Baseline", color="gray", linewidth=2)
axes[1].plot(rl_df["time_s"], rl_df["retry_ratio"], label="RL Agent", color="green", linewidth=2)
axes[1].axvspan(50, 70, alpha=0.15, color="red")
axes[1].set_ylabel("Retry Ratio")
axes[1].legend()
axes[1].set_title("Retry Amplification")

# Panel 3: P99 Latency
axes[2].plot(baseline_df["time_s"], baseline_df["p99"], label="Baseline", color="gray", linewidth=2)
axes[2].plot(rl_df["time_s"], rl_df["p99"], label="RL Agent", color="green", linewidth=2)
axes[2].axvspan(50, 70, alpha=0.15, color="red")
axes[2].set_ylabel("P99 Latency (ms)")
axes[2].legend()
axes[2].set_title("Tail Latency")

# Panels 4-6: RL Agent Actions (one subplot per parameter)
action_colors = {"max_attempts": "#2196F3", "delay_ms": "#FF9800", "budget_ratio": "#9C27B0"}

# Panel 4: max_attempts
axes[3].step(rl_df["time_s"], rl_df["action_max_attempts"], where="post",
             color=action_colors["max_attempts"], linewidth=2)
axes[3].axhline(y=2, color="gray", linestyle="--", alpha=0.5, label="Baseline = 2")
axes[3].axvspan(50, 70, alpha=0.15, color="red")
axes[3].set_ylabel("max_attempts")
axes[3].set_yticks([1, 2, 3, 4])
axes[3].set_ylim(0.5, 4.5)
axes[3].legend(loc="upper right", fontsize=9)
axes[3].set_title("RL Agent Actions Over Time", fontsize=12, fontweight="bold")

# Panel 5: delay_ms
axes[4].step(rl_df["time_s"], rl_df["action_delay_ms"], where="post",
             color=action_colors["delay_ms"], linewidth=2)
axes[4].axhline(y=100, color="gray", linestyle="--", alpha=0.5, label="Baseline = 100ms")
axes[4].axvspan(50, 70, alpha=0.15, color="red")
axes[4].set_ylabel("delay (ms)")
axes[4].set_yticks([50, 100, 200, 300, 500, 750, 1000])
axes[4].set_ylim(0, 1100)
axes[4].legend(loc="upper right", fontsize=9)

# Panel 6: budget_ratio
axes[5].step(rl_df["time_s"], rl_df["action_budget_ratio"], where="post",
             color=action_colors["budget_ratio"], linewidth=2)
axes[5].axhline(y=0.10, color="gray", linestyle="--", alpha=0.5, label="Baseline = 0.10")
axes[5].axvspan(50, 70, alpha=0.15, color="red")
axes[5].set_ylabel("budget ratio")
axes[5].set_yticks([0.02, 0.05, 0.10, 0.20, 0.50])
axes[5].set_ylim(0, 0.55)
axes[5].legend(loc="upper right", fontsize=9)
axes[5].set_xlabel("Time (s)")

plt.tight_layout()
plt.savefig("rl_vs_baseline.png", dpi=150)
plt.show()
print("Saved to rl_vs_baseline.png")

# Print summary comparison
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
print(f"{'Metric':<25} {'Baseline':>12} {'RL Agent':>12}")
print("-"*60)
for col in ["success_rate", "retry_ratio", "p99"]:
    b = baseline_df[col].mean()
    r = rl_df[col].mean()
    better = "✓" if (r > b and col == "success_rate") or (r < b and col != "success_rate") else ""
    print(f"{col:<25} {b:>12.4f} {r:>12.4f}  {better}")