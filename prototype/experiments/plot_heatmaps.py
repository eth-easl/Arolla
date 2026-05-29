#!/usr/bin/env python3
"""plot_heatmaps.py — exploratory visualizations of the prototype-new sweep.

Generates seven figure types into ``outputs/prototype-new/heatmaps/``:

  1a  recovery-time heatmap matrix (one PNG per policy; 5 panels by fault_rate)
  1c  recovery raster (scenarios × time × success_rate; one PNG per policy)
  2a  action-frequency heatmap, faceted phase × recovered/metastable (per RL policy)
  2b  action trajectory phase portrait, 5×5 small multiples (per RL policy)
  2c  single-scenario causal stack (auto-picks one recovered + one metastable for v4)
  2d  observation-feature → action lag-correlation heatmap (per RL policy)
  2e  decision-thrash comparison across RL policies

The two sub-sweeps under ``outputs/prototype-new/full-sweep/`` are merged into
a single logical sweep: ``20260518_152641`` contributes ``rb-rl-v4`` for the
same 25 scenarios that ``20260517_002700`` covers for the other policies.

Run from the repo root:

    python3 prototype/experiments/plot_heatmaps.py
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
SWEEP_ROOTS = [
    REPO_ROOT / "outputs/prototype-new/full-sweep/20260517_002700",
    REPO_ROOT / "outputs/prototype-new/full-sweep/20260518_152641",
]
OUT_DIR = REPO_ROOT / "outputs/prototype-new/heatmaps"

POLICIES = [
    "no-control",
    "envoy-retry-budget",
    "rb-rl-v1-a",
    "rb-rl-v2",
    "rb-rl-v3",
    "rb-rl-v4",
]
RL_POLICIES = ["rb-rl-v1-a", "rb-rl-v2", "rb-rl-v3", "rb-rl-v4"]

POLICY_LABELS = {
    "no-control": "no-control",
    "envoy-retry-budget": "static-RB",
    "rb-rl-v1-a": "RL v1a",
    "rb-rl-v2": "RL v2",
    "rb-rl-v3": "RL v3",
    "rb-rl-v4": "RL v4",
}

SCEN_RE = re.compile(
    r"rate_rps=(?P<rate>\d+)__"
    r"fault_duration=(?P<dur>\d+)__"
    r"fault_rate=cartservice-(?P<frac>\d+)pct"
)

RATES = [1000, 1200, 1400, 1600, 1800]
DURATIONS = [20, 30, 45, 60, 75]
FAULT_RATES = [50, 75, 90, 95, 100]

# Discrete action grid actually observed in rl-decisions.csv across all runs.
PCT_VALUES = [5.0, 10.0, 20.0, 30.0]
MRC_VALUES = [1, 2, 3, 5, 8]

OBS_FIELDS = [
    "success_rate_agg",
    "min_client_success",
    "retry_ratio",
    "window_load_amplification",
    "window_retry_efficiency",
    "retry_fairness_gap",
    "p95_latency_pressure",
    "budget_reject_rate",
    "server_fail_rate",
    "deadline_rate",
    "delta_success_agg",
    "delta_window_load_amplification",
    "budget_utilization",
    "retry_pressure_vs_limit",
    "current_percent_norm",
    "current_min_retry_concurrency_norm",
    "previous_percent_norm",
    "previous_min_retry_concurrency_norm",
]

# Maximum recovery_sec colormap cap. Beyond this we treat the run as "barely
# recovered" — anything missing from summary.csv (empty string) is the truly
# metastable case and gets the hatched overlay.
RECOVERY_CAP_SEC = 180.0

# Common time grid for the 1c raster and 2c causal stack.
RASTER_T_MIN, RASTER_T_MAX, RASTER_DT = -60, 180, 1

# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def parse_scenario(name: str) -> dict[str, int] | None:
    m = SCEN_RE.match(name)
    if not m:
        return None
    return {
        "rate": int(m["rate"]),
        "dur": int(m["dur"]),
        "frac": int(m["frac"]),
        "name": name,
    }


def list_scenarios() -> list[dict[str, Any]]:
    """Return all scenario dicts present in at least one sweep root."""
    seen: dict[str, dict[str, Any]] = {}
    for root in SWEEP_ROOTS:
        if not root.exists():
            continue
        for p in root.iterdir():
            scen = parse_scenario(p.name)
            if scen and p.is_dir():
                seen.setdefault(scen["name"], scen)
    return sorted(seen.values(), key=lambda s: (s["rate"], s["frac"], s["dur"]))


def find_scenario_dir(name: str) -> dict[str, Path]:
    """Map sweep_root → scenario path for both sub-sweeps."""
    out: dict[str, Path] = {}
    for root in SWEEP_ROOTS:
        cand = root / name
        if cand.is_dir():
            out[root.name] = cand
    return out


def load_summary(scen_name: str) -> dict[str, dict[str, Any]]:
    """Merge summary.csv rows from both sub-sweeps. Returns {policy: row_dict}.

    For policies present in timeseries.json but missing from summary.csv (e.g.
    rb-rl-v4 in the 20260518 sweep, where analyze.py was never re-run), we
    backfill ``recovery_sec`` from the success_rate series using the same
    definition as analyze.py:recovery_time_sec — seconds after fault_end
    until success_rate >= 0.95.  Stored as empty string when never recovered,
    matching analyze.py's convention.
    """
    rows: dict[str, dict[str, Any]] = {}
    for path in find_scenario_dir(scen_name).values():
        sf = path / "summary.csv"
        if not sf.exists():
            continue
        with open(sf) as f:
            for row in csv.DictReader(f):
                rows[row["policy"]] = row

    ts_all = load_timeseries(scen_name)
    phases = ts_all.get("_phases") or {}
    fault_end_rel = phases.get("fault_end_rel_s")
    for pol, series in ts_all.items():
        if pol == "_phases" or pol in rows:
            continue
        rec = _compute_recovery_sec_from_ts(series, fault_end_rel)
        rows[pol] = {
            "policy": pol,
            "recovery_sec": "" if rec is None else f"{rec:.1f}",
            "_backfilled": True,
        }
    return rows


def _compute_recovery_sec_from_ts(series: dict[str, Any],
                                    fault_end_rel: float | None) -> float | None:
    """Replicate analyze.py:recovery_time_sec from a timeseries.json series."""
    if fault_end_rel is None:
        return None
    t = series.get("t_rel_s") or []
    sr = series.get("success_rate") or []
    if not t or not sr:
        return None
    for ti, sv in zip(t, sr):
        if sv is None:
            continue
        if ti >= fault_end_rel and float(sv) >= 0.95:
            return float(ti - fault_end_rel)
    return None


def load_timeseries(scen_name: str) -> dict[str, dict[str, Any]]:
    """Merge timeseries.json `series` from both sub-sweeps."""
    out: dict[str, dict[str, Any]] = {}
    phases: dict[str, Any] | None = None
    for path in find_scenario_dir(scen_name).values():
        tj = path / "timeseries.json"
        if not tj.exists():
            continue
        d = json.load(open(tj))
        phases = phases or d.get("phases")
        for pol, series in d.get("series", {}).items():
            out[pol] = series
    out["_phases"] = phases or {}
    return out


def load_rl_decisions(scen_name: str, policy: str) -> pd.DataFrame | None:
    for path in find_scenario_dir(scen_name).values():
        f = path / policy / "rl-controller" / "rl-decisions.csv"
        if f.exists():
            return pd.read_csv(f)
    return None


def load_rl_observations(scen_name: str, policy: str) -> pd.DataFrame | None:
    """Flatten rl-observations.jsonl into a per-tick frame with named obs columns."""
    for path in find_scenario_dir(scen_name).values():
        f = path / policy / "rl-controller" / "rl-observations.jsonl"
        if not f.exists():
            continue
        recs = []
        with open(f) as fh:
            for line in fh:
                d = json.loads(line)
                obs = d.get("observation", [])
                fields = d.get("observation_fields", OBS_FIELDS)
                rec = {fields[i]: obs[i] for i in range(min(len(obs), len(fields)))}
                rec["timestamp"] = d.get("timestamp")
                rec["pct_idx"] = d.get("pct_idx")
                rec["mrc_idx"] = d.get("mrc_idx")
                sel = d.get("selected_budget") or {}
                rec["selected_percent"] = sel.get("percent")
                rec["selected_minRetryConcurrency"] = sel.get("minRetryConcurrency")
                recs.append(rec)
        return pd.DataFrame(recs)
    return None


def parse_recovery_sec(raw: str) -> float | None:
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_metastable(summary_row: dict[str, Any] | None) -> bool:
    if not summary_row:
        return True
    return parse_recovery_sec(summary_row.get("recovery_sec", "")) is None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def ensure_outdir() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def interp_series(t: list[float], y: list[Any], grid: np.ndarray) -> np.ndarray:
    """Interpolate a possibly-irregular series onto ``grid``. None → NaN."""
    t_arr = np.asarray(t, dtype=float)
    y_arr = np.array([float(v) if v is not None else np.nan for v in y], dtype=float)
    mask = np.isfinite(y_arr)
    if mask.sum() < 2:
        return np.full_like(grid, np.nan, dtype=float)
    return np.interp(
        grid,
        t_arr[mask],
        y_arr[mask],
        left=np.nan,
        right=np.nan,
    )


def scenario_label(scen: dict[str, Any]) -> str:
    return f"r{scen['rate']}·d{scen['dur']}·{scen['frac']}%"


# --------------------------------------------------------------------------- #
# Figure 1a — recovery-time heatmap matrix
# --------------------------------------------------------------------------- #


def fig_1a_recovery_heatmaps(scenarios: list[dict[str, Any]]) -> None:
    """One PNG per policy. 5 panels (one per fault_rate), rate × duration grids."""
    # Collect: recovery_sec[policy][frac][rate][dur]
    table: dict[str, dict[int, np.ndarray]] = {
        pol: {fr: np.full((len(RATES), len(DURATIONS)), np.nan) for fr in FAULT_RATES}
        for pol in POLICIES
    }
    metastable: dict[str, dict[int, np.ndarray]] = {
        pol: {fr: np.zeros((len(RATES), len(DURATIONS)), dtype=bool) for fr in FAULT_RATES}
        for pol in POLICIES
    }
    populated: dict[int, np.ndarray] = {
        fr: np.zeros((len(RATES), len(DURATIONS)), dtype=bool) for fr in FAULT_RATES
    }

    for scen in scenarios:
        if scen["rate"] not in RATES or scen["dur"] not in DURATIONS or scen["frac"] not in FAULT_RATES:
            continue
        r = RATES.index(scen["rate"])
        d = DURATIONS.index(scen["dur"])
        populated[scen["frac"]][r, d] = True
        summary = load_summary(scen["name"])
        for pol in POLICIES:
            row = summary.get(pol)
            if row is None:
                continue
            rec = parse_recovery_sec(row.get("recovery_sec", ""))
            if rec is None:
                metastable[pol][scen["frac"]][r, d] = True
            else:
                table[pol][scen["frac"]][r, d] = rec

    cmap = plt.get_cmap("viridis")
    cmap.set_bad(color="#dddddd")

    for pol in POLICIES:
        fig, axes = plt.subplots(1, len(FAULT_RATES), figsize=(3.0 * len(FAULT_RATES), 3.6), sharey=True)
        norm = mcolors.Normalize(vmin=0.0, vmax=RECOVERY_CAP_SEC)
        for ax, frac in zip(axes, FAULT_RATES):
            grid = np.ma.masked_invalid(table[pol][frac])
            im = ax.imshow(grid, cmap=cmap, norm=norm, origin="lower", aspect="auto")
            for ri in range(len(RATES)):
                for di in range(len(DURATIONS)):
                    if not populated[frac][ri, di]:
                        # Scenario point never run anywhere → keep blank
                        ax.add_patch(plt.Rectangle((di - 0.5, ri - 0.5), 1, 1,
                                                    facecolor="white", edgecolor="none"))
                    elif metastable[pol][frac][ri, di]:
                        ax.add_patch(plt.Rectangle((di - 0.5, ri - 0.5), 1, 1,
                                                    facecolor="#b00020", edgecolor="black",
                                                    hatch="///", linewidth=0.4))
                        ax.text(di, ri, "×", ha="center", va="center",
                                color="white", fontsize=9, fontweight="bold")
                    else:
                        val = table[pol][frac][ri, di]
                        color = "white" if val > RECOVERY_CAP_SEC * 0.55 else "black"
                        ax.text(di, ri, f"{val:.0f}", ha="center", va="center",
                                color=color, fontsize=7)
            ax.set_xticks(range(len(DURATIONS)))
            ax.set_xticklabels(DURATIONS)
            ax.set_yticks(range(len(RATES)))
            ax.set_yticklabels(RATES)
            ax.set_xlabel("fault duration (s)")
            ax.set_title(f"fault_rate {frac}%")
        axes[0].set_ylabel("client rate (RPS)")
        cbar = fig.colorbar(im, ax=axes, fraction=0.02, pad=0.02, extend="max")
        cbar.set_label("recovery time (s)  •  red-hatched × = never recovered")
        fig.suptitle(f"1a — recovery time across sweep   policy={POLICY_LABELS[pol]}", y=1.02)
        out = OUT_DIR / f"1a_recovery_heatmap__{pol}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Figure 1c — recovery raster (scenarios × time × success_rate)
# --------------------------------------------------------------------------- #


def fig_1c_recovery_raster(scenarios: list[dict[str, Any]]) -> None:
    """One PNG per policy. Rows = scenarios sorted by recovery_sec, cols = t_rel_s."""
    grid_t = np.arange(RASTER_T_MIN, RASTER_T_MAX + 1, RASTER_DT, dtype=float)

    for pol in POLICIES:
        rows: list[tuple[float, dict[str, Any], np.ndarray]] = []
        for scen in scenarios:
            ts_all = load_timeseries(scen["name"])
            ts = ts_all.get(pol)
            summary = load_summary(scen["name"]).get(pol)
            if ts is None:
                continue
            rec = parse_recovery_sec((summary or {}).get("recovery_sec", "")) if summary else None
            sort_key = rec if rec is not None else 9_999.0  # metastable sinks to the bottom
            row = interp_series(ts.get("t_rel_s", []), ts.get("success_rate", []), grid_t)
            rows.append((sort_key, scen, row))

        rows.sort(key=lambda r: r[0])
        if not rows:
            continue

        mat = np.vstack([r[2] for r in rows])
        labels = [scenario_label(r[1]) for r in rows]
        recs = [r[0] for r in rows]
        durs = [r[1]["dur"] for r in rows]

        fig_h = max(4.0, 0.32 * len(rows) + 1.4)
        fig, ax = plt.subplots(figsize=(11, fig_h))
        cmap = plt.get_cmap("RdYlGn")
        cmap.set_bad(color="#888888")
        im = ax.imshow(
            np.ma.masked_invalid(mat),
            aspect="auto",
            origin="upper",
            cmap=cmap,
            vmin=0.0,
            vmax=1.0,
            extent=(grid_t[0], grid_t[-1], len(rows) - 0.5, -0.5),
        )
        ax.set_xlabel("t_rel_s (s; 0 = fault start)")
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels(
            [f"{lab}  rec={int(rec) if rec < 9000 else '∞'}s" for lab, rec in zip(labels, recs)],
            fontsize=7,
        )
        # Per-row fault span overlay: a thin horizontal segment from 0 to dur
        for i, dur in enumerate(durs):
            ax.plot([0, dur], [i, i], color="black", linewidth=2.5, alpha=0.35,
                    solid_capstyle="butt")
        ax.axvline(0, color="black", linestyle="--", linewidth=0.6, alpha=0.6)
        cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
        cbar.set_label("success_rate")
        ax.set_title(
            f"1c — recovery raster   policy={POLICY_LABELS[pol]}   "
            f"(rows sorted by recovery_sec; black segment = fault window)"
        )
        out = OUT_DIR / f"1c_recovery_raster__{pol}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Figure 2a — action-frequency heatmap, faceted phase × recovered/metastable
# --------------------------------------------------------------------------- #


def _phase_of(t_rel: float, fault_end: float) -> str | None:
    if t_rel < -60:
        return "warmup"
    if t_rel < 0:
        return "prefault"
    if t_rel < fault_end:
        return "fault"
    if t_rel < fault_end + 180:
        return "recovery"
    return None


def fig_2a_action_frequency(scenarios: list[dict[str, Any]]) -> None:
    """For each RL policy: 4×2 grid of 4×5 action heatmaps (phase × outcome)."""
    phases = ["warmup", "prefault", "fault", "recovery"]
    outcomes = ["recovered", "metastable"]

    for pol in RL_POLICIES:
        counts: dict[str, dict[str, np.ndarray]] = {
            ph: {oc: np.zeros((len(PCT_VALUES), len(MRC_VALUES)), dtype=float) for oc in outcomes}
            for ph in phases
        }
        n_scen = {oc: 0 for oc in outcomes}

        for scen in scenarios:
            df = load_rl_decisions(scen["name"], pol)
            if df is None or df.empty:
                continue
            summary = load_summary(scen["name"]).get(pol)
            outcome = "metastable" if is_metastable(summary) else "recovered"
            n_scen[outcome] += 1
            # Translate timestamp → t_rel using timeseries phases if available
            ts_all = load_timeseries(scen["name"])
            phases_meta = ts_all.get("_phases") or {}
            fault_end_rel = phases_meta.get("fault_end_rel_s", scen["dur"])
            # rl-decisions timestamps are absolute; build t_rel_s relative to first tick
            # then shift so fault_start corresponds to absolute fault_start time. The
            # cleanest source is timeline.json (t_fault_start). Recompute from there.
            t_fault_start = _t_fault_start(scen["name"], pol)
            if t_fault_start is None:
                continue
            for _, row in df.iterrows():
                t_rel = float(row["timestamp"]) - t_fault_start
                ph = _phase_of(t_rel, fault_end_rel)
                if ph is None:
                    continue
                try:
                    pi = PCT_VALUES.index(float(row["selected_percent"]))
                    mi = MRC_VALUES.index(int(row["selected_minRetryConcurrency"]))
                except ValueError:
                    continue
                counts[ph][outcome][pi, mi] += 1.0

        fig, axes = plt.subplots(len(phases), len(outcomes), figsize=(7.6, 10.0),
                                  sharex=True, sharey=True)
        for ri, ph in enumerate(phases):
            for ci, oc in enumerate(outcomes):
                ax = axes[ri, ci]
                mat = counts[ph][oc]
                total = mat.sum()
                norm = mat / total if total > 0 else mat
                im = ax.imshow(norm, cmap="magma", aspect="auto", origin="lower",
                               vmin=0.0, vmax=max(0.05, float(norm.max()) if total > 0 else 0.05))
                for pi in range(len(PCT_VALUES)):
                    for mi in range(len(MRC_VALUES)):
                        if total > 0 and norm[pi, mi] > 0.005:
                            color = "white" if norm[pi, mi] < norm.max() * 0.5 else "black"
                            ax.text(mi, pi, f"{norm[pi, mi]*100:.0f}", ha="center",
                                    va="center", fontsize=7, color=color)
                ax.set_xticks(range(len(MRC_VALUES)))
                ax.set_xticklabels(MRC_VALUES)
                ax.set_yticks(range(len(PCT_VALUES)))
                ax.set_yticklabels([f"{v:.0f}" for v in PCT_VALUES])
                if ri == 0:
                    ax.set_title(
                        f"{oc}  (n_scen={n_scen[oc]}, n_ticks={int(total)})",
                        fontsize=10,
                    )
                if ci == 0:
                    ax.set_ylabel(f"{ph}\npercent", fontsize=9)
                if ri == len(phases) - 1:
                    ax.set_xlabel("minRetryConcurrency")
        fig.suptitle(
            f"2a — action-cell frequency (% of ticks)   policy={POLICY_LABELS[pol]}",
            y=0.995,
        )
        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.98))
        out = OUT_DIR / f"2a_action_freq__{pol}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out.relative_to(REPO_ROOT)}")


def _t_fault_start(scen_name: str, policy: str) -> float | None:
    """Read t_fault_start from policy/timeline.json (absolute unix seconds)."""
    for path in find_scenario_dir(scen_name).values():
        tl = path / policy / "timeline.json"
        if tl.exists():
            d = json.load(open(tl))
            tfs = d.get("t_fault_actual_start") or d.get("t_fault_start")
            if tfs:
                return float(tfs)
    return None


# --------------------------------------------------------------------------- #
# Figure 2b — action trajectory phase portrait
# --------------------------------------------------------------------------- #


def fig_2b_action_trajectories(scenarios: list[dict[str, Any]]) -> None:
    """For each RL policy: 5×5 small-multiples grid, one cell per scenario."""
    # Order scenarios by recovery_sec for the chosen policy so the eye sweeps
    # from "easy/recovered" → "metastable" left-to-right, row-by-row.
    for pol in RL_POLICIES:
        scored = []
        for scen in scenarios:
            summary = load_summary(scen["name"]).get(pol)
            rec = parse_recovery_sec((summary or {}).get("recovery_sec", "")) if summary else None
            sort_key = rec if rec is not None else 1e9
            scored.append((sort_key, scen))
        scored.sort(key=lambda x: x[0])

        ncols = 5
        nrows = int(np.ceil(len(scored) / ncols))
        fig, axes = plt.subplots(nrows, ncols, figsize=(2.5 * ncols, 2.4 * nrows),
                                  sharex=True, sharey=True)
        axes = np.atleast_2d(axes)

        for idx, (rec, scen) in enumerate(scored):
            ax = axes[idx // ncols, idx % ncols]
            df = load_rl_decisions(scen["name"], pol)
            if df is None or df.empty:
                ax.set_visible(False)
                continue
            t_fault_start = _t_fault_start(scen["name"], pol)
            t_rel = df["timestamp"].astype(float) - (t_fault_start or 0)
            ax.plot(df["selected_minRetryConcurrency"].astype(float),
                    df["selected_percent"].astype(float),
                    color="#cccccc", linewidth=0.8, zorder=1)
            sc = ax.scatter(df["selected_minRetryConcurrency"].astype(float),
                            df["selected_percent"].astype(float),
                            c=t_rel, cmap="plasma", s=8, vmin=-90, vmax=200, zorder=2)
            ax.set_xlim(min(MRC_VALUES) - 0.5, max(MRC_VALUES) + 0.5)
            ax.set_ylim(min(PCT_VALUES) - 2, max(PCT_VALUES) + 2)
            ax.set_xticks(MRC_VALUES)
            ax.set_yticks(PCT_VALUES)
            ax.tick_params(labelsize=6)
            outcome = "stuck" if rec >= 1e8 else f"{int(rec)}s"
            ax.set_title(f"{scenario_label(scen)}\n{outcome}", fontsize=7)
            ax.grid(True, linewidth=0.3, alpha=0.4)

        # Hide unused axes
        for idx in range(len(scored), nrows * ncols):
            axes[idx // ncols, idx % ncols].set_visible(False)

        cbar = fig.colorbar(sc, ax=axes.ravel().tolist(), fraction=0.018,
                             pad=0.02, location="right")
        cbar.set_label("t_rel_s (0 = fault start)")
        for ax in axes[-1, :]:
            if ax.get_visible():
                ax.set_xlabel("minRetryConcurrency", fontsize=8)
        for ax in axes[:, 0]:
            if ax.get_visible():
                ax.set_ylabel("percent", fontsize=8)
        fig.suptitle(
            f"2b — action trajectory in (mrc, percent)   policy={POLICY_LABELS[pol]}   "
            f"(scenarios sorted by recovery; ‘stuck’ = never recovered)",
            y=1.0,
        )
        # Extra vertical breathing room so per-subplot titles don't collide
        # with the next row's tick labels.
        fig.subplots_adjust(hspace=0.55, wspace=0.20, top=0.93, bottom=0.06)
        out = OUT_DIR / f"2b_action_trajectory__{pol}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Figure 2c — single-scenario causal stack (auto-picks recovered + metastable)
# --------------------------------------------------------------------------- #


def fig_2c_causal_stacks(scenarios: list[dict[str, Any]], policy: str = "rb-rl-v4") -> None:
    """Pick one cleanly-recovered and one metastable scenario for ``policy``;
    plot a 4-panel time series stack for each."""
    recovered: list[tuple[float, dict[str, Any]]] = []
    metastable: list[dict[str, Any]] = []
    for scen in scenarios:
        summary = load_summary(scen["name"]).get(policy)
        rec = parse_recovery_sec((summary or {}).get("recovery_sec", "")) if summary else None
        if rec is None:
            metastable.append(scen)
        else:
            recovered.append((rec, scen))
    recovered.sort(key=lambda x: x[0])
    if not recovered or not metastable:
        print(f"  2c: skipping {policy} — need both recovered and metastable runs.")
        return

    pick_recovered = recovered[0][1]
    # Among metastable ones, pick the highest-fault-rate / highest-rate (the
    # "most clearly broken" run) for a vivid contrast.
    pick_metastable = sorted(
        metastable, key=lambda s: (s["frac"], s["rate"], s["dur"]), reverse=True
    )[0]

    for tag, scen in (("recovered", pick_recovered), ("metastable", pick_metastable)):
        _render_causal_stack(scen, policy, tag)


def _render_causal_stack(scen: dict[str, Any], policy: str, tag: str) -> None:
    ts_all = load_timeseries(scen["name"])
    ts = ts_all.get(policy)
    if ts is None:
        return
    phases_meta = ts_all.get("_phases") or {}
    fault_end = phases_meta.get("fault_end_rel_s", scen["dur"])

    t = np.asarray(ts.get("t_rel_s", []), dtype=float)
    goodput = np.asarray([v or np.nan for v in ts.get("goodput_rps", [])], dtype=float)
    sr = np.asarray([v if v is not None else np.nan for v in ts.get("success_rate", [])], dtype=float)
    amp = np.asarray([v if v is not None else np.nan for v in ts.get("retry_amplification", [])], dtype=float)
    pct = np.asarray([v if v is not None else np.nan for v in ts.get("rl_action_percent", [])], dtype=float)
    mrc = np.asarray([v if v is not None else np.nan for v in ts.get("rl_action_minRetryConcurrency", [])], dtype=float)

    obs_df = load_rl_observations(scen["name"], policy)
    fig, axes = plt.subplots(4, 1, figsize=(11, 9), sharex=True,
                              gridspec_kw={"height_ratios": [1, 1, 1, 1.2]})

    # Only the bottom panel keeps phase labels in its legend; the other panels
    # would otherwise repeat "prefault / fault / recovery" three times.
    def shade_phases(ax: plt.Axes, label: bool = False) -> None:
        ax.axvspan(-60, 0, color="#bbe1ff", alpha=0.25,
                   label="prefault" if label else None)
        ax.axvspan(0, fault_end, color="#ffd6cc", alpha=0.5,
                   label="fault" if label else None)
        ax.axvspan(fault_end, fault_end + 180, color="#e7ffd6", alpha=0.35,
                   label="recovery" if label else None)
        ax.axvline(0, color="black", linewidth=0.6, linestyle="--", alpha=0.5)
        ax.axvline(fault_end, color="black", linewidth=0.6, linestyle="--", alpha=0.5)

    ax = axes[0]
    ax.plot(t, goodput, color="#0a7d3a", linewidth=1.6, label="goodput_rps")
    shade_phases(ax)
    ax.set_ylabel("goodput\n(rps)")
    ax.legend(loc="upper right", fontsize=8)

    ax = axes[1]
    ax.plot(t, sr, color="#0044aa", linewidth=1.4, label="success_rate")
    ax2 = ax.twinx()
    ax2.plot(t, amp, color="#aa0044", linewidth=1.2, label="retry_amplification")
    shade_phases(ax)
    ax.set_ylabel("success_rate")
    ax2.set_ylabel("retry amp", color="#aa0044")
    ax.set_ylim(0, 1.02)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower right", fontsize=8)

    ax = axes[2]
    ax.step(t, pct, where="post", color="#3a3a8e", linewidth=1.6, label="percent")
    ax2 = ax.twinx()
    ax2.step(t, mrc, where="post", color="#cc6a00", linewidth=1.4, label="minRetryConcurrency")
    shade_phases(ax)
    ax.set_ylabel("percent", color="#3a3a8e")
    ax2.set_ylabel("mrc", color="#cc6a00")
    # Pin both axes to the actual discrete action grid so 'percent goes from
    # 10 to 20' isn't visually overwhelmed by matplotlib's auto-range.
    ax.set_ylim(min(PCT_VALUES) - 2, max(PCT_VALUES) + 2)
    ax.set_yticks(PCT_VALUES)
    ax2.set_ylim(min(MRC_VALUES) - 0.5, max(MRC_VALUES) + 0.5)
    ax2.set_yticks(MRC_VALUES)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)

    ax = axes[3]
    if obs_df is not None and not obs_df.empty:
        t_fault_start = _t_fault_start(scen["name"], policy) or 0
        obs_t = obs_df["timestamp"].astype(float) - t_fault_start
        for col in (
            "retry_ratio",
            "server_fail_rate",
            "window_load_amplification",
            "budget_utilization",
        ):
            if col in obs_df.columns:
                ax.plot(obs_t, obs_df[col].astype(float), label=col, linewidth=1.2)
    # Bottom panel is the only one that surfaces the phase legend, to keep
    # the upper panels uncluttered.
    shade_phases(ax, label=True)
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    ax.set_ylabel("obs features\n(model input)")
    ax.set_xlabel("t_rel_s (s)")
    ax.set_xlim(-90, fault_end + 180)

    fig.suptitle(
        f"2c — causal stack   policy={POLICY_LABELS[policy]}   "
        f"scenario={scenario_label(scen)}   [{tag}]",
        y=0.995,
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.985))
    out = OUT_DIR / f"2c_causal_stack__{policy}__{tag}__{scen['name']}.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Figure 2d — observation-feature → action lag-correlation heatmap
# --------------------------------------------------------------------------- #


def fig_2d_lag_correlation(scenarios: list[dict[str, Any]]) -> None:
    """For each RL policy: heatmap of Pearson(feature(t-lag), Δaction(t))
    pooled across all ticks across all scenarios.  Two side-by-side panels:
    Δpercent and ΔminRetryConcurrency."""
    lags = list(range(-5, 6))

    for pol in RL_POLICIES:
        # accumulate (feature_lagged, delta_pct, delta_mrc) triplets
        pooled: dict[int, dict[str, list[float]]] = {
            lag: {"d_pct": [], "d_mrc": [], **{f: [] for f in OBS_FIELDS}} for lag in lags
        }

        for scen in scenarios:
            obs_df = load_rl_observations(scen["name"], pol)
            if obs_df is None or obs_df.empty:
                continue
            obs_df = obs_df.sort_values("timestamp").reset_index(drop=True)
            d_pct = obs_df["selected_percent"].astype(float).diff().fillna(0.0).to_numpy()
            d_mrc = obs_df["selected_minRetryConcurrency"].astype(float).diff().fillna(0.0).to_numpy()
            for lag in lags:
                if lag >= 0:
                    src_slice = slice(0, len(obs_df) - lag) if lag > 0 else slice(None)
                    dst_slice = slice(lag, len(obs_df)) if lag > 0 else slice(None)
                else:
                    src_slice = slice(-lag, len(obs_df))
                    dst_slice = slice(0, len(obs_df) + lag)
                pooled[lag]["d_pct"].extend(d_pct[dst_slice].tolist())
                pooled[lag]["d_mrc"].extend(d_mrc[dst_slice].tolist())
                for f in OBS_FIELDS:
                    if f in obs_df.columns:
                        pooled[lag][f].extend(obs_df[f].astype(float).iloc[src_slice].tolist())

        corr_pct = np.zeros((len(OBS_FIELDS), len(lags)))
        corr_mrc = np.zeros((len(OBS_FIELDS), len(lags)))
        for li, lag in enumerate(lags):
            for fi, f in enumerate(OBS_FIELDS):
                x = np.asarray(pooled[lag][f], dtype=float)
                yp = np.asarray(pooled[lag]["d_pct"], dtype=float)
                ym = np.asarray(pooled[lag]["d_mrc"], dtype=float)
                n = min(len(x), len(yp), len(ym))
                if n < 30:
                    corr_pct[fi, li] = np.nan
                    corr_mrc[fi, li] = np.nan
                    continue
                x, yp, ym = x[:n], yp[:n], ym[:n]
                if np.nanstd(x) < 1e-9 or np.nanstd(yp) < 1e-9:
                    corr_pct[fi, li] = np.nan
                else:
                    corr_pct[fi, li] = np.corrcoef(x, yp)[0, 1]
                if np.nanstd(x) < 1e-9 or np.nanstd(ym) < 1e-9:
                    corr_mrc[fi, li] = np.nan
                else:
                    corr_mrc[fi, li] = np.corrcoef(x, ym)[0, 1]

        fig, axes = plt.subplots(1, 2, figsize=(13, 7), sharey=True)
        for ax, mat, title in (
            (axes[0], corr_pct, "Δ percent"),
            (axes[1], corr_mrc, "Δ minRetryConcurrency"),
        ):
            cmap = plt.get_cmap("RdBu_r").copy()
            cmap.set_bad(color="#dddddd")
            im = ax.imshow(np.ma.masked_invalid(mat), cmap=cmap, vmin=-0.5, vmax=0.5,
                           aspect="auto")
            ax.set_xticks(range(len(lags)))
            ax.set_xticklabels(lags)
            ax.set_yticks(range(len(OBS_FIELDS)))
            ax.set_yticklabels(OBS_FIELDS, fontsize=8)
            ax.set_xlabel("lag (ticks; feature precedes action when lag>0)")
            ax.set_title(title)
            for fi in range(len(OBS_FIELDS)):
                for li in range(len(lags)):
                    v = mat[fi, li]
                    if np.isnan(v):
                        continue
                    if abs(v) > 0.15:
                        ax.text(li, fi, f"{v:+.2f}", ha="center", va="center",
                                fontsize=6,
                                color="white" if abs(v) > 0.3 else "black")
        cbar = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02)
        cbar.set_label("Pearson r  (feature[t-lag],  Δaction[t])")
        fig.suptitle(f"2d — feature → action lag correlation   policy={POLICY_LABELS[pol]}",
                     y=0.99)
        out = OUT_DIR / f"2d_lag_correlation__{pol}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Figure 2e — decision-thrash comparison
# --------------------------------------------------------------------------- #


def fig_2e_decision_thrash(scenarios: list[dict[str, Any]]) -> None:
    """One figure: rows = RL policies, x-axis = patch fraction during fault+recovery,
    grouped by recovered/metastable as side-by-side histograms (kde-ish)."""
    fig, axes = plt.subplots(len(RL_POLICIES), 1, figsize=(9, 2.0 * len(RL_POLICIES)),
                              sharex=True)
    if len(RL_POLICIES) == 1:
        axes = [axes]
    bins = np.linspace(0, 1, 21)

    for ax, pol in zip(axes, RL_POLICIES):
        recovered, metastable = [], []
        for scen in scenarios:
            df = load_rl_decisions(scen["name"], pol)
            if df is None or df.empty:
                continue
            t_fault_start = _t_fault_start(scen["name"], pol)
            ts_all = load_timeseries(scen["name"])
            fault_end_rel = (ts_all.get("_phases") or {}).get("fault_end_rel_s", scen["dur"])
            if t_fault_start is None:
                continue
            t_rel = df["timestamp"].astype(float) - t_fault_start
            mask = (t_rel >= 0) & (t_rel <= fault_end_rel + 180)
            if mask.sum() == 0:
                continue
            patched_frac = float(df.loc[mask, "patched"].astype(int).mean())
            summary = load_summary(scen["name"]).get(pol)
            (metastable if is_metastable(summary) else recovered).append(patched_frac)

        ax.hist([recovered, metastable], bins=bins, stacked=False,
                label=[f"recovered (n={len(recovered)})",
                       f"metastable (n={len(metastable)})"],
                color=["#2a8f3a", "#b00020"], alpha=0.85, edgecolor="black",
                linewidth=0.4)
        ax.set_ylabel(POLICY_LABELS[pol], fontsize=9)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, axis="y", linewidth=0.3, alpha=0.4)

    axes[-1].set_xlabel("fraction of ticks with patched=1   (fault + recovery window)")
    fig.suptitle("2e — decision thrash by policy & outcome", y=0.995)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.98))
    out = OUT_DIR / "2e_decision_thrash.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", default=None,
                    choices=["1a", "1c", "2a", "2b", "2c", "2d", "2e"],
                    help="Restrict to specific figure ids (default: all).")
    args = ap.parse_args()

    ensure_outdir()
    scenarios = list_scenarios()
    print(f"Loaded {len(scenarios)} scenarios across {len(SWEEP_ROOTS)} sub-sweeps.")
    print(f"Output → {OUT_DIR.relative_to(REPO_ROOT)}/")

    figs = {
        "1a": ("recovery heatmap matrix", fig_1a_recovery_heatmaps),
        "1c": ("recovery raster", fig_1c_recovery_raster),
        "2a": ("action-frequency heatmap", fig_2a_action_frequency),
        "2b": ("action trajectory portrait", fig_2b_action_trajectories),
        "2c": ("causal stack (v4)", fig_2c_causal_stacks),
        "2d": ("lag-correlation heatmap", fig_2d_lag_correlation),
        "2e": ("decision thrash histogram", fig_2e_decision_thrash),
    }

    targets = args.only or list(figs.keys())
    for fid in targets:
        label, fn = figs[fid]
        print(f"\n→ {fid} {label}")
        fn(scenarios)

    print("\nDone.")


if __name__ == "__main__":
    main()
