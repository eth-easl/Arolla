#!/usr/bin/env python3
"""Quick smoke test: verify RandomScenarioSimEnv works and train briefly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env
from simulator.rl.random_scenario_env import RandomScenarioSimEnv

YAML_PATH = str(
    Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "token_bucket.yaml"
)

# Validate the env against Gymnasium spec
print("Checking environment …")
env = RandomScenarioSimEnv(YAML_PATH, decision_interval_s=2.0)
check_env(env)
print("Environment OK!\n")

# Quick sanity: reset twice to confirm randomisation
print("Verifying scenarios differ across resets …")
obs1, _ = env.reset()
obs2, _ = env.reset()
assert not (obs1 == obs2).all(), "Two resets produced identical observations!"
print("Confirmed: observations differ between episodes.\n")

# Short training run
print("Training PPO for 10 000 timesteps (quick test) …")
model = PPO(
    "MlpPolicy",
    env,
    verbose=1,
    tensorboard_log="./tb_logs/",
    n_steps=128,
    batch_size=64,
    n_epochs=4,
    learning_rate=3e-4,
)
model.learn(total_timesteps=10_000)
model.save("ppo_random_test")
print("\nTraining done! Model saved to ppo_random_test.zip")

# Run one episode with the trained agent
print("\nRunning one episode with trained agent …")
obs, _ = env.reset()
total_reward = 0.0
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
