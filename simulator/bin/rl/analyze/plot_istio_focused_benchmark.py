#!/usr/bin/env python3
"""Plot success, load amplification, and RL actions for one Istio benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bin/rl for shared + sibling modules
import _pathsetup  # noqa: F401  (adds simulator/src and RL script folders to sys.path)

from matplotlib_safe import configure_matplotlib

configure_matplotlib()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SERIES = {
    "No Budget": ("no_budget_timeseries.csv", "#d32f2f", "--"),
    "Static Budget": ("static_budget_timeseries.csv", "#212121", "-."),
    "RL Agent": ("rl_agent_timeseries.csv", "#2e7d32", "-"),
}


def success_rate(df: pd.DataFrame) -> pd.Series:
    completed = df["success_root"] + df["failure_root"]
    return df["success_root"] / completed.replace(0, np.nan)


def load_amplification(df: pd.DataFrame) -> pd.Series:
    total_attempts = df["root_requests"] + df["retries"]
    return total_attempts / df["root_requests"].replace(0, np.nan)


def shade_fault_windows(ax, scenario_dir: Path) -> None:
    details_path = scenario_dir / "details.json"
    if not details_path.exists():
        return
    details = json.loads(details_path.read_text())
    seen = set()
    for label, start_s, end_s, color in details.get("fault_windows", []):
        key = (label, float(start_s), float(end_s))
        if key in seen:
            continue
        seen.add(key)
        ax.axvspan(float(start_s), float(end_s), color=color, alpha=0.12)
        ax.text(
            float(start_s),
            1.02,
            label,
            transform=ax.get_xaxis_transform(),
            fontsize=8,
            color=color,
            va="bottom",
        )


def plot_focused_benchmark(scenario_dir: Path, output_path: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    for label, (filename, color, linestyle) in SERIES.items():
        path = scenario_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing expected time series: {path}")
        df = pd.read_csv(path)
        axes[0].plot(df["timepoint"], success_rate(df), label=label, color=color, linestyle=linestyle, linewidth=2)
        axes[1].plot(df["timepoint"], load_amplification(df), label=label, color=color, linestyle=linestyle, linewidth=2)

    shade_fault_windows(axes[0], scenario_dir)
    axes[0].set_ylabel("Success rate")
    axes[0].set_ylim(-0.05, 1.08)
    axes[0].set_title("Success Rate")
    axes[0].legend(loc="lower left")
    axes[0].grid(True, alpha=0.3)

    shade_fault_windows(axes[1], scenario_dir)
    axes[1].set_ylabel("Load amplification")
    axes[1].set_title("Load Amplification")
    axes[1].legend(loc="upper left")
    axes[1].grid(True, alpha=0.3)

    actions_path = scenario_dir / "rl_actions.csv"
    if not actions_path.exists():
        raise FileNotFoundError(f"Missing RL actions: {actions_path}")
    actions = pd.read_csv(actions_path)
    ax_percent = axes[2]
    ax_min = ax_percent.twinx()
    ax_percent.step(
        actions["time_s"],
        actions["percent"],
        where="post",
        color="#1976D2",
        linewidth=2,
        label="retryBudget.percent",
    )
    ax_min.step(
        actions["time_s"],
        actions["min_retry_concurrency"],
        where="post",
        color="#7B1FA2",
        linewidth=2,
        label="minRetryConcurrency",
    )
    shade_fault_windows(ax_percent, scenario_dir)
    ax_percent.set_ylabel("Percent", color="#1976D2")
    ax_min.set_ylabel("Min retry concurrency", color="#7B1FA2")
    ax_percent.set_xlabel("Time (s)")
    ax_percent.set_title("RL Agent Actions")
    ax_percent.grid(True, alpha=0.3)
    lines_left, labels_left = ax_percent.get_legend_handles_labels()
    lines_right, labels_right = ax_min.get_legend_handles_labels()
    ax_percent.legend(lines_left + lines_right, labels_left + labels_right, loc="upper right")

    fig.suptitle(scenario_dir.name.replace("_", " ").title(), y=0.995)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180)
    plt.close(fig)
    print(f"Saved focused benchmark plot to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_dir", type=Path, help="Directory containing benchmark artifacts for one scenario.")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path.")
    args = parser.parse_args()

    scenario_dir = args.scenario_dir.resolve()
    output = args.output or scenario_dir / "focused_success_amp_actions.png"
    plot_focused_benchmark(scenario_dir, output.resolve())


if __name__ == "__main__":
    main()
