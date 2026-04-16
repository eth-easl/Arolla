#!/usr/bin/env python3
"""
plot_fairness_two_panel.py — render the §6.4 flagship figure.

Two-panel grouped bar chart comparing arolla vs arolla-fairness across:
  (a) Same-RPS scenario (isolates the cost-scaling mechanism)
  (b) Different-RPS scenario (tests robustness under volume asymmetry)

Each panel shows per-group share/demand ratio (admitted_share / volume_share)
during the fault window, with a 1× baseline line for "proportional admission".

Usage:
    plot_fairness_two_panel.py <same_rps_run_dir> <diff_rps_run_dir> [--output <path>]

Run directories must contain arolla/ and arolla-fairness/ subdirectories with
client-metrics/ CSVs and timeline.json (standard run-experiment.sh output).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


# Default group classification by client name. Override with --polite/--aggressive.
DEFAULT_POLITE = ("client1", "client2", "client3")
DEFAULT_AGGRESSIVE = ("client4", "client5", "client6")

POLICY_LABELS = {
    "arolla": "Arolla",
    "arolla-fairness": "Arolla-fairness",
}
POLICY_ORDER = ["arolla", "arolla-fairness"]

POLITE_COLOR = "#5B9BD5"
AGGRESSIVE_COLOR = "#FF6B6B"
BASELINE_COLOR = "#222222"


def _apply_style() -> None:
    # Paper-style: larger fonts for single-column inclusion.
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        12,
        "axes.labelsize":   16,
        "axes.titlesize":   14,
        "xtick.labelsize":  14,
        "ytick.labelsize":  14,
        "legend.fontsize":  12,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    1.0,
        "figure.dpi":        110,
    })


def load_policy_retries(policy_dir: Path) -> Optional[Tuple[pd.DataFrame, float, float]]:
    """
    Load client-metrics CSVs for a single policy and return
    (df_with_is_retry, t_fault_start, t_fault_end) or None if missing.
    """
    tl_path = policy_dir / "timeline.json"
    if not tl_path.exists():
        return None
    tl = json.loads(tl_path.read_text())
    t_fs = float(tl["t_fault_start"])
    t_fe = float(tl["t_fault_end"])

    frames = []
    for csv in sorted((policy_dir / "client-metrics").glob("*.csv")):
        try:
            frames.append(pd.read_csv(csv))
        except Exception as e:
            print(f"[warn] could not read {csv}: {e}", file=sys.stderr)
    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)
    df["is_retry"] = df["is_retry"].astype(str).str.lower().isin(["true", "1", "t", "yes"])
    return df, t_fs, t_fe


def compute_metrics(
    run_dir: Path,
    polite: Tuple[str, ...],
    aggressive: Tuple[str, ...],
    metric: str,
) -> Dict[str, Dict[str, float]]:
    """
    For each policy in run_dir, compute the requested metric per group.

    metric = "sod"    → admitted_share / volume_share (dimensionless ratio)
           = "count"  → number of admitted retries
           = "rate"   → admission rate (%) = admitted / retries × 100

    Returns {policy: {"polite": value, "aggressive": value,
                      "polite_retries": N, "aggressive_retries": N,
                      "polite_admitted": N, "aggressive_admitted": N}}
    """
    results: Dict[str, Dict[str, float]] = {}
    for policy in POLICY_ORDER:
        pdir = run_dir / policy
        if not pdir.is_dir():
            continue
        loaded = load_policy_retries(pdir)
        if loaded is None:
            continue
        df, t_fs, t_fe = loaded

        fault = df[(df["timestamp"] >= t_fs) & (df["timestamp"] < t_fe)]
        first = fault[~fault["is_retry"]]
        retries = fault[fault["is_retry"]]
        admitted = retries[(retries["status"] > 0) & (retries["status"] != 429)]

        total_first = len(first)
        total_ret = len(retries)
        total_adm = len(admitted)

        p_first = len(first[first["profile"].isin(polite)])
        a_first = len(first[first["profile"].isin(aggressive)])
        p_ret = len(retries[retries["profile"].isin(polite)])
        a_ret = len(retries[retries["profile"].isin(aggressive)])
        p_adm = len(admitted[admitted["profile"].isin(polite)])
        a_adm = len(admitted[admitted["profile"].isin(aggressive)])

        def value_for(g_first: int, g_ret: int, g_adm: int) -> float:
            if metric == "count":
                return float(g_adm)
            if metric == "rate":
                return (g_adm / g_ret * 100.0) if g_ret > 0 else float("nan")
            # share-over-demand: admitted share / first-attempt (request) share.
            # Uses request volume as the demand baseline — retry volume depends
            # on the policy's rejection behavior and is therefore not a clean
            # baseline for "what fair admission would look like."
            if total_first == 0 or total_adm == 0 or g_first == 0:
                return float("nan")
            return (g_adm / total_adm) / (g_first / total_first)

        results[policy] = {
            "polite":              value_for(p_first, p_ret, p_adm),
            "aggressive":          value_for(a_first, a_ret, a_adm),
            "polite_first":        float(p_first),
            "aggressive_first":    float(a_first),
            "polite_retries":      float(p_ret),
            "aggressive_retries":  float(a_ret),
            "polite_admitted":     float(p_adm),
            "aggressive_admitted": float(a_adm),
        }
    return results


def _format_bar_label(val: float, metric: str) -> str:
    if np.isnan(val) or np.isinf(val):
        return "—"
    if metric == "count":
        return f"{int(round(val))}"
    if metric == "rate":
        return f"{val:.2f}%"
    return f"{val:.2f}×"


def _ylabel_for(metric: str) -> str:
    return {
        "count": "# admitted retries",
        "rate":  "Retry admission rate (%)",
        "sod":   "Admitted share / Request-volume share",
    }.get(metric, metric)


def _draw_panel(
    ax: plt.Axes,
    results: Dict[str, Dict[str, float]],
    ymax: float,
    metric: str,
    show_ylabel: bool = True,
    show_legend: bool = True,
) -> None:
    """Render a single fairness panel (bars per policy, two groups)."""
    policies = [p for p in POLICY_ORDER if p in results]
    if not policies:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
        return

    x = np.arange(len(policies))
    bar_width = 0.22

    polite_vals = [results[p]["polite"] for p in policies]
    agg_vals = [results[p]["aggressive"] for p in policies]

    ax.bar(x - bar_width/2, polite_vals, bar_width,
           label="Polite ", color=POLITE_COLOR,
           alpha=0.88, edgecolor="white", linewidth=0.6)
    ax.bar(x + bar_width/2, agg_vals, bar_width,
           label="Aggressive", color=AGGRESSIVE_COLOR,
           alpha=0.88, edgecolor="white", linewidth=0.6)

    # For the share/demand metric, add the 1× proportional baseline.
    if metric == "sod":
        ax.axhline(1.0, color=BASELINE_COLOR, linestyle=(0, (6, 3)),
                   linewidth=1.6, alpha=1.0, zorder=3,
                   label="Proportional (1×)")

    # For the count metric, overlay proportional-to-request-volume targets
    # (not labelled in the legend — called out in the figure caption instead).
    if metric == "count":
        for i, policy in enumerate(policies):
            r = results[policy]
            total_first = r["polite_first"] + r["aggressive_first"]
            total_adm = r["polite_admitted"] + r["aggressive_admitted"]
            if total_first <= 0 or total_adm <= 0:
                continue
            prop_p = total_adm * (r["polite_first"] / total_first)
            prop_a = total_adm * (r["aggressive_first"] / total_first)
            # Slightly wider than the bar so the tick visibly protrudes.
            overhang = bar_width * 0.55
            for xc, prop in [(x[i] - bar_width/2, prop_p),
                             (x[i] + bar_width/2, prop_a)]:
                ax.hlines(prop, xc - overhang, xc + overhang,
                          colors=BASELINE_COLOR, linestyles=(0, (6, 3)),
                          linewidth=1.6, alpha=1.0, zorder=3)

    # Annotate each bar with its numeric value.
    for i, _ in enumerate(policies):
        for xp, val in [(x[i] - bar_width/2, polite_vals[i]),
                        (x[i] + bar_width/2, agg_vals[i])]:
            label = _format_bar_label(val, metric)
            y = 0 if (np.isnan(val) or np.isinf(val)) else val
            ax.text(xp, y + ymax * 0.02, label,
                    ha="center", va="bottom",
                    fontsize=12, color="#333333")

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    ax.set_ylim(0, ymax * 1.05)
    if show_ylabel:
        ax.set_ylabel(_ylabel_for(metric))
    if show_legend:
        # Legend outside the axes, above the plot.
        ax.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, 1.02),
            ncol=2,
            frameon=True,
            fancybox=False,
            edgecolor="#CFCCCC",
            framealpha=0.95,
            fontsize=12,
        )


def _compute_ymax(
    scenarios: Dict[str, Dict[str, Dict[str, float]]],
    metric: str,
) -> float:
    """Compute a shared y-axis upper bound across all scenarios."""
    display_keys = ("polite", "aggressive")
    all_vals: list = []
    for r in scenarios.values():
        for d in r.values():
            for k in display_keys:
                v = d.get(k, float("nan"))
                if not (np.isnan(v) or np.isinf(v)):
                    all_vals.append(v)
    # For count, also include proportional-target heights.
    if metric == "count":
        for r in scenarios.values():
            for d in r.values():
                total_first = d.get("polite_first", 0) + d.get("aggressive_first", 0)
                total_adm = d.get("polite_admitted", 0) + d.get("aggressive_admitted", 0)
                if total_first <= 0 or total_adm <= 0:
                    continue
                for k in ("polite_first", "aggressive_first"):
                    all_vals.append(total_adm * (d[k] / total_first))
    floor = [1.0] if metric == "sod" else [0.0]
    return max(all_vals + floor)


def _render_single(
    results: Dict[str, Dict[str, float]],
    metric: str,
    ymax: float,
    out_path: Path,
) -> None:
    """Render a single figure (one scenario) to out_path."""
    _apply_style()
    fig, ax = plt.subplots(figsize=(4, 2.8))
    _draw_panel(ax, results, ymax=ymax, metric=metric,
                show_ylabel=True, show_legend=True)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("same_rps", type=Path,
                        help="Path to same-RPS run directory")
    parser.add_argument("diff_rps", type=Path,
                        help="Path to different-RPS run directory")
    parser.add_argument("--same-output", type=Path, default=None,
                        help="Output PDF for same-RPS figure "
                             "(default: fairness-same-rps-<metric>.pdf)")
    parser.add_argument("--diff-output", type=Path, default=None,
                        help="Output PDF for diff-RPS figure "
                             "(default: fairness-diff-rps-<metric>.pdf)")
    parser.add_argument("--metric", "-m", default="count",
                        choices=["count", "rate", "sod"],
                        help="count = raw admitted retries (default); "
                             "rate = admission rate %%; "
                             "sod = admitted share / request-volume share")
    parser.add_argument("--polite", nargs="+", default=list(DEFAULT_POLITE),
                        help="Client names to classify as polite")
    parser.add_argument("--aggressive", nargs="+", default=list(DEFAULT_AGGRESSIVE),
                        help="Client names to classify as aggressive")
    args = parser.parse_args()

    polite = tuple(args.polite)
    aggressive = tuple(args.aggressive)
    metric = args.metric

    same = compute_metrics(args.same_rps, polite, aggressive, metric)
    diff = compute_metrics(args.diff_rps, polite, aggressive, metric)

    if not same and not diff:
        print("[error] no policy data found in either run directory", file=sys.stderr)
        return 1

    # Shared y-axis across the two output figures for visual comparability.
    ymax = _compute_ymax({"same": same, "diff": diff}, metric)

    if same:
        out = args.same_output or Path(f"fairness-same-rps-{metric}.pdf")
        _render_single(same, metric, ymax, out)
    if diff:
        out = args.diff_output or Path(f"fairness-diff-rps-{metric}.pdf")
        _render_single(diff, metric, ymax, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
