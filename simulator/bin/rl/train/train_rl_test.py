#!/usr/bin/env python3
"""Quick RL training test using PPO."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env
from simulator.rl.env import RetrySimEnv

yaml_path = str(Path(__file__).resolve().parents[3] / "experiments" / "yaml" / "rl" / "token_bucket.yaml")

# Keep smoke-test artifacts out of the current directory: everything lands in a
# single git-ignored folder under trained_models/.
SMOKE_DIR = Path(__file__).resolve().parents[3] / "trained_models" / "_smoke"
SMOKE_DIR.mkdir(parents=True, exist_ok=True)

# First: verify the env is valid
print("Checking environment...")
env = RetrySimEnv(yaml_path, decision_interval_s=2.0)
check_env(env)
print("Environment OK!\n")

# Train
print("Training PPO for 10,000 timesteps (quick test)...")
model = PPO(
    "MlpPolicy",
    env,
    verbose=1,
    tensorboard_log=str(SMOKE_DIR / "tb_logs"),
    n_steps=128,         # collect 128 steps before each update
    batch_size=64,
    n_epochs=4,
    learning_rate=3e-4,
)
model.learn(total_timesteps=10_000)
model.save(str(SMOKE_DIR / "ppo_test_retry_agent"))
print(f"\nTraining done! Model saved to {SMOKE_DIR / 'ppo_test_retry_agent'}.zip")

# Test the trained agent
print("\nRunning one episode with trained agent...")
obs, _ = env.reset()
total_reward = 0
steps = 0

while True:
    action, _ = model.predict(obs, deterministic=True)
    obs, reward, done, truncated, info = env.step(action)
    total_reward += reward
    steps += 1
    if done:
        break

    

print(f"Episode: {steps} steps, total reward: {total_reward:.2f}")
print(f"Final metrics: {info['metrics']}")