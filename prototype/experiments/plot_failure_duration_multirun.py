#!/usr/bin/env python3
"""
Plot recovery time vs fault duration aggregated across multiple runs.

Scans <sweep_root> for run directories ending with a given RPS suffix,
aggregates recovery_sec from each run's summary.csv, and produces:
  (1) recovery-vs-fault-duration line plot with error bars (median + min/max)
  (2) box plot of recovery times per policy per fault duration

Usage:
  python3 plot_failure_duration_multirun.py <sweep_root> --rps 1200 [--out-dir DIR]

Example:
  python3 plot_failure_duration_multirun.py outputs/nsdi/failure_duration_sweep --rps 1200
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from analyze import (
    POLICY_ORDER, POLICY_LABELS, POLICY_COLORS, POLICY_COLORS_FILL,
    POLICY_MARKERS,
)

WORKLOAD = "post-cart-stress-open"
SWEEP_SUBDIR = "fault-duration"


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


def collect_data(
    sweep_root: Path,
    rps: int,
) -> Dict[str, Dict[float, List[Optional[float]]]]:
    """
    Collect recovery times from all run directories matching the RPS suffix.

    Returns: {policy: {fault_duration: [recovery_sec, ...]}}
    """
    suffix = f"_rps{rps}"
    data: Dict[str, Dict[float, List[Optional[float]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    run_dirs = sorted(
        d for d in sweep_root.iterdir()
        if d.is_dir() and d.name.endswith(suffix)
    )
    if not run_dirs:
        print(f"[error] no directories ending with '{suffix}' in {sweep_root}")
        sys.exit(1)

    print(f"Found {len(run_dirs)} runs matching *{suffix}:")
    for d in run_dirs:
        print(f"  {d.name}")

    for run_dir in run_dirs:
        fd_root = run_dir / WORKLOAD / SWEEP_SUBDIR
        if not fd_root.exists():
            print(f"  [skip] {run_dir.name}: no {WORKLOAD}/{SWEEP_SUBDIR}/")
            continue
        for val_dir in sorted(fd_root.iterdir()):
            if not val_dir.is_dir():
                continue
            try:
                fd_val = float(val_dir.name)
            except ValueError:
                continue
            summary = val_dir / "summary.csv"
            if not summary.exists():
                continue
            import csv
            with open(summary) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pol = row["policy"]
                    rec = row.get("recovery_sec", "")
                    try:
                        rec_val = float(rec)
                        if np.isnan(rec_val):
                            rec_val = None
                    except (ValueError, TypeError):
                        rec_val = None
                    data[pol][fd_val].append(rec_val)

    return dict(data)


def plot_recovery_with_errorbars(
    data: Dict[str, Dict[float, List[Optional[float]]]],
    out_path: Path,
):
    """Line plot of median recovery time with min/max error bars."""
    _paper_style()

    NEVER_Y = 200
    PLOT_MAX = 230
    arrow_offsets = {
        "no-control":         -2.0,
        "circuit-breaker":    -0.7,
        "envoy-retry-budget":  0.7,
        "arolla":              2.0,
    }

    x_values = sorted(set(x for pol_data in data.values() for x in pol_data))
    if not x_values:
        return

    ordered = [p for p in POLICY_ORDER if p in data] + \
              [p for p in data if p not in POLICY_ORDER]

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    # "No recovery" band
    ax.axhspan(NEVER_Y - 4, PLOT_MAX, color="#f5f5f5", zorder=0)
    ax.axhline(y=NEVER_Y - 4, color="#cccccc", linestyle="--", linewidth=0.8)

    for pol in ordered:
        pol_data = data[pol]
        xs = sorted(pol_data.keys())

        rec_xs, rec_medians, rec_lo, rec_hi = [], [], [], []
        never_xs = []

        for x in xs:
            values = pol_data[x]
            valid = [v for v in values if v is not None]
            if not valid:
                never_xs.append(x)
            else:
                # If majority of runs never recovered, mark as never
                if len(valid) < len(values) / 2:
                    never_xs.append(x)
                else:
                    median = float(np.median(valid))
                    rec_xs.append(x)
                    rec_medians.append(median)
                    rec_lo.append(median - min(valid))
                    rec_hi.append(max(valid) - median)

        marker = POLICY_MARKERS.get(pol, "o")
        color = POLICY_COLORS.get(pol, "gray")
        label = POLICY_LABELS.get(pol, pol)

        if rec_xs:
            ax.errorbar(
                rec_xs, rec_medians,
                yerr=[rec_lo, rec_hi],
                color=color, label=label,
                marker=marker, markersize=5, linewidth=1.5,
                capsize=3, capthick=1.0, elinewidth=1.0, zorder=4,
            )
        else:
            ax.plot([], [], color=color, label=label,
                    marker=marker, markersize=5, linewidth=1.5)

        if never_xs:
            offset = arrow_offsets.get(pol, 0)
            ax.scatter(never_xs, [NEVER_Y + offset] * len(never_xs),
                       marker=marker, s=40, color=color, zorder=5)
            if rec_xs:
                last_rec_x = rec_xs[-1]
                later_nevers = [nx for nx in never_xs if nx > last_rec_x]
                if later_nevers:
                    first_after = min(later_nevers)
                    ax.plot([last_rec_x, first_after],
                            [rec_medians[-1], NEVER_Y + offset],
                            color=color, linestyle="--", linewidth=1.2,
                            alpha=0.5, zorder=3)

    ax.text(x_values[0], NEVER_Y + 10, "no recovery", va="center", fontsize=10,
            color="#999999", fontstyle="italic")

    ax.set_xlabel("Fault duration (s)")
    ax.set_ylabel("Recovery time (s)")
    ax.set_xticks(x_values)
    ax.set_ylim(-1, PLOT_MAX)
    ax.grid(True, alpha=0.2, linewidth=0.5)

    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels,
               loc="lower center", bbox_to_anchor=(0.5, 0.85),
               ncol=2, frameon=False,
               handlelength=1.5, columnspacing=1.0,
               borderaxespad=0.0)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_recovery_boxplot(
    data: Dict[str, Dict[float, List[Optional[float]]]],
    out_path: Path,
):
    """Box plot of recovery times grouped by fault duration, one box per policy."""
    _paper_style()

    NEVER_Y = 200
    PLOT_MAX = 230

    x_values = sorted(set(x for pol_data in data.values() for x in pol_data))
    if not x_values:
        return

    ordered = [p for p in POLICY_ORDER if p in data] + \
              [p for p in data if p not in POLICY_ORDER]
    n_pol = len(ordered)

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    ax.axhspan(NEVER_Y - 4, PLOT_MAX, color="#f5f5f5", zorder=0)
    ax.axhline(y=NEVER_Y - 4, color="#cccccc", linestyle="--", linewidth=0.8)

    gaps = [x_values[i+1] - x_values[i] for i in range(len(x_values) - 1)]
    min_gap = min(gaps) if gaps else 5
    box_width = min_gap * 0.8 / n_pol
    arrow_offsets = {
        "no-control":         -3.0,
        "circuit-breaker":    -1.0,
        "envoy-retry-budget":  1.0,
        "arolla":              3.0,
    }

    for pol_idx, pol in enumerate(ordered):
        color = POLICY_COLORS.get(pol, "gray")
        fill = POLICY_COLORS_FILL.get(pol, "lightgray")
        marker = POLICY_MARKERS.get(pol, "o")
        positions = []
        box_data = []

        for fd_val in x_values:
            values = data.get(pol, {}).get(fd_val, [])
            valid = [v for v in values if v is not None]
            pos = fd_val + (pol_idx - (n_pol - 1) / 2) * box_width
            if not valid:
                offset = arrow_offsets.get(pol, 0)
                ax.scatter([pos], [NEVER_Y + offset], marker=marker,
                           s=30, color=color, zorder=5)
            else:
                positions.append(pos)
                box_data.append(valid)

        if box_data:
            bp = ax.boxplot(
                box_data, positions=positions,
                widths=box_width * 0.9, patch_artist=True,
                showfliers=True,
                flierprops=dict(marker=".", markersize=3,
                                markerfacecolor=color, markeredgecolor="none"),
                medianprops=dict(color="black", linewidth=1.2),
                whiskerprops=dict(linewidth=0.8, color=color),
                capprops=dict(linewidth=0.8, color=color),
            )
            for patch in bp["boxes"]:
                patch.set_facecolor(fill)
                patch.set_edgecolor(color)
                patch.set_linewidth(0.8)

    ax.text(x_values[0], NEVER_Y + 10, "no recovery", va="center", fontsize=10,
            color="#999999", fontstyle="italic")

    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=POLICY_COLORS_FILL.get(pol, "lightgray"),
              edgecolor=POLICY_COLORS.get(pol, "gray"),
              label=POLICY_LABELS.get(pol, pol))
        for pol in ordered
    ]
    fig.legend(handles=legend_handles,
               loc="lower center", bbox_to_anchor=(0.5, 1.0),
               ncol=2, frameon=False,
               handlelength=1.5, columnspacing=1.0,
               borderaxespad=0.0)

    ax.set_xticks(x_values)
    ax.set_xticklabels([str(int(v)) for v in x_values])
    ax.set_xlabel("Fault duration (s)")
    ax.set_ylabel("Recovery time (s)")
    ax.set_ylim(-1, PLOT_MAX)
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_recovery_strip(
    data: Dict[str, Dict[float, List[Optional[float]]]],
    out_path: Path,
):
    """Strip plot: each run is a dot, non-recovered runs land in the
    'no recovery' band.  Median shown as a short horizontal bar."""
    _paper_style()

    NEVER_Y = 200
    PLOT_MAX = 230

    x_values = sorted(set(x for pol_data in data.values() for x in pol_data))
    if not x_values:
        return

    ordered = [p for p in POLICY_ORDER if p in data] + \
              [p for p in data if p not in POLICY_ORDER]
    n_pol = len(ordered)

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    # "No recovery" shaded band
    ax.axhspan(NEVER_Y - 4, PLOT_MAX, color="#f5f5f5", zorder=0)
    ax.axhline(y=NEVER_Y - 4, color="#cccccc", linestyle="--", linewidth=0.8)

    # Layout: each policy gets a lane within each fault-duration group.
    gaps = [x_values[i+1] - x_values[i] for i in range(len(x_values) - 1)]
    min_gap = min(gaps) if gaps else 5
    lane_width = min_gap * 0.8 / n_pol
    jitter_half = lane_width * 0.3  # horizontal jitter range

    rng = np.random.default_rng(42)  # deterministic jitter

    # Vertical offsets for never-recovered markers so they don't overlap.
    never_offsets = {
        "no-control":         -3.0,
        "circuit-breaker":    -1.0,
        "envoy-retry-budget":  1.0,
        "arolla":              3.0,
    }

    for pol_idx, pol in enumerate(ordered):
        color = POLICY_COLORS.get(pol, "gray")
        marker = POLICY_MARKERS.get(pol, "o")

        for fd_val in x_values:
            values = data.get(pol, {}).get(fd_val, [])
            if not values:
                continue
            center = fd_val + (pol_idx - (n_pol - 1) / 2) * lane_width

            recovered = [v for v in values if v is not None]
            n_failed = sum(1 for v in values if v is None)

            # Plot recovered runs as filled dots with jitter
            if recovered:
                jx = center + rng.uniform(-jitter_half, jitter_half,
                                          size=len(recovered))
                ax.scatter(jx, recovered, marker=marker, s=18,
                           color=color, alpha=0.7, zorder=4,
                           edgecolors="none")
                # Median bar
                med = float(np.median(recovered))
                bar_half = lane_width * 0.35
                ax.plot([center - bar_half, center + bar_half],
                        [med, med],
                        color=color, linewidth=2.0, solid_capstyle="round",
                        zorder=5)

            # Plot non-recovered runs as hollow dots in the band
            if n_failed > 0:
                offset = never_offsets.get(pol, 0)
                jx = center + rng.uniform(-jitter_half, jitter_half,
                                          size=n_failed)
                ax.scatter(jx, [NEVER_Y + offset] * n_failed,
                           marker=marker, s=18, zorder=5,
                           facecolors="none", edgecolors=color,
                           linewidths=0.8, alpha=0.8)

    ax.text(x_values[0], NEVER_Y + 10, "no recovery", va="center", fontsize=10,
            color="#999999", fontstyle="italic")

    # Legend: filled dot = recovered, hollow dot = not recovered
    import matplotlib.lines as mlines
    legend_handles = []
    for pol in ordered:
        legend_handles.append(
            mlines.Line2D([], [], color=POLICY_COLORS.get(pol, "gray"),
                          marker=POLICY_MARKERS.get(pol, "o"),
                          markersize=5, linestyle="None",
                          label=POLICY_LABELS.get(pol, pol))
        )
    fig.legend(handles=legend_handles,
               loc="lower center", bbox_to_anchor=(0.5, 1.0),
               ncol=2, frameon=False,
               handlelength=1.0, columnspacing=1.0,
               borderaxespad=0.0)

    ax.set_xticks(x_values)
    ax.set_xticklabels([str(int(v)) for v in x_values])
    ax.set_xlabel("Fault duration (s)")
    ax.set_ylabel("Recovery time (s)")
    ax.set_ylim(-1, PLOT_MAX)
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sweep_root", type=Path,
                        help="Root directory containing timestamped run dirs")
    parser.add_argument("--rps", type=int, required=True,
                        help="RPS value to filter runs (matches dirs ending with _rps<N>)")
    parser.add_argument("--step", type=int, default=None,
                        help="Only keep fault durations that are multiples of STEP "
                             "(e.g. --step 10 keeps 10,20,30,...)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory for plots (default: <sweep_root>)")
    args = parser.parse_args()

    out_dir = args.out_dir or args.sweep_root
    out_dir.mkdir(parents=True, exist_ok=True)

    data = collect_data(args.sweep_root, args.rps)

    # Filter to multiples of --step if given
    if args.step:
        for pol in data:
            data[pol] = {fd: vals for fd, vals in data[pol].items()
                         if int(fd) % args.step == 0}
    if not data:
        print("[error] no data collected")
        sys.exit(1)

    # Print summary
    for pol in POLICY_ORDER:
        if pol not in data:
            continue
        print(f"\n  {POLICY_LABELS.get(pol, pol)}:")
        for fd in sorted(data[pol].keys()):
            vals = data[pol][fd]
            valid = [v for v in vals if v is not None]
            if valid:
                print(f"    fd={int(fd):3d}s: median={np.median(valid):.1f}s "
                      f"[{min(valid):.1f}, {max(valid):.1f}] "
                      f"({len(valid)}/{len(vals)} recovered)")
            else:
                print(f"    fd={int(fd):3d}s: never recovered ({len(vals)} runs)")

    plot_recovery_with_errorbars(
        data,
        out_dir / f"recovery-vs-fault-duration-rps{args.rps}.pdf",
    )
    plot_recovery_boxplot(
        data,
        out_dir / f"recovery-boxplot-rps{args.rps}.pdf",
    )
    plot_recovery_strip(
        data,
        out_dir / f"recovery-strip-rps{args.rps}.pdf",
    )


if __name__ == "__main__":
    main()
