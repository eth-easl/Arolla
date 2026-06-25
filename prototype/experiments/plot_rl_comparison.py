#!/usr/bin/env python3
"""
Generate multi-policy retry-budget comparison plots.

Each policy is a "policy-timestamped" run root that contains a flat list
of scenario subdirectories with the standard
``<scenario>/<policy-dir>/{client-metrics,sidecar-stats,rl-controller}/``
layout. Two directory names are recognized:

  - ``rb-rl-v5/``          produced by ``run_full_sweep.sh`` (Phase-B)
  - ``envoy-retry-budget/`` produced by ``run_rl.sh`` (legacy / dev runs)

CLI:
    --policy <path> "<label>"   (repeat for each policy)

The first ``--policy`` is the "current" run: its per-scenario ``plots/``
directories are overwritten with the multi-policy comparison plots
(success-rate, latency-cdf, rps, retry breakdowns, etc).

RL-specific plots (selected_retry_budget, action_analysis, resource_breakdown)
are written for every policy whose scenario contains an ``rl-controller/``
directory, into that policy's own ``<scenario>/plots/`` folder.

Scenario set is the *union* over policies: a scenario only present in some
policies is still plotted, with fewer lines.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# ---------------------------------------------------------------------------
# Import analyze.py from the same directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
import analyze  # noqa: E402
from classify_runs import classify_policy  # noqa: E402

# ---------------------------------------------------------------------------
# Policy registry
# ---------------------------------------------------------------------------
# The underlying Istio mechanism name — used for experiment.json policy_spec lookups.
POLICY = "envoy-retry-budget"

# Policy subdirectory candidates for filesystem discovery, in preference order.
# run_full_sweep.sh renames the RL output dir to "rb-rl-v5/";
# run_rl.sh keeps the Istio name "envoy-retry-budget/".
_RL_POLICY_DIRS = ("rb-rl-v5", "envoy-retry-budget")


def _find_policy_dir(scenario_dir: Path) -> Path:
    """Return the RL policy subdir under *scenario_dir*, trying rb-rl-v5 first."""
    for name in _RL_POLICY_DIRS:
        d = scenario_dir / name
        if d.is_dir():
            return d
    return scenario_dir / _RL_POLICY_DIRS[0]  # fallback (may not exist)

# Palette assigned in --policy declaration order. Calm blue + good green
# preserve the original two-policy look so existing figures still read
# the same when only two policies are passed.
_PALETTE: list[tuple[str, str, Any, str]] = [
    # (color,    fill,      linestyle,            marker)
    ("#3264B4", "#8EAAD6", (0, (4, 2)),          "D"),  # calm blue, long dashes (old STATIC)
    ("#218B21", "#85BF85", "-",                  "o"),  # good green, solid (old RL)
    ("#D62728", "#F2A3A3", (0, (2, 2)),          "s"),  # retry red
    ("#9467BD", "#C5B0D5", (0, (5, 1, 1, 1)),    "^"),  # purple, dash-dot
    ("#E07B00", "#F2BE7F", (0, (1, 1)),          "v"),  # warn orange, dots
    ("#17BECF", "#A8DDE8", "-.",                 "X"),  # teal, dash-dot
    ("#7F7F7F", "#BFBFBF", (0, (3, 1, 1, 1, 1, 1)), "P"),  # gray, dash-dot-dot
]


@dataclass(frozen=True)
class PolicyRun:
    path: Path
    label: str
    key: str               # slug suitable for use as a dict key / file name
    color: str
    fill_color: str
    linestyle: Any
    marker: str


def _slugify(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", label.strip().lower()).strip("_")
    return s or "policy"


def build_policies(pairs: list[tuple[str, str]]) -> list[PolicyRun]:
    """Build PolicyRun objects from (path, label) pairs in declaration order."""
    out: list[PolicyRun] = []
    seen_keys: set[str] = set()
    for i, (path_str, label) in enumerate(pairs):
        base_key = _slugify(label)
        key = base_key
        n = 2
        while key in seen_keys:
            key = f"{base_key}_{n}"
            n += 1
        seen_keys.add(key)
        color, fill, ls, mk = _PALETTE[i % len(_PALETTE)]
        out.append(PolicyRun(
            path=Path(path_str).resolve(),
            label=label,
            key=key,
            color=color,
            fill_color=fill,
            linestyle=ls,
            marker=mk,
        ))
    return out


def _patch_analyze(policies: list[PolicyRun]) -> None:
    """Inject the active policy set into analyze.py's module-level style dicts."""
    analyze.POLICY_LABELS    = {p.key: p.label       for p in policies}
    analyze.POLICY_COLORS    = {p.key: p.color       for p in policies}
    analyze.POLICY_COLORS_FILL = {p.key: p.fill_color for p in policies}
    analyze.POLICY_MARKERS   = {p.key: p.marker      for p in policies}
    analyze.POLICY_LINESTYLES = {p.key: p.linestyle  for p in policies}
    analyze.POLICY_ORDER     = [p.key for p in policies]


# ---------------------------------------------------------------------------
# Custom rps overlay (6 lines: 3 metric series × 2 policies)
# ---------------------------------------------------------------------------

def plot_rps_comparison(
    runs: dict[str, dict],
    out_path: Path,
    experiment: dict,
    policies: list[PolicyRun],
) -> None:
    """
    Requests/s overlay with color=metric-series and linestyle=policy.

    Uses a split legend:
      Left  — metric colors (Root / Retry / Failed)
      Right — policy linestyles
    """
    analyze._apply_paper_style()

    series_colors = {
        "root":   "#1f77b4",
        "retry":  "#ff7f0e",
        "failed": "#d62728",
    }
    series_labels = {
        "root":   "Root requests",
        "retry":  "Retry requests",
        "failed": "Failed requests",
    }

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = analyze._fault_band_x(experiment)

    fig, ax = plt.subplots(figsize=(9, 4.5))

    for p in policies:
        if p.key not in runs:
            continue
        rates = runs[p.key].get("rates") or {}
        for series_key in ("root", "retry", "failed"):
            ts = rates.get(series_key)
            if ts is None or ts.empty:
                continue
            ax.plot(
                ts.index, ts.values,
                color=series_colors[series_key],
                linestyle=p.linestyle,
                linewidth=1.8,
            )

    if fault_sec > 0:
        ax.axvspan(fault_start_x, fault_end_x, color="lightgray", alpha=0.55, zorder=0)
        ymin, ymax = ax.get_ylim()
        ax.text(
            (fault_start_x + fault_end_x) / 2.0,
            ymax - 0.04 * (ymax - ymin), "fault",
            ha="center", va="top",
            fontsize=11, fontstyle="italic", color="#555555",
        )

    if total > 0:
        ax.set_xlim(0, total)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Requests / s")
    ax.grid(True, alpha=0.25, linewidth=0.5)

    # Split legend: metric colours (left) + policy linestyles (right)
    metric_handles = [
        Line2D([0], [0], color=series_colors[k], linewidth=2, label=series_labels[k])
        for k in ("root", "retry", "failed")
    ]
    policy_handles = [
        Line2D([0], [0], color="black", linestyle=p.linestyle, linewidth=2, label=p.label)
        for p in policies if p.key in runs
    ]
    leg1 = ax.legend(handles=metric_handles, loc="upper left",
                     frameon=True, fancybox=False, edgecolor="#888888",
                     framealpha=0.95, fontsize=10)
    ax.add_artist(leg1)
    ax.legend(handles=policy_handles, loc="upper right",
              frameon=True, fancybox=False, edgecolor="#888888",
              framealpha=0.95, fontsize=10)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# RL-specific: decision timeline and resource usage
# ---------------------------------------------------------------------------

def read_decisions(path: Path, t_ref: float | None = None) -> dict[str, list[float]]:
    """Load rl-decisions.csv.

    Args:
        path:   Path to rl-decisions.csv.
        t_ref:  Absolute epoch time to use as x=0.  When provided (e.g. the
                ``t_warmup_end`` value from timeline.json) the returned ``"t"``
                values are aligned with the standard plot x-axis so the fault
                band can be drawn at the correct position.  Falls back to the
                first row's timestamp when omitted.
    """
    if not path.exists():
        return {"t": [], "percent": [], "min": []}
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({
                    "ts": float(row["timestamp"]),
                    "percent": float(row["selected_percent"]),
                    "min": float(row["selected_minRetryConcurrency"]),
                })
            except (KeyError, ValueError):
                continue
    if not rows:
        return {"t": [], "percent": [], "min": []}
    t0 = t_ref if t_ref is not None else rows[0]["ts"]
    return {
        "t": [r["ts"] - t0 for r in rows],
        "percent": [r["percent"] for r in rows],
        "min": [r["min"] for r in rows],
    }


def default_retry_budget_from_experiment(experiment: dict) -> tuple[float, float]:
    """Envoy DestinationRule baseline (same as static mega-fault-sweep RB). Fallback 20%/3."""
    fallback = (20.0, 3.0)
    for spec in experiment.get("policies_spec") or []:
        if spec.get("name") not in _RL_POLICY_DIRS:
            continue
        manifest = spec.get("manifest") or {}
        spec_inner = manifest.get("spec") or {}
        tp = spec_inner.get("trafficPolicy") or {}
        rb = tp.get("retryBudget") or {}
        try:
            pct = float(rb["percent"])
            mrc = float(rb["minRetryConcurrency"])
            return pct, mrc
        except (KeyError, TypeError, ValueError):
            continue
    return fallback


def plot_decisions(
    decisions: dict[str, list[float]],
    out_path: Path,
    *,
    reference_percent: float,
    reference_min: float,
    policy_label: str = "RL",
    experiment: dict | None = None,
) -> None:
    """Selected retry-budget trajectory for one policy, with DR-default refs.

    When *experiment* is supplied the fault window is shaded using the same
    ``prefault_sec`` / ``fault_sec`` values used by the success-rate and
    latency plots, so all figures share the same visual timeline.  This
    requires that ``decisions["t"]`` was computed with ``t_ref=t_warmup_end``
    (pass the ``t_warmup_end`` value from timeline.json to ``read_decisions``).
    """
    if not decisions["t"]:
        return
    analyze._apply_paper_style()
    fig, ax1 = plt.subplots(figsize=(9, 5.0))

    tt = decisions["t"]
    xa, xb = min(tt), max(tt)

    ln_rl_pct, = ax1.plot(
        tt,
        decisions["percent"],
        linestyle="-",
        linewidth=2.0,
        color="tab:blue",
        label=f"{policy_label} retryBudget.percent (agent)",
    )
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("retryBudget.percent", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")

    ln_static_pct = ax1.plot(
        [xa, xb],
        [reference_percent, reference_percent],
        linestyle=(0, (6, 3)),
        linewidth=2.0,
        color="#475569",
        label=f"DR default percent={reference_percent:g}",
    )[0]

    ax2 = ax1.twinx()
    ln_rl_min = ax2.step(
        tt,
        decisions["min"],
        linestyle="-",
        where="post",
        linewidth=2.0,
        color="tab:orange",
        label=f"{policy_label} minRetryConcurrency (agent)",
    )[0]
    ax2.set_ylabel("minRetryConcurrency", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    ln_static_min = ax2.plot(
        [xa, xb],
        [reference_min, reference_min],
        linestyle=(0, (1, 4)),
        linewidth=2.0,
        color="#92400e",
        label=f"DR default minConcurrency={int(reference_min)}",
    )[0]

    # Fault-window shading — same position as success-rate / latency plots.
    if experiment:
        fault_start_x, fault_end_x = analyze._fault_band_x(experiment)
        if fault_end_x > fault_start_x:
            ax1.axvspan(fault_start_x, fault_end_x, color="lightgray", alpha=0.55, zorder=0)
            ymin, ymax = ax1.get_ylim()
            ax1.text(
                (fault_start_x + fault_end_x) / 2.0,
                ymax - 0.06 * (ymax - ymin),
                "fault",
                ha="center", va="top",
                fontsize=11, fontstyle="italic", color="#555555",
            )

    ax1.grid(alpha=0.25)
    handles = (
        ln_rl_pct,
        ln_static_pct,
        ln_rl_min,
        ln_static_min,
    )
    fig.legend(
        handles,
        [h.get_label() for h in handles],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.98),
        ncol=2,
        fontsize=9,
        frameon=True,
        fancybox=False,
        edgecolor="#cbd5e1",
        framealpha=0.96,
    )
    plt.subplots_adjust(top=0.82)
    fig.suptitle(f"Selected retry budget: {policy_label} agent vs DR defaults", y=1.02)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def read_resources(path: Path) -> dict[str, dict[str, list[float]]]:
    from collections import defaultdict
    out: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"t": [], "cpu": [], "mem": []})
    if not path.exists():
        return {}
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append(row | {"_ts": float(row["timestamp"])})
            except (KeyError, ValueError):
                continue
    if not rows:
        return {}
    t0 = rows[0]["_ts"]
    for row in rows:
        try:
            scope = row["scope"]
            cpu = float(row["cpu_mcores"]) if row.get("cpu_mcores") else math.nan
            mem = float(row["memory_mib"]) if row.get("memory_mib") else math.nan
        except ValueError:
            continue
        out[scope]["t"].append(row["_ts"] - t0)
        out[scope]["cpu"].append(cpu)
        out[scope]["mem"].append(mem)
    return dict(out)


def plot_resources(
    resources: dict[str, dict[str, list[float]]],
    out_prefix: Path,
) -> None:
    if not resources:
        return
    analyze._apply_paper_style()
    for metric, ylabel, suffix in [
        ("cpu", "CPU mcores", "cpu"),
        ("mem", "memory MiB", "memory"),
    ]:
        plt.figure(figsize=(9, 4.8))
        for scope, series in resources.items():
            if series["t"]:
                plt.plot(series["t"], series[metric], label=scope, linewidth=1.5)
        plt.xlabel("seconds since sampler start")
        plt.ylabel(ylabel)
        plt.title(f"RL run resource usage: {ylabel}")
        plt.grid(alpha=0.25)
        plt.legend(fontsize=8)
        out_path = out_prefix.with_name(f"{out_prefix.name}-{suffix}.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        plt.tight_layout()
        plt.savefig(out_path, dpi=160)
        plt.close()
        print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Multi-spike annotation helper
# ---------------------------------------------------------------------------

def draw_spike_bands(
    ax: "plt.Axes",
    timeline: dict,
    t_ref: float = 0.0,
    *,
    color: str = "lightgray",
    alpha: float = 0.55,
    label_spikes: bool = True,
) -> None:
    """Shade each fault spike window on *ax*.

    For single-spike runs (``num_spikes`` absent or 1) this is equivalent to
    the standard ``ax.axvspan(fault_start, fault_end, ...)`` shading.
    For multi-spike runs it draws one band per spike.

    All times are in seconds relative to ``t_ref`` (typically ``t_warmup_end``).

    Args:
        ax:          Matplotlib Axes to shade.
        timeline:    Dict loaded from ``timeline.json`` (absolute epoch times).
        t_ref:       Reference epoch time (plot x=0 origin).  When 0 the raw
                     epoch offsets are used as-is; callers that have the t_ref
                     from ``process_policy`` should pass it.
        color:       Fill colour for the spike bands.
        alpha:       Opacity of the fill.
        label_spikes: When True, annotate each band with "spike N / N" for
                      multi-spike runs (or "fault" for single-spike).
    """
    num_spikes = int(timeline.get("num_spikes", 1))

    if num_spikes > 1:
        for i in range(1, num_spikes + 1):
            xs = timeline.get(f"t_spike_{i}_actual_start",
                              timeline.get("t_fault_actual_start",
                                           timeline.get("t_fault_start", 0)))
            xe = timeline.get(f"t_spike_{i}_actual_end",
                              timeline.get("t_fault_actual_end",
                                           timeline.get("t_fault_end", 0)))
            xs_rel = float(xs) - t_ref
            xe_rel = float(xe) - t_ref
            if xe_rel <= xs_rel:
                continue
            ax.axvspan(xs_rel, xe_rel, color=color, alpha=alpha, zorder=0)
            if label_spikes:
                ymin, ymax = ax.get_ylim()
                ax.text(
                    (xs_rel + xe_rel) / 2.0,
                    ymax - 0.06 * (ymax - ymin),
                    f"spike {i}/{num_spikes}",
                    ha="center", va="top",
                    fontsize=9, fontstyle="italic", color="#555555",
                )
    else:
        # Single-spike: use standard fault boundaries.
        t_fault_start = float(timeline.get("t_fault_actual_start",
                                           timeline.get("t_fault_start", 0)))
        t_fault_end   = float(timeline.get("t_fault_actual_end",
                                           timeline.get("t_fault_end", 0)))
        xs_rel = t_fault_start - t_ref
        xe_rel = t_fault_end   - t_ref
        if xe_rel > xs_rel:
            ax.axvspan(xs_rel, xe_rel, color=color, alpha=alpha, zorder=0)
            if label_spikes:
                ymin, ymax = ax.get_ylim()
                ax.text(
                    (xs_rel + xe_rel) / 2.0,
                    ymax - 0.06 * (ymax - ymin),
                    "fault",
                    ha="center", va="top",
                    fontsize=11, fontstyle="italic", color="#555555",
                )


# ---------------------------------------------------------------------------
# Task 2 — Latency-by-phase bar plot (RL vs Static RB)
# ---------------------------------------------------------------------------

def _phase_latency_stats(
    df: "pd.DataFrame",
    t_ref: float,
    phases: list[tuple[str, float, float]],
) -> list[dict]:
    """Compute p95 and mean latency (ms) for each phase window.

    Args:
        df:     Raw attempt DataFrame with ``timestamp`` and ``latency_s``.
        t_ref:  Absolute epoch time of t_warmup_end (the plot x=0 origin).
        phases: List of (name, start_offset, end_offset) tuples where offsets
                are *relative to t_ref* in seconds.
    """
    import pandas as pd  # noqa: PLC0415 — only needed locally

    results = []
    for name, s_off, e_off in phases:
        t_start = t_ref + s_off
        t_end   = t_ref + e_off
        if df.empty or "latency_s" not in df.columns or t_end <= t_start:
            results.append({"phase": name, "p95_ms": float("nan"), "mean_ms": float("nan")})
            continue
        window = df[(df["timestamp"] >= t_start) & (df["timestamp"] < t_end)]
        if window.empty:
            results.append({"phase": name, "p95_ms": float("nan"), "mean_ms": float("nan")})
        else:
            lat_ms = window["latency_s"] * 1_000.0
            results.append({
                "phase":   name,
                "p95_ms":  float(lat_ms.quantile(0.95)),
                "mean_ms": float(lat_ms.mean()),
            })
    return results


def plot_latency_phase_barplot(
    runs: dict[str, dict],
    out_path: "Path",
    experiment: dict,
    policies: list[PolicyRun],
) -> None:
    """Grouped bar chart: client perceived latency per phase for each policy.

    x-axis: scenario phase (prefault / fault / recovery / cooldown).
    Bar groups: p95 and mean for each policy.
    """
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))

    if prefault_sec <= 0 and fault_sec <= 0:
        return  # no useful phase info

    phases = [
        ("Pre-fault",  0,                                            prefault_sec),
        ("Fault",      prefault_sec,                                 prefault_sec + fault_sec),
        ("Recovery",   prefault_sec + fault_sec,                     prefault_sec + fault_sec + recovery_sec),
        ("Cooldown",   prefault_sec + fault_sec + recovery_sec,      prefault_sec + fault_sec + recovery_sec + cooldown_sec),
    ]

    policy_data: dict[str, list[dict]] = {}
    for p in policies:
        run = runs.get(p.key)
        if run is None:
            continue
        df = run.get("df")
        t_ref = run.get("t_ref", 0.0)
        if df is None or df.empty:
            continue
        policy_data[p.key] = _phase_latency_stats(df, t_ref, phases)

    if not policy_data:
        return

    analyze._apply_paper_style()

    phase_names = [p[0] for p in phases]
    n_phases = len(phase_names)
    active = [p for p in policies if p.key in policy_data]
    n_policies = len(active)

    # Two metrics (p95, mean) × n_policies bars per phase group.
    bar_width = max(0.08, min(0.18, 0.7 / max(n_policies * 2, 1)))
    n_bars = n_policies * 2
    x = range(n_phases)
    offsets = [(i - n_bars / 2 + 0.5) * bar_width for i in range(n_bars)]

    fig, ax = plt.subplots(figsize=(10, 5))

    bar_idx = 0
    legend_handles = []
    for p in active:
        stats = policy_data[p.key]
        p95s   = [s["p95_ms"]  for s in stats]
        means_ = [s["mean_ms"] for s in stats]

        b1 = ax.bar(
            [xi + offsets[bar_idx] for xi in x], p95s,
            width=bar_width, color=p.color, alpha=0.85, label=f"{p.label} — p95",
        )
        b2 = ax.bar(
            [xi + offsets[bar_idx + 1] for xi in x], means_,
            width=bar_width, color=p.fill_color, alpha=0.85,
            edgecolor=p.color, linewidth=1.0, label=f"{p.label} — mean",
        )
        legend_handles += [b1, b2]
        bar_idx += 2

    ax.set_xticks(list(x))
    ax.set_xticklabels(phase_names)
    ax.set_ylabel("Client latency (ms)")
    ax.set_title("Client perceived latency by phase")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(handles=legend_handles, fontsize=9,
              frameon=True, fancybox=False, edgecolor="#888888")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Task 3 — Resource usage breakdown (app pods vs RL controller)
# ---------------------------------------------------------------------------

_RL_SCOPE_PATTERNS = ("rl", "controller", "rl-controller")


def _is_rl_scope(scope: str) -> bool:
    s = scope.lower()
    return any(pat in s for pat in _RL_SCOPE_PATTERNS)


def plot_resource_breakdown(
    resources: dict[str, dict[str, list[float]]],
    out_path: "Path",
) -> None:
    """Stacked area chart separating RL controller pods from app pods.

    Writes ``<out_path>-cpu.pdf`` and ``<out_path>-mem.pdf``.
    Falls back to the original flat plot when no RL-scope rows are detected.
    """
    if not resources:
        return

    import pandas as pd  # noqa: PLC0415

    rl_scopes  = {k: v for k, v in resources.items() if _is_rl_scope(k)}
    app_scopes = {k: v for k, v in resources.items() if not _is_rl_scope(k)}

    analyze._apply_paper_style()

    for metric, ylabel, suffix in [
        ("cpu", "CPU (mcores)", "cpu"),
        ("mem", "Memory (MiB)", "mem"),
    ]:
        fig, ax = plt.subplots(figsize=(10, 4.5))

        # Aggregate app pods → single series (sum over time).
        def _aggregate(scopes: dict[str, dict]) -> tuple[list[float], list[float]]:
            if not scopes:
                return [], []
            # Align on a common fine grid using pandas reindex.
            series_list = []
            for v in scopes.values():
                if not v["t"]:
                    continue
                s = pd.Series(v[metric], index=v["t"]).sort_index()
                s = s[~s.index.duplicated()]
                series_list.append(s)
            if not series_list:
                return [], []
            combined = pd.concat(series_list, axis=1).sort_index().interpolate("index").fillna(0)
            agg = combined.sum(axis=1)
            return list(agg.index), list(agg.values)

        t_app,  v_app  = _aggregate(app_scopes)
        t_rl,   v_rl   = _aggregate(rl_scopes)

        # Neutral colors for app/controller breakdown so the chart doesn't
        # clash with any policy's assigned color from --policy.
        _APP_COLOR, _APP_FILL = "#3264B4", "#8EAAD6"   # calm blue
        _CTL_COLOR, _CTL_FILL = "#218B21", "#85BF85"   # good green

        if t_app:
            ax.fill_between(t_app, v_app, alpha=0.55, color=_APP_FILL,
                            label="App pods (total)")
            ax.plot(t_app, v_app, color=_APP_COLOR, linewidth=1.2)

        if t_rl:
            ax.fill_between(t_rl, v_rl, alpha=0.65, color=_CTL_FILL,
                            label="RL controller")
            ax.plot(t_rl, v_rl, color=_CTL_COLOR, linewidth=1.4)
        elif not t_app:
            # No categorised data; fall back to per-scope lines.
            for scope, v in resources.items():
                if v["t"]:
                    ax.plot(v["t"], v[metric], label=scope, linewidth=1.3)

        ax.set_xlabel("Seconds since sampler start")
        ax.set_ylabel(ylabel)
        ax.set_title(f"Resource usage breakdown: {ylabel}")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=9, frameon=True, fancybox=False, edgecolor="#888888")

        suffix_path = out_path.parent / f"{out_path.name}-{suffix}.pdf"
        suffix_path.parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(suffix_path, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {suffix_path}")


# ---------------------------------------------------------------------------
# Task 4 — Cross-policy resource comparison (Static vs RL overhead)
# ---------------------------------------------------------------------------

def plot_resource_comparison(
    scenario_paths: dict[str, Path],
    policies: list[PolicyRun],
    out_path: Path,
    experiment: dict | None = None,
) -> None:
    """Overlay app-pod resource usage across policies and show RL controller overhead.

    Produces two PDF files:
      ``<out_path>-cpu.pdf``  — CPU (mcores)
      ``<out_path>-mem.pdf``  — Memory (MiB)

    Each figure has two panels:
      Top   : aggregate app-pod usage per policy (overlaid lines)
      Bottom: RL controller local process (only present in RL runs)

    The fault window is shaded when *experiment* contains timing information.
    """
    import pandas as pd  # noqa: PLC0415

    # Read resources for every policy that has a resource-usage.csv.
    policy_resources: dict[str, dict[str, dict[str, list[float]]]] = {}
    for p in policies:
        scen_dir = scenario_paths.get(p.key)
        if scen_dir is None:
            continue
        csv_path = _find_policy_dir(scen_dir) / "resource-usage.csv"
        res = read_resources(csv_path)
        if res:
            policy_resources[p.key] = res

    if not policy_resources:
        return

    def _agg_app(res: dict[str, dict[str, list[float]]], metric: str) -> tuple[list[float], list[float]]:
        """Sum non-RL pod scopes into one time series."""
        app = {k: v for k, v in res.items() if not _is_rl_scope(k) and not k.endswith("_node")}
        if not app:
            return [], []
        series_list = []
        for v in app.values():
            if not v["t"]:
                continue
            s = pd.Series(v[metric], index=v["t"]).sort_index()
            s = s[~s.index.duplicated()]
            series_list.append(s)
        if not series_list:
            return [], []
        combined = pd.concat(series_list, axis=1).sort_index().interpolate("index").fillna(0)
        agg = combined.sum(axis=1)
        return list(agg.index), list(agg.values)

    def _rl_series(res: dict[str, dict[str, list[float]]], metric: str) -> tuple[list[float], list[float]]:
        """Aggregate all RL controller scopes."""
        rl = {k: v for k, v in res.items() if _is_rl_scope(k)}
        if not rl:
            return [], []
        series_list = []
        for v in rl.values():
            if not v["t"]:
                continue
            s = pd.Series(v[metric], index=v["t"]).sort_index()
            s = s[~s.index.duplicated()]
            series_list.append(s)
        if not series_list:
            return [], []
        combined = pd.concat(series_list, axis=1).sort_index().interpolate("index").fillna(0)
        agg = combined.sum(axis=1)
        return list(agg.index), list(agg.values)

    # Fault window (relative to sampler start ≈ warmup start).
    prefault_sec = float((experiment or {}).get("prefault_sec", 0))
    warmup_sec   = float((experiment or {}).get("warmup_sec", 0))
    fault_sec    = float((experiment or {}).get("fault_sec", 0))
    # Resource sampler starts right after warmup; fault window offset from t=0 of sampler.
    fault_x0 = prefault_sec
    fault_x1 = prefault_sec + fault_sec

    any_rl = any(
        _rl_series(res, "cpu")[0]
        for res in policy_resources.values()
    )
    n_panels = 2 if any_rl else 1

    analyze._apply_paper_style()

    for metric, ylabel, suffix in [
        ("cpu", "CPU (mcores)", "cpu"),
        ("mem", "Memory (MiB)", "mem"),
    ]:
        fig, axes = plt.subplots(
            n_panels, 1,
            figsize=(10, 4.5 * n_panels),
            sharex=True,
            squeeze=False,
        )
        ax_app = axes[0][0]
        ax_rl  = axes[1][0] if n_panels == 2 else None

        for p in policies:
            res = policy_resources.get(p.key)
            if res is None:
                continue
            t_app, v_app = _agg_app(res, metric)
            if t_app:
                ax_app.plot(
                    t_app, v_app,
                    color=p.color,
                    linestyle=p.linestyle,
                    linewidth=1.8,
                    label=p.label,
                )
            if ax_rl is not None:
                t_rl, v_rl = _rl_series(res, metric)
                if t_rl:
                    ax_rl.plot(
                        t_rl, v_rl,
                        color=p.color,
                        linestyle=p.linestyle,
                        linewidth=1.8,
                        label=p.label,
                    )

        for ax in [ax_app, ax_rl]:
            if ax is None:
                continue
            if fault_sec > 0:
                ax.axvspan(fault_x0, fault_x1, color="lightgray", alpha=0.55, zorder=0)
                ymin, ymax = ax.get_ylim()
                ax.text(
                    (fault_x0 + fault_x1) / 2.0,
                    ymax - 0.06 * (ymax - ymin),
                    "fault",
                    ha="center", va="top",
                    fontsize=10, fontstyle="italic", color="#555555",
                )
            ax.grid(alpha=0.25)
            ax.legend(fontsize=9, frameon=True, fancybox=False, edgecolor="#888888")

        ax_app.set_ylabel(f"App pods — {ylabel}")
        ax_app.set_title(f"Resource comparison across policies: {ylabel}")
        if ax_rl is not None:
            ax_rl.set_ylabel(f"RL controller — {ylabel}")
            ax_rl.set_xlabel("Seconds since sampler start")
        else:
            ax_app.set_xlabel("Seconds since sampler start")

        suffix_path = out_path.parent / f"{out_path.name}-{suffix}.pdf"
        suffix_path.parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(suffix_path, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {suffix_path}")


# ---------------------------------------------------------------------------
# Task 6 — Model action vs metrics analysis
# ---------------------------------------------------------------------------

def read_observations(path: "Path") -> list[dict]:
    """Load rl-observations.jsonl into a list of dicts."""
    if not path.exists():
        return []
    records = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def plot_action_analysis(
    decisions: dict[str, list[float]],
    observations: list[dict],
    out_dir: "Path",
    experiment: dict | None = None,
    *,
    policy_color: str = "#218B21",
) -> None:
    """Two figures analysing what the RL agent does relative to observed metrics.

    Figure 1 — ``action_analysis.pdf``: 3-panel time series
      · Panel 1: success_rate_agg with fault window shaded
      · Panel 2: p95_latency_pressure
      · Panel 3: stepped lines for RB percent and minRetryConcurrency actions

    Figure 2 — ``action_scatter.pdf``: scatter of Δ success_rate (next window
      minus current) against the action taken, to check whether actions
      correlate with improvements.
    """
    if not decisions.get("t") or not observations:
        return

    analyze._apply_paper_style()

    # --- align observations on decision timestamps ---
    obs_ts  = [o.get("timestamp", o.get("ts", float("nan"))) for o in observations]
    success = [o.get("success_rate_agg", float("nan"))       for o in observations]
    latpres = [o.get("p95_latency_pressure", float("nan"))   for o in observations]

    t0_obs = next((t for t in obs_ts if not math.isnan(t)), None)
    if t0_obs is not None:
        obs_t_rel = [t - t0_obs for t in obs_ts]
    else:
        obs_t_rel = list(range(len(obs_ts)))

    t_dec = decisions["t"]
    pct   = decisions["percent"]
    mrc   = decisions["min"]

    # Fault window shading from experiment dict.
    prefault_sec = float((experiment or {}).get("prefault_sec", 0))
    fault_sec    = float((experiment or {}).get("fault_sec", 0))
    fault_x0 = prefault_sec
    fault_x1 = prefault_sec + fault_sec

    # ── Figure 1: 3-panel time series ─────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)

    ax0, ax1, ax2 = axes

    # Panel 0: success rate
    ax0.plot(obs_t_rel, success, color=policy_color, linewidth=1.6, label="success_rate_agg")
    ax0.set_ylabel("Success rate")
    ax0.set_ylim(-0.05, 1.05)
    ax0.grid(alpha=0.25)
    ax0.legend(fontsize=9, loc="lower right")

    # Panel 1: latency pressure
    ax1.plot(obs_t_rel, latpres, color="#e07b00", linewidth=1.6, label="p95_latency_pressure")
    ax1.set_ylabel("Latency pressure")
    ax1.set_ylim(-0.05, 1.05)
    ax1.grid(alpha=0.25)
    ax1.legend(fontsize=9, loc="upper right")

    # Panel 2: discrete actions (stepped)
    ax2.step(t_dec, pct, where="post", color="tab:blue",
             linewidth=2.0, label="percent (%)")
    ax2_twin = ax2.twinx()
    ax2_twin.step(t_dec, mrc, where="post", color="tab:orange",
                  linewidth=2.0, linestyle="--", label="minRetryConcurrency")
    ax2.set_ylabel("RB percent (%)", color="tab:blue")
    ax2.tick_params(axis="y", labelcolor="tab:blue")
    ax2_twin.set_ylabel("minRetryConcurrency", color="tab:orange")
    ax2_twin.tick_params(axis="y", labelcolor="tab:orange")

    # Decision markers
    for t in t_dec:
        ax2.axvline(t, color="gray", linewidth=0.4, alpha=0.4)

    # Fault shading on all panels
    for ax in [ax0, ax1, ax2]:
        if fault_sec > 0:
            ax.axvspan(fault_x0, fault_x1, color="lightgray", alpha=0.5, zorder=0)

    # Combined legend for panel 2
    h1 = [plt.Line2D([0], [0], color="tab:blue",   linewidth=2, label="percent (%)"),
          plt.Line2D([0], [0], color="tab:orange",  linewidth=2, linestyle="--",
                     label="minRetryConcurrency")]
    ax2.legend(handles=h1, fontsize=9, loc="upper right")
    ax2.set_xlabel("Seconds since controller start")

    fig.suptitle("RL agent actions vs observed metrics", fontsize=13)
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "action_analysis.pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_dir / 'action_analysis.pdf'}")

    # ── Figure 2: scatter Δ success_rate vs action ────────────────────────────
    if len(success) < 2:
        return

    # For each observation i, find the most recent decision before obs time i.
    # Δ success = success[i+1] - success[i].
    import numpy as np  # noqa: PLC0415

    delta_success = [
        success[i + 1] - success[i]
        for i in range(len(success) - 1)
        if not (math.isnan(success[i]) or math.isnan(success[i + 1]))
    ]
    if not delta_success or not t_dec:
        return

    # Match each obs window (up to len-1) to the nearest preceding decision.
    def _nearest_preceding(t_query: float, ts: list[float], vals: list[float]) -> float:
        preceding = [(t, v) for t, v in zip(ts, vals) if t <= t_query]
        return preceding[-1][1] if preceding else float("nan")

    matched_pct = []
    matched_mrc = []
    for i in range(len(success) - 1):
        if math.isnan(success[i]) or math.isnan(success[i + 1]):
            continue
        t_q = obs_t_rel[i] if obs_t_rel else float("nan")
        matched_pct.append(_nearest_preceding(t_q, t_dec, pct))
        matched_mrc.append(_nearest_preceding(t_q, t_dec, mrc))

    fig2, (axs0, axs1) = plt.subplots(1, 2, figsize=(11, 4.5))

    for ax_s, matched, xlabel in [
        (axs0, matched_pct, "RB percent (%)"),
        (axs1, matched_mrc, "minRetryConcurrency"),
    ]:
        xs = [v for v in matched if not math.isnan(v)]
        ys = delta_success[: len(xs)]
        ax_s.scatter(xs, ys, alpha=0.55, s=30, color=policy_color, edgecolors="none")
        if xs:
            fit = np.polyfit(xs, ys, 1)
            x_line = np.linspace(min(xs), max(xs), 100)
            ax_s.plot(x_line, np.polyval(fit, x_line),
                      color="#555555", linewidth=1.4, linestyle="--", label="linear fit")
        ax_s.axhline(0, color="gray", linewidth=0.8, linestyle=":")
        ax_s.set_xlabel(xlabel)
        ax_s.set_ylabel("Δ success_rate (next − current window)")
        ax_s.grid(alpha=0.25)
        ax_s.legend(fontsize=8)

    fig2.suptitle("Do RL actions correlate with improvement? (Δ success rate vs action)", fontsize=12)
    fig2.tight_layout()
    fig2.savefig(out_dir / "action_scatter.pdf", bbox_inches="tight")
    plt.close(fig2)
    print(f"  wrote {out_dir / 'action_scatter.pdf'}")


# ---------------------------------------------------------------------------
# Scenario discovery and locator
# ---------------------------------------------------------------------------

def parse_label(label: Path) -> dict[str, str]:
    """Parse rate/fault params from a scenario directory name.

    Both single-component (``rate_rps=...__fault_duration=...``) and the
    legacy slash-separated layout are accepted.
    """
    parsed: dict[str, str] = {}
    for part in label.parts:
        for token in part.replace("__", "\n").splitlines():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key, value = key.strip(), value.strip()
            if key == "fault_rate":
                m = re.search(r"(\d+(?:\.\d+)?)\s*pct", value)
                if m:
                    value = m.group(1)
                elif value.startswith("cartservice-"):
                    inner = value.removeprefix("cartservice-")
                    m2 = re.search(r"(\d+(?:\.\d+)?)", inner)
                    if m2:
                        value = m2.group(1)
            parsed[key] = value
    return parsed


def _canonical_scenario_label(label: Path | str) -> str:
    """Canonical ``rate_rps=N__fault_duration=N__fault_rate=cartservice-Npct``.

    Used to bucket the same scenario across policies even if they happen
    to be stored under slightly different directory names.
    """
    parsed = parse_label(Path(label))
    required = {"rate_rps", "fault_duration", "fault_rate"}
    if not required.issubset(parsed):
        # Fall back to the raw path string.
        return str(label)
    return (
        f"rate_rps={parsed['rate_rps']}__"
        f"fault_duration={parsed['fault_duration']}__"
        f"fault_rate=cartservice-{parsed['fault_rate']}pct"
    )


def locate_scenario_policy_dir(policy_root: Path, scenario_label: str) -> Path | None:
    """Find the ``envoy-retry-budget`` directory for *scenario_label* under *policy_root*.

    Tries (in order for each candidate policy-dir name in ``_RL_POLICY_DIRS``):
      • ``policy_root / scenario_label / <pol>``  (flat layout, current)
      • ``policy_root / * / * / scenario_label / <pol>``
        (legacy nested layout — kept for stragglers that haven't been flattened)
    """
    for pol in _RL_POLICY_DIRS:
        direct = policy_root / scenario_label / pol
        if (direct / "client-metrics").exists():
            return direct

    # Legacy nested layout: try both policy dir names.
    candidates = [
        c for pol in _RL_POLICY_DIRS
        for c in policy_root.glob(f"*/*/{scenario_label}/{pol}/client-metrics")
    ]
    if candidates:
        return candidates[0].parent
    return None


def discover_scenarios(policies: list[PolicyRun]) -> list[dict[str, Any]]:
    """Walk every policy and bucket its scenario directories by canonical label.

    Returns a list of dicts (preserving discovery order across policies):
        [
          {
            "label": "rate_rps=...__fault_duration=...__fault_rate=cartservice-...pct",
            "paths": {policy_key: scenario_dir_path, ...},
          },
          ...
        ]

    A scenario only present in some policies is included; the missing
    policies are simply absent from its ``paths`` dict.
    """
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for p in policies:
        if not p.path.exists():
            print(f"[plot-rl] WARN: policy path missing: {p.path}", file=sys.stderr)
            continue
        for metrics_dir in sorted(
            m for pol in _RL_POLICY_DIRS
            for m in p.path.rglob(f"{pol}/client-metrics")
        ):
            policy_dir = metrics_dir.parent
            scenario_dir = policy_dir.parent
            try:
                rel = scenario_dir.relative_to(p.path)
            except ValueError:
                continue
            canon = _canonical_scenario_label(rel)
            entry = seen.get(canon)
            if entry is None:
                entry = {"label": canon, "paths": {}}
                seen[canon] = entry
                order.append(canon)
            entry["paths"][p.key] = scenario_dir
    return [seen[k] for k in order]


# ---------------------------------------------------------------------------
# Cleanup: remove stale single-comparison PNGs before regenerating
# ---------------------------------------------------------------------------
_STALE_PNG_NAMES = {
    "amplification.png",
    "goodput_rps.png",
    "p95_ms.png",
    "retry_ratio.png",
    "success_rate.png",
}


def _classification_row_for_policy(summary_path: Path, policy_name: str) -> dict[str, str] | None:
    if not summary_path.exists():
        return None
    with summary_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("policy") == policy_name:
                return row
    return None


def write_comparison_health(
    out_dir: Path,
    scenario_paths: dict[str, Path],
    policies: list[PolicyRun],
) -> None:
    """Write a per-policy classification snapshot to *out_dir*.

    Each policy contributes one row; the JSON is keyed by policy key so
    downstream tooling can pick out specific policies by their slug.
    """
    rows: dict[str, dict[str, Any]] = {}
    for p in policies:
        scen_dir = scenario_paths.get(p.key)
        if scen_dir is None:
            continue
        summary_path = scen_dir / "summary.csv"
        row = next(
            (_classification_row_for_policy(summary_path, pol)
             for pol in _RL_POLICY_DIRS
             if _classification_row_for_policy(summary_path, pol) is not None),
            None,
        )
        if row is None:
            continue
        cls = classify_policy(scen_dir, row)
        rows[p.key] = {
            "label": p.label,
            "classification": cls.label,
            "healthy": cls.label == "recovered",
        }
    if not rows:
        return
    payload = {"policies": rows}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "comparison-health.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path}")


def _cleanup_stale_pngs(plots_dir: Path) -> None:
    """Remove old simple-comparison PNGs that are superseded by analyze.py PDFs."""
    for name in _STALE_PNG_NAMES:
        stale = plots_dir / name
        if stale.exists():
            stale.unlink()
            print(f"  removed stale {stale.name}")


# ---------------------------------------------------------------------------
# Per-scenario plotting
# ---------------------------------------------------------------------------

def plot_rl_only_for_policy(
    policy: PolicyRun,
    scenario_dir: Path,
    experiment: dict,
) -> None:
    """Generate RL-specific plots for one policy into ``<scenario_dir>/plots/``.

    Runs ``selected_retry_budget``, resource usage/breakdown,
    ``action_analysis`` and ``action_scatter`` only if the policy run
    contains rl-controller data; otherwise silently no-ops.
    """
    policy_dir = _find_policy_dir(scenario_dir)
    rl_dir = policy_dir / "rl-controller"
    if not rl_dir.exists():
        return

    plots_dir = scenario_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    timeline = analyze.load_timeline(policy_dir)
    t_warmup_end = float(timeline["t_warmup_end"]) if timeline and "t_warmup_end" in timeline else None

    decisions = read_decisions(rl_dir / "rl-decisions.csv", t_ref=t_warmup_end)
    ref_pct, ref_mn = default_retry_budget_from_experiment(experiment)
    plot_decisions(
        decisions,
        plots_dir / "selected_retry_budget.png",
        reference_percent=ref_pct,
        reference_min=ref_mn,
        policy_label=policy.label,
        experiment=experiment,
    )
    resources = read_resources(policy_dir / "resource-usage.csv")
    plot_resources(resources, plots_dir / "resource_usage")
    plot_resource_breakdown(resources, plots_dir / "resource_breakdown")

    observations = read_observations(rl_dir / "rl-observations.jsonl")
    plot_action_analysis(
        decisions, observations, plots_dir, experiment,
        policy_color=policy.color,
    )


def compare_scenario(
    scenario_paths: dict[str, Path],
    policies: list[PolicyRun],
    experiment: dict,
    out_scenario_dir: Path,
) -> None:
    """Generate multi-policy comparison plots into ``<out_scenario_dir>/plots/``.

    Only policies that contributed to *scenario_paths* are plotted. The
    output location is the FIRST policy's scenario folder (so the current
    run's per-scenario ``plots/`` are replaced with the comparison view).
    """
    runs: dict[str, dict] = {}
    for p in policies:
        scen_dir = scenario_paths.get(p.key)
        if scen_dir is None:
            continue
        data = analyze.process_policy(_find_policy_dir(scen_dir), experiment)
        if data:
            runs[p.key] = data

    if not runs:
        print("  [warn] no policy data loaded — skipping comparison", flush=True)
        return

    plots_dir = out_scenario_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    write_comparison_health(out_scenario_dir, scenario_paths, policies)

    _cleanup_stale_pngs(plots_dir)

    # --- Same chart types as analyze.py main() ---
    plot_rps_comparison(runs, plots_dir / "rps.pdf", experiment, policies)

    analyze.plot_success_rate(runs, plots_dir / "success-rate.pdf", experiment)
    analyze.plot_latency_timeseries(runs, plots_dir / "latency-ts.pdf", experiment)
    analyze.plot_latency_cdf(runs, plots_dir / "latency-cdf.pdf", experiment)

    analyze.plot_bar(
        runs, "amplification",
        ylabel="Retry amplification (attempts / first attempts)",
        title="Retry amplification during failure",
        out_path=plots_dir / "amplification.pdf",
        value_fmt="{:.2f}",
    )
    analyze.plot_bar(
        runs, "retry_efficiency_pct",
        ylabel="Retry efficiency (%)",
        title="Retry efficiency during failure",
        out_path=plots_dir / "retry-efficiency.pdf",
        value_fmt="{:.1f}",
    )
    analyze.plot_bar(
        runs, "recovery_sec",
        ylabel="Recovery time (s)",
        title="Time to reach 95% success rate",
        out_path=plots_dir / "recovery-time.pdf",
        value_fmt="{:.1f}",
    )

    analyze.plot_retries_by_upstream(runs, plots_dir / "retries-by-upstream.pdf", experiment)
    analyze.plot_retries_caller_matrix(runs, plots_dir, experiment)

    analyze.plot_retries_stacked_by_caller(runs, plots_dir / "retries-stacked-by-caller.pdf")
    analyze.plot_retries_stacked_by_callee(runs, plots_dir / "retries-stacked-by-callee.pdf")
    analyze.plot_chain_retry(runs, plots_dir / "chain-retry.pdf")

    analyze.plot_retry_status_ts(runs, plots_dir / "retry-status-ts.pdf", experiment)

    analyze.plot_chain_retry_by_phase(runs, plots_dir / "chain-retry-by-phase.pdf")

    plot_latency_phase_barplot(
        runs, plots_dir / "latency_phase_comparison.pdf", experiment, policies,
    )

    plot_resource_comparison(
        scenario_paths, policies, plots_dir / "resource_comparison", experiment,
    )


def plot_retry_budget_for_policy(
    policy: PolicyRun,
    scenario_dir: Path,
    experiment: dict,
) -> bool:
    """Regenerate only ``selected_retry_budget.png`` for one policy."""
    policy_dir = _find_policy_dir(scenario_dir)
    timeline = analyze.load_timeline(policy_dir)
    t_warmup_end = float(timeline["t_warmup_end"]) if timeline and "t_warmup_end" in timeline else None
    decisions = read_decisions(
        policy_dir / "rl-controller" / "rl-decisions.csv",
        t_ref=t_warmup_end,
    )
    if not decisions["t"]:
        print(f"  [warn] no rl-controller decisions for {scenario_dir}", flush=True)
        return False
    plots_dir = scenario_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    ref_pct, ref_mn = default_retry_budget_from_experiment(experiment)
    plot_decisions(
        decisions,
        plots_dir / "selected_retry_budget.png",
        reference_percent=ref_pct,
        reference_min=ref_mn,
        policy_label=policy.label,
        experiment=experiment,
    )
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_experiment_for_scenario(scenario_paths: dict[str, Path]) -> dict:
    """Load the first available ``experiment.json`` for a scenario."""
    for scen_dir in scenario_paths.values():
        exp_json = scen_dir / "experiment.json"
        if exp_json.exists():
            try:
                with exp_json.open() as f:
                    return json.load(f)
            except json.JSONDecodeError:
                continue
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Multi-policy retry-budget comparison plots. "
            "Pass --policy <path> <label> once per policy; the first policy "
            "is the 'current' run whose per-scenario plots/ are replaced "
            "with the comparison view."
        )
    )
    parser.add_argument(
        "--policy",
        action="append",
        nargs=2,
        metavar=("PATH", "LABEL"),
        required=True,
        help=(
            "Policy run root (timestamped) and human-readable label. "
            "Repeat for each policy you want to compare. "
            "Example: --policy outputs/prototype/rb-mega-fault-sweep/20260501_151421 'Static RB'"
        ),
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Only (re)write comparison-health.json metadata, skip figures.",
    )
    parser.add_argument(
        "--retry-budget-plot-only",
        action="store_true",
        help="Only regenerate selected_retry_budget.png for each RL policy.",
    )
    args = parser.parse_args()
    if args.health_only and args.retry_budget_plot_only:
        parser.error("use either --health-only or --retry-budget-plot-only, not both")

    pairs: list[tuple[str, str]] = [(p[0], p[1]) for p in args.policy]
    policies = build_policies(pairs)
    _patch_analyze(policies)

    scenarios = discover_scenarios(policies)
    if not scenarios:
        print("[plot-rl] no scenarios discovered under any --policy root", file=sys.stderr)
        return 1

    print(
        f"[plot-rl] {len(policies)} policy(ies), "
        f"{len(scenarios)} scenario(s) in the union",
        file=sys.stderr,
    )

    count = 0
    for scen in scenarios:
        label = scen["label"]
        paths: dict[str, Path] = scen["paths"]
        experiment = _load_experiment_for_scenario(paths)

        # Owner scenario dir = first --policy that contains this scenario.
        # Comparison plots and per-scenario health JSON are written here.
        owner_key = next((p.key for p in policies if p.key in paths), None)
        if owner_key is None:
            continue
        owner_dir = paths[owner_key]
        owner_policy = next(p for p in policies if p.key == owner_key)

        print(f"[plot-rl] {label}  (owner={owner_policy.label})", flush=True)

        try:
            if args.retry_budget_plot_only:
                for p in policies:
                    scen_dir = paths.get(p.key)
                    if scen_dir is None:
                        continue
                    plot_retry_budget_for_policy(p, scen_dir, experiment)
                count += 1
                continue

            if args.health_only:
                write_comparison_health(owner_dir, paths, policies)
                count += 1
                continue

            # RL-specific plots per policy that has rl-controller data.
            for p in policies:
                scen_dir = paths.get(p.key)
                if scen_dir is None:
                    continue
                plot_rl_only_for_policy(p, scen_dir, experiment)

            # Multi-policy comparison plots into the owner's scenario folder.
            compare_scenario(paths, policies, experiment, owner_dir)
            count += 1
        except Exception as exc:
            print(f"  [error] {exc}", flush=True)

    print(f"[plot-rl] processed {count} scenario(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
