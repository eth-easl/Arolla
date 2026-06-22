#!/usr/bin/env python3
"""Train an absolute-action hybrid (3-knob) RL agent for metastable failures.

The third action dimension controls the event-based refill (tokens added to
the retry bucket per successful attempt), in addition to the existing
time-based refill rate and bucket capacity knobs.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, EvalCallback
from stable_baselines3.common.vec_env import (
    DummyVecEnv,
    SubprocVecEnv,
    VecNormalize,
    sync_envs_normalization,
)

from simulator.rl.hybrid_metastable_env import (
    EVENT_REWARD_MAP,
    HybridMetastableFairnessEnv,
)


YAML_PATH = str(
    Path(__file__).resolve().parents[3]
    / "experiments"
    / "yaml"
    / "rl"
    / "metastable_token_bucket_fairness.yaml"
)
TRAINED_MODELS_DIR = (
    Path(__file__).resolve().parents[3] / "trained_models" / "metastable_hybrid_absolute_action"
)
MODEL_FAMILY = "metastable_hybrid_absolute_action"


def _make_run_dir(total_timesteps: int, scenario_profile: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    steps_label = (
        f"{total_timesteps / 1_000_000:.1f}M"
        if total_timesteps >= 1_000_000
        else f"{total_timesteps // 1_000}k"
    )
    profile_label = scenario_profile.replace("-", "_")
    run_dir = TRAINED_MODELS_DIR / f"run_{profile_label}_{ts}_{steps_label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


class NormSyncCallback(BaseCallback):
    """Sync VecNormalize statistics from training env to eval env after evals."""

    def __init__(self, eval_env):
        super().__init__()
        self.eval_env = eval_env

    def _on_step(self):
        sync_envs_normalization(self.training_env, self.eval_env)
        return True


def make_env(rank: int, scenario_profile: str):
    def _init():
        return HybridMetastableFairnessEnv(
            YAML_PATH,
            decision_interval_s=2.0,
            randomize_scenarios=True,
            scenario_profile=scenario_profile,
        )

    return _init


def write_training_spec(run_dir: Path, total_timesteps: int, scenario_profile: str) -> None:
    spec_path = run_dir / "training_spec.txt"
    model_info_path = run_dir / "model_variant.txt"
    spec_lines = [
        "HybridMetastableFairnessEnv training spec",
        "",
        f"Model family: {MODEL_FAMILY}",
        f"Scenario profile: {scenario_profile}",
        f"YAML template: {YAML_PATH}",
        f"Total timesteps: {total_timesteps:,}",
        "Decision interval: 2.0s",
        "",
        "Observation (22 dims):",
        HybridMetastableFairnessEnv.observation_space_description(),
        "  + event_reward_idx_norm: current event-reward index / 4 (appended)",
        "",
        "Action space:",
        HybridMetastableFairnessEnv.action_space_description(),
        f"Event-reward map (tokens per successful attempt): {EVENT_REWARD_MAP}",
        "",
        "Reward:",
        HybridMetastableFairnessEnv.reward_function_description(),
        "",
    ]
    spec_path.write_text("\n".join(spec_lines))
    model_info_path.write_text(
        "\n".join(
            [
                f"model_family={MODEL_FAMILY}",
                "env_class=HybridMetastableFairnessEnv",
                "action_type=absolute_discrete_selection_three_knobs",
                f"scenario_profile={scenario_profile}",
                "knobs=refill_rate,bucket_capacity,event_reward",
            ]
        )
        + "\n"
    )


def train(
    total_timesteps: int,
    num_workers: int = 4,
    scenario_profile: str = "metastable_fairness",
) -> Path:
    run_dir = _make_run_dir(total_timesteps, scenario_profile)
    model_path = str(run_dir / "model")
    vecnorm_path = str(run_dir / "vecnormalize_stats.pkl")
    best_model_dir = str(run_dir / "best_model")
    eval_log_dir = str(run_dir / "eval_logs")
    tb_log_dir = str(run_dir / "tb_logs")

    print(f"Run directory: {run_dir}")
    print(f"Workers: {num_workers}")
    print(f"Scenario profile: {scenario_profile}")

    if num_workers > 1:
        train_env = SubprocVecEnv([make_env(i, scenario_profile) for i in range(num_workers)])
    else:
        train_env = DummyVecEnv([make_env(0, scenario_profile)])
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    eval_env = DummyVecEnv([make_env(999, scenario_profile)])
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

    print(
        f"Training PPO for {total_timesteps:,} timesteps "
        f"on HybridMetastableFairnessEnv "
        f"(3-knob absolute, profile={scenario_profile})..."
    )
    model.learn(total_timesteps=total_timesteps, callback=eval_callback)

    model.save(model_path)
    train_env.save(vecnorm_path)
    write_training_spec(run_dir, total_timesteps, scenario_profile)

    print(f"\nModel saved to {model_path}.zip")
    print(f"Normalisation stats saved to {vecnorm_path}")
    print(f"Best model saved under {best_model_dir}")

    train_env.close()
    eval_env.close()
    return run_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train hybrid (3-knob) absolute-action metastable RL agent"
    )
    parser.add_argument(
        "--timesteps",
        type=int,
        default=500_000,
        help="Total PPO timesteps (default: 500000)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel environments (default: 4)",
    )
    parser.add_argument(
        "--scenario-profile",
        choices=["metastable_fairness", "switchback_adversarial"],
        default="metastable_fairness",
        help="Training scenario sampler profile (default: metastable_fairness)",
    )
    args = parser.parse_args()

    train(
        total_timesteps=args.timesteps,
        num_workers=args.workers,
        scenario_profile=args.scenario_profile,
    )
