#!/usr/bin/env python3
"""
paper_plotting.py — Generate paper-ready figures from a single experiment run.

Usage:
  python3 paper_plotting.py <run_dir> [output_dir]

Example:
  python3 paper_plotting.py outputs/nsdi/post-cart-stress-open_20260412_165901_star
  python3 paper_plotting.py outputs/nsdi/post-cart-stress-open_20260412_165901_star outputs/nsdi/paper_figs

Produces:
  success-rate.pdf      — End-user success rate vs time
  chain-retry-log.pdf   — Retries by service (log scale, grouped bar)
  latency-ts-p50.pdf    — p50 latency vs time
  latency-ts-p99.pdf    — p99 latency vs time
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict

import matplotlib.pyplot as plt
import numpy as np

# Import data-loading functions from analyze.py.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from analyze import (
    POLICY_ORDER, POLICY_LABELS, POLICY_COLORS, POLICY_COLORS_FILL,
    POLICY_MARKERS, _CHAIN_SERVICE_ORDER, _CHAIN_SERVICE_COLORS,
    process_policy, _fault_band_x,
)

# ---------------------------------------------------------------------------
# Paper figure style
# ---------------------------------------------------------------------------

def _paper_style():
    # Sized for 3-across layout in a 2-column paper (~7in text width).
    # At 3.5in canvas width, LaTeX scales to ~2.3in. Fonts are set so
    # they render at ~8-9pt after scaling.
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
# Success rate vs time
# ---------------------------------------------------------------------------

def plot_success_rate(runs, experiment, out_path):
    _paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs]

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    for policy in ordered:
        ts = runs[policy].get("success_rate")
        if ts is None or ts.empty:
            continue
        ax.plot(ts.index, ts.values,
                color=POLICY_COLORS[policy],
                label=POLICY_LABELS[policy],
                linewidth=2.2)

    if fault_sec > 0:
        ax.axvspan(fault_start_x, fault_end_x, color="lightgray", alpha=0.55, zorder=0)
        ax.text((fault_start_x + fault_end_x) / 2, 101, "fault",
                ha="center", va="top", fontsize=9, fontstyle="italic", color="#555555")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Success rate (%)")
    ax.set_ylim(-5, 105)
    if total > 0:
        ax.set_xlim(0, total)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
    ax.grid(True, alpha=0.15, linewidth=0.5)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Chain retry log-scale grouped bar
# ---------------------------------------------------------------------------

def plot_chain_retry_log(runs, out_path):
    _paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs]
    if not ordered:
        return

    per_policy: Dict[str, Dict[str, int]] = {}
    for policy in ordered:
        deltas = runs[policy].get("run_retry_deltas") or {}
        per_callee: Dict[str, int] = {}
        for (_caller, upstream), counters in deltas.items():
            v = counters.get("upstream_rq_retry", 0)
            if v > 0:
                per_callee[upstream] = per_callee.get(upstream, 0) + v
        per_policy[policy] = per_callee

    all_callees = set()
    for p in per_policy.values():
        all_callees.update(k for k, v in p.items() if v > 0)
    if not all_callees:
        return

    svc_order = [s for s in _CHAIN_SERVICE_ORDER if s in all_callees]
    extras = sorted(s for s in all_callees if s not in _CHAIN_SERVICE_ORDER)
    services = svc_order + extras

    svc_short = {
        "gateway": "Gateway", "frontend": "Frontend",
        "cartservice": "Cart", "productcatalogservice": "ProductCatalog",
        "checkoutservice": "Checkout", "paymentservice": "Payment",
        "currencyservice": "Currency", "shippingservice": "Shipping",
        "emailservice": "Email", "recommendationservice": "Recommend.",
        "adservice": "Ad",
    }

    n_svc = len(services)
    n_pol = len(ordered)
    bar_width = 0.8 / n_pol
    x = np.arange(n_svc)

    def _fmt(v):
        if v >= 1_000_000: return f"{v/1e6:.1f}M"
        if v >= 1_000: return f"{v/1e3:.0f}K"
        return f"{v:.0f}"

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    for i, policy in enumerate(ordered):
        heights = [max(per_policy[policy].get(svc, 0), 0.1) for svc in services]
        ax.bar(
            x + i * bar_width - 0.4 + bar_width / 2,
            heights,
            width=bar_width,
            color=POLICY_COLORS_FILL.get(policy, "lightgray"),
            edgecolor="none",
            label=POLICY_LABELS.get(policy, policy),
        )
        for j, h in enumerate(heights):
            real_h = per_policy[policy].get(services[j], 0)
            if real_h > 0:
                ax.text(x[j] + i * bar_width - 0.4 + bar_width / 2,
                        real_h, _fmt(real_h),
                        ha="center", va="bottom", fontsize=7)

    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xticklabels([svc_short.get(s, s) for s in services])
    ax.set_xlabel("Service")
    ax.set_ylabel("Retries received")
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Latency timeseries (p50 or p99)
# ---------------------------------------------------------------------------

def plot_latency_ts(runs, experiment, out_path, percentile="p50", log_y=True):
    _paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs]

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    any_plotted = False
    for policy in ordered:
        lat_ts = runs[policy].get("client_latency_ts") or {}
        ts = lat_ts.get(percentile)
        if ts is None or ts.empty:
            continue
        ax.plot(ts.index, ts.values,
                color=POLICY_COLORS[policy],
                label=POLICY_LABELS[policy],
                linewidth=2.0)
        any_plotted = True

    if not any_plotted:
        plt.close(fig)
        return

    if fault_sec > 0:
        ax.axvspan(fault_start_x, fault_end_x, color="lightgray", alpha=0.55, zorder=0)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel(f"Latency {percentile} (ms)")
    if log_y:
        ax.set_yscale("log")
    if total > 0:
        ax.set_xlim(0, total)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
    ax.grid(True, alpha=0.15, linewidth=0.5)
    fig.tight_layout()
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

    run_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else run_dir / "paper_figs"
    out_dir.mkdir(parents=True, exist_ok=True)

    experiment = json.loads((run_dir / "experiment.json").read_text())

    # Load each policy's data using analyze.py's process_policy.
    runs = {}
    ordered = [p for p in POLICY_ORDER if (run_dir / p).exists()]
    for policy in ordered:
        result = process_policy(run_dir / policy, experiment)
        if result is not None:
            runs[policy] = result

    if not runs:
        print("[error] no policy data loaded", file=sys.stderr)
        sys.exit(1)

    print(f"paper figures → {out_dir}/")
    plot_success_rate(runs, experiment, out_dir / "success-rate.pdf")
    plot_chain_retry_log(runs, out_dir / "chain-retry-log.pdf")
    plot_latency_ts(runs, experiment, out_dir / "latency-ts-p50-log.pdf", "p50", log_y=True)
    plot_latency_ts(runs, experiment, out_dir / "latency-ts-p50-linear.pdf", "p50", log_y=False)
    plot_latency_ts(runs, experiment, out_dir / "latency-ts-p99-log.pdf", "p99", log_y=True)
    plot_latency_ts(runs, experiment, out_dir / "latency-ts-p99-linear.pdf", "p99", log_y=False)


if __name__ == "__main__":
    main()
