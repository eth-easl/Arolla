#!/usr/bin/env python3
"""
Plot multi-client fairness sensitivity analysis for AIMD vs SYSNAME.

Generates:
  Fig 1: Fairness gap variance decomposition (eta²)
  Fig 2: Fairness gap box plots by primary knob
  Fig 3: Fairness vs success rate scatter (Pareto front)
  Fig 4: AIMD fairness heatmap — failure_threshold × max_rps
  Fig 5: SYSNAME fairness by alpha — per-client success rates

Usage:
    python plotting/plot_fairness.py <aimd_fairness.csv> <sysname_fairness.csv> -o outputs/fairness
"""

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_theme(style="whitegrid", font_scale=1.1)
PALETTE = sns.color_palette("colorblind")
AIMD_COLOR = PALETTE[0]
SYS_COLOR  = PALETTE[2]


def eta_sq(df, col, target):
    grand = df[target].mean()
    ss_total = ((df[target] - grand)**2).sum()
    gm = df.groupby(col)[target].transform("mean")
    return ((gm - grand)**2).sum() / ss_total if ss_total > 0 else 0


def eta_interaction(df, c1, c2, target):
    grand = df[target].mean()
    ss_total = ((df[target] - grand)**2).sum()
    gm_joint = df.groupby([c1, c2])[target].transform("mean")
    ss_joint = ((gm_joint - grand)**2).sum()
    gm1 = df.groupby(c1)[target].transform("mean")
    ss1 = ((gm1 - grand)**2).sum()
    gm2 = df.groupby(c2)[target].transform("mean")
    ss2 = ((gm2 - grand)**2).sum()
    return (ss_joint - ss1 - ss2) / ss_total if ss_total > 0 else 0


AIMD_PARAMS = ["failure_threshold", "max_rps", "additive_step", "decrease_factor", "window_ms"]
AIMD_SHORT  = ["fail_thr", "max_rps", "add_step", "dec_fact", "win_ms"]
SYS_PARAMS  = ["alpha", "beta_down", "beta_up", "window_ms"]


# ── Fig 1: Variance decomposition for fairness ──────────────
def fig1_fairness_variance(aimd, sys_, out):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    for ax, df, params, shorts, color, title in [
        (axes[0], aimd, AIMD_PARAMS, AIMD_SHORT, AIMD_COLOR, "AIMD (1024 runs)"),
        (axes[1], sys_, SYS_PARAMS,  SYS_PARAMS, SYS_COLOR,  "SYSNAME (256 runs)"),
    ]:
        main_etas = [eta_sq(df, p, "fairness_gap") for p in params]
        inter_etas = []
        for p1 in params:
            best = 0.0
            for p2 in params:
                if p2 != p1:
                    best = max(best, eta_interaction(df, p1, p2, "fairness_gap"))
            inter_etas.append(best)

        x = np.arange(len(params))
        w = 0.35
        ax.bar(x - w/2, [e*100 for e in main_etas],  w, label="Main effect", color=color)
        ax.bar(x + w/2, [e*100 for e in inter_etas], w, label="Top interaction", color=color, alpha=0.4)
        ax.set_xticks(x)
        ax.set_xticklabels(shorts, rotation=30, ha="right")
        ax.set_ylabel("Variance explained (%)")
        ax.set_title(title)
        ax.legend(fontsize=9)

    fig.suptitle("Fairness Gap — Variance Decomposition (η²)", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(out / "fig1_fairness_variance.pdf", bbox_inches="tight")
    fig.savefig(out / "fig1_fairness_variance.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig1_fairness_variance")


# ── Fig 2: Box plots of fairness gap by primary knob ────────
def fig2_fairness_boxplots(aimd, sys_, out):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # AIMD by failure_threshold
    plot_df = aimd[["failure_threshold", "fairness_gap"]].copy()
    plot_df["fairness_gap"] *= 100
    plot_df["failure_threshold"] = plot_df["failure_threshold"].astype(str)
    sns.boxplot(data=plot_df, x="failure_threshold", y="fairness_gap",
                ax=axes[0], color=AIMD_COLOR, width=0.5, fliersize=2)
    axes[0].set_xlabel("failure_threshold")
    axes[0].set_ylabel("Fairness gap (%)")
    axes[0].set_title("AIMD")

    # SYSNAME by alpha
    plot_df = sys_[["alpha", "fairness_gap"]].copy()
    plot_df["fairness_gap"] *= 100
    plot_df["alpha"] = plot_df["alpha"].astype(str)
    sns.boxplot(data=plot_df, x="alpha", y="fairness_gap",
                ax=axes[1], color=SYS_COLOR, width=0.5, fliersize=2)
    axes[1].set_xlabel("alpha (α)")
    axes[1].set_ylabel("Fairness gap (%)")
    axes[1].set_title("SYSNAME")

    ymax = max(axes[0].get_ylim()[1], axes[1].get_ylim()[1])
    for ax in axes:
        ax.set_ylim(-0.5, ymax + 0.5)

    fig.suptitle("Fairness Gap by Primary Knob\n|SR_aggressive − SR_polite|, lower is better",
                 fontsize=14, y=1.04)
    fig.tight_layout()
    fig.savefig(out / "fig2_fairness_boxplots.pdf", bbox_inches="tight")
    fig.savefig(out / "fig2_fairness_boxplots.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig2_fairness_boxplots")


# ── Fig 3: Fairness vs success rate Pareto scatter ───────────
def fig3_pareto_scatter(aimd, sys_, out):
    fig, ax = plt.subplots(figsize=(8, 6))

    ax.scatter(aimd["sr_avg"] * 100, aimd["fairness_gap"] * 100,
               alpha=0.15, s=12, color=AIMD_COLOR, label=f"AIMD ({len(aimd)} configs)")
    ax.scatter(sys_["sr_avg"] * 100, sys_["fairness_gap"] * 100,
               alpha=0.3, s=20, color=SYS_COLOR, marker="^", label=f"SYSNAME ({len(sys_)} configs)")

    ax.set_xlabel("Average success rate (%)")
    ax.set_ylabel("Fairness gap (%)  — lower is better")
    ax.set_title("Fairness vs Performance — All Configurations")
    ax.legend()
    ax.set_xlim(25, 95)

    fig.tight_layout()
    fig.savefig(out / "fig3_pareto_scatter.pdf", bbox_inches="tight")
    fig.savefig(out / "fig3_pareto_scatter.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig3_pareto_scatter")


# ── Fig 4: AIMD heatmap failure_threshold × max_rps ─────────
def fig4_aimd_fairness_heatmap(aimd, out):
    pivot = aimd.groupby(["failure_threshold", "max_rps"])["fairness_gap"].mean().unstack() * 100

    fig, ax = plt.subplots(figsize=(7, 5))
    sns.heatmap(pivot, annot=True, fmt=".1f", cmap="YlOrRd",
                ax=ax, cbar_kws={"label": "Mean fairness gap (%)"},
                linewidths=0.5)
    ax.set_xlabel("max_rps")
    ax.set_ylabel("failure_threshold")
    ax.set_title("AIMD: Fairness Gap\nfailure_threshold × max_rps (averaged over other params)")
    fig.tight_layout()
    fig.savefig(out / "fig4_aimd_fairness_heatmap.pdf", bbox_inches="tight")
    fig.savefig(out / "fig4_aimd_fairness_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig4_aimd_fairness_heatmap")


# ── Fig 5: SYSNAME per-client SR by alpha ────────────────────
def fig5_sysname_perclient(sys_, out):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # Per-client success rates by alpha
    for ax, client, label in [
        (axes[0], "success_rate_agg", "Aggressive client"),
        (axes[1], "success_rate_pol", "Polite client"),
    ]:
        grouped = sys_.groupby("alpha")[client].agg(["mean", "std"]).reset_index()
        ax.errorbar(grouped["alpha"].astype(str), grouped["mean"]*100, yerr=grouped["std"]*100,
                     marker="o", capsize=4, color=SYS_COLOR, linewidth=2, markersize=7)
        ax.set_xlabel("alpha (α)")
        ax.set_ylabel("Success rate (%)")
        ax.set_title(label)

    fig.suptitle("SYSNAME: Per-Client Success Rate by α\n(mean ± std over all other params)", fontsize=14, y=1.04)
    fig.tight_layout()
    fig.savefig(out / "fig5_sysname_perclient.pdf", bbox_inches="tight")
    fig.savefig(out / "fig5_sysname_perclient.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig5_sysname_perclient")


# ── Fig 6: SYSNAME fairness — alpha × beta_down ─────────────
def fig6_sysname_fairness_heatmap(sys_, out):
    pivot = sys_.groupby(["alpha", "beta_down"])["fairness_gap"].mean().unstack() * 100

    fig, ax = plt.subplots(figsize=(7, 5))
    sns.heatmap(pivot, annot=True, fmt=".2f", cmap="YlOrRd",
                ax=ax, cbar_kws={"label": "Mean fairness gap (%)"},
                linewidths=0.5)
    ax.set_xlabel("beta_down")
    ax.set_ylabel("alpha (α)")
    ax.set_title("SYSNAME: Fairness Gap\nalpha × beta_down (averaged over other params)")
    fig.tight_layout()
    fig.savefig(out / "fig6_sysname_fairness_heatmap.pdf", bbox_inches="tight")
    fig.savefig(out / "fig6_sysname_fairness_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("✓ fig6_sysname_fairness_heatmap")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("aimd_csv")
    parser.add_argument("sysname_csv")
    parser.add_argument("-o", "--output", default="outputs/fairness")
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    aimd = pd.read_csv(args.aimd_csv)
    sys_ = pd.read_csv(args.sysname_csv)
    print(f"AIMD: {len(aimd)}, SYSNAME: {len(sys_)}\n")

    fig1_fairness_variance(aimd, sys_, out)
    fig2_fairness_boxplots(aimd, sys_, out)
    fig3_pareto_scatter(aimd, sys_, out)
    fig4_aimd_fairness_heatmap(aimd, out)
    fig5_sysname_perclient(sys_, out)
    fig6_sysname_fairness_heatmap(sys_, out)

    print(f"\nAll figures saved to {out}/")


if __name__ == "__main__":
    main()
