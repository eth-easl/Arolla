#!/usr/bin/env python3
"""
paper_plotting.py — Generate paper-ready figures.

Usage:
  # Single experiment run:
  python3 paper_plotting.py <run_dir> [output_dir]

  # Sweep plots (recovery vs parameter):
  python3 paper_plotting.py --sweep-rps <sweep_root> [output_dir]
  python3 paper_plotting.py --sweep-failure-rate <sweep_root> [output_dir]
  python3 paper_plotting.py --sweep-fault-duration <sweep_root> [output_dir]

Examples:
  python3 paper_plotting.py outputs/nsdi/post-cart-stress-open_20260412_165901_star
  python3 paper_plotting.py --sweep-rps outputs/nsdi/rps_sweep/combined
  python3 paper_plotting.py --sweep-failure-rate outputs/nsdi/failure_rate_sweep/combined
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


def _scenario_xticks(total: float):
    """Return evenly-spaced x-axis ticks for timeline plots.

    Shared across all timeline plots (success-rate, latency-p50, latency-p99)
    so tick positions line up across the figures. Without this, matplotlib
    picks ticks based on plot area width, which differs between plots due to
    log-axis tick labels being wider than linear ones.
    """
    if total <= 0:
        return None
    # Aim for 4-7 ticks. Prefer "nice" step values in reading-friendly order:
    # 25, 50, 100, 250, 500 (base-5 multiples) before 20, 200 (base-2 multiples).
    for step in [25, 50, 100, 250, 500, 1000, 20, 10, 200]:
        n = total // step
        if 4 <= n <= 7:
            return np.arange(0, total + 1, step)
    # Fallback: ~5 ticks with a multiple-of-10 step.
    step = max(10, int(total / 5 // 10) * 10)
    return np.arange(0, total + 1, step)


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
        ticks = _scenario_xticks(total)
        if ticks is not None:
            ax.set_xticks(ticks)
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
        ticks = _scenario_xticks(total)
        if ticks is not None:
            ax.set_xticks(ticks)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
    ax.grid(True, alpha=0.15, linewidth=0.5)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Recovery time vs swept parameter (for sweep-level plots)
# ---------------------------------------------------------------------------

import csv
import pandas as pd

def plot_recovery_vs_sweep_paper(
    sweep_root: Path,
    out_path: Path,
    x_label: str = "Load (req/s)",
    format_x: str = "auto",
    x_max: float = None,
):
    """
    Paper-style recovery-time-vs-parameter line plot.
    Same data as analyze.plot_recovery_vs_sweep, but with paper figure sizing.
    """
    _paper_style()

    NEVER_Y = 120
    PLOT_MAX = 130
    arrow_offsets = {
        "no-control":         -2.0,
        "circuit-breaker":    -0.7,
        "envoy-retry-budget":  0.7,
        "arolla":              2.0,
    }

    data: Dict[str, Dict[float, float]] = {}
    for val_dir in sorted(sweep_root.iterdir()):
        if not val_dir.is_dir():
            continue
        summary = val_dir / "summary.csv"
        if not summary.exists():
            continue
        try:
            x_val = float(val_dir.name)
        except ValueError:
            continue
        with open(summary) as f:
            for row in pd.read_csv(f).to_dict("records"):
                pol = row["policy"]
                rec = row.get("recovery_sec")
                try:
                    rec_val = float(rec)
                    if np.isnan(rec_val):
                        rec_val = None
                except (ValueError, TypeError):
                    rec_val = None
                data.setdefault(pol, {})[x_val] = rec_val

    x_values = sorted(set(x for pol_data in data.values() for x in pol_data))
    if x_max is not None:
        x_values = [x for x in x_values if x <= x_max]
        for pol in data:
            data[pol] = {x: v for x, v in data[pol].items() if x <= x_max}
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

        rec_xs, rec_ys = [], []
        never_xs = []
        for x in xs:
            v = pol_data[x]
            if v is None:
                never_xs.append(x)
            else:
                rec_xs.append(x)
                rec_ys.append(v)

        marker = POLICY_MARKERS.get(pol, "o")
        color = POLICY_COLORS.get(pol, "gray")
        label = POLICY_LABELS.get(pol, pol)

        if rec_xs:
            ax.plot(rec_xs, rec_ys, color=color, label=label,
                    marker=marker, markersize=5, linewidth=1.5, zorder=4)
        else:
            ax.plot([], [], color=color, label=label,
                    marker=marker, markersize=5, linewidth=1.5)

        if never_xs:
            offset = arrow_offsets.get(pol, 0)
            never_y = [NEVER_Y + offset] * len(never_xs)
            ax.scatter(never_xs, never_y,
                       marker=marker, s=40, color=color, zorder=5)
            # Dashed line from last recovered point to the first
            # never-recovered point AFTER it (not the global min,
            # since earlier x values may also be "never").
            if rec_xs:
                last_rec_x = rec_xs[-1]
                later_nevers = [nx for nx in never_xs if nx > last_rec_x]
                if later_nevers:
                    first_after = min(later_nevers)
                    ax.plot([last_rec_x, first_after],
                            [rec_ys[-1], NEVER_Y + offset],
                            color=color, linestyle="--", linewidth=1.2,
                            alpha=0.5, zorder=3)

    ax.text(x_values[0], NEVER_Y, "no recovery", va="center", fontsize=8,
            color="#999999", fontstyle="italic")

    ax.set_xlabel(x_label)
    ax.set_ylabel("Recovery Time (s)")
    ax.set_xticks(x_values)
    if format_x == "auto":
        ax.set_xticklabels(
            [f"{v/1000:.1f}k" if v >= 1000 else str(int(v)) for v in x_values]
        )
    ax.set_ylim(-1, PLOT_MAX)
    ax.grid(True, alpha=0.2, linewidth=0.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Overhead: latency vs throughput for all policies (prefault steady state)
# ---------------------------------------------------------------------------

def plot_overhead_latency(
    sweep_root: Path,
    out_path: Path,
    percentile: str = "p99",
    x_max: float = None,
    log_y: bool = False,
    y_max: float = None,
    y_min: float = None,
):
    """
    Plot prefault steady-state latency vs offered load for all policies.
    Uses the RPS sweep data — each RPS directory contains a full 4-policy
    experiment. Extracts latency from successful first-attempt requests
    during the prefault phase (no fault, no retries).
    """
    _paper_style()
    import glob as globmod

    data: Dict[str, Dict[float, float]] = {}  # {policy: {rps: latency_ms}}
    for val_dir in sorted(sweep_root.iterdir()):
        if not val_dir.is_dir():
            continue
        try:
            rps = float(val_dir.name)
        except ValueError:
            continue
        if x_max is not None and rps > x_max:
            continue

        for pol in POLICY_ORDER:
            pol_dir = val_dir / pol
            tl_file = pol_dir / "timeline.json"
            if not tl_file.exists():
                continue
            tl = json.loads(tl_file.read_text())
            t_warmup_end = tl["t_warmup_end"]
            t_fault_start = tl["t_fault_start"]

            lats = []
            for f in sorted(globmod.glob(str(pol_dir / "client-metrics/client_attempts.shard*.csv"))):
                with open(f) as fh:
                    reader = csv.reader(fh)
                    next(reader, None)
                    for row in reader:
                        try:
                            ts = float(row[0])
                            ok = row[10]
                            lat = float(row[11])
                            attempt = int(row[7])
                        except (ValueError, IndexError):
                            continue
                        if ok == "1" and ts >= t_warmup_end and ts < t_fault_start and attempt == 1:
                            lats.append(lat * 1000)
            if len(lats) < 100:
                continue  # skip anomalous runs
            lats.sort()
            n = len(lats)
            pct_map = {
                "p50": n // 2,
                "p90": int(n * 0.90),
                "p95": int(n * 0.95),
                "p99": int(n * 0.99),
            }
            idx = pct_map.get(percentile, int(n * 0.99))
            data.setdefault(pol, {})[rps] = lats[idx]

    x_values = sorted(set(x for pol_data in data.values() for x in pol_data))
    if not x_values:
        return

    ordered = [p for p in POLICY_ORDER if p in data]

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    for pol in ordered:
        pol_data = data[pol]
        xs = sorted(pol_data.keys())
        ys = [pol_data[x] for x in xs]
        ax.plot(xs, ys,
                color=POLICY_COLORS[pol],
                label=POLICY_LABELS[pol],
                marker=POLICY_MARKERS[pol],
                markersize=5, linewidth=1.5)

    ax.set_xlabel("Load (req/s)")
    ax.set_ylabel(f"Latency {percentile} (ms)")
    if log_y:
        ax.set_yscale("log")
    if y_min is not None or y_max is not None:
        ax.set_ylim(bottom=y_min, top=y_max)
    ax.set_xticks(x_values)
    ax.set_xticklabels(
        [f"{v/1000:.1f}k" if v >= 1000 else str(int(v)) for v in x_values]
    )
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Overhead bar chart: p50 + p99 side by side at a single RPS
# ---------------------------------------------------------------------------

def plot_overhead_boxplot(
    sweep_root: Path,
    out_path: Path,
    x_max: float = None,
    y_max: float = None,
    y_min: float = None,
):
    """
    Boxplot of steady-state latency across all RPS values for each policy.
    Only includes successful first-attempt requests during the prefault
    phase (no fault, no retries). Excludes RPS values where a policy
    has fewer than 100 successful prefault requests (indicates prefault
    failure / contamination).
    """
    _paper_style()
    import glob as globmod

    # Collect all prefault latencies per policy across all RPS values.
    pol_lats: Dict[str, list] = {}
    for val_dir in sorted(sweep_root.iterdir()):
        if not val_dir.is_dir():
            continue
        try:
            rps = float(val_dir.name)
        except ValueError:
            continue
        if x_max is not None and rps > x_max:
            continue

        for pol in POLICY_ORDER:
            pol_dir = val_dir / pol
            tl_file = pol_dir / "timeline.json"
            if not tl_file.exists():
                continue
            tl = json.loads(tl_file.read_text())
            t_warmup_end = tl["t_warmup_end"]
            t_fault_start = tl["t_fault_start"]

            lats = []
            for f in sorted(globmod.glob(str(pol_dir / "client-metrics/client_attempts.shard*.csv"))):
                with open(f) as fh:
                    reader = csv.reader(fh)
                    next(reader, None)
                    for row in reader:
                        try:
                            ts = float(row[0]); ok = row[10]
                            lat = float(row[11]); attempt = int(row[7])
                        except (ValueError, IndexError):
                            continue
                        if ok == "1" and ts >= t_warmup_end and ts < t_fault_start and attempt == 1:
                            lats.append(lat * 1000)
            if len(lats) < 100:
                continue  # skip contaminated runs
            pol_lats.setdefault(pol, []).extend(lats)

    ordered = [p for p in POLICY_ORDER if p in pol_lats]
    if not ordered:
        return

    fig, ax = plt.subplots(figsize=(3.6, 2.8))

    box_data = [pol_lats[p] for p in ordered]
    colors = [POLICY_COLORS_FILL.get(p, "lightgray") for p in ordered]
    edge_colors = [POLICY_COLORS.get(p, "gray") for p in ordered]

    bp = ax.boxplot(
        box_data,
        labels=[POLICY_LABELS.get(p, p).replace(' ', '\n') for p in ordered],
        patch_artist=True,
        widths=0.5,
        showfliers=False,
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(linewidth=1.2),
        capprops=dict(linewidth=1.2),
    )
    for patch, fc, ec in zip(bp["boxes"], colors, edge_colors):
        patch.set_facecolor(fc)
        patch.set_edgecolor(ec)
        patch.set_linewidth(1.2)

    ax.set_ylabel("Latency (ms)", fontsize=14)
    ax.set_ylim(bottom=y_min if y_min is not None else 0,
                top=y_max)
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.tick_params(axis="x", labelsize=13, rotation=15)
    ax.tick_params(axis="y", labelsize=13)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_overhead_bar(
    sweep_root: Path,
    out_path: Path,
    target_rps: float = 1000,
):
    """
    Grouped bar chart showing p50 and p99 latency for each policy at a
    single RPS point. Cleaner than a line plot for overhead comparison
    since it avoids contaminated data points at other RPS values.
    """
    _paper_style()
    import glob as globmod

    rps_dir = sweep_root / str(int(target_rps))
    if not rps_dir.exists():
        print(f"[skip] {rps_dir} not found")
        return

    pol_lats: Dict[str, Dict[str, float]] = {}  # {policy: {"p50": ms, "p99": ms}}
    for pol in POLICY_ORDER:
        pol_dir = rps_dir / pol
        tl_file = pol_dir / "timeline.json"
        if not tl_file.exists():
            continue
        tl = json.loads(tl_file.read_text())
        t_warmup_end = tl["t_warmup_end"]
        t_fault_start = tl["t_fault_start"]

        lats = []
        for f in sorted(globmod.glob(str(pol_dir / "client-metrics/client_attempts.shard*.csv"))):
            with open(f) as fh:
                reader = csv.reader(fh)
                next(reader, None)
                for row in reader:
                    try:
                        ts = float(row[0]); ok = row[10]
                        lat = float(row[11]); attempt = int(row[7])
                    except (ValueError, IndexError):
                        continue
                    if ok == "1" and ts >= t_warmup_end and ts < t_fault_start and attempt == 1:
                        lats.append(lat * 1000)
        if len(lats) < 100:
            continue
        lats.sort()
        n = len(lats)
        pol_lats[pol] = {
            "p50": lats[n // 2],
            "p99": lats[int(n * 0.99)],
        }

    ordered = [p for p in POLICY_ORDER if p in pol_lats]
    if not ordered:
        return

    percentiles = ["p50", "p99"]
    n_pol = len(ordered)
    bar_width = 0.35
    x = np.arange(n_pol)

    fig, ax = plt.subplots(figsize=(3.5, 2.8))

    pct_colors = {"p50": "#888888", "p99": "#cccccc"}
    for i, pct in enumerate(percentiles):
        vals = [pol_lats[p][pct] for p in ordered]
        ax.bar(x + i * bar_width - bar_width / 2, vals,
                      width=bar_width,
                      color=pct_colors[pct],
                      edgecolor="none",
                      label=pct)
        for j, v in enumerate(vals):
            ax.text(x[j] + i * bar_width - bar_width / 2, v + 0.5,
                    f"{v:.1f}", ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in ordered],
                       fontsize=12)
    ax.set_ylabel("Latency (ms)", fontsize=12)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.2, linewidth=0.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 0.95),
              ncol=2, frameon=False,
              handlelength=1.5, columnspacing=1.0)
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

    # --- Sweep mode ---
    sweep_modes = {
        "--sweep-rps":            ("Load (req/s)", "auto", "recovery-vs-load.pdf"),
        "--sweep-failure-rate":   ("Failure rate (%)", "raw", "recovery-vs-failure-rate.pdf"),
        "--sweep-fault-duration": ("Fault duration (s)", "raw", "recovery-vs-fault-duration.pdf"),
    }
    if sys.argv[1] in sweep_modes:
        x_label, fmt, default_name = sweep_modes[sys.argv[1]]
        remaining = sys.argv[2:]
        # Parse optional --x-max N
        xmax = None
        if "--x-max" in remaining:
            idx = remaining.index("--x-max")
            xmax = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        sweep_root = Path(remaining[0])
        out_path = Path(remaining[1]) if len(remaining) > 1 else sweep_root / default_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"sweep figure → {out_path}")
        plot_recovery_vs_sweep_paper(sweep_root, out_path, x_label=x_label, format_x=fmt, x_max=xmax)
        return

    # --- Overhead mode ---
    if sys.argv[1] == "--overhead":
        remaining = sys.argv[2:]
        xmax = None
        pct = "p99"
        if "--x-max" in remaining:
            idx = remaining.index("--x-max")
            xmax = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        if "--percentile" in remaining:
            idx = remaining.index("--percentile")
            pct = remaining[idx + 1]
            remaining = remaining[:idx] + remaining[idx + 2:]
        ymin = None
        ymax = None
        if "--y-min" in remaining:
            idx = remaining.index("--y-min")
            ymin = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        if "--y-max" in remaining:
            idx = remaining.index("--y-max")
            ymax = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        sweep_root = Path(remaining[0])
        out_dir = Path(remaining[1]) if len(remaining) > 1 else sweep_root
        out_dir.mkdir(parents=True, exist_ok=True)
        for log_y, suffix in [(False, "linear"), (True, "log")]:
            out_path = out_dir / f"overhead-{pct}-{suffix}.pdf"
            print(f"overhead figure → {out_path}")
            plot_overhead_latency(sweep_root, out_path, percentile=pct, x_max=xmax, log_y=log_y, y_max=ymax, y_min=ymin)
        return

    # --- Overhead bar mode ---
    if sys.argv[1] == "--overhead-bar":
        remaining = sys.argv[2:]
        target_rps = 1000.0
        if "--rps" in remaining:
            idx = remaining.index("--rps")
            target_rps = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        sweep_root = Path(remaining[0])
        out_path = Path(remaining[1]) if len(remaining) > 1 else sweep_root / f"overhead-bar-{int(target_rps)}.pdf"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"overhead bar → {out_path}")
        plot_overhead_bar(sweep_root, out_path, target_rps=target_rps)
        return

    # --- Overhead boxplot mode ---
    if sys.argv[1] == "--overhead-boxplot":
        remaining = sys.argv[2:]
        xmax = None
        ymin = None
        ymax = None
        if "--x-max" in remaining:
            idx = remaining.index("--x-max")
            xmax = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        if "--y-min" in remaining:
            idx = remaining.index("--y-min")
            ymin = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        if "--y-max" in remaining:
            idx = remaining.index("--y-max")
            ymax = float(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        sweep_root = Path(remaining[0])
        out_path = Path(remaining[1]) if len(remaining) > 1 else sweep_root / "overhead-boxplot.pdf"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"overhead boxplot → {out_path}")
        plot_overhead_boxplot(sweep_root, out_path, x_max=xmax, y_max=ymax, y_min=ymin)
        return

    # --- Single run mode ---
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
