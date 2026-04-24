#!/usr/bin/env python3
"""
plot_rb_sensitivity.py — Envoy retry-budget parameter sensitivity figure.

Reads the rb-grid sweep output and produces a 2-panel sensitivity figure
showing recovery time vs. each retry-budget parameter, with the other
parameter fixed at its default:

  (a) Recovery time vs. budget_percent  (min_retry_concurrency = 3)
  (b) Recovery time vs. min_retry_concurrency  (budget_percent = 20)

Usage:
  python3 plot_rb_sensitivity.py <sweep_root> [output.pdf]

  <sweep_root> should contain budget_percent=X__min_retry_concurrency=Y
  subdirectories (as produced by the rb-grid sweep), each with a
  summary.csv.

Example:
  python3 plot_rb_sensitivity.py \\
      outputs/nsdi/rb-grid/20260415_214854_fault10/post-cart-stress-open/rb-grid
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

RB_COLOR = "#3264B4"   # calmblue (matches analyze.py POLICY_COLORS)
RB_MARKER = "D"

# Fixed (default) values for each panel's held-constant parameter.
DEFAULT_MRC = 3       # held constant in the budget_percent panel
DEFAULT_BP = 20.0     # held constant in the min_retry_concurrency panel

# "No recovery" sentinel height.
NEVER_Y = 200
PLOT_MAX = 230

# Broken y-axis: bottom row shows the recovered range [0, BREAK_LOW];
# top row shows the "no recovery" band [BREAK_HIGH, PLOT_MAX]. Collapses
# the empty middle so the figure height can be reduced without losing
# either region.
BREAK_LOW = 180       # top of the lower axis
BREAK_HIGH = 200      # bottom of the upper axis


def _paper_style():
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

_GRID_DIR_RE = re.compile(
    r"^budget_percent=(?P<bp>[\d.]+)__min_retry_concurrency=(?P<mrc>\d+)$"
)


def _recovery_from_summary(summary: Path) -> Optional[float]:
    """Return envoy-retry-budget's recovery_sec from a summary.csv, or None."""
    if not summary.exists():
        return None
    df = pd.read_csv(summary)
    rows = df[df["policy"] == "envoy-retry-budget"]
    if rows.empty:
        return None
    rec = rows.iloc[0].get("recovery_sec")
    try:
        rec_val = float(rec)
        if np.isnan(rec_val):
            return None
        return rec_val
    except (ValueError, TypeError):
        return None


def load_sweep_1d(sweep_dir: Path, value_type=float
                  ) -> Tuple[List, List[Optional[float]]]:
    """Load a 1D sensitivity sweep: <sweep_dir>/<value>/summary.csv."""
    entries: List[Tuple[float, Optional[float]]] = []
    if not sweep_dir.is_dir():
        return [], []
    for val_dir in sorted(sweep_dir.iterdir()):
        if not val_dir.is_dir():
            continue
        try:
            val = value_type(val_dir.name)
        except ValueError:
            continue
        rec = _recovery_from_summary(val_dir / "summary.csv")
        entries.append((val, rec))
    entries.sort(key=lambda e: e[0])
    return [e[0] for e in entries], [e[1] for e in entries]


def load_grid(sweep_root: Path) -> Dict[Tuple[float, int], Optional[float]]:
    """{(bp, mrc): recovery_sec} from a 2D grid sweep layout."""
    data: Dict[Tuple[float, int], Optional[float]] = {}
    for val_dir in sorted(sweep_root.iterdir()):
        if not val_dir.is_dir():
            continue
        m = _GRID_DIR_RE.match(val_dir.name)
        if not m:
            continue
        bp = float(m.group("bp"))
        mrc = int(m.group("mrc"))
        data[(bp, mrc)] = _recovery_from_summary(val_dir / "summary.csv")
    return data


def slice_by_bp(
    data: Dict[Tuple[float, int], Optional[float]],
    mrc_fixed: int,
) -> Tuple[List[float], List[Optional[float]]]:
    """Values along budget_percent axis at mrc = mrc_fixed."""
    pts = [(bp, rec) for (bp, mrc), rec in data.items() if mrc == mrc_fixed]
    pts.sort(key=lambda e: e[0])
    return [bp for bp, _ in pts], [rec for _, rec in pts]


def slice_by_mrc(
    data: Dict[Tuple[float, int], Optional[float]],
    bp_fixed: float,
) -> Tuple[List[int], List[Optional[float]]]:
    """Values along min_retry_concurrency axis at bp = bp_fixed."""
    pts = [(mrc, rec) for (bp, mrc), rec in data.items() if bp == bp_fixed]
    pts.sort(key=lambda e: e[0])
    return [mrc for mrc, _ in pts], [rec for _, rec in pts]


def load_slices(sweep_root: Path):
    """Return ((bp_vals, bp_recs), (mrc_vals, mrc_recs)) from either layout.

    Prefers the 1D sweep layout (rb-budget/ + rb-min-concurrency/) if
    present, otherwise falls back to the 2D grid layout.
    """
    rb_budget_dir = sweep_root / "rb-budget"
    rb_mrc_dir = sweep_root / "rb-min-concurrency"
    if rb_budget_dir.is_dir() and rb_mrc_dir.is_dir():
        bp_vals, bp_recs = load_sweep_1d(rb_budget_dir, value_type=float)
        mrc_vals, mrc_recs = load_sweep_1d(rb_mrc_dir, value_type=int)
        return (bp_vals, bp_recs), (mrc_vals, mrc_recs)
    data = load_grid(sweep_root)
    if data:
        bp_vals, bp_recs = slice_by_bp(data, DEFAULT_MRC)
        mrc_vals, mrc_recs = slice_by_mrc(data, DEFAULT_BP)
        return (bp_vals, bp_recs), (mrc_vals, mrc_recs)
    return ([], []), ([], [])


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _fmt_bp(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return str(v)


def _draw_break_marks(ax_top, ax_bot):
    """Diagonal slash marks at the broken-axis seam on both axes."""
    d = 0.015
    kw = dict(color="k", clip_on=False, linewidth=0.8)
    ax_top.plot((-d, +d), (-d, +d), transform=ax_top.transAxes, **kw)
    ax_top.plot((1 - d, 1 + d), (-d, +d), transform=ax_top.transAxes, **kw)
    ax_bot.plot((-d, +d), (1 - d, 1 + d), transform=ax_bot.transAxes, **kw)
    ax_bot.plot((1 - d, 1 + d), (1 - d, 1 + d), transform=ax_bot.transAxes, **kw)


def _plot_panel(ax, values, recs, xlabel, default_val, label_values_fn):
    """Draw one panel on a single axis.

    Recovered points are plotted at their recovery-time values; runs that
    never recovered are marked at the sentinel NEVER_Y inside a shaded
    'no recovery' band at the top.
    """
    x_pos = np.arange(len(values))

    rec_x, rec_y, never_x = [], [], []
    for i, r in enumerate(recs):
        if r is None:
            never_x.append(i)
        else:
            rec_x.append(i)
            rec_y.append(r)

    # Shaded no-recovery band at the top of the axis.
    ax.axhspan(NEVER_Y - 5, PLOT_MAX, color="#f5f5f5", zorder=0)
    ax.axhline(y=NEVER_Y - 5, color="#cccccc",
               linestyle="--", linewidth=0.8)

    # Recovered line.
    if rec_x:
        ax.plot(rec_x, rec_y, color=RB_COLOR, marker=RB_MARKER,
                markersize=6, linewidth=1.8, zorder=4)

    # No-recovery markers.
    if never_x:
        ax.scatter(never_x, [NEVER_Y] * len(never_x),
                   marker=RB_MARKER, s=60, color=RB_COLOR,
                   zorder=5, edgecolors="white", linewidths=0.5)

    # Default-value highlight (vertical dotted line + gold star).
    if default_val in values:
        di = values.index(default_val)
        ax.axvline(x=di, color="#cccccc", linestyle=":",
                   linewidth=0.8, zorder=1)
        default_rt = recs[di]
        y_star = default_rt if default_rt is not None else NEVER_Y
        ax.plot(di, y_star, marker="*", markersize=14,
                color=RB_COLOR, zorder=6,
                markeredgecolor="white", markeredgewidth=0.8)

    ax.set_xticks(x_pos)
    ax.set_xticklabels(
        label_values_fn(values),
        rotation=45, ha="right", rotation_mode="anchor",
    )
    ax.set_xlim(-0.5, len(values) - 0.5)
    ax.set_ylim(-5, PLOT_MAX)
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.set_xlabel(xlabel)


def plot_sensitivity(sweep_root: Path, out_path: Path):
    _paper_style()
    (bp_vals, bp_recs), (mrc_vals, mrc_recs) = load_slices(sweep_root)

    if not bp_vals and not mrc_vals:
        print(f"[error] no data under {sweep_root}", file=sys.stderr)
        return
    if not bp_vals or not mrc_vals:
        print(f"[error] missing slices (bp={len(bp_vals)},"
              f" mrc={len(mrc_vals)})", file=sys.stderr)
        return

    fig, axes = plt.subplots(
        1, 2,
        figsize=(4.4, 1.8),
        gridspec_kw={"wspace": 0.18},
        sharey=True,
    )

    panels = [
        (bp_vals, bp_recs, "budget (%)", DEFAULT_BP,
         lambda vs: [_fmt_bp(v) for v in vs]),
        (mrc_vals, mrc_recs, "retry concurrency", DEFAULT_MRC,
         lambda vs: [str(v) for v in vs]),
    ]

    for col, (values, recs, xlabel, default_val, label_fn) in enumerate(panels):
        _plot_panel(axes[col], values, recs, xlabel,
                    default_val, label_fn)

    axes[0].set_ylabel("Recovery time (s)")

    # "No recovery" annotation in both panels, inside the shaded band.
    for col in range(2):
        axes[col].text(
            -0.45, NEVER_Y + (PLOT_MAX - NEVER_Y) / 2,
            "no recovery",
            va="center", fontsize=9, color="#999999", fontstyle="italic",
        )

    # Align xlabels horizontally so both sit at the same y-position.
    fig.align_xlabels(axes[:])

    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sweep_root = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else sweep_root / "rb-sensitivity.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plot_sensitivity(sweep_root, out_path)


if __name__ == "__main__":
    main()
