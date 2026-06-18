#!/usr/bin/env python3
"""Plot action correlations for the top external telemetry signals."""

from __future__ import annotations

import argparse
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


PHASES = ["pre_fault", "fault", "recovery", "post_recovery"]
TARGETS = [
    ("action_percent", r"$b$"),
    ("action_min_retry_concurrency", r"$m$"),
]

FEATURE_LABELS = {
    "window_load_amplification": r"$a$",
    "delta_window_load_amplification": r"$\Delta a$",
    "budget_reject_rate": r"$\rho_B$",
}


def top_external_features(importance_df: pd.DataFrame, n: int) -> list[str]:
    external = importance_df[importance_df["feature_kind"] == "external telemetry"]
    return (
        external.sort_values("combined_rank_score", ascending=False)
        .head(n)["feature"]
        .tolist()
    )


def format_feature_name(name: str) -> str:
    return FEATURE_LABELS.get(name, name.replace("_", " "))


def correlation_matrix(
    corr_df: pd.DataFrame,
    features: list[str],
    target: str,
) -> np.ndarray:
    pivot = (
        corr_df[corr_df["target"] == target]
        .pivot_table(index="feature", columns="phase", values="spearman", aggfunc="mean")
        .reindex(index=features, columns=PHASES)
    )
    return pivot.to_numpy(dtype=float)


def annotate_heatmap(ax, values: np.ndarray) -> None:
    for row in range(values.shape[0]):
        for col in range(values.shape[1]):
            value = values[row, col]
            if np.isnan(value):
                label = "n/a"
                color = "black"
            else:
                label = f"{value:+.2f}"
                color = "white" if abs(value) > 0.55 else "black"
            ax.text(col, row, label, ha="center", va="center", fontsize=7, color=color)


def plot_top_signal_action_correlations(signal_dir: Path, output: Path, top_n: int) -> None:
    importance_df = pd.read_csv(signal_dir / "combined_signal_importance.csv")
    corr_df = pd.read_csv(signal_dir / "phase_signal_action_correlations.csv")
    features = top_external_features(importance_df, top_n)

    values = np.hstack([
        correlation_matrix(corr_df, features, target)
        for target, _ in TARGETS
    ])
    phase_labels = ["pre\nfault", "fault", "rec.", "post\nrec."]
    x_labels = phase_labels * len(TARGETS)

    fig, ax = plt.subplots(figsize=(4.8, 2.35), constrained_layout=True)
    image = ax.imshow(values, cmap="coolwarm", vmin=-1, vmax=1, aspect="auto")
    annotate_heatmap(ax, values)

    ax.set_xticks(np.arange(values.shape[1]), x_labels)
    ax.set_yticks(np.arange(len(features)), [format_feature_name(feature) for feature in features])
    ax.tick_params(axis="x", labelrotation=0, labelsize=7, pad=1)
    ax.tick_params(axis="y", labelsize=12, pad=4)

    # Keep the two action blocks visually joined while marking the boundary.
    ax.axvline(len(PHASES) - 0.5, color="black", linewidth=2.4)
    ax.text(1.5, 1.05, TARGETS[0][1], transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=12)
    ax.text(5.5, 1.05, TARGETS[1][1], transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=12)

    colorbar = fig.colorbar(image, ax=ax, shrink=0.9, pad=0.02)
    colorbar.set_label("Spearman", fontsize=8)
    colorbar.ax.tick_params(labelsize=7)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=240, bbox_inches="tight")
    plt.close(fig)

    export_rows = []
    for feature in features:
        for phase in PHASES:
            for target, _ in TARGETS:
                match = corr_df[
                    (corr_df["feature"] == feature)
                    & (corr_df["phase"] == phase)
                    & (corr_df["target"] == target)
                ]
                value = float(match["spearman"].iloc[0]) if not match.empty else np.nan
                export_rows.append({
                    "feature": feature,
                    "phase": phase,
                    "target": target,
                    "spearman": value,
                })
    pd.DataFrame(export_rows).to_csv(output.with_suffix(".csv"), index=False)
    print(f"Saved top signal/action correlation plot to {output}")
    print(f"Saved plotted values to {output.with_suffix('.csv')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signal_dir", type=Path, help="Directory containing signal analysis CSV files.")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path.")
    parser.add_argument("--top-n", type=int, default=3, help="Number of external telemetry signals to show.")
    args = parser.parse_args()

    signal_dir = args.signal_dir.resolve()
    output = args.output or signal_dir / "top3_signal_action_correlations.png"
    plot_top_signal_action_correlations(signal_dir, output.resolve(), args.top_n)


if __name__ == "__main__":
    main()
