#!/usr/bin/env python3
"""Signal analysis for the final Istio retry-budget PPO controller.

The script is intentionally simulator-first: it replays the trained model in
the same Gymnasium environment used for evaluation, records every decision
input, and then combines model-internal importance with inference-time
counterfactuals. Outputs are written under the model run directory by default.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from eval_rl_scenario import compute_metrics, detect_fault_windows
from simulator.rl.istio_retry_budget_env import (
    IstioRetryBudgetMetastableEnv,
    MIN_RETRY_CONCURRENCY_MAP,
    PERCENT_MAP,
)


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUN_DIR = (
    ROOT
    / "trained_models"
    / "istio_retry_budget_metastable"
    / "RB-RL.v5"
)
TRAIN_YAML = ROOT / "experiments" / "yaml" / "rl" / "istio_retry_budget_metastable.yaml"
BENCHMARK_DIR = ROOT / "experiments" / "yaml" / "rl" / "istio_retry_budget_benchmarks"
DEFAULT_BENCHMARKS = [
    "istio_partial_failure",
    "istio_load_spike",
    "istio_compound_failure",
    "istio_switchback_adversarial",
]

FEATURE_NAMES = list(IstioRetryBudgetMetastableEnv.OBSERVATION_FEATURES)
FEATURE_GROUPS = {
    "success_rate_agg": "client health",
    "min_client_success": "client health",
    "retry_ratio": "retry cost",
    "window_load_amplification": "retry cost",
    "window_retry_efficiency": "retry cost",
    "retry_fairness_gap": "retry cost",
    "p95_latency_pressure": "overload",
    "budget_reject_rate": "overload",
    "server_fail_rate": "overload",
    "deadline_rate": "overload",
    "delta_success_agg": "trend",
    "delta_window_load_amplification": "trend",
    "budget_utilization": "budget state",
    "retry_pressure_vs_limit": "budget state",
    "current_percent_norm": "action memory",
    "current_min_retry_concurrency_norm": "action memory",
    "previous_percent_norm": "action memory",
    "previous_min_retry_concurrency_norm": "action memory",
}
FEATURE_DESCRIPTIONS = {
    "success_rate_agg": "Aggregate per-attempt success rate across all clients.",
    "min_client_success": "Worst per-client attempt success rate in the observation window.",
    "retry_ratio": "Fraction of attempts that are retries.",
    "window_load_amplification": "Total attempts divided by first attempts; the retry amplification signal.",
    "window_retry_efficiency": "Fraction of retry attempts that eventually succeeded.",
    "retry_fairness_gap": "Mismatch between retry share and first-attempt share across clients.",
    "p95_latency_pressure": "Attempt p95 latency divided by the attempt timeout.",
    "budget_reject_rate": "Fraction of retry admissions rejected by the Istio retry budget.",
    "server_fail_rate": "Fraction of attempts failing as server failure or queue full.",
    "deadline_rate": "Fraction of attempts timing out or reaching the attempt timeout.",
    "delta_success_agg": "Short-window change in aggregate success rate.",
    "delta_window_load_amplification": "Short-window change in load amplification.",
    "budget_utilization": "Active retries divided by the current retry concurrency limit.",
    "retry_pressure_vs_limit": "Retry attempt rate divided by the current concurrency limit.",
    "current_percent_norm": "Current retryBudget.percent divided by 100.",
    "current_min_retry_concurrency_norm": "Current minRetryConcurrency divided by 10.",
    "previous_percent_norm": "Previous retryBudget.percent divided by 100.",
    "previous_min_retry_concurrency_norm": "Previous minRetryConcurrency divided by 10.",
}
TELEMETRY_FEATURES = [
    name
    for name in FEATURE_NAMES
    if FEATURE_GROUPS[name] != "action memory"
]
ACTION_MEMORY_FEATURES = [
    name
    for name in FEATURE_NAMES
    if FEATURE_GROUPS[name] == "action memory"
]


@dataclass(frozen=True)
class ScenarioSpec:
    label: str
    yaml_path: Path
    seed: int
    randomized: bool
    scenario_profile: str = "metastable_fairness"


def parse_csv_ints(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_csv_strings(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def model_path_for_run(run_dir: Path) -> Path:
    model_path = run_dir / "model"
    if not model_path.with_suffix(".zip").exists() and not model_path.exists():
        raise FileNotFoundError(f"Missing model checkpoint at {model_path}.zip")
    return model_path


def make_vecnormalize(
    vecnorm_path: Path,
    yaml_path: Path,
    decision_interval_s: float,
    observation_window_s: float,
    delta_window_s: float,
    randomize_scenarios: bool,
    scenario_profile: str,
) -> VecNormalize:
    def make_env():
        return IstioRetryBudgetMetastableEnv(
            yaml_path=str(yaml_path),
            decision_interval_s=decision_interval_s,
            observation_window_s=observation_window_s,
            delta_window_s=delta_window_s,
            randomize_scenarios=randomize_scenarios,
            scenario_profile=scenario_profile,
        )

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    venv.training = False
    venv.norm_reward = False
    return venv


def normalize_obs(vecnorm: VecNormalize, obs: np.ndarray) -> np.ndarray:
    return vecnorm.normalize_obs(obs[None, :].astype(np.float32))[0].astype(np.float32)


def denormalize_feature_value(vecnorm: VecNormalize, feature_idx: int, normalized_value: float) -> float:
    mean = np.asarray(vecnorm.obs_rms.mean, dtype=np.float64)[feature_idx]
    var = np.asarray(vecnorm.obs_rms.var, dtype=np.float64)[feature_idx]
    return float(normalized_value * math.sqrt(var + vecnorm.epsilon) + mean)


def get_action_details(model: PPO, normalized_obs_batch: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    obs_tensor = torch.as_tensor(normalized_obs_batch.copy(), dtype=torch.float32, device=model.device)
    with torch.no_grad():
        dist = model.policy.get_distribution(obs_tensor)
        actions = dist.get_actions(deterministic=True).detach().cpu().numpy().astype(int)
        distributions = dist.distribution
        percent_probs = distributions[0].probs.detach().cpu().numpy()
        min_probs = distributions[1].probs.detach().cpu().numpy()
    return actions, percent_probs, min_probs


def selected_joint_probability(
    actions: np.ndarray,
    percent_probs: np.ndarray,
    min_probs: np.ndarray,
) -> np.ndarray:
    rows = np.arange(actions.shape[0])
    return percent_probs[rows, actions[:, 0]] * min_probs[rows, actions[:, 1]]


def phase_for_time(time_s: float, fault_windows: Iterable[tuple]) -> str:
    windows = [(float(w[1]), float(w[2])) for w in fault_windows]
    if any(start <= time_s <= end for start, end in windows):
        return "fault"
    ended = [end for _, end in windows if end < time_s]
    if ended:
        latest_end = max(ended)
        if time_s <= latest_end + 15.0:
            return "recovery"
        return "post_recovery"
    return "pre_fault"


def windows_with_colors(windows: Iterable[tuple]) -> list[tuple[str, float, float, str]]:
    converted = []
    for window in windows:
        if len(window) == 4:
            converted.append(window)
        else:
            name, start, end = window
            color = "red" if "failure" in str(name).lower() else "orange"
            converted.append((name, start, end, color))
    return converted


def scenario_family(label: str, randomized: bool, scenario_profile: str) -> str:
    if randomized:
        return f"randomized_{scenario_profile}"
    if "partial_failure" in label:
        return "single_partial_failure"
    if "load_spike" in label:
        return "single_load_spike"
    if "compound" in label:
        return "compound_failure"
    if "switchback" in label:
        return "switchback_adversarial"
    return "unknown"


def scenario_scope(label: str, randomized: bool, scenario_profile: str) -> str:
    family = scenario_family(label, randomized, scenario_profile)
    if family in {"single_partial_failure", "single_load_spike"}:
        return "generalized_single_fault"
    if family in {"compound_failure", "switchback_adversarial"}:
        return "generalized_multi_fault"
    return "randomized_metastable"


def build_scenarios(
    benchmark_names: list[str],
    benchmark_seeds: list[int],
    random_seeds: list[int],
    random_profiles: list[str],
) -> list[ScenarioSpec]:
    scenarios: list[ScenarioSpec] = []
    for name in benchmark_names:
        yaml_path = BENCHMARK_DIR / f"{name}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"Benchmark YAML not found: {yaml_path}")
        for seed in benchmark_seeds:
            scenarios.append(ScenarioSpec(name, yaml_path, seed, randomized=False))
    for profile in random_profiles:
        for seed in random_seeds:
            scenarios.append(
                ScenarioSpec(
                    label=f"random_{profile}",
                    yaml_path=TRAIN_YAML,
                    seed=seed,
                    randomized=True,
                    scenario_profile=profile,
                )
            )
    return scenarios


def collect_rollout(
    spec: ScenarioSpec,
    model: PPO,
    run_dir: Path,
    decision_interval_s: float,
    observation_window_s: float,
    delta_window_s: float,
    mask_feature: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    mask_label = FEATURE_NAMES[mask_feature] if mask_feature is not None else "baseline"
    print(f"  rollout {spec.label} seed={spec.seed} mask={mask_label}", flush=True)
    vecnorm = make_vecnormalize(
        run_dir / "vecnormalize_stats.pkl",
        spec.yaml_path,
        decision_interval_s,
        observation_window_s,
        delta_window_s,
        spec.randomized,
        spec.scenario_profile,
    )
    env = IstioRetryBudgetMetastableEnv(
        yaml_path=str(spec.yaml_path),
        decision_interval_s=decision_interval_s,
        observation_window_s=observation_window_s,
        delta_window_s=delta_window_s,
        randomize_scenarios=spec.randomized,
        scenario_profile=spec.scenario_profile,
    )

    rows = []
    obs, _ = env.reset(seed=spec.seed)
    fault_windows = windows_with_colors(env.fault_windows if spec.randomized else detect_fault_windows(str(spec.yaml_path)))
    family = scenario_family(spec.label, spec.randomized, spec.scenario_profile)
    scope = scenario_scope(spec.label, spec.randomized, spec.scenario_profile)
    step_idx = 0
    while True:
        raw_obs = obs.astype(np.float32)
        norm_obs = normalize_obs(vecnorm, raw_obs)
        inference_obs = norm_obs.copy()
        if mask_feature is not None:
            inference_obs[mask_feature] = 0.0
        action, percent_probs, min_probs = get_action_details(model, inference_obs[None, :])
        action = action[0]
        percent_probs = percent_probs[0]
        min_probs = min_probs[0]

        next_obs, reward, done, _, info = env.step(action)
        hist = env.history[-1]
        phase = phase_for_time(float(hist["time_s"]), fault_windows)
        row = {
            "scenario": spec.label,
            "scenario_family": family,
            "scenario_scope": scope,
            "scenario_profile": spec.scenario_profile,
            "yaml": str(spec.yaml_path),
            "seed": spec.seed,
            "randomized": int(spec.randomized),
            "step": step_idx,
            "time_s": hist["time_s"],
            "phase": phase,
            "reward": float(reward),
            "action_percent_idx": int(action[0]),
            "action_min_idx": int(action[1]),
            "action_percent": float(hist["action_percent"]),
            "action_min_retry_concurrency": int(hist["action_min_retry_concurrency"]),
            "action_joint_prob": float(percent_probs[int(action[0])] * min_probs[int(action[1])]),
            "action_percent_prob": float(percent_probs[int(action[0])]),
            "action_min_prob": float(min_probs[int(action[1])]),
            "retry_concurrency_limit": float(hist["retry_concurrency_limit"]),
            "fault_active": int(hist["fault_active"]),
            "recovery_active": int(hist["recovery_active"]),
            "mask_feature": FEATURE_NAMES[mask_feature] if mask_feature is not None else "",
        }
        for name, value in hist.items():
            if name not in row:
                row[name] = value
        for idx, name in enumerate(FEATURE_NAMES):
            row[f"raw_{name}"] = float(raw_obs[idx])
            row[f"norm_{name}"] = float(norm_obs[idx])
        for idx, value in enumerate(percent_probs):
            row[f"prob_percent_{PERCENT_MAP[idx]:g}"] = float(value)
        for idx, value in enumerate(min_probs):
            row[f"prob_min_{MIN_RETRY_CONCURRENCY_MAP[idx]}"] = float(value)
        rows.append(row)

        obs = next_obs
        step_idx += 1
        if done:
            break

    metrics = compute_metrics(env.clients, fault_windows, label="RL Agent")
    metrics_summary = {
        "scenario": spec.label,
        "scenario_family": family,
        "scenario_scope": scope,
        "scenario_profile": spec.scenario_profile,
        "yaml": str(spec.yaml_path),
        "seed": spec.seed,
        "randomized": int(spec.randomized),
        "mask_feature": FEATURE_NAMES[mask_feature] if mask_feature is not None else "",
        "recovered": int(all(np.isfinite(metrics["recovery_times"]))),
        "avg_recovery": float(metrics["avg_recovery"]),
        "load_amp": float(metrics["load_amp"]),
        "retry_eff": float(metrics["retry_eff"]) if np.isfinite(metrics["retry_eff"]) else np.nan,
        "sr_fault_agg": float(metrics["sr_fault_agg"]) if np.isfinite(metrics["sr_fault_agg"]) else np.nan,
        "p95": float(metrics["p95"]),
        "p99": float(metrics["p99"]),
    }
    env.close()
    vecnorm.close()
    return pd.DataFrame(rows), metrics_summary


def write_feature_dictionary(output_dir: Path) -> pd.DataFrame:
    rows = []
    for idx, name in enumerate(FEATURE_NAMES):
        rows.append(
            {
                "index": idx,
                "feature": name,
                "group": FEATURE_GROUPS[name],
                "description": FEATURE_DESCRIPTIONS[name],
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(output_dir / "feature_dictionary.csv", index=False)
    return df


def find_first_linear(module: nn.Module) -> nn.Linear | None:
    if isinstance(module, nn.Linear):
        return module
    for child in module.children():
        found = find_first_linear(child)
        if found is not None:
            return found
    return None


def learned_importance(
    model: PPO,
    run_dir: Path,
    rollout_df: pd.DataFrame,
    output_dir: Path,
) -> pd.DataFrame:
    linear = find_first_linear(model.policy.mlp_extractor.policy_net)
    if linear is None:
        raise RuntimeError("Could not find first policy Linear layer.")
    weights = linear.weight.detach().cpu().numpy()
    first_layer = np.abs(weights).mean(axis=0)

    vecnorm = make_vecnormalize(
        run_dir / "vecnormalize_stats.pkl",
        TRAIN_YAML,
        5.0,
        10.0,
        5.0,
        False,
        "metastable_fairness",
    )
    obs_std = np.sqrt(np.asarray(vecnorm.obs_rms.var, dtype=np.float64) + vecnorm.epsilon)
    vecnorm.close()

    norm_cols = [f"norm_{name}" for name in FEATURE_NAMES]
    sample = rollout_df[norm_cols].dropna().to_numpy(dtype=np.float32)
    if len(sample) > 500:
        sample = sample[np.linspace(0, len(sample) - 1, 500).astype(int)]

    obs_tensor = torch.as_tensor(sample.copy(), dtype=torch.float32, device=model.device)
    obs_tensor.requires_grad_(True)
    dist = model.policy.get_distribution(obs_tensor)
    actions = dist.get_actions(deterministic=True)
    log_prob = dist.log_prob(actions).sum()
    model.policy.zero_grad()
    log_prob.backward()
    selected_action_grad = np.abs(obs_tensor.grad.detach().cpu().numpy()).mean(axis=0)

    percent_grad = gradient_for_action_dimension(model, sample, action_dim=0)
    min_grad = gradient_for_action_dimension(model, sample, action_dim=1)

    df = pd.DataFrame(
        {
            "feature": FEATURE_NAMES,
            "group": [FEATURE_GROUPS[name] for name in FEATURE_NAMES],
            "first_layer_abs_weight": first_layer,
            "first_layer_raw_scale": first_layer / obs_std,
            "selected_action_gradient": selected_action_grad,
            "percent_gradient": percent_grad,
            "min_retry_concurrency_gradient": min_grad,
        }
    )
    for col in [
        "first_layer_abs_weight",
        "first_layer_raw_scale",
        "selected_action_gradient",
        "percent_gradient",
        "min_retry_concurrency_gradient",
    ]:
        total = df[col].sum()
        df[f"{col}_share"] = df[col] / total if total > 0 else 0.0
    df["rank_score"] = (
        df["first_layer_abs_weight_share"]
        + df["selected_action_gradient_share"]
        + df["percent_gradient_share"]
        + df["min_retry_concurrency_gradient_share"]
    ) / 4.0
    df = df.sort_values("rank_score", ascending=False)
    df.to_csv(output_dir / "learned_importance.csv", index=False)
    return df


def gradient_for_action_dimension(model: PPO, sample: np.ndarray, action_dim: int) -> np.ndarray:
    obs_tensor = torch.as_tensor(sample.copy(), dtype=torch.float32, device=model.device)
    obs_tensor.requires_grad_(True)
    dist = model.policy.get_distribution(obs_tensor)
    distributions = dist.distribution
    probs = distributions[action_dim].probs
    selected = torch.argmax(probs, dim=1)
    log_probs = torch.log(probs[torch.arange(probs.shape[0], device=model.device), selected] + 1e-12)
    model.policy.zero_grad()
    log_probs.sum().backward()
    return np.abs(obs_tensor.grad.detach().cpu().numpy()).mean(axis=0)


def inference_occlusion(model: PPO, rollout_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    norm_cols = [f"norm_{name}" for name in FEATURE_NAMES]
    obs = rollout_df[norm_cols].to_numpy(dtype=np.float32)
    original_actions = rollout_df[["action_percent_idx", "action_min_idx"]].to_numpy(dtype=int)
    _, original_percent_probs, original_min_probs = get_action_details(model, obs)
    original_joint = selected_joint_probability(original_actions, original_percent_probs, original_min_probs)

    rows = []
    for idx, name in enumerate(FEATURE_NAMES):
        masked = obs.copy()
        masked[:, idx] = 0.0
        masked_actions, masked_percent_probs, masked_min_probs = get_action_details(model, masked)
        masked_joint = selected_joint_probability(original_actions, masked_percent_probs, masked_min_probs)
        rows.append(
            {
                "feature": name,
                "group": FEATURE_GROUPS[name],
                "any_action_change_rate": float(np.mean(np.any(masked_actions != original_actions, axis=1))),
                "percent_change_rate": float(np.mean(masked_actions[:, 0] != original_actions[:, 0])),
                "min_retry_concurrency_change_rate": float(np.mean(masked_actions[:, 1] != original_actions[:, 1])),
                "mean_original_joint_prob": float(np.mean(original_joint)),
                "mean_masked_original_joint_prob": float(np.mean(masked_joint)),
                "mean_joint_prob_drop": float(np.mean(original_joint - masked_joint)),
                "median_joint_prob_drop": float(np.median(original_joint - masked_joint)),
            }
        )
    df = pd.DataFrame(rows)
    df["rank_score"] = (
        df["any_action_change_rate"]
        + df["percent_change_rate"]
        + df["min_retry_concurrency_change_rate"]
        + df["mean_joint_prob_drop"].clip(lower=0.0)
    ) / 4.0
    df = df.sort_values("rank_score", ascending=False)
    df.to_csv(output_dir / "inference_occlusion_importance.csv", index=False)
    return df


def phase_correlations(rollout_df: pd.DataFrame, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    action_targets = ["action_percent", "action_min_retry_concurrency", "reward"]
    outcome_targets = ["window_load_amp", "budget_reject_rate", "retry_ratio", "success_rate"]
    action_rows = []
    outcome_rows = []
    for phase, phase_df in rollout_df.groupby("phase"):
        for feature in FEATURE_NAMES:
            x = phase_df[f"raw_{feature}"].to_numpy(dtype=float)
            for target in action_targets:
                corr = safe_spearman(x, phase_df[target].to_numpy(dtype=float))
                action_rows.append({"phase": phase, "feature": feature, "target": target, "spearman": corr})
            for target in outcome_targets:
                if target in phase_df:
                    corr = safe_spearman(x, phase_df[target].to_numpy(dtype=float))
                    outcome_rows.append({"phase": phase, "feature": feature, "target": target, "spearman": corr})
    action_df = pd.DataFrame(action_rows)
    outcome_df = pd.DataFrame(outcome_rows)
    action_df.to_csv(output_dir / "phase_signal_action_correlations.csv", index=False)
    outcome_df.to_csv(output_dir / "phase_signal_outcome_correlations.csv", index=False)
    return action_df, outcome_df


def safe_spearman(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3 or np.nanstd(x[mask]) == 0 or np.nanstd(y[mask]) == 0:
        return np.nan
    x_rank = pd.Series(x[mask]).rank(method="average").to_numpy(dtype=float)
    y_rank = pd.Series(y[mask]).rank(method="average").to_numpy(dtype=float)
    if np.nanstd(x_rank) == 0 or np.nanstd(y_rank) == 0:
        return np.nan
    return float(np.corrcoef(x_rank, y_rank)[0, 1])


def run_counterfactuals(
    model: PPO,
    run_dir: Path,
    features: list[str],
    benchmark_names: list[str],
    seeds: list[int],
    decision_interval_s: float,
    observation_window_s: float,
    delta_window_s: float,
    output_dir: Path,
) -> pd.DataFrame:
    rows = []
    specs = build_scenarios(benchmark_names, seeds, [], "metastable_fairness")
    for spec in specs:
        _, base_metrics = collect_rollout(
            spec,
            model,
            run_dir,
            decision_interval_s,
            observation_window_s,
            delta_window_s,
            mask_feature=None,
        )
        base_metrics["variant"] = "baseline"
        rows.append(base_metrics)
        for feature in features:
            masked_idx = FEATURE_NAMES.index(feature)
            _, metrics = collect_rollout(
                spec,
                model,
                run_dir,
                decision_interval_s,
                observation_window_s,
                delta_window_s,
                mask_feature=masked_idx,
            )
            metrics["variant"] = f"mask_{feature}"
            metrics["delta_avg_recovery"] = metrics["avg_recovery"] - base_metrics["avg_recovery"]
            metrics["delta_load_amp"] = metrics["load_amp"] - base_metrics["load_amp"]
            metrics["delta_sr_fault_agg"] = metrics["sr_fault_agg"] - base_metrics["sr_fault_agg"]
            rows.append(metrics)
    df = pd.DataFrame(rows)
    df.to_csv(output_dir / "counterfactual_ablation_summary.csv", index=False)
    return df


def plot_barh(df: pd.DataFrame, value: str, label: str, title: str, path: Path, top_n: int = 18) -> None:
    plot_df = df.sort_values(value, ascending=True).tail(top_n)
    fig, ax = plt.subplots(figsize=(10, max(5, 0.38 * len(plot_df))))
    ax.barh(plot_df["feature"], plot_df[value], color="#1f77b4")
    ax.set_xlabel(label)
    ax.set_title(title)
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_telemetry_vs_memory_importance(
    learned_df: pd.DataFrame,
    occlusion_df: pd.DataFrame,
    output_dir: Path,
) -> pd.DataFrame:
    learned = learned_df[["feature", "group", "rank_score"]].rename(columns={"rank_score": "learned_rank_score"})
    occlusion = occlusion_df[["feature", "rank_score"]].rename(columns={"rank_score": "inference_rank_score"})
    combined = learned.merge(occlusion, on="feature", how="outer").fillna(0.0)
    combined["combined_rank_score"] = (combined["learned_rank_score"] + combined["inference_rank_score"]) / 2.0
    combined["feature_kind"] = np.where(combined["group"] == "action memory", "controller memory", "external telemetry")
    combined = combined.sort_values("combined_rank_score", ascending=False)
    combined.to_csv(output_dir / "combined_signal_importance.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharex=False)
    for ax, kind in zip(axes, ["external telemetry", "controller memory"]):
        subset = combined[combined["feature_kind"] == kind].sort_values("combined_rank_score", ascending=True)
        ax.barh(subset["feature"], subset["combined_rank_score"], color="#1f77b4" if kind == "external telemetry" else "#7f7f7f")
        ax.set_title(kind.title())
        ax.set_xlabel("Mean of learned and inference importance")
        ax.grid(True, axis="x", alpha=0.25)
    fig.suptitle("Telemetry Signals vs Controller Memory")
    fig.tight_layout()
    fig.savefig(output_dir / "telemetry_vs_action_memory_importance.png", dpi=170, bbox_inches="tight")
    plt.close(fig)
    return combined


def plot_family_recovery_summary(family_summary: pd.DataFrame, output_dir: Path) -> None:
    if family_summary.empty:
        return
    labels = family_summary["scenario_family"].tolist()
    x = np.arange(len(labels))
    fig, axes = plt.subplots(1, 2, figsize=(15, 5))
    axes[0].bar(x, family_summary["recovery_capped_mean_s"], yerr=family_summary["recovery_capped_sem_s"], color="#d62728", capsize=4)
    axes[0].set_xticks(x, labels, rotation=30, ha="right")
    axes[0].set_ylabel("Recovery time with censored cap (s)")
    axes[0].set_title("Recovery Difficulty by Fault Family")
    axes[0].grid(True, axis="y", alpha=0.25)

    axes[1].bar(x, family_summary["recovered_rate"], color="#2ca02c")
    axes[1].set_xticks(x, labels, rotation=30, ha="right")
    axes[1].set_ylim(0, 1.05)
    axes[1].set_ylabel("Recovered share")
    axes[1].set_title("Recovery Success by Fault Family")
    axes[1].grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_dir / "fault_family_recovery_summary.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def plot_action_distribution_by_phase_family(rollout_df: pd.DataFrame, output_dir: Path) -> None:
    phases = ["pre_fault", "fault", "recovery", "post_recovery"]
    families = list(rollout_df["scenario_scope"].drop_duplicates())
    fig, axes = plt.subplots(len(families), 2, figsize=(14, max(4, 3.5 * len(families))), squeeze=False)
    for row_idx, family in enumerate(families):
        family_df = rollout_df[rollout_df["scenario_scope"] == family]
        for col_idx, action_col in enumerate(["action_percent", "action_min_retry_concurrency"]):
            ax = axes[row_idx][col_idx]
            data = [
                family_df.loc[family_df["phase"] == phase, action_col].dropna().to_numpy()
                for phase in phases
            ]
            ax.boxplot(data, showfliers=False)
            ax.set_xticks(np.arange(1, len(phases) + 1), phases, rotation=25, ha="right")
            ax.set_title(f"{family}: {action_col}")
            ax.grid(True, axis="y", alpha=0.25)
    fig.suptitle("Budget Action Distribution by Phase and Fault Scope", y=0.995)
    fig.tight_layout()
    fig.savefig(output_dir / "action_distribution_by_phase_family.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def plot_top_signal_by_phase_scope(rollout_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    rows = []
    for scope, scope_df in rollout_df.groupby("scenario_scope"):
        for phase, phase_df in scope_df.groupby("phase"):
            if len(phase_df) < 3:
                continue
            for feature in TELEMETRY_FEATURES:
                corr_percent = abs(safe_spearman(phase_df[f"raw_{feature}"].to_numpy(float), phase_df["action_percent"].to_numpy(float)))
                corr_min = abs(safe_spearman(phase_df[f"raw_{feature}"].to_numpy(float), phase_df["action_min_retry_concurrency"].to_numpy(float)))
                finite_corrs = [corr for corr in [corr_percent, corr_min] if np.isfinite(corr)]
                score = float(np.mean(finite_corrs)) if finite_corrs else np.nan
                rows.append(
                    {
                        "scenario_scope": scope,
                        "phase": phase,
                        "feature": feature,
                        "mean_abs_action_corr": score,
                        "abs_percent_corr": corr_percent,
                        "abs_min_corr": corr_min,
                    }
                )
    top_df = (
        pd.DataFrame(rows)
        .sort_values("mean_abs_action_corr", ascending=False)
        .groupby(["scenario_scope", "phase"], as_index=False)
        .head(5)
    )
    top_df.to_csv(output_dir / "top_telemetry_signals_by_phase_scope.csv", index=False)
    if top_df.empty:
        return top_df

    scopes = list(top_df["scenario_scope"].drop_duplicates())
    fig, axes = plt.subplots(len(scopes), 1, figsize=(13, max(4, 4 * len(scopes))), squeeze=False)
    for ax, scope in zip(axes.flatten(), scopes):
        subset = top_df[top_df["scenario_scope"] == scope].copy()
        subset["label"] = subset["phase"] + ": " + subset["feature"]
        subset = subset.sort_values("mean_abs_action_corr", ascending=True)
        ax.barh(subset["label"], subset["mean_abs_action_corr"], color="#ff7f0e")
        ax.set_title(f"Top telemetry signals by phase: {scope}")
        ax.set_xlabel("Mean absolute correlation with action")
        ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_dir / "top_telemetry_signals_by_phase_scope.png", dpi=170, bbox_inches="tight")
    plt.close(fig)
    return top_df


def plot_recovery_vs_key_signals(rollout_df: pd.DataFrame, metrics_df: pd.DataFrame, output_dir: Path) -> None:
    metrics_enriched = add_recovery_classes(metrics_df)
    fault_recovery = rollout_df[rollout_df["phase"].isin(["fault", "recovery"])].copy()
    key_signals = ["raw_window_load_amplification", "raw_delta_window_load_amplification", "raw_budget_reject_rate", "raw_retry_pressure_vs_limit"]
    agg = (
        fault_recovery.groupby(["scenario", "seed"], as_index=False)[key_signals]
        .mean()
        .merge(metrics_enriched[["scenario", "seed", "scenario_scope", "recovery_capped_s", "recovered"]], on=["scenario", "seed"], how="left")
    )
    fig, axes = plt.subplots(2, 2, figsize=(13, 10))
    axes = axes.flatten()
    for ax, signal in zip(axes, key_signals):
        for scope, scope_df in agg.groupby("scenario_scope"):
            ax.scatter(scope_df[signal], scope_df["recovery_capped_s"], label=scope, alpha=0.75)
        ax.set_xlabel(signal.replace("raw_", ""))
        ax.set_ylabel("Recovery time with cap (s)")
        ax.set_title(f"Recovery vs {signal.replace('raw_', '')}")
        ax.grid(True, alpha=0.25)
    axes[0].legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_dir / "recovery_vs_key_signals.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def plot_phase_distributions(rollout_df: pd.DataFrame, output_dir: Path) -> None:
    phases = ["pre_fault", "fault", "recovery", "post_recovery"]
    fig, axes = plt.subplots(6, 3, figsize=(16, 22))
    axes = axes.flatten()
    for ax, feature in zip(axes, FEATURE_NAMES):
        data = [
            rollout_df.loc[rollout_df["phase"] == phase, f"raw_{feature}"].dropna().to_numpy()
            for phase in phases
        ]
        ax.boxplot(data, showfliers=False)
        ax.set_xticks(np.arange(1, len(phases) + 1), phases)
        ax.set_title(feature)
        ax.tick_params(axis="x", labelrotation=30)
        ax.grid(True, axis="y", alpha=0.25)
    for ax in axes[len(FEATURE_NAMES):]:
        ax.axis("off")
    fig.suptitle("Observable Signal Distributions by Phase", y=0.995)
    fig.tight_layout()
    fig.savefig(output_dir / "phase_feature_distributions.png", dpi=160, bbox_inches="tight")
    plt.close(fig)


def plot_heatmap(corr_df: pd.DataFrame, path: Path, title: str) -> None:
    phases = [phase for phase in ["pre_fault", "fault", "recovery", "post_recovery"] if phase in set(corr_df["phase"])]
    targets = list(corr_df["target"].drop_duplicates())
    fig, axes = plt.subplots(len(targets), len(phases), figsize=(4.2 * len(phases), 3.2 * len(targets)), squeeze=False)
    for row_idx, target in enumerate(targets):
        for col_idx, phase in enumerate(phases):
            ax = axes[row_idx][col_idx]
            pivot = (
                corr_df[(corr_df["phase"] == phase) & (corr_df["target"] == target)]
                .set_index("feature")
                .reindex(FEATURE_NAMES)
            )
            values = pivot["spearman"].to_numpy(dtype=float).reshape(-1, 1)
            im = ax.imshow(values, cmap="coolwarm", vmin=-1, vmax=1, aspect="auto")
            ax.set_title(f"{phase} -> {target}")
            ax.set_xticks([])
            ax.set_yticks(np.arange(len(FEATURE_NAMES)))
            ax.set_yticklabels(FEATURE_NAMES if col_idx == 0 else [])
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.7, label="Spearman correlation")
    fig.suptitle(title, y=0.995)
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def plot_counterfactuals(counter_df: pd.DataFrame, output_dir: Path) -> None:
    masked = counter_df[counter_df["variant"] != "baseline"].copy()
    if masked.empty:
        return
    masked = add_recovery_classes(masked)
    grouped = (
        masked.groupby("mask_feature", as_index=False)
        .agg(
            recovered_rate=("recovered", "mean"),
            avg_recovery=("avg_recovery", finite_mean),
            recovery_capped_mean_s=("recovery_capped_s", "mean"),
            recovery_capped_sem_s=("recovery_capped_s", finite_sem),
            delta_load_amp=("delta_load_amp", finite_mean),
            delta_sr_fault_agg=("delta_sr_fault_agg", finite_mean),
        )
        .sort_values(["recovered_rate", "recovery_capped_mean_s"], ascending=[True, False])
    )
    grouped.to_csv(output_dir / "counterfactual_ablation_grouped.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].bar(grouped["mask_feature"], grouped["recovery_capped_mean_s"], yerr=grouped["recovery_capped_sem_s"], color="#d62728", capsize=4)
    axes[0].set_ylabel("Mean recovery time with censored cap (s)")
    axes[0].set_title("Recovery Under Masked Signal")
    axes[0].tick_params(axis="x", labelrotation=35)
    axes[0].grid(True, axis="y", alpha=0.25)

    axes[1].bar(grouped["mask_feature"], grouped["recovered_rate"], color="#2ca02c")
    axes[1].set_ylim(0, 1.05)
    axes[1].set_ylabel("Recovered scenario share")
    axes[1].set_title("Recovery Count Under Masked Signal")
    axes[1].tick_params(axis="x", labelrotation=35)
    axes[1].grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_dir / "counterfactual_recovery_impact.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    scoped = (
        masked.groupby(["scenario_scope", "mask_feature"], as_index=False)
        .agg(recovered_rate=("recovered", "mean"), recovery_capped_mean_s=("recovery_capped_s", "mean"))
        .sort_values(["scenario_scope", "recovered_rate", "recovery_capped_mean_s"], ascending=[True, True, False])
    )
    scoped.to_csv(output_dir / "counterfactual_ablation_by_fault_scope.csv", index=False)


def finite_mean(values: pd.Series) -> float:
    finite = values[np.isfinite(values)]
    return float(finite.mean()) if len(finite) else float("inf")


def finite_sem(values: pd.Series) -> float:
    finite = values[np.isfinite(values)]
    if len(finite) <= 1:
        return 0.0
    return float(finite.std(ddof=1) / math.sqrt(len(finite)))


def recovery_with_cap(value: float, cap: float = 60.0) -> float:
    return cap if not np.isfinite(value) else float(value)


def add_recovery_classes(metrics_df: pd.DataFrame, recovery_cap_s: float = 60.0) -> pd.DataFrame:
    enriched = metrics_df.copy()
    enriched["recovery_capped_s"] = enriched["avg_recovery"].map(lambda value: recovery_with_cap(value, recovery_cap_s))
    enriched["recovery_class"] = np.select(
        [
            enriched["recovered"] == 0,
            enriched["recovery_capped_s"] >= 15.0,
        ],
        ["censored_or_not_recovered", "slow_recovery_metastable_like"],
        default="quick_recovery",
    )
    return enriched


def write_family_summaries(metrics_df: pd.DataFrame, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    enriched = add_recovery_classes(metrics_df)
    enriched.to_csv(output_dir / "rollout_metrics_enriched.csv", index=False)
    summary = (
        enriched.groupby(["scenario_scope", "scenario_family"], as_index=False)
        .agg(
            runs=("seed", "count"),
            recovered_rate=("recovered", "mean"),
            recovery_capped_mean_s=("recovery_capped_s", "mean"),
            recovery_capped_sem_s=("recovery_capped_s", finite_sem),
            load_amp_mean=("load_amp", "mean"),
            sr_fault_mean=("sr_fault_agg", "mean"),
        )
        .sort_values(["scenario_scope", "scenario_family"])
    )
    summary.to_csv(output_dir / "family_recovery_summary.csv", index=False)

    class_summary = (
        enriched.groupby(["scenario_scope", "recovery_class"], as_index=False)
        .agg(runs=("seed", "count"), load_amp_mean=("load_amp", "mean"), recovery_capped_mean_s=("recovery_capped_s", "mean"))
        .sort_values(["scenario_scope", "recovery_class"])
    )
    class_summary.to_csv(output_dir / "recovery_class_summary.csv", index=False)
    return summary, class_summary


def plot_representative_timelines(rollout_df: pd.DataFrame, top_features: list[str], output_dir: Path) -> None:
    scenarios = (
        rollout_df.groupby(["scenario", "seed"], as_index=False)
        .agg(min_success=("success_rate", "min"), max_load_amp=("window_load_amp", "max"))
        .sort_values(["min_success", "max_load_amp"], ascending=[True, False])
        .head(2)
    )
    for _, scenario_row in scenarios.iterrows():
        scenario = scenario_row["scenario"]
        seed = int(scenario_row["seed"])
        df = rollout_df[(rollout_df["scenario"] == scenario) & (rollout_df["seed"] == seed)].sort_values("time_s")
        fig, axes = plt.subplots(5, 1, figsize=(14, 15), sharex=True)
        axes[0].plot(df["time_s"], df["success_rate"], label="success", color="#2ca02c")
        axes[0].set_ylabel("Success")
        axes[0].legend()
        axes[1].plot(df["time_s"], df["retry_ratio"], label="retry ratio", color="#9467bd")
        axes[1].plot(df["time_s"], df["window_load_amp"], label="load amp", color="#ff7f0e")
        axes[1].set_ylabel("Retry pressure")
        axes[1].legend()
        axes[2].plot(df["time_s"], df["raw_budget_reject_rate"], label="budget reject", color="#d62728")
        axes[2].plot(df["time_s"], df["raw_retry_pressure_vs_limit"], label="retry pressure/limit", color="#8c564b")
        axes[2].set_ylabel("Budget signals")
        axes[2].legend()
        axes[3].step(df["time_s"], df["action_percent"], where="post", label="percent", color="#1f77b4")
        axes[3].step(df["time_s"], df["action_min_retry_concurrency"], where="post", label="min concurrency", color="#17becf")
        axes[3].set_ylabel("Action")
        axes[3].legend()
        for feature in top_features[:4]:
            axes[4].plot(df["time_s"], df[f"raw_{feature}"], label=feature)
        axes[4].set_ylabel("Top signals")
        axes[4].set_xlabel("Time (s)")
        axes[4].legend(loc="best")
        for ax in axes:
            for phase in ["fault", "recovery"]:
                phase_df = df[df["phase"] == phase]
                if not phase_df.empty:
                    ax.axvspan(phase_df["time_s"].min(), phase_df["time_s"].max(), alpha=0.08, color="red" if phase == "fault" else "green")
            ax.grid(True, alpha=0.25)
        fig.suptitle(f"Representative Timeline: {scenario}, seed={seed}")
        fig.tight_layout()
        safe_name = f"{scenario}_seed{seed}".replace("/", "_")
        fig.savefig(output_dir / f"timeline_{safe_name}.png", dpi=170, bbox_inches="tight")
        plt.close(fig)


def write_report(
    output_dir: Path,
    feature_df: pd.DataFrame,
    learned_df: pd.DataFrame,
    occlusion_df: pd.DataFrame,
    counter_df: pd.DataFrame,
    rollout_df: pd.DataFrame,
    metrics_df: pd.DataFrame,
    family_summary: pd.DataFrame,
    combined_importance: pd.DataFrame,
    top_phase_scope: pd.DataFrame,
) -> None:
    top_learned = learned_df.head(8)
    top_occ = occlusion_df.head(8)
    top_telemetry = combined_importance[combined_importance["feature_kind"] == "external telemetry"].head(8)
    top_memory = combined_importance[combined_importance["feature_kind"] == "controller memory"].head(8)
    grouped_counter = (
        add_recovery_classes(counter_df[counter_df["variant"] != "baseline"])
        .groupby("mask_feature", as_index=False)
        .agg(recovered_rate=("recovered", "mean"), recovery_capped_mean_s=("recovery_capped_s", "mean"))
        .sort_values(["recovered_rate", "recovery_capped_mean_s"], ascending=[True, False])
    )

    def md_table(df: pd.DataFrame, columns: list[str]) -> str:
        small = df[columns].copy()
        for col in small.columns:
            if pd.api.types.is_float_dtype(small[col]):
                small[col] = small[col].map(lambda x: "inf" if np.isinf(x) else f"{x:.4g}")
        header = "| " + " | ".join(small.columns) + " |"
        separator = "| " + " | ".join(["---"] * len(small.columns)) + " |"
        rows = [
            "| " + " | ".join(str(value) for value in row) + " |"
            for row in small.itertuples(index=False, name=None)
        ]
        return "\n".join([header, separator, *rows])

    phase_counts = rollout_df["phase"].value_counts().reindex(["pre_fault", "fault", "recovery", "post_recovery"]).fillna(0).astype(int)
    scope_counts = rollout_df[["scenario_scope", "scenario", "seed"]].drop_duplicates()["scenario_scope"].value_counts().to_dict()
    metrics_enriched = add_recovery_classes(metrics_df)
    recovery_class_counts = metrics_enriched["recovery_class"].value_counts().to_dict()
    report = f"""# Istio Retry-Budget RL Signal Analysis

This report analyzes the final `istio_retry_budget_metastable` PPO controller. It separates **learned importance** (what the policy network is sensitive to) from **inference importance** (which signals change actions when removed from real rollout states).

## Dataset

- Recorded decisions: {len(rollout_df)}
- Scenarios/seeds: {rollout_df[['scenario', 'seed']].drop_duplicates().shape[0]}
- Phase counts: {phase_counts.to_dict()}
- Fault-scope counts: {scope_counts}
- Recovery classes: {recovery_class_counts}
- Observation space: 18 caller-side, per-attempt features.
- Action space: `retryBudget.percent` in `{PERCENT_MAP}` and `minRetryConcurrency` in `{MIN_RETRY_CONCURRENCY_MAP}`.

## Feature Groups

{md_table(feature_df, ['index', 'feature', 'group', 'description'])}

## Top Learned Signals

These features have the strongest combined first-layer and gradient sensitivity in the PPO policy.

{md_table(top_learned, ['feature', 'group', 'rank_score', 'first_layer_abs_weight_share', 'selected_action_gradient_share'])}

## Telemetry vs Controller Memory

The controller can rely on two kinds of signals: external telemetry that says something about the service state, and action-memory features that tell it what budget it already applied. For explaining metastable failures to lab members, the telemetry ranking is usually the more useful one; action memory mostly explains action stability and hysteresis.

**Top external telemetry signals**

{md_table(top_telemetry, ['feature', 'group', 'combined_rank_score', 'learned_rank_score', 'inference_rank_score'])}

**Top controller-memory signals**

{md_table(top_memory, ['feature', 'group', 'combined_rank_score', 'learned_rank_score', 'inference_rank_score'])}

## Top Inference-Critical Signals

These features most often changed the selected action, or reduced the probability of the original action, when masked to the training mean.

{md_table(top_occ, ['feature', 'group', 'rank_score', 'any_action_change_rate', 'percent_change_rate', 'min_retry_concurrency_change_rate', 'mean_joint_prob_drop'])}

## Counterfactual Rollout Impact

These rows show what happens when top-ranked signals are masked during closed-loop simulator rollouts. The recovery time is capped for censored/non-recovered runs, so it should be read together with the recovered share.

{md_table(grouped_counter, ['mask_feature', 'recovered_rate', 'recovery_capped_mean_s']) if not grouped_counter.empty else 'No counterfactual rows generated.'}

## Metastable vs Generalized Fault Families

The report argues that recovery-time separation is strongest around metastable failures, but the final discussion also asks whether the controller generalizes beyond one sustained fault. The analysis therefore labels scenarios as single-fault, multi-fault, or randomized-metastable and summarizes them separately.

{md_table(family_summary, ['scenario_scope', 'scenario_family', 'runs', 'recovered_rate', 'recovery_capped_mean_s', 'load_amp_mean'])}

## Phase-Specific Telemetry Signals

This table highlights the telemetry signals most correlated with budget actions in each phase and fault scope. It is meant to support discussion figures: what the controller watches during fault, and what changes around recovery.

{md_table(top_phase_scope.head(16), ['scenario_scope', 'phase', 'feature', 'mean_abs_action_corr']) if not top_phase_scope.empty else 'No phase-specific telemetry rows generated.'}

## How To Read The Plots

- `phase_feature_distributions.png`: shows what each observable signal looks like before the fault, during the fault, and during recovery.
- `learned_importance_bar.png`: model-internal ranking from network weights and gradients.
- `inference_occlusion_importance.png`: action sensitivity when each signal is removed at inference.
- `phase_signal_action_correlation_heatmap.png`: phase-specific relationship between signals and chosen budget settings.
- `phase_signal_outcome_correlation_heatmap.png`: phase-specific relationship between signals and recovery/metastability indicators.
- `counterfactual_recovery_impact.png`: closed-loop recovery impact when top signals are masked.
- `fault_family_recovery_summary.png`: recovery behavior split into single-fault, multi-fault, and randomized-metastable groups.
- `telemetry_vs_action_memory_importance.png`: separates true observable system signals from controller memory.
- `top_telemetry_signals_by_phase_scope.png`: compact, report-friendly view of important signals per phase and fault scope.
- `recovery_vs_key_signals.png`: links key signals directly to recovery difficulty.
- `action_distribution_by_phase_family.png`: shows when the model tightens or opens the budget across fault types.
- `timeline_*.png`: representative scenario walkthroughs showing signal movement, budget actions, and recovery.

## Caveats

Correlation plots are explanatory, not causal. The masked-feature counterfactuals are closer to causal evidence, but they still test the trained simulator environment rather than the live cluster. All model-facing analysis uses `VecNormalize`; skipping it would analyze a different input scale than the policy actually sees.
"""
    (output_dir / "SIGNAL_ANALYSIS.md").write_text(report, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze signals for the final Istio retry-budget PPO model.")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR), help="Model run directory.")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to RUN_DIR/signal_analysis.")
    parser.add_argument("--benchmarks", default=",".join(DEFAULT_BENCHMARKS), help="Comma-separated benchmark stems.")
    parser.add_argument("--benchmark-seeds", default="42,43,44,45,46", help="Comma-separated fixed-scenario seeds.")
    parser.add_argument("--random-seeds", default="100,101,102,103,104,105,106,107,108,109", help="Comma-separated randomized training-template seeds.")
    parser.add_argument(
        "--random-profiles",
        default="metastable_fairness,switchback_adversarial",
        help="Comma-separated randomized scenario profiles.",
    )
    parser.add_argument("--decision-interval-s", type=float, default=5.0)
    parser.add_argument("--observation-window-s", type=float, default=10.0)
    parser.add_argument("--delta-window-s", type=float, default=5.0)
    parser.add_argument("--counterfactual-top-n", type=int, default=7)
    parser.add_argument("--counterfactual-seeds", default="42,43,44", help="Comma-separated benchmark seeds for closed-loop ablations.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_dir / "signal_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading model from {run_dir}", flush=True)
    model = PPO.load(str(model_path_for_run(run_dir)), device="cpu")
    print("Model loaded.", flush=True)
    scenarios = build_scenarios(
        parse_csv_strings(args.benchmarks),
        parse_csv_ints(args.benchmark_seeds),
        parse_csv_ints(args.random_seeds),
        parse_csv_strings(args.random_profiles),
    )

    all_rollouts = []
    metrics_rows = []
    for spec in scenarios:
        rollout, metrics = collect_rollout(
            spec,
            model,
            run_dir,
            args.decision_interval_s,
            args.observation_window_s,
            args.delta_window_s,
        )
        all_rollouts.append(rollout)
        metrics_rows.append(metrics)

    print("Writing rollout datasets.", flush=True)
    rollout_df = pd.concat(all_rollouts, ignore_index=True)
    metrics_df = pd.DataFrame(metrics_rows)
    metrics_df.to_csv(output_dir / "rollout_metrics.csv", index=False)
    family_summary, recovery_class_summary = write_family_summaries(metrics_df, output_dir)
    recovery_labels = add_recovery_classes(metrics_df)[
        ["scenario", "seed", "recovered", "recovery_capped_s", "recovery_class"]
    ]
    rollout_df = rollout_df.merge(recovery_labels, on=["scenario", "seed"], how="left")
    rollout_df.to_csv(output_dir / "rollout_decisions.csv", index=False)

    feature_df = write_feature_dictionary(output_dir)
    print("Computing learned importance.", flush=True)
    learned_df = learned_importance(model, run_dir, rollout_df, output_dir)
    print("Computing inference occlusion importance.", flush=True)
    occlusion_df = inference_occlusion(model, rollout_df, output_dir)
    print("Computing phase correlations.", flush=True)
    action_corr_df, outcome_corr_df = phase_correlations(rollout_df, output_dir)
    combined_importance = plot_telemetry_vs_memory_importance(learned_df, occlusion_df, output_dir)

    top_telemetry = combined_importance[combined_importance["feature_kind"] == "external telemetry"]["feature"].head(args.counterfactual_top_n).tolist()
    top_memory = combined_importance[combined_importance["feature_kind"] == "controller memory"]["feature"].head(2).tolist()
    top_features = list(dict.fromkeys([*top_telemetry, *top_memory]))[: args.counterfactual_top_n]
    print(f"Running counterfactuals for: {', '.join(top_features)}", flush=True)
    counter_df = run_counterfactuals(
        model,
        run_dir,
        top_features,
        parse_csv_strings(args.benchmarks),
        parse_csv_ints(args.counterfactual_seeds),
        args.decision_interval_s,
        args.observation_window_s,
        args.delta_window_s,
        output_dir,
    )

    print("Generating plots and report.", flush=True)
    plot_phase_distributions(rollout_df, output_dir)
    top_phase_scope = plot_top_signal_by_phase_scope(rollout_df, output_dir)
    plot_barh(learned_df, "rank_score", "Combined learned importance", "Learned Signal Importance", output_dir / "learned_importance_bar.png")
    plot_barh(occlusion_df, "rank_score", "Inference occlusion importance", "Inference-Critical Signals", output_dir / "inference_occlusion_importance.png")
    plot_barh(learned_df, "percent_gradient_share", "Gradient share", "Signals Driving retryBudget.percent", output_dir / "learned_importance_percent_head.png")
    plot_barh(learned_df, "min_retry_concurrency_gradient_share", "Gradient share", "Signals Driving minRetryConcurrency", output_dir / "learned_importance_min_head.png")
    plot_heatmap(action_corr_df, output_dir / "phase_signal_action_correlation_heatmap.png", "Signal-to-Action Correlations by Phase")
    plot_heatmap(outcome_corr_df, output_dir / "phase_signal_outcome_correlation_heatmap.png", "Signal-to-Outcome Correlations by Phase")
    plot_counterfactuals(counter_df, output_dir)
    plot_family_recovery_summary(family_summary, output_dir)
    plot_action_distribution_by_phase_family(rollout_df, output_dir)
    plot_recovery_vs_key_signals(rollout_df, metrics_df, output_dir)
    plot_representative_timelines(rollout_df, top_features, output_dir)

    manifest = {
        "run_dir": str(run_dir),
        "output_dir": str(output_dir),
        "features": FEATURE_NAMES,
        "top_counterfactual_features": top_features,
        "num_decisions": int(len(rollout_df)),
        "num_scenarios": int(rollout_df[["scenario", "seed"]].drop_duplicates().shape[0]),
        "scenario_scopes": sorted(rollout_df["scenario_scope"].dropna().unique().tolist()),
        "timing": {
            "decision_interval_s": args.decision_interval_s,
            "observation_window_s": args.observation_window_s,
            "delta_window_s": args.delta_window_s,
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_report(
        output_dir,
        feature_df,
        learned_df,
        occlusion_df,
        counter_df,
        rollout_df,
        metrics_df,
        family_summary,
        combined_importance,
        top_phase_scope,
    )
    print(f"Signal analysis complete. Outputs written to {output_dir}")


if __name__ == "__main__":
    main()
