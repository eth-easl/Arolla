#!/usr/bin/env python3
"""
Correlation and mutual information from RL rollout data.

Collects (observation_before_step, reward, action) over many episodes, then
computes:
  - Pearson & Spearman correlation of each feature with reward
  - Pairwise Pearson correlation matrix of all features (+ reward)
  - Mutual information (MI) between each feature and reward (continuous)

Usage:
  python simulator/bin/analyze_rollout_correlation.py --episodes 50

  python simulator/bin/analyze_rollout_correlation.py \\
      --model simulator/trained_models/run_.../model \\
      --episodes 30 --output-dir ./rollout_analysis
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from simulator.rl.random_scenario_env import RandomScenarioSimEnv

# Must match RandomScenarioSimEnv._get_obs() order
FEATURE_NAMES = RandomScenarioSimEnv.OBSERVATION_FEATURES

YAML_PATH = str(
    Path(__file__).parent.parent / "experiments" / "yaml" / "rl" / "token_bucket.yaml"
)


def _load_obs_normalizer(model_path: str, decision_interval_s: float) -> VecNormalize:
    """Load saved VecNormalize stats so rollout analysis uses training-time scaling."""
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(
            f"Missing VecNormalize stats at {vecnorm_path}. "
            "Rollout analysis for trained models must use the matching normalization."
        )

    def make_env():
        return RandomScenarioSimEnv(YAML_PATH, decision_interval_s=decision_interval_s)

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


def _spearman_corr_with_reward(df: pd.DataFrame, feature_cols: list[str], reward_col: str) -> pd.Series:
    """
    Spearman correlation without SciPy: Pearson correlation of rank-transformed columns.
    (pandas corr(method='spearman') imports scipy.stats.spearmanr.)
    """
    y_rank = df[reward_col].rank(method="average")
    return pd.Series(
        {col: df[col].rank(method="average").corr(y_rank, method="pearson") for col in feature_cols},
        dtype=np.float64,
    )


def _mutual_info_sklearn(X: np.ndarray, y: np.ndarray, random_state: int = 0) -> np.ndarray:
    """Per-feature MI with y; shape (n_features,)."""
    try:
        from sklearn.feature_selection import mutual_info_regression
    except ImportError as e:
        raise ImportError(
            "mutual_info_regression requires scikit-learn. "
            "Install with: pip install scikit-learn"
        ) from e
    mi = mutual_info_regression(
        X, y.ravel(), random_state=random_state, n_neighbors=3
    )
    return mi


def collect_rollouts(
    n_episodes: int,
    seed: int,
    model_path: str | None,
    deterministic: bool,
    decision_interval_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns
    -------
    obs_before : (n_steps, n_features) — state when the action was chosen
    rewards    : (n_steps,)
    actions    : (n_steps, 2) — discrete indices [refill_idx, cap_idx]
    """
    env = RandomScenarioSimEnv(YAML_PATH, decision_interval_s=decision_interval_s)
    model = None
    obs_normalizer = None
    if model_path:
        model = PPO.load(model_path, env=env)
        obs_normalizer = _load_obs_normalizer(model_path, decision_interval_s)

    obs_list: list[np.ndarray] = []
    reward_list: list[float] = []
    action_list: list[np.ndarray] = []

    for ep in range(n_episodes):
        obs, _ = env.reset(seed=seed + ep)
        done = False
        while not done:
            obs_before = obs.astype(np.float64).copy()
            if model is None:
                action = env.action_space.sample()
            else:
                normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
                action, _ = model.predict(normalized_obs, deterministic=deterministic)
            obs, reward, done, _, _ = env.step(action)
            obs_list.append(obs_before)
            reward_list.append(float(reward))
            action_list.append(np.asarray(action, dtype=np.int64).copy())

    if obs_normalizer is not None:
        obs_normalizer.close()

    return (
        np.stack(obs_list, axis=0),
        np.asarray(reward_list, dtype=np.float64),
        np.stack(action_list, axis=0),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlation / MI on RL rollout data (RandomScenarioSimEnv)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Path to PPO checkpoint (no .zip). If omitted, actions are random.",
    )
    parser.add_argument("--episodes", type=int, default=40, help="Number of episodes")
    parser.add_argument("--seed", type=int, default=0, help="Base RNG seed for reset")
    parser.add_argument(
        "--decision-interval",
        type=float,
        default=2.0,
        help="Seconds between RL decisions (must match training)",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Use deterministic policy (only with --model)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="rollout_analysis",
        help="Directory for CSV and PNG outputs",
    )
    parser.add_argument(
        "--no-mi",
        action="store_true",
        help="Skip mutual information (no sklearn needed)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Collecting rollouts: episodes={args.episodes}, "
        f"model={'random' if args.model is None else args.model}"
    )
    X, y, actions = collect_rollouts(
        n_episodes=args.episodes,
        seed=args.seed,
        model_path=args.model,
        deterministic=args.deterministic,
        decision_interval_s=args.decision_interval,
    )
    n_steps, n_feat = X.shape
    assert n_feat == len(FEATURE_NAMES), (n_feat, len(FEATURE_NAMES))
    print(f"  Collected {n_steps} transitions.\n")

    df = pd.DataFrame(X, columns=FEATURE_NAMES)
    df["reward"] = y

    # --- Pearson / Spearman vs reward ---
    pearson_r = df[FEATURE_NAMES].corrwith(df["reward"], method="pearson")
    spearman_r = _spearman_corr_with_reward(df, FEATURE_NAMES, "reward")

    table = pd.DataFrame(
        {
            "feature": FEATURE_NAMES,
            "pearson_vs_reward": pearson_r.values,
            "spearman_vs_reward": spearman_r.values,
        }
    )

    if not args.no_mi:
        try:
            mi = _mutual_info_sklearn(X, y)
            table["mutual_info_vs_reward"] = mi
        except ImportError as e:
            print(f"Warning: {e}\n")
            table["mutual_info_vs_reward"] = np.nan

    table = table.sort_values("pearson_vs_reward", key=np.abs, ascending=False)
    csv_path = out_dir / "feature_vs_reward.csv"
    table.to_csv(csv_path, index=False)
    print(f"Saved {csv_path}")

    # --- Full correlation matrix (features + reward) ---
    corr_pearson = df.corr(method="pearson")
    corr_path = out_dir / "correlation_matrix_pearson.csv"
    corr_pearson.to_csv(corr_path)
    print(f"Saved {corr_path}")

    # --- Heatmap: Pearson features only ---
    fig, ax = plt.subplots(figsize=(12, 10))
    c = ax.imshow(corr_pearson.loc[FEATURE_NAMES, FEATURE_NAMES].values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(FEATURE_NAMES)))
    ax.set_yticks(range(len(FEATURE_NAMES)))
    ax.set_xticklabels(FEATURE_NAMES, rotation=90, fontsize=7)
    ax.set_yticklabels(FEATURE_NAMES, fontsize=7)
    ax.set_title("Pearson correlation — observation features (rollout data)")
    plt.colorbar(c, ax=ax, fraction=0.046, pad=0.04)
    plt.tight_layout()
    heatmap_path = out_dir / "correlation_heatmap_features.png"
    plt.savefig(heatmap_path, dpi=150)
    plt.close()
    print(f"Saved {heatmap_path}")

    # --- Bar: |Pearson| and MI vs reward ---
    fig, axes = plt.subplots(1, 2 if "mutual_info_vs_reward" in table.columns else 1, figsize=(14, 7))
    if not isinstance(axes, np.ndarray):
        axes = np.array([axes])

    order = np.argsort(-np.abs(pearson_r.values))
    names_ord = [FEATURE_NAMES[i] for i in order]
    axes[0].barh(names_ord, np.abs(pearson_r.values[order]), color="steelblue")
    axes[0].set_xlabel("|Pearson| vs reward")
    axes[0].set_title("Linear correlation with reward")

    if "mutual_info_vs_reward" in table.columns and not table["mutual_info_vs_reward"].isna().all():
        mi_vals = table.set_index("feature").reindex(FEATURE_NAMES)["mutual_info_vs_reward"].values
        order_mi = np.argsort(-mi_vals)
        names_mi = [FEATURE_NAMES[i] for i in order_mi]
        axes[1].barh(names_mi, mi_vals[order_mi], color="coral")
        axes[1].set_xlabel("Mutual information (nats)")
        axes[1].set_title("MI(feature; reward) — non-linear dependence")

    plt.tight_layout()
    bar_path = out_dir / "feature_importance_rollout.png"
    plt.savefig(bar_path, dpi=150)
    plt.close()
    print(f"Saved {bar_path}")

    # --- Optional: MI with discrete action (refill index) ---
    if not args.no_mi:
        try:
            from sklearn.feature_selection import mutual_info_classif

            a0 = actions[:, 0]
            mi_a0 = mutual_info_classif(X, a0, random_state=args.seed, discrete_features=False)
            t2 = pd.DataFrame({"feature": FEATURE_NAMES, "mi_vs_refill_idx": mi_a0})
            t2 = t2.sort_values("mi_vs_refill_idx", ascending=False)
            t2.to_csv(out_dir / "feature_vs_refill_action_mi.csv", index=False)
            print(f"Saved {out_dir / 'feature_vs_refill_action_mi.csv'}")
        except ImportError:
            pass

    print("\nDone.")


if __name__ == "__main__":
    main()
