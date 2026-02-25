#!/usr/bin/env python3
"""
Paper figure generation for SYSNAME evaluation.

Produces publication-quality plots matching the evaluation plan:
  - C1: Core mechanism time-series (goodput, amplification, p99)
  - C2: Amplification vs failure severity
  - C4: Client scaling (amplification vs N)
  - D1: Chain depth service-level metrics
  - D3: E2E budget sweep
  - F1: Fairness (per-tenant goodput)
  - S1: Alpha parameter sensitivity

Usage:
  python plotting/plot_sysname_paper.py <results_dir> -o <output_dir>

  <results_dir> should contain subdirectories like:
    c1_recovery/, c1_baseline_nocontrol/, c1_baseline_static/, c1_baseline_aimd/
    c2_severity_sweep/, c2_baseline_nocontrol/, ...
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Style constants (LaTeX-friendly, publication-ready)
# ---------------------------------------------------------------------------

# Strategy colors (consistent across all figures)
STRATEGY_COLORS = {
    "sysname":   "#2ca02c",   # green
    "nocontrol": "#d62728",   # red
    "static":    "#1f77b4",   # blue
    "aimd":      "#ff7f0e",   # orange
}

STRATEGY_LABELS = {
    "sysname":   "SYSNAME (L1+L2)",
    "nocontrol": "No control",
    "static":    "Static budget",
    "aimd":      "AIMD",
}

STRATEGY_LINESTYLES = {
    "sysname":   "-",
    "nocontrol": "-",
    "static":    "--",
    "aimd":      "-.",
}

STRATEGY_ORDER = ["nocontrol", "static", "aimd", "sysname"]

plt.rcParams.update({
    "font.size": 11,
    "axes.labelsize": 12,
    "axes.titlesize": 13,
    "legend.fontsize": 10,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "lines.linewidth": 2.0,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
})


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def find_result_dir(results_dir: Path, prefix: str) -> Path | None:
    """Find a result subdirectory by prefix (handles timestamped names)."""
    # Exact match first
    exact = results_dir / prefix
    if exact.is_dir():
        return exact
    # Prefix match (e.g., c1_baseline_nocontrol_20260225_212254)
    matches = sorted(results_dir.glob(f"{prefix}_*"))
    dirs = [m for m in matches if m.is_dir()]
    if dirs:
        return dirs[-1]  # most recent timestamp
    return None


def load_timeseries(csv_path: Path) -> pd.DataFrame:
    """Load time-series CSV and compute derived columns."""
    df = pd.read_csv(csv_path)
    df = df.sort_values("timepoint")
    # Remove last incomplete bucket
    if len(df) > 1:
        df = df.iloc[:-1]
    # Per-interval metrics (not cumulative)
    df["goodput"] = df["success_root"]
    df["attempts"] = df["root_requests"] + df["retries"]
    if df["root_requests"].sum() > 0:
        df["amplification"] = df["attempts"] / df["root_requests"].replace(0, np.nan)
    else:
        df["amplification"] = 1.0
    return df


def load_timeseries_aggregated(csv_path: Path) -> pd.DataFrame:
    """Load time-series with multiple clients, aggregate across all."""
    df = pd.read_csv(csv_path)
    df = df.sort_values("timepoint")
    if len(df) > 1:
        # Remove last timepoint (incomplete)
        last_t = df["timepoint"].max()
        df = df[df["timepoint"] < last_t]

    SUM_COLS = ["root_requests", "retries", "success_root", "completed",
                "failure_root", "failure_retry", "failure_queue_full",
                "failure_deadline", "failure_server", "total_request", "total_failure"]
    LATENCY_COLS = ["p50", "p90", "p95", "p99"]

    agg = {}
    for c in SUM_COLS:
        if c in df.columns:
            agg[c] = "sum"
    for c in LATENCY_COLS:
        if c in df.columns:
            agg[c] = "max"

    grouped = df.groupby("timepoint").agg(agg).reset_index()
    grouped["goodput"] = grouped["success_root"]
    grouped["attempts"] = grouped["root_requests"] + grouped["retries"]
    grouped["amplification"] = grouped["attempts"] / grouped["root_requests"].replace(0, np.nan)
    return grouped


def load_sweep_summary(csv_path: Path) -> pd.DataFrame:
    """Load sweep summary CSV."""
    return pd.read_csv(csv_path)


def smooth(series, window=3):
    """Simple rolling average for noisy time-series."""
    return series.rolling(window=window, min_periods=1, center=True).mean()


def add_fault_span(ax, start, end, label=None):
    """Add shaded fault window to axes."""
    ax.axvspan(start, end, color="red", alpha=0.08, zorder=0)
    if label:
        ax.text((start + end) / 2, ax.get_ylim()[1] * 0.95, label,
                ha="center", va="top", fontsize=8, color="red", alpha=0.6)


# ---------------------------------------------------------------------------
# Figure C1: Core mechanism time-series
# ---------------------------------------------------------------------------

def plot_c1_timeseries(results_dir: Path, output_dir: Path):
    """
    Slide 30: Time-series during fault + recovery.
    3-panel: goodput, attempt rate, p99 latency.
    One line per strategy.
    """
    # Map directory names to strategy keys
    strategy_dirs = {
        "sysname":   "c1_recovery",
        "nocontrol": "c1_baseline_nocontrol",
        "static":    "c1_baseline_static",
        "aimd":      "c1_baseline_aimd",
    }

    data = {}
    for key, dirname in strategy_dirs.items():
        rdir = find_result_dir(results_dir, dirname)
        if rdir and (rdir / "output.csv").exists():
            data[key] = load_timeseries_aggregated(rdir / "output.csv")
        else:
            print(f"  [C1] Skipping {key}: {dirname} not found")

    if not data:
        print("  [C1] No data found, skipping")
        return

    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)

    # Panel 1: Goodput (success/s)
    ax = axes[0]
    for key in STRATEGY_ORDER:
        if key not in data:
            continue
        df = data[key]
        ax.plot(df["timepoint"], smooth(df["goodput"], 3),
                color=STRATEGY_COLORS[key],
                linestyle=STRATEGY_LINESTYLES[key],
                label=STRATEGY_LABELS[key])
    ax.set_ylabel("Goodput (req/s)")
    ax.set_title("C1: Core Mechanism — Recovery Under Correlated Failure")
    ax.legend(loc="lower right", framealpha=0.9)
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    # Panel 2: Attempt rate (total load)
    ax = axes[1]
    for key in STRATEGY_ORDER:
        if key not in data:
            continue
        df = data[key]
        ax.plot(df["timepoint"], smooth(df["attempts"], 3),
                color=STRATEGY_COLORS[key],
                linestyle=STRATEGY_LINESTYLES[key],
                label=STRATEGY_LABELS[key])
    ax.set_ylabel("Total attempts/s")
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    # Panel 3: p99 latency
    ax = axes[2]
    for key in STRATEGY_ORDER:
        if key not in data:
            continue
        df = data[key]
        if "p99" in df.columns:
            ax.plot(df["timepoint"], smooth(df["p99"], 3),
                    color=STRATEGY_COLORS[key],
                    linestyle=STRATEGY_LINESTYLES[key],
                    label=STRATEGY_LABELS[key])
    ax.set_ylabel("p99 Latency (ms)")
    ax.set_xlabel("Time (s)")
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "c1_timeseries.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "c1_timeseries.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure C2: Amplification vs failure severity
# ---------------------------------------------------------------------------

def plot_c2_severity(results_dir: Path, output_dir: Path):
    """
    Amplification and success rate vs p_fail, one line per strategy.
    """
    strategy_dirs = {
        "sysname":   "c2_severity_sweep",
        "nocontrol": "c2_baseline_nocontrol",
        "static":    "c2_baseline_static",
        "aimd":      "c2_baseline_aimd",
    }

    data = {}
    for key, dirname in strategy_dirs.items():
        rdir = find_result_dir(results_dir, dirname)
        if not rdir:
            print(f"  [C2] Skipping {key}: {dirname} not found")
            continue
        csv = rdir / "summary_client.csv"
        if not csv.exists():
            csv = rdir / "summary.csv"
        if csv.exists():
            df = load_sweep_summary(csv)
            # Find the p_fail column
            pfail_col = [c for c in df.columns if "p_fail" in c]
            if pfail_col:
                df["p_fail"] = df[pfail_col[0]]
                # Aggregate across replicas
                agg = df.groupby("p_fail").agg({
                    "total_requests": "sum",
                    "total_attempts": "sum",
                    "success_rate": "mean",
                    "goodput_rps": "sum",
                }).reset_index()
                agg["amplification"] = agg["total_attempts"] / agg["total_requests"]
                data[key] = agg
            else:
                print(f"  [C2] No p_fail column in {csv}")
        else:
            print(f"  [C2] Skipping {key}: no summary CSV in {rdir}")

    if not data:
        print("  [C2] No data found, skipping")
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Left: Amplification vs p_fail
    for key in STRATEGY_ORDER:
        if key not in data:
            continue
        df = data[key]
        ax1.plot(df["p_fail"], df["amplification"],
                 color=STRATEGY_COLORS[key],
                 linestyle=STRATEGY_LINESTYLES[key],
                 marker="o", markersize=5,
                 label=STRATEGY_LABELS[key])
    ax1.set_xlabel("Failure probability")
    ax1.set_ylabel("Amplification factor")
    ax1.set_title("C2: Amplification vs Failure Severity")
    ax1.legend(framealpha=0.9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0.9)

    # Right: Success rate vs p_fail
    for key in STRATEGY_ORDER:
        if key not in data:
            continue
        df = data[key]
        ax2.plot(df["p_fail"], df["success_rate"] * 100,
                 color=STRATEGY_COLORS[key],
                 linestyle=STRATEGY_LINESTYLES[key],
                 marker="o", markersize=5,
                 label=STRATEGY_LABELS[key])
    ax2.set_xlabel("Failure probability")
    ax2.set_ylabel("Success rate (%)")
    ax2.set_title("C2: Success Rate vs Failure Severity")
    ax2.legend(framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "c2_severity.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "c2_severity.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure C4: Client scaling
# ---------------------------------------------------------------------------

def plot_c4_scaling(results_dir: Path, output_dir: Path):
    """
    Slide 5: Amplification vs client count N.
    SYSNAME should stay flat; static budget should grow.
    """
    strategy_dirs = {
        "sysname":   "c4_scaling",
        "nocontrol": "c4_baseline_nocontrol",
        "static":    "c4_baseline_static",
    }

    data = {}
    for key, dirname in strategy_dirs.items():
        rdir = find_result_dir(results_dir, dirname)
        if not rdir:
            print(f"  [C4] Skipping {key}: {dirname} not found")
            continue
        csv = rdir / "summary.csv"
        if csv.exists():
            df = load_sweep_summary(csv)
            replica_col = [c for c in df.columns if "replicas" in c]
            if replica_col:
                df["N"] = df[replica_col[0]]
                agg = df.groupby("N").agg({
                    "total_requests": "sum",
                    "total_attempts": "sum",
                    "success_rate": "mean",
                    "goodput_rps": "sum",
                }).reset_index()
                agg["amplification"] = agg["total_attempts"] / agg["total_requests"]
                data[key] = agg
        else:
            print(f"  [C4] Skipping {key}: no summary.csv in {rdir}")

    if not data:
        print("  [C4] No data found, skipping")
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    for key in ["nocontrol", "static", "sysname"]:
        if key not in data:
            continue
        df = data[key]
        ax1.plot(df["N"], df["amplification"],
                 color=STRATEGY_COLORS[key],
                 linestyle=STRATEGY_LINESTYLES[key],
                 marker="o", markersize=6,
                 label=STRATEGY_LABELS[key])
    ax1.set_xlabel("Number of clients (N)")
    ax1.set_ylabel("Amplification factor")
    ax1.set_title("C4: Amplification vs Client Count")
    ax1.set_xscale("log")
    ax1.xaxis.set_major_formatter(ticker.ScalarFormatter())
    ax1.legend(framealpha=0.9)
    ax1.grid(True, alpha=0.3)

    for key in ["nocontrol", "static", "sysname"]:
        if key not in data:
            continue
        df = data[key]
        ax2.plot(df["N"], df["goodput_rps"],
                 color=STRATEGY_COLORS[key],
                 linestyle=STRATEGY_LINESTYLES[key],
                 marker="o", markersize=6,
                 label=STRATEGY_LABELS[key])
    ax2.set_xlabel("Number of clients (N)")
    ax2.set_ylabel("Aggregate goodput (req/s)")
    ax2.set_title("C4: Goodput vs Client Count")
    ax2.set_xscale("log")
    ax2.xaxis.set_major_formatter(ticker.ScalarFormatter())
    ax2.legend(framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "c4_scaling.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "c4_scaling.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure D1: Chain depth service-level metrics
# ---------------------------------------------------------------------------

def plot_d1_chain(results_dir: Path, output_dir: Path):
    """
    D1: Per-service metrics over time in the 5-hop chain.
    Shows how SYSNAME bounds amplification at each hop.
    """
    rdir = find_result_dir(results_dir, "d1_chain")
    if not rdir:
        print("  [D1] Skipping: d1_chain not found")
        return

    svc_csv = rdir / "service_metrics.csv"
    client_csv = rdir / "output.csv"

    if not svc_csv.exists():
        print(f"  [D1] Skipping: {svc_csv} not found")
        return

    svc_df = pd.read_csv(svc_csv)
    svc_df = svc_df.sort_values("timepoint")
    services = svc_df["service"].unique()

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    # Panel 1: Per-service success rate over time
    ax = axes[0]
    cmap = plt.cm.viridis(np.linspace(0.2, 0.9, len(services)))
    for i, svc in enumerate(["leaf", "hop4", "hop3", "hop2", "ingress"]):
        if svc not in services:
            continue
        sdf = svc_df[svc_df["service"] == svc]
        ax.plot(sdf["timepoint"], smooth(sdf["success_rate"] * 100, 3),
                color=cmap[i], label=svc, linewidth=1.8)
    ax.set_ylabel("Success rate (%)")
    ax.set_title("D1: Per-Service Success Rate in 5-Hop Chain")
    ax.legend(loc="lower right", ncol=3, framealpha=0.9)
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    # Panel 2: Per-service retry count
    ax = axes[1]
    for i, svc in enumerate(["leaf", "hop4", "hop3", "hop2", "ingress"]):
        if svc not in services:
            continue
        sdf = svc_df[svc_df["service"] == svc]
        ax.plot(sdf["timepoint"], smooth(sdf["retries"], 3),
                color=cmap[i], label=svc, linewidth=1.8)
    ax.set_ylabel("Retries per second")
    ax.set_xlabel("Time (s)")
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "d1_chain.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "d1_chain.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure D3: E2E budget sweep
# ---------------------------------------------------------------------------

def plot_d3_budget(results_dir: Path, output_dir: Path):
    """
    D3: Metrics vs end-to-end retry budget B.
    Bar chart: success rate, amplification for each B value.
    """
    rdir = find_result_dir(results_dir, "d3_budget_sweep")
    if not rdir:
        print("  [D3] Skipping: d3_budget_sweep not found")
        return
    csv = rdir / "summary_client.csv"
    if not csv.exists():
        csv = rdir / "summary.csv"
    if not csv.exists():
        print(f"  [D3] Skipping: no summary CSV in {rdir}")
        return

    df = load_sweep_summary(csv)
    budget_col = [c for c in df.columns if "e2e_retry_budget" in c]
    if not budget_col:
        print(f"  [D3] No budget column found")
        return

    df["B"] = df[budget_col[0]].astype(int)
    agg = df.groupby("B").agg({
        "total_requests": "sum",
        "total_attempts": "sum",
        "success_rate": "mean",
        "mean_latency_ms": "mean",
        "p99_latency_ms": "mean",
        "goodput_rps": "sum",
    }).reset_index()
    agg["amplification"] = agg["total_attempts"] / agg["total_requests"]

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))

    # Bar 1: Success rate
    ax = axes[0]
    bars = ax.bar(agg["B"].astype(str), agg["success_rate"] * 100,
                  color=STRATEGY_COLORS["sysname"], alpha=0.8, edgecolor="black", linewidth=0.5)
    ax.set_xlabel("E2E Budget (B)")
    ax.set_ylabel("Success rate (%)")
    ax.set_title("Success Rate vs Budget")
    ax.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, agg["success_rate"] * 100):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                f"{val:.1f}", ha="center", va="bottom", fontsize=9)

    # Bar 2: Amplification
    ax = axes[1]
    bars = ax.bar(agg["B"].astype(str), agg["amplification"],
                  color=STRATEGY_COLORS["aimd"], alpha=0.8, edgecolor="black", linewidth=0.5)
    ax.set_xlabel("E2E Budget (B)")
    ax.set_ylabel("Amplification")
    ax.set_title("Amplification vs Budget")
    ax.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, agg["amplification"]):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.005,
                f"{val:.3f}", ha="center", va="bottom", fontsize=9)

    # Bar 3: p99 latency
    ax = axes[2]
    bars = ax.bar(agg["B"].astype(str), agg["p99_latency_ms"],
                  color=STRATEGY_COLORS["nocontrol"], alpha=0.8, edgecolor="black", linewidth=0.5)
    ax.set_xlabel("E2E Budget (B)")
    ax.set_ylabel("p99 Latency (ms)")
    ax.set_title("p99 Latency vs Budget")
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    out = output_dir / "d3_budget_sweep.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "d3_budget_sweep.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure F1: Fairness (per-tenant goodput)
# ---------------------------------------------------------------------------

def plot_f1_fairness(results_dir: Path, output_dir: Path):
    """
    Slide 32: Per-tenant goodput over time.
    Shows aggressive tenant's budget shrinks while well-behaved is protected.
    """
    # Try SYSNAME first, then baseline
    pairs = [
        ("sysname", "f1_fairness"),
        ("nocontrol", "f1_baseline_nocontrol"),
    ]

    data = {}
    for key, dirname in pairs:
        rdir = find_result_dir(results_dir, dirname)
        if rdir and (rdir / "output.csv").exists():
            df = pd.read_csv(rdir / "output.csv")
            df = df.sort_values("timepoint")
            data[key] = df

    if not data:
        print("  [F1] No data found, skipping")
        return

    n_panels = len(data)
    fig, axes_flat = plt.subplots(1, n_panels, figsize=(6 * n_panels, 5), squeeze=False)
    axes = axes_flat[0]

    tenant_colors = {"good-tenant": "#2ca02c", "bad-tenant": "#d62728"}
    tenant_labels = {"good-tenant": "Well-behaved tenant", "bad-tenant": "Aggressive tenant"}

    for idx, (key, df) in enumerate(data.items()):
        ax = axes[idx]
        tenants = df["client_name"].unique() if "client_name" in df.columns else []

        for tenant in sorted(tenants):
            tdf = df[df["client_name"] == tenant]
            color = tenant_colors.get(tenant, "gray")
            label = tenant_labels.get(tenant, tenant)
            ax.plot(tdf["timepoint"], smooth(tdf["success_root"], 3),
                    color=color, label=label, linewidth=2)

        title_suffix = STRATEGY_LABELS.get(key, key)
        ax.set_title(f"F1: Per-Tenant Goodput — {title_suffix}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Goodput (req/s)")
        ax.legend(framealpha=0.9)
        add_fault_span(ax, 30, 90)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "f1_fairness.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "f1_fairness.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure S1: Alpha parameter sensitivity
# ---------------------------------------------------------------------------

def plot_s1_sensitivity(results_dir: Path, output_dir: Path):
    """
    Slide 32: Performance vs alpha (retry ratio parameter).
    Shows broad safe region.
    """
    rdir = find_result_dir(results_dir, "s1_param_sweep")
    if not rdir:
        print("  [S1] Skipping: s1_param_sweep not found")
        return
    csv = rdir / "summary_client.csv"
    if not csv.exists():
        csv = rdir / "summary.csv"
    if not csv.exists():
        print(f"  [S1] Skipping: no summary CSV in {rdir}")
        return

    df = load_sweep_summary(csv)
    alpha_col = [c for c in df.columns if "alpha" in c]
    if not alpha_col:
        print(f"  [S1] No alpha column found in {list(df.columns)}")
        return

    df["alpha"] = df[alpha_col[0]]
    agg = df.groupby("alpha").agg({
        "total_requests": "sum",
        "total_attempts": "sum",
        "success_rate": "mean",
        "mean_latency_ms": "mean",
        "p99_latency_ms": "mean",
        "goodput_rps": "sum",
    }).reset_index()
    agg["amplification"] = agg["total_attempts"] / agg["total_requests"]

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(14, 4.5))

    # Success rate vs alpha
    ax1.plot(agg["alpha"], agg["success_rate"] * 100,
             color=STRATEGY_COLORS["sysname"], marker="o", markersize=7, linewidth=2)
    ax1.set_xlabel(r"$\alpha$ (retry ratio)")
    ax1.set_ylabel("Success rate (%)")
    ax1.set_title(r"Success Rate vs $\alpha$")
    ax1.grid(True, alpha=0.3)
    # Mark safe region
    ax1.axvspan(0.01, 0.2, color="green", alpha=0.06, label="Safe region")
    ax1.legend(framealpha=0.9)

    # Amplification vs alpha
    ax2.plot(agg["alpha"], agg["amplification"],
             color=STRATEGY_COLORS["aimd"], marker="s", markersize=7, linewidth=2)
    ax2.set_xlabel(r"$\alpha$ (retry ratio)")
    ax2.set_ylabel("Amplification")
    ax2.set_title(r"Amplification vs $\alpha$")
    ax2.grid(True, alpha=0.3)
    ax2.axvspan(0.01, 0.2, color="green", alpha=0.06, label="Safe region")
    ax2.legend(framealpha=0.9)
    # Reference: 1 + alpha line
    alphas = np.linspace(0.01, 0.5, 50)
    ax2.plot(alphas, 1 + alphas, color="gray", linestyle=":", alpha=0.5, label=r"$1+\alpha$ bound")
    ax2.legend(framealpha=0.9)

    # Mean latency vs alpha
    ax3.plot(agg["alpha"], agg["mean_latency_ms"],
             color=STRATEGY_COLORS["nocontrol"], marker="D", markersize=7, linewidth=2)
    ax3.set_xlabel(r"$\alpha$ (retry ratio)")
    ax3.set_ylabel("Mean latency (ms)")
    ax3.set_title(r"Latency vs $\alpha$")
    ax3.grid(True, alpha=0.3)
    ax3.axvspan(0.01, 0.2, color="green", alpha=0.06, label="Safe region")
    ax3.legend(framealpha=0.9)

    plt.tight_layout()
    out = output_dir / "s1_sensitivity.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "s1_sensitivity.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Figure S2: Component ablation (SYSNAME-only time-series)
# ---------------------------------------------------------------------------

def plot_s2_ablation(results_dir: Path, output_dir: Path):
    """
    S2: Single-run SYSNAME time-series showing behavior during fault.
    Goodput, retries, amplification over time.
    """
    rdir = find_result_dir(results_dir, "s2_ablation")
    if not rdir:
        print("  [S2] Skipping: s2_ablation not found")
        return
    csv = rdir / "output.csv"
    if not csv.exists():
        print(f"  [S2] Skipping: {csv} not found")
        return

    df = load_timeseries(csv)

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)

    # Goodput + attempts
    ax = axes[0]
    ax.plot(df["timepoint"], smooth(df["goodput"], 3),
            color=STRATEGY_COLORS["sysname"], label="Goodput", linewidth=2)
    ax.plot(df["timepoint"], smooth(df["attempts"], 3),
            color=STRATEGY_COLORS["nocontrol"], label="Total attempts",
            linewidth=1.5, alpha=0.7)
    ax.set_ylabel("Requests/s")
    ax.set_title("S2: SYSNAME L1+L2 Ablation — Single Service, 50% Failure")
    ax.legend(framealpha=0.9)
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    # Retries
    ax = axes[1]
    ax.plot(df["timepoint"], smooth(df["retries"], 3),
            color=STRATEGY_COLORS["aimd"], label="Retries/s", linewidth=2)
    ax.fill_between(df["timepoint"], 0, smooth(df["retries"], 3),
                    color=STRATEGY_COLORS["aimd"], alpha=0.15)
    ax.set_ylabel("Retries/s")
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    # Amplification
    ax = axes[2]
    amp = smooth(df["amplification"], 5)
    ax.plot(df["timepoint"], amp,
            color=STRATEGY_COLORS["static"], linewidth=2)
    ax.axhline(y=1.1, color="gray", linestyle=":", alpha=0.5, label=r"$1+\alpha=1.1$")
    ax.set_ylabel("Amplification")
    ax.set_xlabel("Time (s)")
    ax.legend(framealpha=0.9)
    add_fault_span(ax, 30, 60)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / "s2_ablation.pdf"
    fig.savefig(out)
    fig.savefig(output_dir / "s2_ablation.png")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate paper figures for SYSNAME evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python plotting/plot_sysname_paper.py results/sysname_20260225_210608 -o paper_figures/
        """)
    parser.add_argument("results_dir", help="Directory containing experiment results")
    parser.add_argument("-o", "--output", default="paper_figures",
                        help="Output directory for plots (default: paper_figures/)")

    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Results directory: {results_dir}")
    print(f"Output directory:  {output_dir}")
    print()

    # Generate all figures
    figures = [
        ("C1: Core mechanism time-series", plot_c1_timeseries),
        ("C2: Failure severity sweep", plot_c2_severity),
        ("C4: Client scaling", plot_c4_scaling),
        ("D1: Chain depth metrics", plot_d1_chain),
        ("D3: E2E budget sweep", plot_d3_budget),
        ("F1: Fairness", plot_f1_fairness),
        ("S1: Alpha sensitivity", plot_s1_sensitivity),
        ("S2: Ablation", plot_s2_ablation),
    ]

    for name, func in figures:
        print(f"[{name}]")
        try:
            func(results_dir, output_dir)
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
        print()

    print("Done! Figures saved to:", output_dir)


if __name__ == "__main__":
    main()
