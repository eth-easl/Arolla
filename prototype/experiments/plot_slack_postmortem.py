#!/usr/bin/env python3
"""
plot_slack_postmortem.py — client-observed success rate under
Slack 2022-02-22 reproduction, with and without Arolla.

Usage:
    python3 plot_slack_postmortem.py <no_control.csv> <arolla.csv> [out.pdf]

Each CSV must have columns: Timestamp, Success Rate (%).
The two runs are aligned so that t=0 is the first sample at which
success rate first drops below 100%.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

# Colors matching the rest of the paper's figures.
POLICY_COLORS = {
    "no-control": "#C94C4C",
    "arolla":     "#218B21",
}

# Linestyles matching the success-rate/latency plots in paper_plotting.py.
POLICY_LINESTYLES = {
    "no-control": (0, (6, 2)),    # long dashes
    "arolla":     "-",            # solid (hero)
}


def _paper_style():
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        10,
        "axes.labelsize":   10,
        "xtick.labelsize":  9,
        "ytick.labelsize":  9,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    0.8,
        "lines.linewidth":   1.5,
        "figure.dpi":        150,
    })


def load_aligned(csv_path: Path) -> pd.DataFrame:
    """Read a {Timestamp, Success Rate (%)} CSV and align t=0 to the
    first sample (start of the trace)."""
    df = pd.read_csv(csv_path)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    t0 = df["Timestamp"].iloc[0]
    df["t_sec"] = (df["Timestamp"] - t0).dt.total_seconds()
    return df


# Experiment timeline (seconds since trace start).
CACHE_REMOVED  = 120   # memcached deployment deleted
CACHE_RESTORED = 180   # memcached pods back up (but cold)


def plot(no_ctl_csv: Path, arolla_csv: Path, out_path: Path,
         x_window: tuple = (0, 400)):
    _paper_style()
    no_ctl = load_aligned(no_ctl_csv)
    arolla = load_aligned(arolla_csv)

    fig, ax = plt.subplots(figsize=(3.0, 2.0))

    # Shade the "cache down" window.
    ax.axvspan(CACHE_REMOVED, CACHE_RESTORED,
               color="#fce6e6", alpha=0.5, zorder=0)

    # Vertical markers at the two events.
    for t in (CACHE_REMOVED, CACHE_RESTORED):
        ax.axvline(x=t, color="#888888", linestyle="--",
                   linewidth=0.8, alpha=0.7, zorder=1)

    # Labels for the two events, each with a small arrow pointing at the
    # corresponding vline. Fan them out so they don't stack on top of
    # each other (the 60-second fault window is too narrow for that).
    arrow_kw = dict(arrowstyle="->", color="#888888",
                    lw=0.7, shrinkA=0, shrinkB=2)
    ax.annotate("cache removed",
                xy=(CACHE_REMOVED, 102),
                xytext=(CACHE_REMOVED + 20, 116),
                ha="right", va="bottom", fontsize=8,
                color="#555555", fontstyle="italic",
                arrowprops=arrow_kw)
    ax.annotate("cache restored",
                xy=(CACHE_RESTORED, 102),
                xytext=(CACHE_RESTORED + 4, 116),
                ha="left", va="bottom", fontsize=8,
                color="#555555", fontstyle="italic",
                arrowprops=arrow_kw)

    # Data.
    ax.plot(no_ctl["t_sec"], no_ctl["Success Rate (%)"],
            color=POLICY_COLORS["no-control"],
            linestyle=POLICY_LINESTYLES["no-control"],
            label="No control",
            linewidth=1.8, zorder=3)
    ax.plot(arolla["t_sec"], arolla["Success Rate (%)"],
            color=POLICY_COLORS["arolla"],
            linestyle=POLICY_LINESTYLES["arolla"],
            label="Arolla",
            linewidth=1.8, zorder=4)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Success rate (%)")
    ax.set_ylim(-5, 125)
    ax.set_xlim(*x_window)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02),
              ncol=2, frameon=False,
              handlelength=2.0, columnspacing=1.4,
              borderaxespad=0.0)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    no_ctl = Path(sys.argv[1])
    arolla = Path(sys.argv[2])
    out = (Path(sys.argv[3]) if len(sys.argv) > 3
           else Path("slack-postmortem-success-rate.pdf"))
    plot(no_ctl, arolla, out)


if __name__ == "__main__":
    main()
