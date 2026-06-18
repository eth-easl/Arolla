#!/usr/bin/env python3
"""
plot_arolla_sensitivity_multirun.py — Arolla parameter sensitivity figure,
aggregated across multiple runs with a shaded min/max band.

Scans <sweep_root> for run directories ending with `_fault<N>` and
aggregates recovery_sec from each run's summary.csv. Produces a single
3-panel figure (r, t, C) with:
  - median line across runs
  - min/max shaded band
  - "no recovery" markers for values where most runs never recovered

Usage:
  python3 plot_arolla_sensitivity_multirun.py <sweep_root> --fault 25 [--out PDF]

Example:
  python3 plot_arolla_sensitivity_multirun.py \\
      outputs/nsdi/arolla-sensitivity --fault 25
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

AROLLA_COLOR = "#218B21"
AROLLA_FILL = "#8FCF8F"
AROLLA_MARKER = "o"

# Per-fault plot styles. Same color keeps the Arolla policy identity;
# linestyle+marker distinguish fault levels. Extend this mapping if you
# want to compare more fault rates.
FAULT_STYLES = {
    10: dict(color=AROLLA_COLOR, fill=AROLLA_FILL, marker="o", linestyle="-"),
    20: dict(color=AROLLA_COLOR, fill=AROLLA_FILL, marker="^", linestyle="-."),
    25: dict(color=AROLLA_COLOR, fill=AROLLA_FILL, marker="s", linestyle="--"),
    50: dict(color=AROLLA_COLOR, fill=AROLLA_FILL, marker="D", linestyle=":"),
}

WORKLOAD = "post-cart-stress-open"

# (sweep_dir_name, x_label, default_value)
PANELS = [
    ("arolla-r", r"$r$ (event-refill rate)", 0.1),
    ("arolla-t", r"$t$ (time-refill rate)", 1.0),
    ("arolla-c", r"$C$ (bucket capacity)", 20.0),
]

NEVER_Y = 200
PLOT_MAX = 230


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


def _recovery_from_summary(summary: Path) -> Optional[float]:
    if not summary.exists():
        return None
    df = pd.read_csv(summary)
    rows = df[df["policy"] == "arolla"]
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


def load_sweep(sweep_dir: Path) -> Dict[float, Optional[float]]:
    """Load {value: recovery_sec} for one run's sweep directory."""
    out: Dict[float, Optional[float]] = {}
    if not sweep_dir.is_dir():
        return out
    for val_dir in sorted(sweep_dir.iterdir()):
        if not val_dir.is_dir():
            continue
        try:
            val = float(val_dir.name)
        except ValueError:
            continue
        out[val] = _recovery_from_summary(val_dir / "summary.csv")
    return out


def collect_panel(
    run_dirs: List[Path], sweep_name: str,
) -> Dict[float, List[Optional[float]]]:
    """Aggregate {value: [rec_run1, rec_run2, ...]} across runs for one panel."""
    data: Dict[float, List[Optional[float]]] = defaultdict(list)
    for run_dir in run_dirs:
        sweep_dir = run_dir / WORKLOAD / sweep_name
        for val, rec in load_sweep(sweep_dir).items():
            data[val].append(rec)
    return dict(data)


def _fmt_val(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return str(v)


def _plot_panel(ax,
                per_fault_data: Dict[Optional[int], Dict[float, List[Optional[float]]]],
                default_val: float, xlabel: str):
    """Plot one panel with median line + min/max shaded band per fault group."""
    # Union of x-values across all fault groups so shared ticks line up.
    all_values = sorted({
        v for data in per_fault_data.values() for v in data.keys()
    })
    if not all_values:
        ax.set_visible(False)
        return

    x_pos = np.arange(len(all_values))
    value_to_i = {v: i for i, v in enumerate(all_values)}

    # No-recovery band.
    ax.axhspan(NEVER_Y - 5, PLOT_MAX, color="#f5f5f5", zorder=0)
    ax.axhline(y=NEVER_Y - 5, color="#cccccc",
               linestyle="--", linewidth=0.8)

    default_i = all_values.index(default_val) if default_val in all_values else None

    for fault in sorted(per_fault_data.keys(),
                        key=lambda x: (x is None, x)):
        data = per_fault_data[fault]
        style = (FAULT_STYLES.get(fault)
                 if fault is not None
                 else dict(color=AROLLA_COLOR, fill=AROLLA_FILL,
                           marker=AROLLA_MARKER, linestyle="-"))
        if style is None:
            style = dict(color=AROLLA_COLOR, fill=AROLLA_FILL,
                         marker=AROLLA_MARKER, linestyle="-")

        rec_x, rec_med, rec_lo, rec_hi = [], [], [], []
        never_x = []
        for v, recs in data.items():
            valid = [r for r in recs if r is not None]
            i = value_to_i[v]
            if not valid or len(valid) < len(recs) / 2:
                never_x.append(i)
            else:
                rec_x.append(i)
                rec_med.append(float(np.median(valid)))
                rec_lo.append(float(min(valid)))
                rec_hi.append(float(max(valid)))

        # Sort by x for line plotting.
        if rec_x:
            order = np.argsort(rec_x)
            rec_x = [rec_x[k] for k in order]
            rec_med = [rec_med[k] for k in order]
            rec_lo = [rec_lo[k] for k in order]
            rec_hi = [rec_hi[k] for k in order]

            label = None if fault is None else f"{fault}% failure"
            ax.fill_between(rec_x, rec_lo, rec_hi,
                            color=style["fill"], alpha=0.22, zorder=2,
                            linewidth=0)
            ax.plot(rec_x, rec_med, color=style["color"],
                    marker=style["marker"], linestyle=style["linestyle"],
                    markersize=5, linewidth=1.6, zorder=4, label=label)

        if never_x:
            ax.scatter(never_x, [NEVER_Y] * len(never_x),
                       marker=style["marker"], s=50, color=style["color"],
                       zorder=5, edgecolors="white", linewidths=0.5)

        # Star at the default value for this fault group.
        if default_i is not None:
            recs = data.get(default_val, [])
            valid_default = [r for r in recs if r is not None]
            if valid_default and len(valid_default) >= len(recs) / 2:
                y_star = float(np.median(valid_default))
                ax.plot(default_i, y_star, marker="*", markersize=13,
                        color=style["color"], zorder=6,
                        markeredgecolor="white", markeredgewidth=0.8)

    ax.set_xticks(x_pos)
    ax.set_xticklabels(
        [_fmt_val(v) for v in all_values],
        rotation=45, ha="right", rotation_mode="anchor",
    )
    ax.set_xlim(-0.5, len(all_values) - 0.5)
    ax.set_ylim(-5, PLOT_MAX)
    ax.set_yticks([0, 100, 200])
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.set_xlabel(xlabel)


def plot(per_fault_runs: Dict[Optional[int], List[Path]], out_path: Path):
    _paper_style()

    fig, axes = plt.subplots(
        1, 3,
        figsize=(6.6, 1.8),
        gridspec_kw={"wspace": 0.12},
        sharey=True,
    )

    any_plotted = False
    for col, (sweep_name, xlabel, default_val) in enumerate(PANELS):
        per_fault_data: Dict[Optional[int], Dict[float, List[Optional[float]]]] = {}
        for fault, runs in per_fault_runs.items():
            per_fault_data[fault] = collect_panel(runs, sweep_name)
        if not any(per_fault_data.values()):
            print(f"  [skip] no data for {sweep_name}", file=sys.stderr)
            axes[col].set_visible(False)
            continue
        _plot_panel(axes[col], per_fault_data, default_val, xlabel)
        any_plotted = True

    if not any_plotted:
        print("[error] no panels had data", file=sys.stderr)
        return

    axes[0].set_ylabel("Recovery time (s)")

    # Only add a legend when comparing multiple fault groups.
    if len([f for f in per_fault_runs.keys() if f is not None]) > 1:
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels,
                   loc="lower center", bbox_to_anchor=(0.5, 0.85),
                   ncol=2, frameon=False,
                   handlelength=2.2, columnspacing=1.6,
                   borderaxespad=0.0)

    for col in range(3):
        if axes[col].get_visible():
            axes[col].text(
                -0.45, NEVER_Y + (PLOT_MAX - NEVER_Y) / 2,
                "no recovery",
                va="center", fontsize=9, color="#999999", fontstyle="italic",
            )

    fig.align_xlabels(axes[:])
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def _summarize(per_fault_runs: Dict[Optional[int], List[Path]]) -> None:
    for fault, run_dirs in per_fault_runs.items():
        label = "all runs" if fault is None else f"fault{fault}"
        print(f"\n[{label}]")
        for sweep_name, _, _ in PANELS:
            data = collect_panel(run_dirs, sweep_name)
            print(f"  {sweep_name}:")
            for v in sorted(data.keys()):
                recs = data[v]
                valid = [r for r in recs if r is not None]
                if valid:
                    print(f"    {v}: median={np.median(valid):.1f}s "
                          f"[{min(valid):.1f}, {max(valid):.1f}] "
                          f"({len(valid)}/{len(recs)} recovered)")
                else:
                    print(f"    {v}: never recovered ({len(recs)} runs)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("sweep_root", type=Path,
                        help="Root containing timestamped run dirs")
    parser.add_argument("--fault", type=int, nargs="+", required=True,
                        help="Fault percent suffix(es) to match "
                             "(matches dirs ending with _fault<N>); "
                             "pass multiple values (e.g. --fault 10 25) "
                             "to compare fault levels on the same plot "
                             "with distinct linestyles")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output PDF path (default: <sweep_root>/"
                             "arolla-sensitivity-fault<N>-multirun.pdf)")
    args = parser.parse_args()

    per_fault_runs: Dict[Optional[int], List[Path]] = {}
    for f in args.fault:
        suffix = f"_fault{f}"
        runs = sorted(
            d for d in args.sweep_root.iterdir()
            if d.is_dir() and d.name.endswith(suffix)
        )
        if not runs:
            print(f"[warn] no run dirs matching *_fault{f} "
                  f"in {args.sweep_root}", file=sys.stderr)
            continue
        per_fault_runs[f] = runs

    if not per_fault_runs:
        print(f"[error] no run dirs matching any of {args.fault} "
              f"in {args.sweep_root}", file=sys.stderr)
        sys.exit(1)

    total = sum(len(v) for v in per_fault_runs.values())
    print(f"Found {total} runs across "
          f"{len(per_fault_runs)} fault group(s):")
    for fault, runs in per_fault_runs.items():
        for d in runs:
            print(f"  [fault{fault}] {d.name}")

    _summarize(per_fault_runs)

    if args.out is None:
        stem = ("arolla-sensitivity-"
                + "-".join(f"fault{f}" for f in sorted(args.fault))
                + "-multirun.pdf")
        args.out = args.sweep_root / stem
    args.out.parent.mkdir(parents=True, exist_ok=True)
    plot(per_fault_runs, args.out)


if __name__ == "__main__":
    main()
