#!/usr/bin/env python3
"""
plot_arolla_sensitivity.py — 3-panel Arolla parameter sensitivity figure.

Reads sweep output from run_sweep.sh and produces a paper-ready 3-panel
figure showing recovery time vs. each Arolla token-bucket parameter:

  (a) Recovery time vs. r   (event-refill rate)
  (b) Recovery time vs. t   (time-refill rate)
  (c) Recovery time vs. C   (bucket capacity)

Usage:
  python3 plot_arolla_sensitivity.py <sweep_root> [output.pdf]

  <sweep_root> should contain arolla-r/, arolla-t/, arolla-c/ directories,
  each with per-value subdirectories containing summary.csv.

Examples:
  # After running the sweep:
  ./run_sweep.sh sweeps/arolla-sensitivity.yaml

  # Plot all three panels:
  python3 plot_arolla_sensitivity.py \\
      outputs/prototype/arolla-sensitivity/20260413/post-cart-stress-open

  # Explicit output path:
  python3 plot_arolla_sensitivity.py \\
      outputs/prototype/arolla-sensitivity/20260413/post-cart-stress-open \\
      figures/arolla-sensitivity.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

AROLLA_COLOR = "#218B21"       # goodputgreen (from analyze.py)
AROLLA_MARKER = "o"

# Panel definitions: (sweep_dir_name, x_label, default_value)
PANELS = [
    ("arolla-r", r"$r$ (event-refill rate)", 0.1),
    ("arolla-t", r"$t$ (time-refill rate)", 1.0),
    ("arolla-c", r"$C$ (bucket capacity)", 20.0),
]

# "No recovery" sentinel: points that never recovered are plotted here.
NEVER_Y = 300
PLOT_MAX = 330

# Broken y-axis: bottom row shows the recovered range [0, BREAK_LOW];
# top row shows the "no recovery" band [BREAK_HIGH, PLOT_MAX]. Collapses
# the empty middle so the figure height can be reduced without losing
# either region.
BREAK_LOW = 150       # top of the lower axis
BREAK_HIGH = 295      # bottom of the upper axis


def _paper_style():
    """One-shot rcParams for paper figures (matches paper_plotting.py)."""
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        11,
        "axes.labelsize":   12,
        "axes.titlesize":   12,
        "xtick.labelsize":  10,
        "ytick.labelsize":  10,
        "legend.fontsize":  10,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    0.8,
        "lines.linewidth":   1.5,
        "figure.dpi":        150,
    })


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_sweep(sweep_dir: Path) -> Tuple[List[float], List[Optional[float]]]:
    """
    Load recovery times from a sweep directory.

    Returns (values, recovery_times) where recovery_times[i] is None
    if the system never recovered for values[i].
    """
    entries: List[Tuple[float, Optional[float]]] = []

    for val_dir in sorted(sweep_dir.iterdir()):
        if not val_dir.is_dir():
            continue
        summary = val_dir / "summary.csv"
        if not summary.exists():
            continue
        try:
            x_val = float(val_dir.name)
        except ValueError:
            continue

        df = pd.read_csv(summary)
        arolla_rows = df[df["policy"] == "arolla"]
        if arolla_rows.empty:
            continue

        rec = arolla_rows.iloc[0].get("recovery_sec")
        try:
            rec_val = float(rec)
            if np.isnan(rec_val):
                rec_val = None
        except (ValueError, TypeError):
            rec_val = None

        entries.append((x_val, rec_val))

    # Sort by parameter value.
    entries.sort(key=lambda e: e[0])
    values = [e[0] for e in entries]
    recovery_times = [e[1] for e in entries]
    return values, recovery_times


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _fmt_val(v: float) -> str:
    """Format a parameter value for tick labels."""
    if v == int(v):
        return str(int(v))
    return str(v)


def _draw_break_marks(ax_top, ax_bot):
    """Draw diagonal slash marks at the broken-axis seam on both axes."""
    d = 0.015
    kw = dict(color="k", clip_on=False, linewidth=0.8)
    ax_top.plot((-d, +d), (-d, +d), transform=ax_top.transAxes, **kw)
    ax_top.plot((1 - d, 1 + d), (-d, +d), transform=ax_top.transAxes, **kw)
    ax_bot.plot((-d, +d), (1 - d, 1 + d), transform=ax_bot.transAxes, **kw)
    ax_bot.plot((1 - d, 1 + d), (1 - d, 1 + d), transform=ax_bot.transAxes, **kw)


def _plot_panel(ax_top, ax_bot, values, recovery_times, default_val, xlabel):
    """Draw one panel across a (top, bottom) pair of axes with a broken y."""
    x_pos = np.arange(len(values))

    rec_x, rec_y = [], []
    never_x = []
    for i, rt in enumerate(recovery_times):
        if rt is None:
            never_x.append(i)
        else:
            rec_x.append(i)
            rec_y.append(rt)

    # Recovered points live in the bottom axis.
    if rec_x:
        ax_bot.plot(rec_x, rec_y, color=AROLLA_COLOR, marker=AROLLA_MARKER,
                    markersize=6, linewidth=1.8, zorder=4)

    # Never-recovered points live in the top axis.
    if never_x:
        ax_top.scatter(never_x, [NEVER_Y] * len(never_x),
                       marker=AROLLA_MARKER, s=50, color=AROLLA_COLOR,
                       zorder=5, edgecolors="white", linewidths=0.5)

    # Default-value highlight.
    if default_val in values:
        di = values.index(default_val)
        for a in (ax_top, ax_bot):
            a.axvline(x=di, color="#cccccc", linestyle=":",
                      linewidth=0.8, zorder=1)
        default_rt = recovery_times[di]
        if default_rt is not None:
            ax_bot.plot(di, default_rt, marker="*", markersize=14,
                        color=AROLLA_COLOR, zorder=6,
                        markeredgecolor="white", markeredgewidth=0.8)
        else:
            ax_top.plot(di, NEVER_Y, marker="*", markersize=14,
                        color=AROLLA_COLOR, zorder=6,
                        markeredgecolor="white", markeredgewidth=0.8)

    for a in (ax_top, ax_bot):
        a.set_xticks(x_pos)
        a.set_xlim(-0.5, len(values) - 0.5)
        a.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax_top.set_xticklabels([])
    # Rotate 45° with ha="right": each label's right edge anchors at the tick,
    # giving clean diagonal placement. The steeper angle (vs 30°) minimizes
    # the perceived vertical offset between labels of different character widths.
    ax_bot.set_xticklabels(
        [_fmt_val(v) for v in values],
        rotation=45, ha="right", rotation_mode="anchor",
    )
    ax_bot.set_xlabel(xlabel)


def plot_sensitivity(sweep_root: Path, out_path: Path):
    """Generate the 3-panel sensitivity figure with a broken y-axis."""
    _paper_style()

    # 2x3 grid: top row = "no recovery" band (short), bottom row = recovered
    # region (tall). Height ratio 1:5 collapses the empty middle.
    fig, axes = plt.subplots(
        2, 3,
        figsize=(6.6, 1.8),
        gridspec_kw={"height_ratios": [1, 5], "hspace": 0.08, "wspace": 0.12},
        sharex="col",
    )

    for col, (sweep_name, xlabel, default_val) in enumerate(PANELS):
        ax_top = axes[0, col]
        ax_bot = axes[1, col]

        sweep_dir = sweep_root / sweep_name
        if not sweep_dir.is_dir():
            print(f"  [skip] {sweep_dir} not found", file=sys.stderr)
            ax_top.set_visible(False)
            ax_bot.set_visible(False)
            continue

        values, recovery_times = load_sweep(sweep_dir)
        if not values:
            print(f"  [skip] no data in {sweep_dir}", file=sys.stderr)
            ax_top.set_visible(False)
            ax_bot.set_visible(False)
            continue

        # Shade the "no recovery" region in the top panel.
        ax_top.axhspan(BREAK_HIGH, PLOT_MAX, color="#f5f5f5", zorder=0)

        _plot_panel(ax_top, ax_bot, values, recovery_times,
                    default_val, xlabel)

        # Y-limits split across the break.
        ax_top.set_ylim(BREAK_HIGH, PLOT_MAX)
        ax_bot.set_ylim(-1, BREAK_LOW)

        # Hide interior spines and the x-ticks on the top axis.
        ax_top.spines["bottom"].set_visible(False)
        ax_bot.spines["top"].set_visible(False)
        ax_top.tick_params(bottom=False)

        # Diagonal break marks at the seam.
        _draw_break_marks(ax_top, ax_bot)

        # Y-tick labels only on the leftmost column.
        if col > 0:
            ax_top.tick_params(labelleft=False)
            ax_bot.tick_params(labelleft=False)

    # Only show the sentinel tick in the top row.
    for col in range(3):
        axes[0, col].set_yticks([NEVER_Y])

    axes[1, 0].set_ylabel("Recovery time (s)")

    # "No recovery" annotation on every visible top panel.
    for col in range(3):
        if axes[0, col].get_visible():
            axes[0, col].text(
                0.02, NEVER_Y + 10, "no recovery",
                va="center", fontsize=10, color="#999999", fontstyle="italic",
                transform=axes[0, col].get_yaxis_transform(),
            )

    # Align x-labels across panels. With rotated tick labels of different
    # character widths, each panel's tick-label block has a different height,
    # which pushes xlabels to different y-positions. align_xlabels pins them
    # to a common y.
    fig.align_xlabels(axes[1, :])

    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    sweep_root = Path(sys.argv[1])
    if len(sys.argv) > 2:
        out_path = Path(sys.argv[2])
    else:
        out_path = sweep_root / "arolla-sensitivity.pdf"

    if not sweep_root.is_dir():
        print(f"[error] not a directory: {sweep_root}", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"figure -> {out_path}")
    plot_sensitivity(sweep_root, out_path)


if __name__ == "__main__":
    main()
