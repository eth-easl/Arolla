#!/usr/bin/env python3
"""Evaluate a hybrid (3-knob) metastable RL agent.

Works for both hybrid model families saved under ``trained_models/``:

- ``metastable_hybrid_absolute_action`` (env: ``HybridMetastableFairnessEnv``)
- ``metastable_hybrid_relative_action`` (env: ``HybridRelativeActionMetastableFairnessEnv``)

The script auto-detects which hybrid variant a checkpoint belongs to by reading
``model_variant.txt`` from the run directory. The env class is the only thing
that differs at evaluation time; observation/action shapes and the artifacts
pipeline are otherwise identical to ``eval_rl_metastable_{absolute,relative}``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from eval_rl_scenario import (
    compute_metrics,
    detect_fault_windows,
    plot_comparison,
    print_comparison,
    run_best_static_budget,
    run_no_budget,
    run_static_budget,
    run_static_budget_with_override,
    save_metrics_artifacts,
)
from simulator.rl.hybrid_metastable_env import (
    HybridMetastableFairnessEnv,
    HybridRelativeActionMetastableFairnessEnv,
)


# Map model_variant.txt env_class values and CLI aliases to concrete env classes.
_HYBRID_ENV_CLASSES = {
    "HybridMetastableFairnessEnv": HybridMetastableFairnessEnv,
    "HybridRelativeActionMetastableFairnessEnv": HybridRelativeActionMetastableFairnessEnv,
    "absolute": HybridMetastableFairnessEnv,
    "relative": HybridRelativeActionMetastableFairnessEnv,
}


def _parse_model_variant(model_path: str) -> dict[str, str]:
    """Read the ``model_variant.txt`` that training writes next to the model.

    Returns a dict of ``key=value`` pairs (empty dict if the file is missing).
    We tolerate a missing file so the user can still force a variant via CLI.
    """
    variant_path = Path(model_path).resolve().parent / "model_variant.txt"
    if not variant_path.exists():
        return {}

    parsed: dict[str, str] = {}
    for line in variant_path.read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def resolve_hybrid_env_class(model_path: str, explicit_variant: str | None = None):
    """Pick the correct hybrid env class for ``model_path``.

    Priority:
      1. ``explicit_variant`` (CLI override) if provided.
      2. ``env_class`` entry in the checkpoint's ``model_variant.txt``.

    Raises a helpful error if the variant cannot be determined or does not
    correspond to one of the two hybrid env classes.
    """
    if explicit_variant and explicit_variant != "auto":
        if explicit_variant not in _HYBRID_ENV_CLASSES:
            raise ValueError(
                f"Unknown --variant '{explicit_variant}'. "
                f"Expected one of: auto, relative, absolute."
            )
        return _HYBRID_ENV_CLASSES[explicit_variant]

    metadata = _parse_model_variant(model_path)
    env_class_name = metadata.get("env_class")
    if env_class_name is None:
        raise FileNotFoundError(
            f"Could not auto-detect hybrid variant: no model_variant.txt next "
            f"to {model_path!r}. Re-run with --variant relative or "
            f"--variant absolute."
        )
    if env_class_name not in _HYBRID_ENV_CLASSES:
        raise ValueError(
            f"model_variant.txt reports env_class={env_class_name!r}, which is "
            f"not a hybrid env. This script only supports "
            f"HybridMetastableFairnessEnv and HybridRelativeActionMetastableFairnessEnv."
        )
    return _HYBRID_ENV_CLASSES[env_class_name]


def _load_obs_normalizer(
    env_cls, model_path: str, yaml_path: str, decision_interval_s: float
) -> VecNormalize:
    """Rebuild the ``VecNormalize`` that wrapped the env during training.

    Stable-Baselines3 stores only the raw policy weights in the ``.zip``; the
    running mean/var used to normalise observations is written separately as
    ``vecnormalize_stats.pkl`` by the training scripts. At eval time we have to
    reconstruct the same wrapper so ``predict`` sees observations in the same
    scale the policy learned against.
    """
    vecnorm_path = Path(model_path).resolve().parent / "vecnormalize_stats.pkl"
    if not vecnorm_path.exists():
        raise FileNotFoundError(f"Missing VecNormalize stats at {vecnorm_path}")

    def make_env():
        return env_cls(
            yaml_path=yaml_path,
            decision_interval_s=decision_interval_s,
            randomize_scenarios=False,
        )

    venv = DummyVecEnv([make_env])
    venv = VecNormalize.load(str(vecnorm_path), venv)
    # At eval we only normalise observations; we don't want SB3 to keep updating
    # the running stats and we want raw (un-normalised) rewards for diagnostics.
    venv.training = False
    venv.norm_reward = False
    return venv


def run_rl_agent(
    yaml_path: str,
    model_path: str,
    env_cls,
    decision_interval_s: float = 2.0,
    seed: int = 42,
):
    """Roll out one deterministic episode of the hybrid policy.

    Returns ``(clients, actions_df)`` where ``actions_df`` includes the third
    knob (``event_reward``) in addition to ``refill_rate`` and ``bucket_capacity``.
    The downstream plotter only looks at the first two columns, so adding the
    third one is backwards-compatible and gets captured in the CSV artifacts.
    """
    env = env_cls(
        yaml_path=yaml_path,
        decision_interval_s=decision_interval_s,
        randomize_scenarios=False,
    )
    obs_normalizer = _load_obs_normalizer(env_cls, model_path, yaml_path, decision_interval_s)
    model = PPO.load(model_path, env=obs_normalizer)

    obs, _ = env.reset(seed=seed)
    while True:
        normalized_obs = obs_normalizer.normalize_obs(obs[None, :])[0]
        action, _ = model.predict(normalized_obs, deterministic=True)
        obs, _, done, _, _ = env.step(action)
        if done:
            break

    actions_df = pd.DataFrame(
        [
            {
                "time_s": row["time_s"],
                "refill_rate": row["action_refill_rate"],
                "bucket_capacity": row["action_bucket_capacity"],
                # Populated by the hybrid env after the first step; default 0.0
                # guards the (very unlikely) case where a row somehow lacks it.
                "event_reward": row.get("action_event_reward", 0.0),
            }
            for row in env.history
        ]
    )
    clients = env.clients
    env.close()
    obs_normalizer.close()
    return clients, actions_df


def evaluate_scenario(
    model_path: str,
    yaml_path: str,
    seed: int = 42,
    plot_path: str | None = None,
    artifacts_dir: str | None = None,
    print_table: bool = True,
    show_plot: bool = True,
    variant: str | None = None,
    include_best_static: bool = True,
    fixed_static_refill_rate: int | None = None,
    fixed_static_bucket_capacity: int | None = None,
):
    """Evaluate a hybrid RL agent against three baselines on a single scenario.

    The four baselines are:

    1. **No Budget** — no retry budget at all; worst-case retry amplification.
    2. **Static Budget** — by default the budget exactly as written in the
       YAML. Optionally this can be overridden to the same fixed
       ``(refill_rate, bucket_capacity)`` across every scenario.
    3. **Best Static** — the best single ``(refill_rate, bucket_capacity)`` pair
       from a sweep over the RL's *own* action grid. This is the tightest
       honest upper bound for any stationary policy the RL could have
       converged to. Any edge RL shows over this line is attributable to
       being non-stationary. Enable/disable via ``include_best_static``.
    4. **RL Agent** — the trained hybrid policy.
    """
    env_cls = resolve_hybrid_env_class(model_path, explicit_variant=variant)
    fault_windows = detect_fault_windows(yaml_path)

    if (fixed_static_refill_rate is None) != (fixed_static_bucket_capacity is None):
        raise ValueError(
            "fixed_static_refill_rate and fixed_static_bucket_capacity must be "
            "provided together."
        )

    print(f"Scenario : {yaml_path}")
    print(f"Model    : {model_path}")
    print(f"Env      : {env_cls.__name__}")
    print(f"Seed     : {seed}")
    print()

    total_stages = 4 if include_best_static else 3

    print(f"[1/{total_stages}] Running scenario WITHOUT budget …")
    nb_clients = run_no_budget(yaml_path, seed=seed)

    if fixed_static_refill_rate is None:
        print(f"[2/{total_stages}] Running scenario with STATIC budget (YAML) …")
        st_clients = run_static_budget(yaml_path, seed=seed)
        static_budget_choice = None
    else:
        print(
            f"[2/{total_stages}] Running scenario with FIXED STATIC budget "
            f"(refill_rate={fixed_static_refill_rate}, "
            f"bucket_capacity={fixed_static_bucket_capacity}) …"
        )
        st_clients = run_static_budget_with_override(
            yaml_path,
            refill_rate=fixed_static_refill_rate,
            bucket_capacity=fixed_static_bucket_capacity,
            seed=seed,
        )
        static_budget_choice = {
            "refill_rate": int(fixed_static_refill_rate),
            "bucket_capacity": int(fixed_static_bucket_capacity),
        }

    best_static_choice = None
    bs_clients = None
    if include_best_static:
        print(
            f"[3/{total_stages}] Sweeping BEST STATIC over RL action grid "
            f"(this runs ~25 episodes) …"
        )
        bs_clients, best_static_choice, _ = run_best_static_budget(
            yaml_path, fault_windows, seed=seed
        )
        print(
            f"        best static: refill_rate={best_static_choice['refill_rate']}, "
            f"bucket_capacity={best_static_choice['bucket_capacity']}"
        )

    print(f"[{total_stages}/{total_stages}] Running scenario with HYBRID METASTABLE RL AGENT …")
    rl_clients, rl_actions = run_rl_agent(yaml_path, model_path, env_cls, seed=seed)

    results = [
        compute_metrics(nb_clients, fault_windows, "No Budget"),
        compute_metrics(st_clients, fault_windows, "Static Budget"),
    ]
    if static_budget_choice is not None:
        results[1]["static_budget_choice"] = static_budget_choice
    if bs_clients is not None:
        bs_metrics = compute_metrics(bs_clients, fault_windows, "Best Static")
        bs_metrics["best_static_choice"] = best_static_choice
        results.append(bs_metrics)
    results.append(compute_metrics(rl_clients, fault_windows, "RL Agent"))

    if print_table:
        print_comparison(results, fault_windows)
        if static_budget_choice is not None:
            print(
                f"Fixed Static choice: refill_rate={static_budget_choice['refill_rate']}, "
                f"bucket_capacity={static_budget_choice['bucket_capacity']}"
            )
        if best_static_choice is not None:
            print(
                f"Best Static choice: refill_rate={best_static_choice['refill_rate']}, "
                f"bucket_capacity={best_static_choice['bucket_capacity']}"
            )

    if plot_path is not None:
        plot_comparison(
            results,
            fault_windows,
            rl_actions_df=rl_actions,
            save_path=plot_path,
            show_plot=show_plot,
        )

    if artifacts_dir is not None:
        save_metrics_artifacts(results, rl_actions, fault_windows, Path(artifacts_dir))

    return results, rl_actions, fault_windows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate hybrid (3-knob) metastable RL agent")
    parser.add_argument("--model", required=True, help="Path to saved PPO model (without .zip)")
    parser.add_argument("--yaml", required=True, help="Path to scenario YAML")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="eval_comparison.png", help="Output plot filename")
    parser.add_argument(
        "--variant",
        choices=["auto", "relative", "absolute"],
        default="auto",
        help="Hybrid variant (default: auto-detect from model_variant.txt)",
    )
    parser.add_argument(
        "--no-best-static",
        dest="include_best_static",
        action="store_false",
        help=(
            "Skip the Best-Static grid sweep baseline "
            "(sweep adds ~25 episodes per scenario, ~20s wall time)."
        ),
    )
    parser.set_defaults(include_best_static=True)
    parser.add_argument(
        "--static-refill-rate",
        type=int,
        default=None,
        help="Override the YAML static baseline with this refill rate across scenarios.",
    )
    parser.add_argument(
        "--static-bucket-capacity",
        type=int,
        default=None,
        help="Override the YAML static baseline with this bucket capacity across scenarios.",
    )
    args = parser.parse_args()

    evaluate_scenario(
        model_path=args.model,
        yaml_path=args.yaml,
        seed=args.seed,
        plot_path=args.output,
        print_table=True,
        show_plot=True,
        variant=args.variant,
        include_best_static=args.include_best_static,
        fixed_static_refill_rate=args.static_refill_rate,
        fixed_static_bucket_capacity=args.static_bucket_capacity,
    )
