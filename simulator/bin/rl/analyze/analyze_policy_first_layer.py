#!/usr/bin/env python3
"""
First-layer weight analysis for a Stable-Baselines3 PPO (MlpPolicy) checkpoint.

Computes mean |weight| per input dimension on the first Linear layer of the
policy (actor) trunk, optionally scaled by VecNormalize per-feature std.

Usage:
  python simulator/bin/rl/analyze/analyze_policy_first_layer.py \\
      --model simulator/trained_models/run_YYYY-MM-DD_HH-MM-SS_500k/model

  # With VecNormalize stats from the same training run (recommended):
  python simulator/bin/rl/analyze/analyze_policy_first_layer.py \\
      --model simulator/trained_models/run_.../model \\
      --vecnorm simulator/trained_models/run_.../vecnormalize_stats.pkl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch.nn as nn
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from simulator.rl.random_scenario_env import RandomScenarioSimEnv

# Must match observation space from simulator.rl.random_scenario_env.RandomScenarioSimEnv
FEATURE_NAMES = RandomScenarioSimEnv.OBSERVATION_FEATURES


def _find_first_linear(module: nn.Module) -> nn.Linear | None:
    """Return the first nn.Linear in a Sequential (depth-first)."""
    if isinstance(module, nn.Linear):
        return module
    if isinstance(module, nn.Sequential):
        for child in module:
            found = _find_first_linear(child)
            if found is not None:
                return found
    return None


def _policy_first_linear(model: PPO) -> nn.Linear:
    policy = model.policy
    net = policy.mlp_extractor.policy_net
    linear = _find_first_linear(net)
    if linear is None:
        raise RuntimeError(
            "Could not find nn.Linear in policy.mlp_extractor.policy_net. "
            "Inspect with: print(model.policy.mlp_extractor.policy_net)"
        )
    return linear


def _load_vecnorm_std(vecnorm_path: Path) -> np.ndarray | None:
    """Return sqrt(var + eps) per obs dim from a saved VecNormalize pickle."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from simulator.rl.random_scenario_env import RandomScenarioSimEnv

    yaml_path = str(
        Path(__file__).resolve().parents[3]
        / "experiments"
        / "yaml"
        / "rl"
        / "token_bucket.yaml"
    )

    def make_env():
        return RandomScenarioSimEnv(yaml_path, decision_interval_s=2.0)

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    # Running variance: SB3 stores in obs_rms
    var = np.asarray(venv.obs_rms.var, dtype=np.float64)
    eps = float(venv.epsilon)
    std = np.sqrt(var + eps)
    venv.close()
    return std


def main() -> None:
    parser = argparse.ArgumentParser(description="PPO first-layer weight importance")
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Path to saved model (without .zip), e.g. .../trained_models/run_.../model",
    )
    parser.add_argument(
        "--vecnorm",
        type=str,
        default=None,
        help="Optional path to vecnormalize_stats.pkl for per-feature std scaling",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="policy_first_layer_importance.png",
        help="Output PNG path",
    )
    args = parser.parse_args()

    model_path = Path(args.model)
    model = PPO.load(str(model_path))

    linear = _policy_first_linear(model)
    W = linear.weight.detach().cpu().numpy()  # (out_features, in_features)

    if W.shape[1] != len(FEATURE_NAMES):
        raise ValueError(
            f"Weight matrix in_features={W.shape[1]} but FEATURE_NAMES has "
            f"{len(FEATURE_NAMES)} entries — update FEATURE_NAMES in this script."
        )

    # Mean absolute weight per input column (each column = one obs dimension)
    importance = np.abs(W).mean(axis=0)

    std = None
    if args.vecnorm:
        std = _load_vecnorm_std(Path(args.vecnorm))
        if std.shape[0] != importance.shape[0]:
            raise ValueError("VecNormalize obs dim does not match policy input dim")

    # Print table
    print("First Linear:", linear)
    print(f"Weight shape: {W.shape} (out_features, in_features)\n")
    print(f"{'Feature':<22} {'mean|W|':>12} ", end="")
    if std is not None:
        print(f"{'std(obs)':>12} {'mean|W|*std':>14}")
    else:
        print()

    order = np.argsort(-importance)
    for i in order:
        name = FEATURE_NAMES[i]
        row = f"{name:<22} {importance[i]:12.6f} "
        if std is not None:
            row += f"{std[i]:12.6f} {(importance[i] * std[i]):14.6f}"
        print(row)

    # Plot
    n = len(FEATURE_NAMES)
    fig_h = max(6.0, 0.35 * n)
    fig, axes = plt.subplots(1, 2 if std is not None else 1, figsize=(14, fig_h))

    if std is None:
        ax = axes if isinstance(axes, plt.Axes) else axes[0]
        idx = np.argsort(importance)
        ax.barh(np.array(FEATURE_NAMES)[idx], importance[idx], color="steelblue")
        ax.set_xlabel("mean |weight| (first Linear, policy trunk)")
        ax.set_title("First-layer importance (raw weights)")
    else:
        ax0, ax1 = axes
        idx = np.argsort(importance)
        ax0.barh(np.array(FEATURE_NAMES)[idx], importance[idx], color="steelblue")
        ax0.set_xlabel("mean |weight|")
        ax0.set_title("Normalized-obs space")

        effective = importance * std
        idx2 = np.argsort(effective)
        ax1.barh(np.array(FEATURE_NAMES)[idx2], effective[idx2], color="coral")
        ax1.set_xlabel("mean |weight| × std(obs)")
        ax1.set_title("Approx. sensitivity to raw obs")

    plt.tight_layout()
    out = Path(args.output)
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\nSaved figure to {out.resolve()}")
    plt.show()


if __name__ == "__main__":
    main()