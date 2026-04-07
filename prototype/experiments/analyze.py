#!/usr/bin/env python3
"""
analyze.py — aggregate prototype experiment results and render plots.

Reads a run directory produced by run-experiment.sh and writes:

    runs/<ts>/summary.csv           — per-policy metrics in a single table
    runs/<ts>/plots/goodput.pdf     — goodput vs time, one line per policy
    runs/<ts>/plots/amplification.pdf   — retry amplification bar chart
    runs/<ts>/plots/retry-efficiency.pdf — retry efficiency bar chart

Data sources:
  - <policy>/client-metrics/*.csv   — per-request rows from traffic_gen.py
  - <policy>/timeline.json          — phase timestamps
  - <policy>/sidecar-stats/*.stats  — Envoy admin /stats dumps (for Arolla
                                      counters + request totals)
  - experiment.json                 — top-level run descriptor

Metrics computed (all during the fault window):
  - amplification  = total attempts / unique first-attempt requests
  - retry_eff_pct  = (retries that ended ok) / total retries × 100
  - avg_goodput    = mean successful requests per second
  - recovery_sec   = seconds from fault_end until goodput ≥ 95% of pre-fault
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Style — matches the paper figure aesthetic (recovery-overload reference)
# ---------------------------------------------------------------------------

POLICY_LABELS = {
    "no-control":         "No control",
    "circuit-breaker":    "Circuit breaker",
    "envoy-retry-budget": "Envoy budget",
    "arolla":             "Arolla",
}

POLICY_COLORS = {
    "no-control":         "#d62728",  # red
    "circuit-breaker":    "#ff7f0e",  # orange
    "envoy-retry-budget": "#2ca02c",  # green
    "arolla":             "#1f77b4",  # blue
}

POLICY_ORDER = ["no-control", "circuit-breaker", "envoy-retry-budget", "arolla"]


def _apply_paper_style() -> None:
    """One-shot rcParams update — call before drawing the first figure."""
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        12,
        "axes.labelsize":   13,
        "axes.titlesize":   13,
        "xtick.labelsize":  11,
        "ytick.labelsize":  11,
        "legend.fontsize":  11,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    1.0,
        "lines.linewidth":   2.0,
        "figure.dpi":        110,
    })


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_client_csv(client_dir: Path) -> pd.DataFrame:
    """Concatenate every CSV under client_dir. Empty frame if none found."""
    frames: List[pd.DataFrame] = []
    if not client_dir.exists():
        return pd.DataFrame()
    for csv in sorted(client_dir.glob("*.csv")):
        try:
            df = pd.read_csv(csv)
            frames.append(df)
        except Exception as e:
            print(f"[warn] could not read {csv}: {e}", file=sys.stderr)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # traffic_gen.py schema: timestamp, profile, worker, request_id, path,
    #                       attempt, is_retry, status, ok, latency_s
    # Normalize types we rely on.
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    if "attempt" in df.columns:
        df["attempt"] = pd.to_numeric(df["attempt"], errors="coerce").fillna(1).astype(int)
    if "ok" in df.columns:
        df["ok"] = df["ok"].astype(str).str.lower().isin(["true", "1", "t", "yes"])
    if "is_retry" in df.columns:
        df["is_retry"] = df["is_retry"].astype(str).str.lower().isin(["true", "1", "t", "yes"])
    return df


def load_timeline(policy_dir: Path) -> Optional[dict]:
    f = policy_dir / "timeline.json"
    if not f.exists():
        return None
    with open(f) as fh:
        return json.load(fh)


def parse_envoy_stats(stats_file: Path) -> Dict[str, float]:
    """Parse an Envoy /stats dump (one 'name: value' per line)."""
    out: Dict[str, float] = {}
    if not stats_file.exists():
        return out
    for line in stats_file.read_text().splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        try:
            out[key] = float(val)
        except ValueError:
            # skip histogram lines etc.
            continue
    return out


# Regex used by parse_istio_duration_buckets(). Compiled once.
import re as _re
_BUCKET_LINE = _re.compile(
    r'istio_request_duration_milliseconds_bucket\{([^}]*)\}\s+(\S+)'
)
_LABEL_PAIR = _re.compile(r'(\w+)="([^"]*)"')


def _read_bucket_map(prom_file: Path) -> Dict[str, Dict[str, float]]:
    """
    Low-level helper: parse one `.prom` file and return
      {destination_service_name -> {le_str -> cumulative_count}}
    aggregated across every non-`le` label combination, filtering to
    `reporter="destination"` (the receiving sidecar's view).
    """
    out: Dict[str, Dict[str, float]] = {}
    if not prom_file.exists():
        return out
    with open(prom_file) as f:
        for line in f:
            m = _BUCKET_LINE.match(line)
            if not m:
                continue
            labels_str, value_str = m.groups()
            labels = dict(_LABEL_PAIR.findall(labels_str))
            if labels.get("reporter") != "destination":
                continue
            svc = labels.get("destination_service_name") or ""
            le = labels.get("le") or ""
            if not svc or not le:
                continue
            try:
                count = float(value_str)
            except ValueError:
                continue
            if svc not in out:
                out[svc] = {}
            out[svc][le] = out[svc].get(le, 0.0) + count
    return out


def parse_istio_duration_buckets(
    prom_file: Path,
    baseline_file: Optional[Path] = None,
) -> Dict[str, List[tuple]]:
    """
    Parse `istio_request_duration_milliseconds_bucket` Prometheus histograms
    from a single <svc>.prom file.

    Returns a dict {destination_service_name -> sorted [(le_ms, count), ...]}
    aggregated across all non-`le` label combinations (source workload,
    response code, response flags, etc.) for requests where
    `reporter="destination"` — i.e. as reported by the *receiving* sidecar,
    which is the cleanest per-hop latency view.

    If `baseline_file` is provided (e.g. a `<svc>.pre.prom` snapshot taken
    at the start of the experiment), bucket counts from that file are
    subtracted so the result reflects only traffic that happened *during*
    the experiment. Sidecar stats counters are lifetime-cumulative, so
    without a baseline subtraction the CDF would be dominated by whatever
    traffic was sent before this run.

    If a bucket in the baseline is larger than in the post-run file (can
    happen if the sidecar was restarted mid-experiment — counters reset),
    the diff is clamped to zero.
    """
    post = _read_bucket_map(prom_file)
    pre  = _read_bucket_map(baseline_file) if baseline_file else {}

    # Diff: for each (svc, le), post - pre, clamped to zero.
    diffed: Dict[str, Dict[str, float]] = {}
    for svc, bk_post in post.items():
        bk_pre = pre.get(svc, {})
        sub_out: Dict[str, float] = {}
        for le, post_cnt in bk_post.items():
            pre_cnt = bk_pre.get(le, 0.0)
            delta = post_cnt - pre_cnt
            if delta < 0:
                delta = 0.0  # counter reset → treat as fresh from zero
            sub_out[le] = delta
        if any(v > 0 for v in sub_out.values()):
            diffed[svc] = sub_out

    # Convert {le_str -> count} to sorted [(le_ms_float, count), ...]
    import math
    result: Dict[str, List[tuple]] = {}
    for svc, buckets in diffed.items():
        pts = []
        for le_str, cnt in buckets.items():
            le = math.inf if le_str == "+Inf" else float(le_str)
            pts.append((le, cnt))
        pts.sort()
        result[svc] = pts
    return result


def client_latency_timeseries(df: pd.DataFrame, t_ref: float, t_end: float,
                              bin_sec: float = 1.0) -> Dict[str, pd.Series]:
    """
    Per-second client-observed latency percentiles (p50, p95, p99).

    Returns {percentile_name -> pd.Series} where each series is indexed by
    bin (seconds since t_ref) and the value is that percentile of latencies
    in that bin (in milliseconds). Bins outside the observed data range
    stay NaN so matplotlib draws gaps.

    Counts ALL attempts (not just successful ones), so this surfaces both
    healthy-tail latency and failure-induced latency spikes uniformly.
    """
    empty = {"p50": pd.Series(dtype=float),
             "p95": pd.Series(dtype=float),
             "p99": pd.Series(dtype=float)}
    if df.empty or "latency_s" not in df.columns:
        return empty

    window = df[(df["timestamp"] >= t_ref) & (df["timestamp"] <= t_end)].copy()
    if window.empty:
        return empty
    window["bin"] = ((window["timestamp"] - t_ref) // bin_sec).astype(int)
    window["latency_ms"] = window["latency_s"] * 1000.0

    grouped = window.groupby("bin")["latency_ms"]
    p50 = grouped.quantile(0.50)
    p95 = grouped.quantile(0.95)
    p99 = grouped.quantile(0.99)

    n_bins = int((t_end - t_ref) // bin_sec) + 1
    return {
        "p50": p50.reindex(range(n_bins)),
        "p95": p95.reindex(range(n_bins)),
        "p99": p99.reindex(range(n_bins)),
    }


def client_latency_by_phase(df: pd.DataFrame, t_ref: float,
                            experiment: dict) -> Dict[str, np.ndarray]:
    """
    Split client-observed request latencies by scenario phase.

    `t_ref` is typically `timeline.t_warmup_end` so that phase boundaries
    line up with the rest of the analyzer. Returns
    {phase_name -> sorted ndarray of latencies in ms} for phases that
    actually have data.
    """
    if df.empty or "latency_s" not in df.columns:
        return {}

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))

    phases = [
        ("pre-fault", 0.0,                              prefault_sec),
        ("fault",     prefault_sec,                     prefault_sec + fault_sec),
        ("recovery",  prefault_sec + fault_sec,         prefault_sec + fault_sec + recovery_sec),
        ("cooldown",  prefault_sec + fault_sec + recovery_sec,
                      prefault_sec + fault_sec + recovery_sec + cooldown_sec),
    ]

    df = df.copy()
    df["t_rel"] = df["timestamp"] - t_ref
    df["latency_ms"] = df["latency_s"] * 1000.0

    out: Dict[str, np.ndarray] = {}
    for name, lo, hi in phases:
        if hi <= lo:
            continue
        sub = df[(df["t_rel"] >= lo) & (df["t_rel"] < hi)]
        if sub.empty:
            continue
        out[name] = np.sort(sub["latency_ms"].values)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def goodput_timeseries(df: pd.DataFrame, t_ref: float, t_end: float,
                       bin_sec: float = 1.0) -> pd.Series:
    """
    Successful requests per bin_sec, from t_ref to t_end.

    The x-axis of the returned Series is in seconds relative to `t_ref`
    (typically `timeline.t_warmup_end`, i.e. the start of the pre-fault
    baseline). Data before t_ref is dropped entirely so the x-axis starts
    at 0 = end of warmup. Bins within the observed data range are filled
    with 0 (real measurement); bins outside the observed range stay NaN so
    matplotlib draws gaps instead of misleading flatlines.

    A "successful request" is one where ok == True. The client generator
    emits one row per attempt, and breaks the retry loop on the first ok,
    so counting ok rows is equivalent to counting unique successful logical
    requests (matches the paper's "goodput" definition).
    """
    if df.empty:
        return pd.Series(dtype=float)
    ok = df[df["ok"] & (df["timestamp"] >= t_ref) & (df["timestamp"] <= t_end)].copy()
    if ok.empty:
        return pd.Series(dtype=float)
    ok["bin"] = ((ok["timestamp"] - t_ref) // bin_sec).astype(int)
    counts = ok.groupby("bin").size().astype(float)

    # Fill zeros only inside the observed data window; outside stays NaN.
    observed_min = int((df[df["timestamp"] >= t_ref]["timestamp"].min() - t_ref) // bin_sec)
    observed_max = int((df[df["timestamp"] <= t_end]["timestamp"].max() - t_ref) // bin_sec)
    observed_range = range(max(0, observed_min), observed_max + 1)
    counts = counts.reindex(observed_range, fill_value=0.0)

    n_bins = int((t_end - t_ref) // bin_sec) + 1
    counts = counts.reindex(range(n_bins))  # NaN outside observed range
    return counts


def request_rate_timeseries(df: pd.DataFrame, t_ref: float, t_end: float,
                            bin_sec: float = 1.0) -> Dict[str, pd.Series]:
    """
    Per-second attempt-level rates, broken down into three categories:

      - 'root'   : attempts where `attempt == 1` (new logical request starts)
      - 'retry'  : attempts where `attempt > 1`  (retries of a failing logical
                   request, including BOTH client-side retries that hit the
                   wire AND any that failed network-level before a response)
      - 'failed' : attempts where `ok == False`  (regardless of root/retry;
                   every failed attempt is the thing that generates more load
                   downstream via the retry filter chain)

    The categories are NOT mutually exclusive. A failed first attempt is
    counted in both 'root' and 'failed'. That's intentional — the point of
    the plot is to see each category's rate on its own axis.

    Same x-axis convention as the other timeseries helpers: bins are indexed
    relative to `t_ref` (= warmup_end). Bins inside the observed data range
    are filled with 0 (real measurement of "no events in this second"); bins
    outside the observed range stay NaN so matplotlib draws gaps.
    """
    empty = {"root": pd.Series(dtype=float),
             "retry": pd.Series(dtype=float),
             "failed": pd.Series(dtype=float)}
    if df.empty:
        return empty

    window = df[(df["timestamp"] >= t_ref) & (df["timestamp"] <= t_end)].copy()
    if window.empty:
        return empty
    window["bin"] = ((window["timestamp"] - t_ref) // bin_sec).astype(int)

    n_bins = int((t_end - t_ref) // bin_sec) + 1
    observed_min = int(window["bin"].min())
    observed_max = int(window["bin"].max())
    observed_range = range(max(0, observed_min), observed_max + 1)

    def _rate(mask: pd.Series) -> pd.Series:
        counts = window[mask].groupby("bin").size().astype(float)
        counts = counts.reindex(observed_range, fill_value=0.0)
        return counts.reindex(range(n_bins))  # NaN outside observed

    return {
        "root":   _rate(window["attempt"] == 1),
        "retry":  _rate(window["attempt"] > 1),
        "failed": _rate(~window["ok"].astype(bool)),
    }


def success_rate_timeseries(df: pd.DataFrame, t_ref: float, t_end: float,
                            bin_sec: float = 1.0,
                            smooth_win: int = 5) -> pd.Series:
    """
    Per-request end-user success rate over time, in percent.

    The x-axis is in seconds relative to `t_ref` (typically warmup_end).
    Data before t_ref is dropped — the x-axis starts at 0 = start of the
    pre-fault baseline. Warmup data is not shown.

    Each *logical* request (grouped by request_id) contributes one data
    point: the final attempt's `ok` value. That point is binned by the
    final attempt's timestamp. For each bin:

        sr(bin) = 100 * (# requests where last attempt ok) / (# requests)

    A rolling mean of `smooth_win` bins is applied for readability
    (default 5s). Bins with no completed requests stay NaN.
    """
    if df.empty or "request_id" not in df.columns:
        return pd.Series(dtype=float)

    # Final attempt per logical request = row with max timestamp for each id.
    last = (
        df.sort_values("timestamp")
          .groupby("request_id", as_index=False)
          .tail(1)
          .copy()
    )

    in_window = last[(last["timestamp"] >= t_ref) & (last["timestamp"] <= t_end)]
    if in_window.empty:
        return pd.Series(dtype=float)

    in_window = in_window.copy()
    in_window["bin"] = ((in_window["timestamp"] - t_ref) // bin_sec).astype(int)

    per_bin = in_window.groupby("bin").agg(
        total=("ok", "count"),
        ok=("ok", "sum"),
    )
    per_bin["sr"] = per_bin["ok"].astype(float) / per_bin["total"].astype(float) * 100.0

    n_bins = int((t_end - t_ref) // bin_sec) + 1
    sr = per_bin["sr"].reindex(range(n_bins))  # NaN for empty bins
    if smooth_win > 1:
        sr = sr.rolling(window=smooth_win, min_periods=1, center=True).mean()
    return sr


def amplification(df: pd.DataFrame, t_start: float, t_end: float) -> float:
    """
    Retry amplification factor: total attempts / unique first-attempt requests
    within the fault window.

    Paper §6.1: "ratio of total retries (across all hops) to original failed
    requests at the injection point." Our client CSV only observes the
    originating client's view; per-hop retry counts come from sidecar stats.
    For a first approximation we use client-observed attempts, which is a
    lower bound on the true per-hop amplification.
    """
    if df.empty:
        return 0.0
    fault = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
    if fault.empty:
        return 0.0
    total_attempts = len(fault)
    first_attempts = (fault["attempt"] == 1).sum()
    return total_attempts / first_attempts if first_attempts > 0 else 0.0


def retry_efficiency_pct(df: pd.DataFrame, t_start: float, t_end: float) -> float:
    """Fraction of retries (attempt > 1) that ended ok, in percent."""
    if df.empty:
        return 0.0
    fault = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
    retries = fault[fault["attempt"] > 1]
    if len(retries) == 0:
        return 0.0
    ok_retries = retries["ok"].sum()
    return float(ok_retries) / float(len(retries)) * 100.0


def recovery_time_sec(goodput: pd.Series, fault_end_bin: int,
                      pre_fault_bin_start: int, pre_fault_bin_end: int,
                      target_pct: float = 95.0) -> Optional[float]:
    """
    Seconds from fault_end until goodput first reaches target_pct of the
    pre-fault mean. Returns None if it never recovers within the series.
    """
    if goodput.empty:
        return None
    pre = goodput.loc[pre_fault_bin_start:pre_fault_bin_end]
    if pre.empty or pre.mean() == 0:
        return None
    target = pre.mean() * (target_pct / 100.0)
    post = goodput.loc[fault_end_bin:]
    hit = post[post >= target]
    if hit.empty:
        return None
    return float(hit.index[0] - fault_end_bin)


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def _normalize_to_baseline(ts: pd.Series, baseline: float) -> pd.Series:
    """Divide a time series by a scalar baseline. Safe for baseline ≤ 0."""
    if baseline is None or baseline <= 0:
        return ts.astype(float)
    return ts.astype(float) / baseline


def _plot_timeseries(
    runs: Dict[str, dict],
    key: str,
    ylabel: str,
    out_path: Path,
    experiment: dict,
    *,
    ylim: Optional[tuple] = None,
    figsize: tuple = (6.4, 4.0),
) -> None:
    """
    Overlay a per-policy time series with a fault-region shade.

    The x-axis is the **scenario timeline** from `experiment.json`:

        x=0                                 → start of pre-fault baseline
        x=prefault_sec                      → fault injected
        x=prefault_sec+fault_sec            → fault removed
        x=+recovery_sec                     → load stops
        x=total                             → end of cooldown

    The plot x-axis is explicitly pinned to [0, total] via `set_xlim`, and
    data is plotted at its actual scenario-relative bin index. If the
    client's data arrives late (e.g. warmup_sec=0 with a slow client
    startup), the leading region is visibly empty — that's intentional, it
    shows *which part of the scenario the client missed*.
    """
    _apply_paper_style()
    fig, ax = plt.subplots(figsize=figsize)
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    # All coordinates come from experiment.json durations.
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total_measured_duration = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x = prefault_sec
    fault_end_x   = prefault_sec + fault_sec

    for policy in ordered:
        d = runs[policy]
        ts = d.get(key)
        if ts is None or ts.empty:
            continue
        color = POLICY_COLORS.get(policy, "C0")
        label = POLICY_LABELS.get(policy, policy)
        ax.plot(ts.index, ts.values, label=label, color=color)

    # Fault region — light gray rectangle with italic "fault" label inside.
    if fault_sec > 0:
        ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                   alpha=0.55, zorder=0)
        ymin, ymax = ax.get_ylim() if ylim is None else ylim
        label_y = ymax - 0.04 * (ymax - ymin)
        ax.text(
            (fault_start_x + fault_end_x) / 2.0,
            label_y, "fault",
            ha="center", va="top",
            fontsize=12, fontstyle="italic", color="#555555",
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    if ylim is not None:
        ax.set_ylim(*ylim)
    # Pin the x-axis to the scenario duration so 0 and the full scenario
    # are always visible, regardless of where actual data lands.
    if total_measured_duration > 0:
        ax.set_xlim(0, total_measured_duration)
    ax.legend(
        loc="lower right",
        frameon=True,
        fancybox=False,
        edgecolor="#888888",
        framealpha=0.95,
    )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def histogram_percentile(pts: List[tuple], p: float) -> Optional[float]:
    """
    Linear-interpolated percentile from a sorted list of
    [(le_ms, cumulative_count), ...] histogram bucket points.

    `p` is a probability in (0, 1). Returns None if pts is empty or total
    count is zero. If the target percentile falls in the +Inf bucket we
    return the last finite `le` as a conservative lower bound.
    """
    if not pts:
        return None
    total = pts[-1][1]
    if total <= 0:
        return None
    target = p * total
    prev_le: Optional[float] = None
    prev_cnt = 0.0
    last_finite_le = None
    for le, cnt in pts:
        if le != float("inf"):
            last_finite_le = le
        if cnt >= target:
            # Target falls inside this bucket.
            if le == float("inf"):
                return last_finite_le  # upper bound unknown
            if prev_le is None or cnt == prev_cnt:
                return le
            # Linear interpolate between prev_le and le.
            frac = (target - prev_cnt) / (cnt - prev_cnt)
            return prev_le + frac * (le - prev_le)
        prev_le = le
        prev_cnt = cnt
    return last_finite_le


def print_latency_summary(runs: Dict[str, dict]) -> None:
    """
    Print two tables to stdout:
      - Client-observed per-attempt p50/p99 latency, split by phase
      - Per-service inbound p50/p99 latency, from Istio histogram buckets

    Called after summary.csv so the per-service percentiles are the last
    thing on the terminal and easy to copy into service-retries.yaml.
    """
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]

    # ---- Client-side ----
    has_client_data = any(
        (runs[p].get("client_latency_by_phase") or {}) for p in ordered_policies
    )
    if has_client_data:
        print("\n=== Client-side per-attempt latency (ms) ===")
        print(f"{'policy':<20s} {'phase':<12s} {'n':>8s} {'p50':>9s} {'p99':>9s}")
        print("-" * 60)
        for policy in ordered_policies:
            cl = runs[policy].get("client_latency_by_phase") or {}
            for phase in ("pre-fault", "fault", "recovery", "cooldown"):
                samples = cl.get(phase)
                if samples is None or len(samples) == 0:
                    continue
                p50 = float(np.percentile(samples, 50))
                p99 = float(np.percentile(samples, 99))
                print(f"{policy:<20s} {phase:<12s} {len(samples):>8d} "
                      f"{p50:>8.1f}  {p99:>8.1f}")

    # ---- Per-service (from histogram buckets) ----
    has_svc_data = any(
        (runs[p].get("service_buckets") or {}) for p in ordered_policies
    )
    if has_svc_data:
        print("\n=== Per-service inbound latency (ms, reporter=destination) ===")
        print(f"{'policy':<20s} {'service':<24s} {'scope':<9s} "
              f"{'n':>8s} {'p50':>9s} {'p99':>9s}")
        print("-" * 82)
        for policy in ordered_policies:
            buckets = runs[policy].get("service_buckets") or {}
            scope = "run" if runs[policy].get("service_buckets_scoped") else "lifetime"
            # Order by descending total count so busy services print first.
            svc_order = sorted(
                buckets.keys(),
                key=lambda s: -(buckets[s][-1][1] if buckets[s] else 0),
            )
            for svc in svc_order:
                pts = buckets[svc]
                if not pts:
                    continue
                total = pts[-1][1]
                if total <= 0:
                    continue
                p50 = histogram_percentile(pts, 0.50) or 0.0
                p99 = histogram_percentile(pts, 0.99) or 0.0
                print(f"{policy:<20s} {svc:<24s} {scope:<9s} "
                      f"{int(total):>8d} {p50:>8.1f}  {p99:>8.1f}")
    print()


def plot_latency_timeseries(runs: Dict[str, dict], out_path: Path, experiment: dict):
    """
    Per-second client-observed latency percentiles (p50/p95/p99) over time,
    one row per policy, log-scale y-axis. Useful for spotting:

      - tail-latency growth as load builds
      - mesh-retry-induced latency plateaus (p99 climbing toward client timeout)
      - the moment a fault trigger fires (sudden latency spike)
      - whether recovery is monotone or oscillating
    """
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    n = len(ordered)
    if n == 0:
        return

    # Scenario coordinates (same as the other timeseries plots)
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x = prefault_sec
    fault_end_x   = prefault_sec + fault_sec

    pct_colors = {
        "p50": "#1f77b4",   # blue
        "p95": "#ff7f0e",   # orange
        "p99": "#d62728",   # red
    }
    pct_labels = {"p50": "p50", "p95": "p95", "p99": "p99"}

    fig, axes = plt.subplots(n, 1, figsize=(7.2, 3.0 * n), sharex=True, squeeze=False)

    for row, policy in enumerate(ordered):
        ax = axes[row][0]
        d = runs[policy]
        lat_ts = d.get("client_latency_ts") or {}

        for pct in ("p50", "p95", "p99"):
            ts = lat_ts.get(pct)
            if ts is None or ts.empty:
                continue
            ax.plot(ts.index, ts.values,
                    label=pct_labels[pct], color=pct_colors[pct], linewidth=1.6)

        # Fault region shading
        if fault_sec > 0:
            ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                       alpha=0.55, zorder=0)
            ymin, ymax = ax.get_ylim()
            ax.text(
                (fault_start_x + fault_end_x) / 2.0,
                ymax * 0.92, "fault",
                ha="center", va="top",
                fontsize=11, fontstyle="italic", color="#555555",
            )

        ax.set_yscale("log")
        ax.set_ylabel("Client latency (ms)")
        if total > 0:
            ax.set_xlim(0, total)
        ax.grid(True, which="both", alpha=0.25, linewidth=0.5)
        ax.legend(loc="upper right", frameon=True, fancybox=False,
                  edgecolor="#888888", framealpha=0.95, fontsize=10)
        if n > 1:
            ax.set_title(POLICY_LABELS.get(policy, policy),
                         fontsize=12, loc="left", pad=4)

    axes[-1][0].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_latency_cdf(runs: Dict[str, dict], out_path: Path, experiment: dict):
    """
    Two-panel CDF plot (side-by-side):
      Left  — client-side end-to-end per-attempt latency, broken down by phase
      Right — per-service inbound latency from istio_request_duration_milliseconds
              histograms in <svc>.prom (reporter=destination, i.e. server view)

    Single-policy runs get one figure. For multi-policy runs we stack one
    row per policy.
    """
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    n = len(ordered)
    if n == 0:
        return

    fig, axes = plt.subplots(n, 2, figsize=(11.0, 3.8 * n), squeeze=False)

    # Consistent colors for phases (left subplot) and services (right subplot).
    phase_colors = {
        "pre-fault": "#1f77b4",  # blue
        "fault":     "#d62728",  # red
        "recovery":  "#2ca02c",  # green
        "cooldown":  "#7f7f7f",  # gray
    }
    # Cycle service colors from the default tab10 palette; deterministic by name.
    service_tab = plt.cm.tab10.colors

    for row, policy in enumerate(ordered):
        ax_client = axes[row][0]
        ax_svc    = axes[row][1]
        d = runs[policy]

        # ---------- LEFT: client-side CDF per phase ---------------------------
        client_lat = d.get("client_latency_by_phase") or {}
        for phase in ("pre-fault", "fault", "recovery", "cooldown"):
            samples = client_lat.get(phase)
            if samples is None or len(samples) == 0:
                continue
            ys = np.arange(1, len(samples) + 1) / len(samples)
            ax_client.plot(samples, ys,
                           label=f"{phase} (n={len(samples)})",
                           color=phase_colors.get(phase, "black"),
                           linewidth=1.8)
        ax_client.set_xscale("log")
        ax_client.set_xlabel("Client-observed latency (ms)")
        ax_client.set_ylabel("CDF")
        ax_client.set_ylim(0, 1.02)
        ax_client.grid(True, which="both", alpha=0.25, linewidth=0.5)
        ax_client.legend(loc="lower right", frameon=True, fancybox=False,
                         edgecolor="#888888", framealpha=0.95, fontsize=10)
        if n > 1:
            ax_client.set_title(f"{POLICY_LABELS.get(policy, policy)} — client",
                                fontsize=12, loc="left", pad=4)
        else:
            ax_client.set_title("Client-side per-attempt latency",
                                fontsize=12, loc="left", pad=4)

        # ---------- RIGHT: per-service server-side CDF ------------------------
        svc_buckets = d.get("service_buckets") or {}
        if svc_buckets:
            # Order services by total count so the busiest appear first in the
            # legend (and get the most eye-catching color).
            svc_ordered = sorted(
                svc_buckets.keys(),
                key=lambda s: -(svc_buckets[s][-1][1] if svc_buckets[s] else 0),
            )
            for i, svc in enumerate(svc_ordered):
                pts = svc_buckets[svc]
                if not pts:
                    continue
                total = pts[-1][1]
                if total <= 0:
                    continue
                xs = [le for le, _ in pts if le != float("inf")]
                ys = [cnt / total for le, cnt in pts if le != float("inf")]
                if not xs:
                    continue
                ax_svc.plot(xs, ys,
                            label=f"{svc} (n={int(total)})",
                            color=service_tab[i % len(service_tab)],
                            linewidth=1.8, marker="o", markersize=3)
        ax_svc.set_xscale("log")
        ax_svc.set_xlabel("Per-hop service latency (ms)")
        ax_svc.set_ylabel("CDF")
        ax_svc.set_ylim(0, 1.02)
        ax_svc.grid(True, which="both", alpha=0.25, linewidth=0.5)
        ax_svc.legend(loc="lower right", frameon=True, fancybox=False,
                      edgecolor="#888888", framealpha=0.95, fontsize=9)
        # Scope note: labeled "(run)" when pre.prom baselines were present
        # so bucket counts are diffed; "(lifetime)" when they weren't.
        scope_note = " (run)" if d.get("service_buckets_scoped") else " (lifetime)"
        if n > 1:
            ax_svc.set_title(f"{POLICY_LABELS.get(policy, policy)} — services{scope_note}",
                             fontsize=12, loc="left", pad=4)
        else:
            ax_svc.set_title(f"Per-service inbound latency{scope_note}",
                             fontsize=12, loc="left", pad=4)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_rps(runs: Dict[str, dict], out_path: Path, experiment: dict):
    """
    Per-second request-rate breakdown: root / retry / failed as three lines.

    For single-policy runs this is one axes with three lines. For multi-policy
    runs we stack one subplot per policy with a shared x-axis so you can
    compare how retries and failures evolve under each policy.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    n = len(ordered)
    if n == 0:
        return

    # Scenario coordinates
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x = prefault_sec
    fault_end_x   = prefault_sec + fault_sec

    # Colors: matplotlib defaults (blue/orange/red map cleanly to
    # root/retry/failed).
    series_colors = {
        "root":   "#1f77b4",  # blue
        "retry":  "#ff7f0e",  # orange
        "failed": "#d62728",  # red
    }
    series_labels = {
        "root":   "Root requests",
        "retry":  "Retry requests",
        "failed": "Failed requests",
    }

    fig, axes = plt.subplots(n, 1, figsize=(7.2, 3.2 * n), sharex=True)
    if n == 1:
        axes = [axes]

    for ax, policy in zip(axes, ordered):
        d = runs[policy]
        rates = d.get("rates")
        if not rates:
            continue

        for series_key in ("root", "retry", "failed"):
            ts = rates.get(series_key)
            if ts is None or ts.empty:
                continue
            ax.plot(ts.index, ts.values,
                    label=series_labels[series_key],
                    color=series_colors[series_key])

        if fault_sec > 0:
            ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                       alpha=0.55, zorder=0)
            ymin, ymax = ax.get_ylim()
            label_y = ymax - 0.04 * (ymax - ymin)
            ax.text(
                (fault_start_x + fault_end_x) / 2.0,
                label_y, "fault",
                ha="center", va="top",
                fontsize=11, fontstyle="italic", color="#555555",
            )

        if n > 1:
            ax.set_title(POLICY_LABELS.get(policy, policy), fontsize=12,
                         loc="left", pad=4)
        ax.set_ylabel("Requests / s")
        if total > 0:
            ax.set_xlim(0, total)
        ax.legend(loc="upper right", frameon=True, fancybox=False,
                  edgecolor="#888888", framealpha=0.95)

    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_success_rate(runs: Dict[str, dict], out_path: Path, experiment: dict):
    """End-user success rate (%) vs time, one line per policy."""
    _plot_timeseries(
        runs,
        key="success_rate",
        ylabel="Success rate (%)",
        out_path=out_path,
        experiment=experiment,
        ylim=(-5, 105),
    )


def plot_bar(runs: Dict[str, dict], key: str, ylabel: str, title: str,
             out_path: Path, value_fmt: str = "{:.2f}"):
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    labels = [POLICY_LABELS.get(p, p) for p in ordered]
    values = [runs[p][key] for p in ordered]
    colors = [POLICY_COLORS.get(p, "gray") for p in ordered]

    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    bars = ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.8)
    if values:
        ymax = max(values) if any(values) else 1.0
        for bar, v in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + ymax * 0.02,
                value_fmt.format(v), ha="center", va="bottom", fontsize=11,
            )
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_policy(policy_dir: Path, experiment: dict) -> Optional[dict]:
    timeline = load_timeline(policy_dir)
    if not timeline:
        print(f"[warn] {policy_dir.name}: no timeline.json", file=sys.stderr)
        return None

    df = load_client_csv(policy_dir / "client-metrics")
    if df.empty:
        print(f"[warn] {policy_dir.name}: no client CSV data", file=sys.stderr)
        return None

    t_start = float(timeline["t_start"])
    t_fault_start = float(timeline["t_fault_start"])
    t_fault_end = float(timeline["t_fault_end"])
    t_prefault_end = float(timeline["t_prefault_end"])
    t_warmup_end = float(timeline["t_warmup_end"])
    t_cooldown_end = float(timeline["t_cooldown_end"])

    # Time-series x-axis is relative to warmup_end: t=0 is where the
    # pre-fault baseline starts. Warmup data is dropped from the plot.
    t_ref = t_warmup_end

    goodput = goodput_timeseries(df, t_ref, t_cooldown_end, bin_sec=1.0)
    success_rate = success_rate_timeseries(
        df, t_ref, t_cooldown_end, bin_sec=1.0, smooth_win=5,
    )
    rates = request_rate_timeseries(df, t_ref, t_cooldown_end, bin_sec=1.0)
    client_latency_ts = client_latency_timeseries(
        df, t_ref, t_cooldown_end, bin_sec=1.0,
    )
    amp = amplification(df, t_fault_start, t_fault_end)
    reff = retry_efficiency_pct(df, t_fault_start, t_fault_end)

    # All bin coordinates are relative to t_ref (warmup_end).
    # Pre-fault baseline runs from bin 0 to prefault_end.
    pre_start_bin = 0
    pre_end_bin = int(t_prefault_end - t_ref)
    fault_start_bin = int(t_fault_start - t_ref)
    fault_end_bin = int(t_fault_end - t_ref)

    recovery = recovery_time_sec(goodput, fault_end_bin, pre_start_bin, pre_end_bin)
    avg_goodput_fault = float(goodput.loc[fault_start_bin:fault_end_bin].mean()) \
        if not goodput.empty else 0.0
    avg_goodput_pre = float(goodput.loc[pre_start_bin:pre_end_bin].mean()) \
        if not goodput.empty else 0.0

    # Client-side latency split by scenario phase (for the CDF plot).
    client_latency_phase = client_latency_by_phase(df, t_ref, experiment)

    # Per-service inbound-latency histograms aggregated across every .prom
    # file in sidecar-stats/. Each sidecar only reports data for requests it
    # received (reporter=destination), so pooling all files gives us one
    # bucket series per destination service.
    #
    # When a <svc>.pre.prom baseline snapshot exists (written by
    # run-experiment.sh before the load starts), we pass it to the parser
    # so bucket counts are diffed and reflect only this run's traffic.
    service_buckets: Dict[str, List[tuple]] = {}
    service_buckets_scoped = False  # True if .pre.prom baselines were used
    sidecar_dir_for_buckets = policy_dir / "sidecar-stats"
    if sidecar_dir_for_buckets.exists():
        for prom_file in sorted(sidecar_dir_for_buckets.glob("*.prom")):
            # Skip the .pre.prom snapshots — they're paired with their
            # post-run siblings below, not parsed on their own.
            if prom_file.name.endswith(".pre.prom"):
                continue
            baseline_path = prom_file.with_name(
                prom_file.stem + ".pre.prom"
            )
            baseline = baseline_path if baseline_path.exists() else None
            if baseline is not None:
                service_buckets_scoped = True
            parsed = parse_istio_duration_buckets(prom_file, baseline)
            for svc, pts in parsed.items():
                if svc not in service_buckets:
                    service_buckets[svc] = pts
                # If another file already contributed this svc, keep the
                # existing series — sidecars that received traffic for `svc`
                # all report the same diffed bucket counts.

    # Try to enrich with Arolla sidecar counters (if policy is arolla).
    sidecar_dir = policy_dir / "sidecar-stats"
    arolla_admitted = 0.0
    arolla_rejected = 0.0
    if sidecar_dir.exists():
        for stats_file in sidecar_dir.glob("*.stats"):
            parsed = parse_envoy_stats(stats_file)
            for name, val in parsed.items():
                if "arolla_retries_admitted_total" in name:
                    arolla_admitted += val
                elif "arolla_retries_rejected_total" in name:
                    arolla_rejected += val

    return {
        "goodput": goodput,
        "success_rate": success_rate,
        "rates": rates,
        "client_latency_ts": client_latency_ts,
        "client_latency_by_phase": client_latency_phase,
        "service_buckets": service_buckets,
        "service_buckets_scoped": service_buckets_scoped,
        "fault_start_bin": fault_start_bin,
        "fault_end_bin": fault_end_bin,
        "amplification": amp,
        "retry_efficiency_pct": reff,
        "avg_goodput_fault": avg_goodput_fault,
        "avg_goodput_pre": avg_goodput_pre,
        "recovery_sec": recovery if recovery is not None else float("nan"),
        "arolla_admitted": arolla_admitted,
        "arolla_rejected": arolla_rejected,
    }


def main():
    if len(sys.argv) < 2:
        print("usage: analyze.py <run_dir>", file=sys.stderr)
        sys.exit(1)
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"[error] not a directory: {run_dir}", file=sys.stderr)
        sys.exit(1)

    exp_json = run_dir / "experiment.json"
    experiment = {}
    if exp_json.exists():
        with open(exp_json) as f:
            experiment = json.load(f)

    policies = experiment.get("policies") or \
        [p.name for p in run_dir.iterdir() if p.is_dir() and p.name != "plots"]
    scenario_title = experiment.get("scenario_title", "Prototype experiment")

    runs: Dict[str, dict] = {}
    for policy in policies:
        pdir = run_dir / policy
        if not pdir.is_dir():
            continue
        result = process_policy(pdir, experiment)
        if result is not None:
            runs[policy] = result

    if not runs:
        print("[error] no policy results could be loaded", file=sys.stderr)
        sys.exit(2)

    # ---- summary.csv ----
    rows = []
    for policy, d in runs.items():
        rows.append({
            "policy": policy,
            "amplification": d["amplification"],
            "retry_efficiency_pct": d["retry_efficiency_pct"],
            "avg_goodput_prefault": d["avg_goodput_pre"],
            "avg_goodput_fault": d["avg_goodput_fault"],
            "recovery_sec": d["recovery_sec"],
            "arolla_admitted": d["arolla_admitted"],
            "arolla_rejected": d["arolla_rejected"],
        })
    summary_df = pd.DataFrame(rows)
    summary_path = run_dir / "summary.csv"
    summary_df.to_csv(summary_path, index=False)
    print(f"\nwrote {summary_path}")
    print(summary_df.to_string(index=False))

    # ---- latency p50/p99 tables (client-side + per-service) ----
    print_latency_summary(runs)

    # ---- plots ----
    plots_dir = run_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    print(f"\nplots:")
    plot_rps(runs, plots_dir / "rps.pdf", experiment)
    plot_success_rate(runs, plots_dir / "success-rate.pdf", experiment)
    plot_latency_timeseries(runs, plots_dir / "latency-ts.pdf", experiment)
    plot_latency_cdf(runs, plots_dir / "latency-cdf.pdf", experiment)
    plot_bar(runs, "amplification",
             ylabel="Retry amplification (attempts / first attempts)",
             title="Retry amplification during failure",
             out_path=plots_dir / "amplification.pdf",
             value_fmt="{:.2f}")
    plot_bar(runs, "retry_efficiency_pct",
             ylabel="Retry efficiency (%)",
             title="Retry efficiency during failure",
             out_path=plots_dir / "retry-efficiency.pdf",
             value_fmt="{:.1f}")
    plot_bar(runs, "recovery_sec",
             ylabel="Recovery time (s)",
             title="Time to reach 95% of pre-fault goodput",
             out_path=plots_dir / "recovery-time.pdf",
             value_fmt="{:.1f}")


if __name__ == "__main__":
    main()
