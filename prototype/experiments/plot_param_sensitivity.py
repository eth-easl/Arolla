#!/usr/bin/env python3
"""
plot_param_sensitivity.py — general multi-panel parameter sensitivity figure.

Reads a sweep YAML config for panel metadata (names, labels, defaults) and
the corresponding output directories for data, then produces a paper-ready
figure with one panel per swept parameter.

Works for any mechanism: Arolla, circuit-breaker, or retry-budget.

Usage:
  python3 plot_param_sensitivity.py <sweep_config.yaml> <sweep_root> [output.pdf]

  <sweep_root> should contain one subdirectory per sweep name (e.g.
  arolla-r/, cb-threshold/, rb-budget/), each with per-value subdirectories
  containing summary.csv.

Examples:
  # Arolla (3 panels):
  python3 plot_param_sensitivity.py \\
      sweeps/arolla-sensitivity.yaml \\
      outputs/prototype/arolla-sensitivity/20260413/post-cart-stress-open

  # Circuit breaker (4 panels):
  python3 plot_param_sensitivity.py \\
      sweeps/cb-sensitivity.yaml \\
      outputs/prototype/cb-sensitivity/20260413/post-cart-stress-open

  # Retry budget (2 panels):
  python3 plot_param_sensitivity.py \\
      sweeps/rb-sensitivity.yaml \\
      outputs/prototype/rb-sensitivity/20260413/post-cart-stress-open
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

# Per-policy colors (from analyze.py).
POLICY_COLORS = {
    "no-control":                  "#C83232",
    "circuit-breaker":             "#E68C14",
    "circuit-breaker-consecutive": "#E68C14",  # same family as CB
    "envoy-retry-budget":          "#3264B4",
    "arolla":                      "#218B21",
}
POLICY_MARKERS = {
    "no-control":                  "s",
    "circuit-breaker":             "^",
    "circuit-breaker-consecutive": "v",
    "envoy-retry-budget":          "D",
    "arolla":                      "o",
}

# Defaults for "no recovery" sentinel band. Overridden when --ymax is used.
_DEFAULT_NEVER_Y = 70
_DEFAULT_PLOT_MAX = 78


def _paper_style():
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        11,
        "axes.labelsize":   12,
        "axes.titlesize":   12,
        "xtick.labelsize":  9,
        "ytick.labelsize":  10,
        "legend.fontsize":  10,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    0.8,
        "lines.linewidth":   1.5,
        "figure.dpi":        150,
    })


# ---------------------------------------------------------------------------
# Value parsing (handles numeric values and duration strings like "5s")
# ---------------------------------------------------------------------------

def parse_numeric(s: str) -> float:
    """Parse a string that may be numeric or a duration (e.g. '5s', '500ms')."""
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    raise ValueError(f"Cannot parse as numeric: {s!r}")


def fmt_tick(v: Any) -> str:
    """Format a parameter value for tick labels, preserving the original form."""
    s = str(v)
    # If it's a clean integer float like 10.0, show as "10"
    try:
        f = float(s)
        if f == int(f) and "." not in s and "s" not in s:
            return str(int(f))
    except ValueError:
        pass
    return s


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_sweep_data(
    sweep_dir: Path, policy: str,
) -> Tuple[List[str], List[Optional[float]]]:
    """
    Load recovery times from a sweep output directory.

    Returns (raw_values, recovery_times) where raw_values[i] is the
    directory name (string) and recovery_times[i] is None if the system
    never recovered.
    """
    entries: List[Tuple[float, str, Optional[float]]] = []

    for val_dir in sorted(sweep_dir.iterdir()):
        if not val_dir.is_dir():
            continue
        summary = val_dir / "summary.csv"
        if not summary.exists():
            continue

        raw_label = val_dir.name
        try:
            sort_key = parse_numeric(raw_label)
        except ValueError:
            continue

        df = pd.read_csv(summary)
        rows = df[df["policy"] == policy]
        if rows.empty:
            continue

        rec = rows.iloc[0].get("recovery_sec")
        try:
            rec_val = float(rec)
            if np.isnan(rec_val):
                rec_val = None
        except (ValueError, TypeError):
            rec_val = None

        entries.append((sort_key, raw_label, rec_val))

    entries.sort(key=lambda e: e[0])
    raw_values = [e[1] for e in entries]
    recovery_times = [e[2] for e in entries]
    return raw_values, recovery_times


# ---------------------------------------------------------------------------
# YAML config parsing
# ---------------------------------------------------------------------------

def load_sweep_config(config_path: Path) -> List[Dict]:
    """Parse sweep YAML and return list of sweep definitions."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    defaults = config.get("defaults", {})
    sweeps = []
    for s in config["sweeps"]:
        base = dict(defaults)
        base.update(s.get("base", {}))
        s["base"] = base
        sweeps.append(s)
    return sweeps


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_sensitivity(
    sweeps: List[Dict],
    sweep_root: Path,
    out_path: Path,
    ymax: Optional[float] = None,
):
    """Generate the multi-panel sensitivity figure."""
    _paper_style()

    # Compute "no recovery" band position. When ymax is set, place the
    # band at the top of the visible range; when ymax is None, use the
    # hardcoded defaults (suitable for recovery times up to ~60s).
    if ymax is not None:
        PLOT_MAX = ymax * 1.12
        NEVER_Y = ymax * 1.02
    else:
        PLOT_MAX = _DEFAULT_PLOT_MAX
        NEVER_Y = _DEFAULT_NEVER_Y

    n_panels = len(sweeps)
    if n_panels == 0:
        print("[error] no sweeps defined", file=sys.stderr)
        return

    # Layout: 1xN for 1-3 panels, 2x2 for 4 panels, 2xN for 5+.
    if n_panels <= 3:
        nrows, ncols = 1, n_panels
        fig_w = max(3.5, 3.5 * n_panels)
        fig_h = 2.5
    elif n_panels == 4:
        nrows, ncols = 2, 2
        fig_w, fig_h = 7.0, 5.0
    else:
        ncols = math.ceil(n_panels / 2)
        nrows = 2
        fig_w = 3.5 * ncols
        fig_h = 5.0

    fig, axes_raw = plt.subplots(
        nrows, ncols, figsize=(fig_w, fig_h),
        sharey=True, squeeze=False,
    )
    axes = axes_raw.flatten()

    # Hide unused panels.
    for i in range(n_panels, len(axes)):
        axes[i].set_visible(False)

    panel_labels = [
        f"({chr(ord('a') + i)})" for i in range(n_panels)
    ]

    for idx, sweep in enumerate(sweeps):
        ax = axes[idx]
        sweep_name = sweep["name"]
        policy = sweep["policies"][0]
        param = sweep["parameter"]
        param_name = param["name"]
        default_raw = param.get("default")

        color = POLICY_COLORS.get(policy, "#333333")
        marker = POLICY_MARKERS.get(policy, "o")

        sweep_dir = sweep_root / sweep_name
        if not sweep_dir.is_dir():
            print(f"  [skip] {sweep_dir} not found", file=sys.stderr)
            ax.set_visible(False)
            continue

        raw_values, recovery_times = load_sweep_data(sweep_dir, policy)
        if not raw_values:
            print(f"  [skip] no data in {sweep_dir}", file=sys.stderr)
            ax.set_visible(False)
            continue

        # Evenly-spaced x positions (categorical).
        x_pos = np.arange(len(raw_values))

        # Separate recovered vs. never-recovered.
        rec_x, rec_y = [], []
        never_x = []
        for i, rt in enumerate(recovery_times):
            if rt is None:
                never_x.append(i)
            else:
                rec_x.append(i)
                rec_y.append(rt)

        # "No recovery" band.
        ax.axhspan(NEVER_Y - 4, PLOT_MAX, color="#f5f5f5", zorder=0)
        ax.axhline(y=NEVER_Y - 4, color="#cccccc", linestyle="--",
                    linewidth=0.8)

        # Recovered points.
        if rec_x:
            ax.plot(rec_x, rec_y, color=color, marker=marker,
                    markersize=6, linewidth=1.8, zorder=4)

        # Never-recovered points.
        if never_x:
            ax.scatter(never_x, [NEVER_Y] * len(never_x),
                       marker=marker, s=50, color=color,
                       zorder=5, edgecolors="white", linewidths=0.5)
            if rec_x:
                last_rec_idx = rec_x[-1]
                later = [nx for nx in never_x if nx > last_rec_idx]
                if later:
                    ax.plot([last_rec_idx, later[0]],
                            [rec_y[-1], NEVER_Y],
                            color=color, linestyle="--",
                            linewidth=1.2, alpha=0.5, zorder=3)

        # Highlight default value.
        if default_raw is not None:
            default_str = str(default_raw)
            if default_str in raw_values:
                di = raw_values.index(default_str)
                ax.axvline(x=di, color="#cccccc", linestyle=":",
                           linewidth=0.8, zorder=1)
                y_star = recovery_times[di] if recovery_times[di] is not None else NEVER_Y
                ax.plot(di, y_star, marker="*", markersize=14,
                        color=color, zorder=6,
                        markeredgecolor="white", markeredgewidth=0.8)

        # X-axis.
        ax.set_xticks(x_pos)
        ax.set_xticklabels([fmt_tick(v) for v in raw_values])
        ax.set_xlabel(param_name.replace("_", " "))
        ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
        ax.set_title(panel_labels[idx], fontsize=11, fontweight="bold", pad=8)

    # "No recovery" label on first visible panel.
    for ax in axes[:n_panels]:
        if ax.get_visible():
            ax.text(0.02, NEVER_Y, "no recovery", va="center", fontsize=8,
                    color="#999999", fontstyle="italic",
                    transform=ax.get_yaxis_transform())
            break

    # Y-axis label on leftmost panel(s).
    for r in range(nrows):
        axes_raw[r, 0].set_ylabel("Recovery time (s)")
    y_lo = -0.5 if (ymax is not None and ymax <= 15) else -1
    axes[0].set_ylim(y_lo, PLOT_MAX)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Multi-panel parameter sensitivity figure.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", type=Path, help="Sweep YAML config file")
    parser.add_argument("sweep_root", type=Path, help="Directory with sweep output")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="Output PDF path (default: <sweep_root>/<config_stem>.pdf)")
    parser.add_argument("--ymax", type=float, default=None,
                        help="Y-axis maximum (recovery time in seconds)")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"[error] config not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    if not args.sweep_root.is_dir():
        print(f"[error] not a directory: {args.sweep_root}", file=sys.stderr)
        sys.exit(1)

    out_path = args.output or (args.sweep_root / f"{args.config.stem}.pdf")
    sweeps = load_sweep_config(args.config)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"figure -> {out_path}")
    plot_sensitivity(sweeps, args.sweep_root, out_path, ymax=args.ymax)


if __name__ == "__main__":
    main()
