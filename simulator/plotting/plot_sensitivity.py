#!/usr/bin/env python3
"""
Plot combinatorial parameter sensitivity analysis for AIMD vs SYSNAME.

Uses **during-failure success rate** (t=30-60s) as the primary metric,
and includes recovery time-series for representative configs.

Generates:
  Fig 1: Variance decomposition (eta²) — main effects + interactions
  Fig 2: AIMD main effects — during-failure SR vs each parameter
  Fig 3: SYSNAME main effects — during-failure SR vs each parameter
  Fig 4: AIMD interaction heatmap — failure_threshold × max_rps
  Fig 5: Box plots — primary knob distributions (during-failure SR)
  Fig 6: AIMD interaction matrix (lower triangle)
  Fig 7: Recovery time-series — best/worst/median configs per policy

Usage:
    python plotting/plot_sensitivity.py <aimd_phase.csv> <sysname_phase.csv> \\
        --aimd-ts-dir <aimd_sweep>/by_client/client \\
        --sys-ts-dir  <sys_sweep>/by_client/client \\
        -o outputs/sensitivity
"""

import argparse
import sys
from pathlib import Path
from itertools import combinations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns

# ── style ────────────────────────────────────────────────────
sns.set_theme(style="whitegrid", font_scale=1.1)
PALETTE = sns.color_palette("colorblind")
AIMD_COLOR = PALETTE[0]   # blue
SYS_COLOR  = PALETTE[2]   # green
TARGET = "sr_fail"         # during-failure success rate
TARGET_LABEL = "During-failure success rate (%)"

# ── parameter definitions ────────────────────────────────────
AIMD_PARAMS = {
    "failure_thr": "failure_threshold",
    "max_rps":     "max_rps",
    "add_step":    "additive_step",
    "dec_factor":  "decrease_factor",
    "window_ms":   "window_ms",
}

SYS_PARAMS = {
    "alpha":     "alpha",
    "beta_down": "beta_down",
    "beta_up":   "beta_up",
    "window_ms": "window_ms",
}


# ── helpers ──────────────────────────────────────────────────
def eta_squared(df, col, target=TARGET):
    grand = df[target].mean()
    ss_total = ((df[target] - grand) ** 2).sum()
    gm = df.groupby(col)[target].transform("mean")
    ss_between = ((gm - grand) ** 2).sum()
    return ss_between / ss_total if ss_total > 0 else 0.0


def eta_interaction(df, c1, c2, target=TARGET):
    grand = df[target].mean()
    ss_total = ((df[target] - grand) ** 2).sum()
    gm_joint = df.groupby([c1, c2])[target].transform("mean")
    ss_joint = ((gm_joint - grand) ** 2).sum()
    gm1 = df.groupby(c1)[target].transform("mean")
    ss1 = ((gm1 - grand) ** 2).sum()
    gm2 = df.groupby(c2)[target].transform("mean")
    ss2 = ((gm2 - grand) ** 2).sum()
    return (ss_joint - ss1 - ss2) / ss_total if ss_total > 0 else 0.0


# ── Figure 1: variance decomposition ────────────────────────
def fig_variance_decomposition(aimd, sys_, out):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    for ax, df, params, color, title in [
        (axes[0], aimd, AIMD_PARAMS, AIMD_COLOR, "AIMD (5 params, 1024 runs)"),
        (axes[1], sys_, SYS_PARAMS,  SYS_COLOR,  "SYSNAME (4 params, 256 runs)"),
    ]:
        names = list(params.keys())
        cols  = list(params.values())
        main_etas = [eta_squared(df, c) for c in cols]

        inter_etas = []
        for n1, c1 in params.items():
            best = 0.0
            for n2, c2 in params.items():
                if n2 == n1:
                    continue
                val = eta_interaction(df, c1, c2)
                best = max(best, val)
            inter_etas.append(best)

        x = np.arange(len(names))
        w = 0.35
        ax.bar(x - w/2, [e * 100 for e in main_etas],  w,
               label="Main effect", color=color)
        ax.bar(x + w/2, [e * 100 for e in inter_etas], w,
               label="Top 2-way interaction", color=color, alpha=0.4)

        ax.set_xticks(x)
        ax.set_xticklabels(names, rotation=30, ha="right")
        ax.set_ylabel("Variance explained (%)")
        ax.set_title(title)
        ax.legend(loc="upper right", fontsize=9)
        ax.set_ylim(0, max(max(main_etas), max(inter_etas)) * 100 * 1.25)

    fig.suptitle("Variance Decomposition (η²) — During-Failure Success Rate", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(out / "fig1_variance_decomposition.pdf", bbox_inches="tight")
    fig.savefig(out / "fig1_variance_decomposition.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig1_variance_decomposition")


# ── Figure 2 & 3: main effects plots ────────────────────────
def fig_main_effects(df, params, color, policy_name, fig_name, out):
    n = len(params)
    fig, axes = plt.subplots(1, n, figsize=(3.5 * n, 4), sharey=True)
    if n == 1:
        axes = [axes]

    for ax, (name, col) in zip(axes, params.items()):
        grouped = df.groupby(col)[TARGET].agg(["mean", "std"]).reset_index()
        vals = grouped[col].astype(str)
        ax.errorbar(
            vals, grouped["mean"] * 100, yerr=grouped["std"] * 100,
            marker="o", capsize=4, color=color, linewidth=2, markersize=7,
        )
        ax.set_xlabel(name)
        ax.set_title(name, fontsize=11)
        ax.tick_params(axis="x", rotation=45)

    axes[0].set_ylabel(TARGET_LABEL)
    fig.suptitle(f"{policy_name} — Main Effects on During-Failure SR (mean ± std)", fontsize=13, y=1.03)
    fig.tight_layout()
    fig.savefig(out / f"{fig_name}.pdf", bbox_inches="tight")
    fig.savefig(out / f"{fig_name}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ {fig_name}")


# ── Figure 4: AIMD interaction heatmap ───────────────────────
def fig_aimd_interaction_heatmap(aimd, out):
    col_ft = AIMD_PARAMS["failure_thr"]
    col_mr = AIMD_PARAMS["max_rps"]

    pivot = aimd.groupby([col_ft, col_mr])[TARGET].mean().unstack() * 100

    fig, ax = plt.subplots(figsize=(7, 5))
    sns.heatmap(
        pivot, annot=True, fmt=".1f", cmap="YlGnBu",
        ax=ax, cbar_kws={"label": TARGET_LABEL},
        linewidths=0.5,
    )
    ax.set_xlabel("max_rps")
    ax.set_ylabel("failure_threshold")
    ax.set_title("AIMD: failure_threshold × max_rps\n(during-failure SR, averaged over other params)")
    fig.tight_layout()
    fig.savefig(out / "fig4_aimd_interaction_heatmap.pdf", bbox_inches="tight")
    fig.savefig(out / "fig4_aimd_interaction_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig4_aimd_interaction_heatmap")


# ── Figure 5: box plots comparing primary knob ───────────────
def fig_primary_knob_boxplots(aimd, sys_, out):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # AIMD: failure_threshold
    col_ft = AIMD_PARAMS["failure_thr"]
    aimd_plot = aimd[[col_ft, TARGET]].copy()
    aimd_plot[TARGET] *= 100
    aimd_plot[col_ft] = aimd_plot[col_ft].astype(str)
    sns.boxplot(data=aimd_plot, x=col_ft, y=TARGET,
                ax=axes[0], color=AIMD_COLOR, width=0.5, fliersize=2)
    axes[0].set_xlabel("failure_threshold")
    axes[0].set_ylabel(TARGET_LABEL)
    axes[0].set_title("AIMD: by failure_threshold\n(boxes span all other param combos)")

    # SYSNAME: alpha
    col_a = SYS_PARAMS["alpha"]
    sys_plot = sys_[[col_a, TARGET]].copy()
    sys_plot[TARGET] *= 100
    sys_plot[col_a] = sys_plot[col_a].astype(str)
    sns.boxplot(data=sys_plot, x=col_a, y=TARGET,
                ax=axes[1], color=SYS_COLOR, width=0.5, fliersize=2)
    axes[1].set_xlabel("alpha (α)")
    axes[1].set_ylabel(TARGET_LABEL)
    axes[1].set_title("SYSNAME: by α\n(boxes span all other param combos)")

    ymin = min(axes[0].get_ylim()[0], axes[1].get_ylim()[0])
    ymax = max(axes[0].get_ylim()[1], axes[1].get_ylim()[1])
    for ax in axes:
        ax.set_ylim(ymin - 0.5, ymax + 0.5)

    fig.suptitle("Primary Knob — During-Failure Success Rate Distribution", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(out / "fig5_primary_knob_boxplots.pdf", bbox_inches="tight")
    fig.savefig(out / "fig5_primary_knob_boxplots.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig5_primary_knob_boxplots")


# ── Figure 6: AIMD interaction matrix ────────────────────────
def fig_aimd_all_interactions(aimd, out):
    names = list(AIMD_PARAMS.keys())
    cols  = list(AIMD_PARAMS.values())
    n = len(names)
    mat = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i == j:
                mat[i, j] = eta_squared(aimd, cols[i]) * 100
            else:
                mat[i, j] = eta_interaction(aimd, cols[i], cols[j]) * 100

    fig, ax = plt.subplots(figsize=(7, 6))
    mask = np.zeros_like(mat, dtype=bool)
    for i in range(n):
        for j in range(i + 1, n):
            mask[i, j] = True

    sns.heatmap(
        pd.DataFrame(mat, index=names, columns=names),
        annot=True, fmt=".1f", cmap="OrRd", mask=mask,
        ax=ax, cbar_kws={"label": "Variance explained (%)"},
        linewidths=0.5, vmin=0,
    )
    ax.set_title("AIMD: Main Effects (diagonal) & 2-Way Interactions (%)\nDuring-Failure SR")
    fig.tight_layout()
    fig.savefig(out / "fig6_aimd_interaction_matrix.pdf", bbox_inches="tight")
    fig.savefig(out / "fig6_aimd_interaction_matrix.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig6_aimd_interaction_matrix")


# ── Figure 7: recovery time-series ───────────────────────────
def fig_recovery_timeseries(aimd_df, sys_df, aimd_ts_dir, sys_ts_dir, out):
    """Plot per-second success rate time-series for best/worst/median configs."""

    def pick_configs(df, params, n_pick=3):
        """Pick best, median, worst by during-failure SR."""
        sorted_df = df.sort_values(TARGET).reset_index(drop=True)
        idx_worst  = 0
        idx_median = len(sorted_df) // 2
        idx_best   = len(sorted_df) - 1
        picks = {
            "worst":  sorted_df.iloc[idx_worst],
            "median": sorted_df.iloc[idx_median],
            "best":   sorted_df.iloc[idx_best],
        }
        return picks

    def find_ts_file(ts_dir, row, params):
        """Find the time-series CSV matching the parameter row."""
        # Reconstruct filename from params
        parts = []
        for col in sorted(params.values()):
            val = row[col]
            # Format: key_value
            if val == int(val):
                parts.append(f"{col}_{int(val)}")
            else:
                parts.append(f"{col}_{val}")
        fname = "_".join(parts) + ".csv"
        fpath = ts_dir / fname
        if fpath.exists():
            return fpath
        # Fallback: try to find closest match
        for f in ts_dir.glob("*.csv"):
            match = True
            for col in params.values():
                val = row[col]
                if val == int(val):
                    token = f"{col}_{int(val)}"
                else:
                    token = f"{col}_{val}"
                if token not in f.stem:
                    match = False
                    break
            if match:
                return f
        return None

    def load_and_compute_sr(fpath):
        """Load time-series and compute per-second success rate."""
        ts = pd.read_csv(fpath)
        ts["sr"] = ts["success_root"] / ts["root_requests"]
        return ts[["timepoint", "sr"]]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=True)
    line_styles = {"best": "-", "median": "--", "worst": ":"}
    line_colors = {"best": PALETTE[2], "median": PALETTE[1], "worst": PALETTE[3]}

    for ax, df, params, ts_dir, title, main_param in [
        (axes[0], aimd_df, AIMD_PARAMS, aimd_ts_dir, "AIMD", "failure_thr"),
        (axes[1], sys_df,  SYS_PARAMS,  sys_ts_dir,  "SYSNAME", "alpha"),
    ]:
        if ts_dir is None:
            ax.text(0.5, 0.5, "No time-series dir", ha="center", va="center",
                    transform=ax.transAxes)
            continue

        picks = pick_configs(df, params)
        for label, row in picks.items():
            fpath = find_ts_file(ts_dir, row, params)
            if fpath is None:
                continue
            ts = load_and_compute_sr(fpath)
            # Build legend label from key params
            param_col = params[main_param]
            param_val = row[param_col]
            ax.plot(
                ts["timepoint"], ts["sr"] * 100,
                linestyle=line_styles[label], color=line_colors[label],
                linewidth=2, alpha=0.9,
                label=f"{label} ({main_param}={param_val}, fail_SR={row[TARGET]*100:.1f}%)",
            )

        ax.axvspan(30, 60, color="red", alpha=0.08, label="Failure period")
        ax.axvline(60, color="gray", linestyle=":", alpha=0.5)
        ax.set_xlabel("Time (s)")
        ax.set_title(title)
        ax.legend(fontsize=8, loc="lower right")

    axes[0].set_ylabel("Success rate (%)")
    fig.suptitle("Recovery Behavior — Per-Second Success Rate", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(out / "fig7_recovery_timeseries.pdf", bbox_inches="tight")
    fig.savefig(out / "fig7_recovery_timeseries.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig7_recovery_timeseries")


# ── main ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Plot sensitivity analysis")
    parser.add_argument("aimd_csv", help="AIMD phase_metrics.csv")
    parser.add_argument("sysname_csv", help="SYSNAME phase_metrics.csv")
    parser.add_argument("--aimd-ts-dir", default=None, help="AIMD by_client/client dir")
    parser.add_argument("--sys-ts-dir",  default=None, help="SYSNAME by_client/client dir")
    parser.add_argument("-o", "--output", default="outputs/sensitivity", help="Output directory")
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    aimd = pd.read_csv(args.aimd_csv)
    sys_ = pd.read_csv(args.sysname_csv)

    print(f"Loaded AIMD: {len(aimd)} runs, SYSNAME: {len(sys_)} runs")
    print(f"Metric: during-failure success rate (t=30-60s)\n")

    fig_variance_decomposition(aimd, sys_, out)
    fig_main_effects(aimd, AIMD_PARAMS, AIMD_COLOR, "AIMD", "fig2_aimd_main_effects", out)
    fig_main_effects(sys_, SYS_PARAMS, SYS_COLOR, "SYSNAME", "fig3_sysname_main_effects", out)
    fig_aimd_interaction_heatmap(aimd, out)
    fig_primary_knob_boxplots(aimd, sys_, out)
    fig_aimd_all_interactions(aimd, out)

    aimd_ts = Path(args.aimd_ts_dir) if args.aimd_ts_dir else None
    sys_ts  = Path(args.sys_ts_dir)  if args.sys_ts_dir  else None
    if aimd_ts or sys_ts:
        fig_recovery_timeseries(aimd, sys_, aimd_ts, sys_ts, out)

    print(f"\nAll figures saved to {out}/")


if __name__ == "__main__":
    main()
