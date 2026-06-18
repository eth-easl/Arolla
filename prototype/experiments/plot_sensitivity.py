#!/usr/bin/env python3
"""
plot_sensitivity.py — produce the sensitivity-analysis figures.

Reads sweep output directories under outputs/prototype/sensitivity/
and produces:

  Figure A: recovery time vs. parameter value (OPAT, 4 panels)
  Figure B: recovery time vs. failure characteristics (2 panels)
  Ablation: mixed refill vs. single refill source (1 panel)

Usage:
  python3 plot_sensitivity.py <sensitivity_output_root> [--out <dir>]

  sensitivity_output_root is the parent directory containing one
  subdirectory per sweep (e.g. outputs/prototype/sensitivity/).
  --out defaults to <sensitivity_output_root>/plots/.

Each sweep subdirectory has one subdirectory per parameter value, and
each of those contains a summary.csv produced by analyze.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


# -------------------------------------------------------------------------
# Style
# -------------------------------------------------------------------------

POLICY_LABELS = {
    "no-control":         "No control",
    "circuit-breaker":    "CB (θ=50%)",
    "envoy-retry-budget": "Envoy (b=20%)",
    "arolla":             "Arolla (defaults)",
}

POLICY_COLORS = {
    "no-control":         "#888888",
    "circuit-breaker":    "#d62728",
    "envoy-retry-budget": "#ff7f0e",
    "arolla":             "#2ca02c",
}

POLICY_MARKERS = {
    "no-control":         "x",
    "circuit-breaker":    "s",
    "envoy-retry-budget": "D",
    "arolla":             "o",
}

POLICY_ORDER = ["no-control", "circuit-breaker", "envoy-retry-budget", "arolla"]

NO_RECOVERY_SENTINEL = 35.0   # y-value for "did not recover" points
NO_RECOVERY_THRESHOLD = 30.0  # dashed line — anything above = no recovery


def _apply_style():
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        11,
        "axes.labelsize":   12,
        "axes.titlesize":   13,
        "xtick.labelsize":  10,
        "ytick.labelsize":  10,
        "legend.fontsize":  10,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    1.0,
        "lines.linewidth":   2.0,
        "figure.dpi":        150,
    })


# -------------------------------------------------------------------------
# Data loading
# -------------------------------------------------------------------------

def load_sweep(
    root: Path,
    sweep_name: str,
) -> Dict[str, Dict[str, pd.Series]]:
    """
    Load a sweep's results.

    Returns { policy: pd.Series } where the Series is indexed by the
    parameter value (as a string) and the value is recovery_sec.

    For multi-policy sweeps (Figure B), multiple policies appear.
    For single-policy sweeps (Figure A, ablation), only one.
    """
    sweep_dir = root / sweep_name
    if not sweep_dir.is_dir():
        print(f"[warn] sweep dir not found: {sweep_dir}", file=sys.stderr)
        return {}

    # Each subdirectory is named by the parameter value.
    # run-experiment.sh may nest the actual run output 1-2 levels deeper
    # (e.g. <value>/<profile>/<profile>_<timestamp>/summary.csv) depending
    # on the -o flag and the profile name. We search recursively up to 3
    # levels to find the summary.csv.
    results: Dict[str, List[Tuple[str, float]]] = {}
    for value_dir in sorted(sweep_dir.iterdir()):
        if not value_dir.is_dir():
            continue
        value_label = value_dir.name
        # Find summary.csv anywhere under value_dir, up to 3 levels deep.
        summary = None
        for csv_path in value_dir.rglob("summary.csv"):
            # Prefer the shallowest one (closest to value_dir).
            if summary is None or len(csv_path.parts) < len(summary.parts):
                summary = csv_path
        if summary is None:
            continue
        df = pd.read_csv(summary)
        for _, row in df.iterrows():
            policy = row["policy"]
            rec = row.get("recovery_sec")
            if pd.isna(rec) or rec is None or rec == "":
                rec = NO_RECOVERY_SENTINEL
            else:
                rec = float(rec)
            results.setdefault(policy, []).append((value_label, rec))

    # Convert to Series per policy.
    out = {}
    for policy, pairs in results.items():
        idx = [p[0] for p in pairs]
        vals = [p[1] for p in pairs]
        out[policy] = pd.Series(vals, index=idx, name=policy)
    return out


def _try_numeric_index(series: pd.Series) -> Tuple[np.ndarray, np.ndarray, bool]:
    """
    Try to convert a Series index to numeric. Returns (x, y, is_numeric).
    If the index is non-numeric (e.g. '500ms'), returns string positions.
    """
    try:
        x = np.array([float(v.rstrip("ms").rstrip("s")) for v in series.index])
        return x, series.values.astype(float), True
    except (ValueError, AttributeError):
        return np.arange(len(series)), series.values.astype(float), False


# -------------------------------------------------------------------------
# Figure A: recovery time vs. parameter value (OPAT)
# -------------------------------------------------------------------------

def plot_figure_a(root: Path, out_path: Path):
    """
    Four panels: (a) CB θ, (b) Envoy b%, (c) Arolla r, (d) Arolla C.
    """
    panels = [
        ("cb-threshold",  "circuit-breaker",    "CB threshold θ (%)",    "#d62728", "s"),
        ("rb-budget",     "envoy-retry-budget", "Envoy budget b (%)",    "#ff7f0e", "D"),
        ("arolla-r",      "arolla",             "Deposit ratio r",       "#2ca02c", "o"),
        ("arolla-c",      "arolla",             "Bucket capacity C",     "#2ca02c", "o"),
    ]

    _apply_style()
    fig, axes = plt.subplots(1, 4, figsize=(18, 4.5), sharey=True)

    for ax, (sweep_name, policy, xlabel, color, marker) in zip(axes, panels):
        data = load_sweep(root, sweep_name)
        series = data.get(policy)

        if series is not None and not series.empty:
            x, y, is_numeric = _try_numeric_index(series)

            # Safe region shading: values where recovery < threshold
            safe_mask = y < NO_RECOVERY_THRESHOLD
            if safe_mask.any() and is_numeric:
                safe_x = x[safe_mask]
                ax.axvspan(safe_x.min(), safe_x.max(),
                           color=color, alpha=0.08, zorder=0)
                # "safe region" label
                mid_x = (safe_x.min() + safe_x.max()) / 2
                ax.text(mid_x, 2.0, "safe\nregion",
                        ha="center", va="bottom",
                        fontsize=9, color=color, alpha=0.6)

            if is_numeric:
                ax.plot(x, y, color=color, marker=marker, markersize=7,
                        linewidth=2, zorder=3)
                ax.set_xlabel(xlabel)
                # Log scale for Arolla r and C
                if "r" in sweep_name.split("-")[-1] or "c" in sweep_name.split("-")[-1]:
                    ax.set_xscale("log")
            else:
                ax.plot(range(len(series)), y, color=color, marker=marker,
                        markersize=7, linewidth=2, zorder=3)
                ax.set_xticks(range(len(series)))
                ax.set_xticklabels(series.index, rotation=45, ha="right")
                ax.set_xlabel(xlabel)
        else:
            ax.text(0.5, 0.5, f"no data\n({sweep_name})",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="gray")
            ax.set_xlabel(xlabel)

        # "no recovery" dashed line
        ax.axhline(NO_RECOVERY_THRESHOLD, color="gray", linestyle=":",
                    linewidth=1, alpha=0.7)
        if ax == axes[-1]:
            ax.text(ax.get_xlim()[1], NO_RECOVERY_THRESHOLD + 0.5,
                    "no recovery", ha="right", va="bottom",
                    fontsize=9, color="gray", fontstyle="italic")

        ax.grid(True, alpha=0.2, linewidth=0.5)

    axes[0].set_ylabel("Recovery time (s)")
    axes[0].set_ylim(0, NO_RECOVERY_SENTINEL + 3)

    # Panel labels
    for i, ax in enumerate(axes):
        ax.set_title(f"({chr(97+i)})", fontsize=12, loc="left", pad=4)

    fig.suptitle(
        "Figure A: Recovery time vs. parameter value "
        "(50% failure, 60s duration, defaults for other params)",
        fontsize=13, y=1.02,
    )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# -------------------------------------------------------------------------
# Figure B: recovery time vs. failure characteristics
# -------------------------------------------------------------------------

def plot_figure_b(root: Path, out_path: Path):
    """
    Two panels: (a) vary failure rate, (b) vary failure duration.
    All mechanisms at default parameters.
    """
    panels = [
        ("failure-rate",     "Failure rate (%)",    "(a) Vary failure rate (fixed 60s duration)"),
        ("failure-duration", "Failure duration (s)", "(b) Vary failure duration (fixed 50% rate)"),
    ]

    _apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), sharey=True)

    for ax, (sweep_name, xlabel, title) in zip(axes, panels):
        data = load_sweep(root, sweep_name)

        ordered = [p for p in POLICY_ORDER if p in data] + \
                  [p for p in data if p not in POLICY_ORDER]

        for policy in ordered:
            series = data[policy]
            x, y, is_numeric = _try_numeric_index(series)
            label = POLICY_LABELS.get(policy, policy)
            color = POLICY_COLORS.get(policy, "gray")
            marker = POLICY_MARKERS.get(policy, "o")

            linestyle = "--" if policy == "no-control" else "-"
            ax.plot(x, y, color=color, marker=marker, markersize=7,
                    linewidth=2, linestyle=linestyle, label=label, zorder=3)

        # "no recovery" dashed line
        ax.axhline(NO_RECOVERY_THRESHOLD, color="gray", linestyle=":",
                    linewidth=1, alpha=0.7)
        ax.text(ax.get_xlim()[1] if ax.get_xlim()[1] > 1 else 1,
                NO_RECOVERY_THRESHOLD + 0.5,
                "no recovery", ha="right", va="bottom",
                fontsize=9, color="gray", fontstyle="italic")

        ax.set_xlabel(xlabel)
        ax.set_title(title, fontsize=12, loc="left", pad=4)
        ax.grid(True, alpha=0.2, linewidth=0.5)
        ax.legend(loc="upper left", frameon=True, fancybox=False,
                  edgecolor="#888888", framealpha=0.95)

    axes[0].set_ylabel("Recovery time (s)")
    axes[0].set_ylim(0, NO_RECOVERY_SENTINEL + 3)

    fig.suptitle(
        "Figure B: Recovery time vs. failure characteristics "
        "(all mechanisms at default parameters)",
        fontsize=13, y=1.02,
    )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# -------------------------------------------------------------------------
# Ablation: mixed refill vs. single refill source
# -------------------------------------------------------------------------

def plot_ablation(root: Path, out_path: Path):
    """
    One panel: three curves (mixed, event-only, time-only) across
    failure rates.
    """
    configs = [
        ("ablation-mixed-default", "Mixed refill (default)", "#2ca02c", "-",  "o"),
        ("ablation-event-only",    "Event refill only (t=0)", "#1f77b4", "--", "^"),
        ("ablation-time-only",     "Time refill only (r=0)",  "#9467bd", "--", "v"),
    ]

    _apply_style()
    fig, ax = plt.subplots(figsize=(7, 5))

    for sweep_name, label, color, ls, marker in configs:
        data = load_sweep(root, sweep_name)
        series = data.get("arolla")
        if series is None or series.empty:
            ax.text(0.5, 0.5, f"no data\n({sweep_name})",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="gray")
            continue
        x, y, _ = _try_numeric_index(series)
        ax.plot(x, y, color=color, marker=marker, markersize=7,
                linewidth=2, linestyle=ls, label=label, zorder=3)

    ax.axhline(NO_RECOVERY_THRESHOLD, color="gray", linestyle=":",
                linewidth=1, alpha=0.7)
    ax.text(ax.get_xlim()[1] if ax.get_xlim()[1] > 1 else 90,
            NO_RECOVERY_THRESHOLD + 0.5,
            "no recovery", ha="right", va="bottom",
            fontsize=9, color="gray", fontstyle="italic")

    ax.set_xlabel("Failure rate (%)")
    ax.set_ylabel("Recovery time (s)")
    ax.set_ylim(0, NO_RECOVERY_SENTINEL + 3)
    ax.set_title("Ablation: mixed refill vs. single refill source",
                 fontsize=13, loc="left", pad=4)
    ax.grid(True, alpha=0.2, linewidth=0.5)
    ax.legend(loc="upper left", frameon=True, fancybox=False,
              edgecolor="#888888", framealpha=0.95)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

AVAILABLE_FIGURES = {
    "a":        "Figure A: parameter sensitivity (OPAT, 4 panels)",
    "b":        "Figure B: failure characteristics (2 panels)",
    "ablation": "Ablation: mixed refill vs. single refill source",
}


def main():
    parser = argparse.ArgumentParser(
        description="Plot sensitivity analysis figures from sweep outputs."
    )
    parser.add_argument("root", type=Path,
                        help="Sensitivity output root directory "
                             "(e.g. outputs/prototype/sensitivity/)")
    parser.add_argument("figures", nargs="*", default=[],
                        help="Which figures to plot: a, b, ablation. "
                             "Omit to plot all.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output directory for figures "
                             "(default: <root>/plots/)")
    # Fix argparse not liking empty nargs='*' with choices — use a
    # post-parse check instead.
    args = parser.parse_args()

    # Default to all figures if none specified.
    selected = set(args.figures) if args.figures else set(AVAILABLE_FIGURES.keys())
    # Validate
    for f in selected:
        if f not in AVAILABLE_FIGURES:
            parser.error(f"unknown figure: {f}. "
                         f"Choose from: {', '.join(AVAILABLE_FIGURES.keys())}")

    out_dir = args.out or args.root / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"sensitivity output root: {args.root}")
    print(f"figure output dir:       {out_dir}")
    print(f"figures to plot:         {', '.join(sorted(selected))}")
    print()

    if "a" in selected:
        plot_figure_a(args.root, out_dir / "fig-a-parameter-sensitivity.pdf")
    if "b" in selected:
        plot_figure_b(args.root, out_dir / "fig-b-failure-characteristics.pdf")
    if "ablation" in selected:
        plot_ablation(args.root, out_dir / "ablation-refill-source.pdf")

    print("\ndone.")


if __name__ == "__main__":
    main()
