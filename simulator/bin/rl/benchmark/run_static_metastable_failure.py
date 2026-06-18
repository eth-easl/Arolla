#!/usr/bin/env python3
"""Run a metastable-failure debug scenario with static/no-retry/no-budget baselines.

This helper is meant for debugging and intuition-building. It runs the
``metastable_failure_fairness`` benchmark after first making it harsher by
default, then compares:

1. a chosen fixed static retry budget
2. an explicit no-retry baseline
3. a no-budget baseline
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from eval_rl_metastable_hybrid import resolve_hybrid_env_class, run_rl_agent
from matplotlib_safe import configure_matplotlib
from eval_rl_scenario import compute_metrics, detect_fault_windows
from simulator.config.loader import ConfigLoader
from simulator.config.schema import ExperimentConfig, LoadSpikeConfig, PartialFailureConfig
from simulator.utils.time import s_to_ns

configure_matplotlib()


YAML_PATH = (
    Path(__file__).resolve().parents[3]
    / "experiments"
    / "yaml"
    / "rl"
    / "metastable_benchmarks"
    / "metastable_failure_fairness.yaml"
)


def _json_ready(value):
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _build_and_drive(config: ExperimentConfig):
    sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
    service = list(services.values())[0]
    service.enable_live_buffer()

    for wl, client in zip(workloads, clients):
        wl.drive(sim, lambda s, c=client: c.start_request(s))

    episode_end = max(s_to_ns(workload.duration_s) for workload in workloads)
    return sim, clients, service, workloads, episode_end


def _make_harder_config(config: ExperimentConfig) -> ExperimentConfig:
    service = config.services[0]
    service_timeout = (
        service.timeout.model_copy(update={"attempt_ms": 144})
        if service.timeout is not None
        else None
    )
    harder_service = service.model_copy(
        update={
            "latency": service.latency.model_copy(update={"median_ms": 65, "lognorm_sigma": 0.55}),
            "queue_capacity": 600,
            "timeout": service_timeout,
            "partial_failures": [
                PartialFailureConfig(start_s=20, end_s=40, p_fail=0.72),
            ],
        }
    )

    harder_clients = []
    spike_multipliers = [3.6, 3.1, 2.7]
    for idx, client in enumerate(config.clients):
        client_timeout = (
            client.timeout.model_copy(update={"attempt_ms": 144})
            if client.timeout is not None
            else None
        )
        workload = client.workload.model_copy(
            update={
                "duration_s": 90,
                "load_spikes": [
                    LoadSpikeConfig(start_s=45, end_s=65, rps_multiplier=spike_multipliers[idx]),
                ],
            }
        )
        harder_clients.append(
            client.model_copy(
                update={
                    "workload": workload,
                    "timeout": client_timeout,
                }
            )
        )

    return config.model_copy(
        update={
            "name": f"{config.name}_harder",
            "services": [harder_service],
            "clients": harder_clients,
        }
    )


def _make_collapse_config(config: ExperimentConfig) -> ExperimentConfig:
    """Much harsher regime intended to make the no-budget baseline collapse."""
    service = config.services[0]
    service_timeout = (
        service.timeout.model_copy(update={"attempt_ms": 120})
        if service.timeout is not None
        else None
    )
    harder_service = service.model_copy(
        update={
            "latency": service.latency.model_copy(update={"median_ms": 70, "lognorm_sigma": 0.60}),
            "queue_capacity": 200,
            "timeout": service_timeout,
            "partial_failures": [
                PartialFailureConfig(start_s=16, end_s=42, p_fail=0.85),
            ],
        }
    )

    base_rps_scale = [1.20, 1.18, 1.15]
    spike_multipliers = [4.2, 3.7, 3.2]
    harder_clients = []
    for idx, client in enumerate(config.clients):
        client_timeout = (
            client.timeout.model_copy(update={"attempt_ms": 120})
            if client.timeout is not None
            else None
        )
        workload = client.workload.model_copy(
            update={
                "base_rps": max(1.0, client.workload.base_rps * base_rps_scale[idx]),
                "duration_s": 95,
                "load_spikes": [
                    LoadSpikeConfig(start_s=46, end_s=76, rps_multiplier=spike_multipliers[idx]),
                ],
            }
        )
        harder_clients.append(
            client.model_copy(
                update={
                    "workload": workload,
                    "timeout": client_timeout,
                }
            )
        )

    return config.model_copy(
        update={
            "name": f"{config.name}_collapse",
            "services": [harder_service],
            "clients": harder_clients,
        }
    )


def _make_meltdown_config(config: ExperimentConfig) -> ExperimentConfig:
    """Extreme regime where even fixed static budgets should struggle to recover."""
    service = config.services[0]
    service_timeout = (
        service.timeout.model_copy(update={"attempt_ms": 96})
        if service.timeout is not None
        else None
    )
    meltdown_service = service.model_copy(
        update={
            "latency": service.latency.model_copy(update={"median_ms": 75, "lognorm_sigma": 0.65}),
            "queue_capacity": 80,
            "timeout": service_timeout,
            "partial_failures": [
                PartialFailureConfig(start_s=12, end_s=48, p_fail=0.92),
            ],
        }
    )

    base_rps_scale = [1.40, 1.35, 1.30]
    spike_multipliers = [5.0, 4.4, 3.8]
    meltdown_clients = []
    for idx, client in enumerate(config.clients):
        client_timeout = (
            client.timeout.model_copy(update={"attempt_ms": 96})
            if client.timeout is not None
            else None
        )
        client_retry = (
            client.retry.model_copy(update={"max_attempts": 6, "delay_ms": 0})
            if client.retry is not None
            else None
        )
        workload = client.workload.model_copy(
            update={
                "base_rps": max(1.0, client.workload.base_rps * base_rps_scale[idx]),
                "duration_s": 100,
                "load_spikes": [
                    LoadSpikeConfig(start_s=44, end_s=84, rps_multiplier=spike_multipliers[idx]),
                ],
            }
        )
        meltdown_clients.append(
            client.model_copy(
                update={
                    "workload": workload,
                    "timeout": client_timeout,
                    "retry": client_retry,
                }
            )
        )

    return config.model_copy(
        update={
            "name": f"{config.name}_meltdown",
            "services": [meltdown_service],
            "clients": meltdown_clients,
        }
    )


def _run_static_budget(config: ExperimentConfig, refill_rate: int, bucket_capacity: int, seed: int):
    service = config.services[0]
    budget_cfg = service.global_retry_budget.model_copy(
        update={"target_rps": int(refill_rate), "max_burst": int(bucket_capacity)}
    )
    new_service = service.model_copy(update={"global_retry_budget": budget_cfg})
    config = config.model_copy(update={"services": [new_service], "seed": seed})
    sim, clients, _, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def _run_no_retry(config: ExperimentConfig, seed: int):
    clients = [client.model_copy(update={"retry": None}) for client in config.clients]
    services = [
        service.model_copy(update={"global_retry_budget": None, "aimd_global_retry_budget": None})
        for service in config.services
    ]
    config = config.model_copy(update={"clients": clients, "services": services, "seed": seed})
    sim, clients, _, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def _run_no_budget(config: ExperimentConfig, seed: int):
    services = [
        service.model_copy(update={"global_retry_budget": None, "aimd_global_retry_budget": None})
        for service in config.services
    ]
    config = config.model_copy(update={"services": services, "seed": seed})
    sim, clients, _, _, _ = _build_and_drive(config)
    sim.run()
    return clients


def _run_hybrid_model(
    config: ExperimentConfig,
    model_path: str,
    variant: str | None,
    seed: int,
):
    """Run a trained hybrid model against this in-memory scenario config."""
    with tempfile.TemporaryDirectory(prefix="hybrid_debug_") as tmpdir:
        yaml_path = Path(tmpdir) / "scenario.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                config.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
            )
        )
        env_cls = resolve_hybrid_env_class(model_path, explicit_variant=variant)
        return run_rl_agent(str(yaml_path), model_path, env_cls, seed=seed)


def _save_plot(
    results: list[dict],
    fault_windows,
    refill_rate: int,
    bucket_capacity: int,
    save_path: Path,
    rl_actions: pd.DataFrame | None = None,
) -> None:
    has_rl_actions = rl_actions is not None and not rl_actions.empty
    has_event_reward = has_rl_actions and "event_reward" in rl_actions.columns
    n_panels = 6 if has_event_reward else 5 if has_rl_actions else 4
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 4 * n_panels), sharex=True)
    styles = {
        "Static Budget": ("#424242", "-"),
        "No Budget": ("#1976D2", "-."),
        "RL Agent": ("#2E7D32", "-"),
    }

    for result in results:
        ts_df = result["ts_df"]
        label = result["label"]
        if label not in styles:
            continue
        color, linestyle = styles[label]
        completed = ts_df["success_root"] + ts_df["failure_root"]
        success_rate = ts_df["success_root"] / completed.replace(0, np.nan)
        load_amp = (ts_df["root_requests"] + ts_df["retries"]) / ts_df["root_requests"].replace(0, np.nan)

        axes[0].plot(ts_df["timepoint"], success_rate, color=color, linestyle=linestyle, linewidth=2, label=label)
        axes[1].plot(ts_df["timepoint"], load_amp, color=color, linestyle=linestyle, linewidth=2, label=label)
        axes[2].plot(ts_df["timepoint"], ts_df["p99"], color=color, linestyle=linestyle, linewidth=2, label=label)
        axes[3].step(ts_df["timepoint"], ts_df["retries"], where="post", color=color, linestyle=linestyle, linewidth=2, label=label)

    axes[0].set_ylabel("Success Rate")
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_title("Success Rate Over Time")
    axes[0].legend(loc="lower left")

    axes[1].set_ylabel("Load Amp")
    axes[1].set_title("Load Amplification")
    axes[1].legend(loc="upper right")

    axes[2].set_ylabel("P99 (ms)")
    axes[2].set_title("Tail Latency")
    axes[2].legend(loc="upper right")

    axes[3].set_ylabel("Retries")
    axes[3].set_xlabel("Time (s)")
    axes[3].set_title("Retries Per Bucket")
    axes[3].legend(loc="upper right")

    if has_rl_actions:
        ax_left = axes[4]
        ax_right = ax_left.twinx()
        ax_left.step(
            rl_actions["time_s"],
            rl_actions["refill_rate"],
            where="post",
            color="#2196F3",
            linewidth=2,
            label="Refill Rate",
        )
        ax_right.step(
            rl_actions["time_s"],
            rl_actions["bucket_capacity"],
            where="post",
            color="#9C27B0",
            linewidth=2,
            label="Bucket Capacity",
        )
        ax_left.set_ylabel("Refill Rate", color="#2196F3")
        ax_right.set_ylabel("Bucket Capacity", color="#9C27B0")
        lines_l, labels_l = ax_left.get_legend_handles_labels()
        lines_r, labels_r = ax_right.get_legend_handles_labels()
        ax_left.legend(lines_l + lines_r, labels_l + labels_r, loc="upper right")
        ax_left.set_title("RL Agent Decisions")

    if has_event_reward:
        axes[5].step(
            rl_actions["time_s"],
            rl_actions["event_reward"],
            where="post",
            color="#FF9800",
            linewidth=2,
            label="Event Reward",
        )
        axes[5].set_ylabel("Event Reward")
        axes[5].set_title("RL Agent Event-Reward Knob")
        axes[5].legend(loc="upper right")

    for _, start, end, color in fault_windows:
        for ax in axes:
            ax.axvspan(start, end, alpha=0.12, color=color)
            ax.grid(True, alpha=0.3)

    fig.suptitle(
        f"Metastable Failure Debug (static refill_rate={refill_rate}, "
        f"bucket_capacity={bucket_capacity})",
        fontsize=14,
    )
    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    plt.close(fig)
    print(f"Saved plot to {save_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a metastable_failure_fairness scenario with static, no-retry, and no-budget baselines"
    )
    parser.add_argument("--refill-rate", type=int, required=True, help="Static retry refill rate")
    parser.add_argument(
        "--bucket-capacity",
        type=int,
        required=True,
        help="Static retry bucket capacity",
    )
    parser.add_argument("--seed", type=int, default=42, help="Simulation seed (default: 42)")
    parser.add_argument(
        "--model",
        default=None,
        help="Optional hybrid model path (without .zip) to evaluate on this scenario.",
    )
    parser.add_argument(
        "--variant",
        choices=["auto", "relative", "absolute"],
        default="auto",
        help="Hybrid model variant if --model is set (default: auto-detect).",
    )
    parser.add_argument(
        "--scenario-mode",
        choices=["original", "harder", "collapse", "meltdown"],
        default="harder",
        help=(
            "Scenario strength preset: "
            "'original' uses YAML as-is, "
            "'harder' is moderately harsher, "
            "'collapse' is tuned so no-budget is much more likely to fall apart, "
            "'meltdown' is tuned so even fixed static budgets struggle to recover."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional directory to save summary.csv, timeseries.csv, details.json, and plot.png",
    )
    parser.add_argument(
        "--plot-path",
        default=None,
        help="Optional explicit output path for the PNG plot.",
    )
    args = parser.parse_args()

    yaml_path = str(YAML_PATH.resolve())
    base_config = ConfigLoader.load_from_file(yaml_path)
    if args.scenario_mode == "original":
        config = base_config
    elif args.scenario_mode == "harder":
        config = _make_harder_config(base_config)
    elif args.scenario_mode == "collapse":
        config = _make_collapse_config(base_config)
    else:
        config = _make_meltdown_config(base_config)
    fault_windows = detect_fault_windows(yaml_path)
    if args.scenario_mode == "harder":
        fault_windows = [
            ("Partial Failure", 20.0, 40.0, "red"),
            ("Load Spike", 45.0, 65.0, "orange"),
            ("Load Spike", 45.0, 65.0, "orange"),
            ("Load Spike", 45.0, 65.0, "orange"),
        ]
    elif args.scenario_mode == "collapse":
        fault_windows = [
            ("Partial Failure", 16.0, 42.0, "red"),
            ("Load Spike", 46.0, 76.0, "orange"),
            ("Load Spike", 46.0, 76.0, "orange"),
            ("Load Spike", 46.0, 76.0, "orange"),
        ]
    elif args.scenario_mode == "meltdown":
        fault_windows = [
            ("Partial Failure", 12.0, 48.0, "red"),
            ("Load Spike", 44.0, 84.0, "orange"),
            ("Load Spike", 44.0, 84.0, "orange"),
            ("Load Spike", 44.0, 84.0, "orange"),
        ]

    static_clients = _run_static_budget(
        config,
        refill_rate=args.refill_rate,
        bucket_capacity=args.bucket_capacity,
        seed=args.seed,
    )
    no_retry_clients = _run_no_retry(config, seed=args.seed)
    no_budget_clients = _run_no_budget(config, seed=args.seed)
    static_metrics = compute_metrics(static_clients, fault_windows, "Static Budget")
    no_retry_metrics = compute_metrics(no_retry_clients, fault_windows, "No Retry")
    no_budget_metrics = compute_metrics(no_budget_clients, fault_windows, "No Budget")
    results = [static_metrics, no_retry_metrics, no_budget_metrics]
    rl_actions = None
    if args.model:
        rl_clients, rl_actions = _run_hybrid_model(
            config,
            model_path=args.model,
            variant=args.variant,
            seed=args.seed,
        )
        results.append(compute_metrics(rl_clients, fault_windows, "RL Agent"))

    print(f"Scenario        : {yaml_path}")
    print(f"Refill rate     : {args.refill_rate}")
    print(f"Bucket capacity : {args.bucket_capacity}")
    print(f"Seed            : {args.seed}")
    print(f"Scenario mode   : {args.scenario_mode}")
    if args.model:
        print(f"Model           : {args.model}")
    print()
    for metrics in results:
        print(f"[{metrics['label']}]")
        print(f"  Fault success   : {metrics['sr_fault_agg']:.4f}")
        print(f"  Load amp        : {metrics['load_amp']:.4f}")
        print(f"  Retry efficiency: {metrics['retry_eff']:.4f}")
        print(f"  Avg recovery (s): {metrics['avg_recovery']}")
        print(f"  P50 (ms)        : {metrics['p50']:.2f}")
        print(f"  P95 (ms)        : {metrics['p95']:.2f}")
        print(f"  P99 (ms)        : {metrics['p99']:.2f}")
        print()

    plot_path = Path(args.plot_path) if args.plot_path is not None else None
    if args.output_dir is None and plot_path is None:
        return

    out_dir = None
    if args.output_dir is not None:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    if out_dir is not None:
        summary = pd.DataFrame(
            [
                {
                    "label": metrics["label"],
                    "refill_rate": args.refill_rate if metrics["label"] == "Static Budget" else None,
                    "bucket_capacity": args.bucket_capacity if metrics["label"] == "Static Budget" else None,
                    "sr_fault_agg": metrics["sr_fault_agg"],
                    "load_amp": metrics["load_amp"],
                    "retry_eff": metrics["retry_eff"],
                    "avg_recovery": metrics["avg_recovery"],
                    "p50": metrics["p50"],
                    "p95": metrics["p95"],
                    "p99": metrics["p99"],
                }
                for metrics in results
            ]
        )
        summary.to_csv(out_dir / "summary.csv", index=False)
        static_metrics["ts_df"].to_csv(out_dir / "static_budget_timeseries.csv", index=False)
        no_retry_metrics["ts_df"].to_csv(out_dir / "no_retry_timeseries.csv", index=False)
        no_budget_metrics["ts_df"].to_csv(out_dir / "no_budget_timeseries.csv", index=False)
        if args.model:
            rl_metrics = next(result for result in results if result["label"] == "RL Agent")
            rl_metrics["ts_df"].to_csv(out_dir / "rl_agent_timeseries.csv", index=False)
            if rl_actions is not None:
                rl_actions.to_csv(out_dir / "rl_actions.csv", index=False)

        details = {
            "scenario": yaml_path,
            "scenario_mode": args.scenario_mode,
            "fault_windows": _json_ready(fault_windows),
            "refill_rate": args.refill_rate,
            "bucket_capacity": args.bucket_capacity,
            "seed": args.seed,
            "model": args.model,
            "variants": _json_ready(
                {
                    metrics["label"]: {
                        "sr_fault_agg": metrics["sr_fault_agg"],
                        "sr_fault_per_client": metrics["sr_fault_per_client"],
                        "load_amp": metrics["load_amp"],
                        "retry_eff": metrics["retry_eff"],
                        "retry_share": metrics["retry_share"],
                        "recovery_times": metrics["recovery_times"],
                        "avg_recovery": metrics["avg_recovery"],
                        "p50": metrics["p50"],
                        "p95": metrics["p95"],
                        "p99": metrics["p99"],
                    }
                    for metrics in results
                }
            ),
        }
        (out_dir / "details.json").write_text(json.dumps(details, indent=2))

    if plot_path is None and out_dir is not None:
        plot_path = out_dir / "plot.png"
    if plot_path is not None:
        _save_plot(
            results,
            fault_windows,
            args.refill_rate,
            args.bucket_capacity,
            plot_path,
            rl_actions=rl_actions,
        )


if __name__ == "__main__":
    main()
