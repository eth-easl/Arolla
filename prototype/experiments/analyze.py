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
import re as _re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from sustained_recovery import per_spike_recovery

# ---------------------------------------------------------------------------
# Style — matches the paper figure aesthetic (recovery-overload reference)
# ---------------------------------------------------------------------------

POLICY_LABELS = {
    "no-control":         "No control",
    "circuit-breaker":    "Circuit breaker",
    "envoy-retry-budget": "Retry budget",
    "arolla":             "Arolla",
    "arolla-fairness":    "Arolla (fairness)",
}

POLICY_COLORS = {
    "no-control":         "#C83232",  # retryred
    "circuit-breaker":    "#E68C14",  # warnorg
    "envoy-retry-budget": "#3264B4",  # calmblue
    "arolla":             "#218B21",  # goodputgreen
    "arolla-fairness":    "#1B6B6B",  # teal
}

# !55 variants (55% color + 45% white) for bar fills.
POLICY_COLORS_FILL = {
    "no-control":         "#E18E8E",  # retryred!55
    "circuit-breaker":    "#F1C07E",  # warnorg!55
    "envoy-retry-budget": "#8EAAD6",  # calmblue!55
    "arolla":             "#85BF85",  # goodputgreen!55
    "arolla-fairness":    "#7BB8B8",  # teal!55
}

POLICY_MARKERS = {
    "no-control":         "s",   # square
    "circuit-breaker":    "^",   # triangle
    "envoy-retry-budget": "D",   # diamond
    "arolla":             "o",   # circle
    "arolla-fairness":    "v",   # inverted triangle
}

POLICY_LINESTYLES = {
    "no-control":         (0, (4, 2)),                # long dashes
    "circuit-breaker":    (0, (1, 1.5)),              # dense dots
    "envoy-retry-budget": (0, (2, 1.5)),              # short dashes
    "arolla":             "-",                        # solid (hero)
    "arolla-fairness":    (0, (5, 1.2, 1, 1.2, 1, 1.2)),  # dash-dot-dot
}

POLICY_ORDER = ["no-control", "circuit-breaker", "envoy-retry-budget", "arolla", "arolla-fairness"]


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


def _fault_band_x(experiment: dict) -> tuple:
    """
    Return (fault_start_x, fault_end_x) for the figure fault-band shading,
    in seconds since the prefault start (= the figure x-axis origin).

    Always uses the *intended* fault window from `experiment["prefault_sec"]`
    and `experiment["fault_sec"]`, i.e. the schedule the runner was
    configured to follow. This matches the figure caption ("fault: 10 s")
    and the methodology description in the paper.

    Note: the per-policy `t_fault_actual_*` timestamps in timeline.json
    record when chaos-apply / chaos-delete kubectl calls actually returned,
    which can drift several seconds from the intended boundary because of
    snapshot wall time and chaos-mesh dispatch latency. We deliberately
    do NOT use them here — they're useful for forensics (and the analyzer
    still consumes them inside process_policy() for the per-bin
    calculations) but they make the figure band visually inconsistent
    with where the failures actually start.
    """
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    return prefault_sec, prefault_sec + fault_sec


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


# Phase boundaries are recorded as filename suffixes by run-experiment.sh's
# dump_sidecar_stats. The order here defines the snapshot sequence and
# therefore the per-phase deltas computed below.
#
#   .pre          → captured before warmup
#   .warmup_end   → captured at the end of warmup
#   .fault_start  → captured at the end of prefault (right before fault)
#   .fault_end    → captured at the end of fault (right after trigger removed)
#   .recovery_end → captured at the end of recovery
#   ""            → captured after cooldown (post-run)
#
# Older runs only have .pre and "" — the parser tolerates missing snapshots
# and skips phases that don't have both endpoints.
PHASE_SNAPSHOTS = [
    ("warmup",   ".pre",          ".warmup_end"),
    ("prefault", ".warmup_end",   ".fault_start"),
    ("fault",    ".fault_start",  ".fault_end"),
    ("recovery", ".fault_end",    ".recovery_end"),
    ("cooldown", ".recovery_end", ""),
]

# Counter families we expose for the per-phase retry plots. Each one is
# scraped from cluster.outbound|<port>||<fqdn>;.upstream_rq_<name>.
RETRY_COUNTERS = (
    "upstream_rq_retry",
    "upstream_rq_retry_success",
    "upstream_rq_retry_overflow",
    "upstream_rq_retry_limit_exceeded",
)

# Compiled once. Matches a line like:
#   cluster.outbound|7070||cartservice.online-boutique.svc.cluster.local;.upstream_rq_retry: 12345
_RETRY_LINE = _re.compile(
    r'^cluster\.outbound\|(\d+)\|\|([^;]+);\.(upstream_rq_retry(?:_success|_overflow|_limit_exceeded)?):\s+(\d+)\s*$'
)


def _parse_retry_counters(stats_file: Path) -> Dict[Tuple[str, str], int]:
    """
    Extract per-upstream retry counters from one Envoy /stats dump.
    Returns {(upstream_short_name, counter_name): value}, where the
    upstream short name is the first DNS label of the upstream FQDN
    (e.g. "cartservice"). Lines that don't match are silently skipped.
    """
    out: Dict[Tuple[str, str], int] = {}
    if not stats_file.exists():
        return out
    for line in stats_file.read_text().splitlines():
        m = _RETRY_LINE.match(line)
        if not m:
            continue
        _port, fqdn, counter, value = m.groups()
        upstream = fqdn.split('.', 1)[0]   # "cartservice.online..." → "cartservice"
        out[(upstream, counter)] = int(value)
    return out


def discover_services_from_sidecar_dir(sidecar_dir: Path) -> List[str]:
    """
    Infer the set of caller services that have stats files in `sidecar_dir`.
    Returns a sorted list of service names (one per `<svc>.stats` file,
    deduped across phase suffixes).
    """
    if not sidecar_dir.exists():
        return []
    services = set()
    for path in sidecar_dir.glob("*.stats"):
        # Strip phase suffix if present. Filenames look like:
        #   frontend.stats
        #   frontend.pre.stats
        #   frontend.fault_start.stats
        # We want "frontend" in all three cases.
        stem = path.stem  # drops .stats
        # If the stem contains a dot, the part before the first dot is the
        # service name. (Service names themselves contain no dots.)
        svc = stem.split('.', 1)[0]
        services.add(svc)
    return sorted(services)


def parse_phase_retry_deltas(
    sidecar_dir: Path,
    services: List[str],
) -> Dict[str, Dict[Tuple[str, str], Dict[str, int]]]:
    """
    Walk every <svc>.<phase>.stats file in `sidecar_dir` and compute
    per-phase deltas for each (caller, upstream) pair across the four
    retry counters.

    Returns a nested structure:
        result[phase][(caller, upstream)][counter] = delta_value

    where `phase` is one of warmup/prefault/fault/recovery/cooldown.

    A phase is included in the result only if BOTH of its snapshot
    endpoints exist on disk for at least one caller. This prevents
    older runs (which only have .pre and "" snapshots) from producing
    bogus negative deltas where a missing snapshot is treated as zero.
    """
    result: Dict[str, Dict[Tuple[str, str], Dict[str, int]]] = {}
    if not sidecar_dir.exists():
        return result

    # Track which (caller, suffix) snapshots actually exist on disk so
    # we can distinguish "snapshot exists, all counters were zero" from
    # "snapshot doesn't exist". We need this distinction because phase
    # deltas are arithmetic on counter values, and treating a missing
    # snapshot as {} (zero counters) silently gives nonsense for runs
    # that only captured a subset of the phase boundaries.
    snapshot_exists: Dict[Tuple[str, str], bool] = {}
    snapshots: Dict[Tuple[str, str], Dict[Tuple[str, str], int]] = {}
    for caller in services:
        for _phase, _start_suffix, _end_suffix in PHASE_SNAPSHOTS:
            for suffix in (_start_suffix, _end_suffix):
                key = (caller, suffix)
                if key in snapshots:
                    continue
                stats_file = sidecar_dir / f"{caller}{suffix}.stats"
                snapshot_exists[key] = stats_file.exists()
                snapshots[key] = _parse_retry_counters(stats_file)

    # Compute deltas per phase.
    for phase, start_suffix, end_suffix in PHASE_SNAPSHOTS:
        phase_data: Dict[Tuple[str, str], Dict[str, int]] = {}
        for caller in services:
            # Skip this caller for this phase if either endpoint is missing.
            if not snapshot_exists.get((caller, start_suffix), False):
                continue
            if not snapshot_exists.get((caller, end_suffix), False):
                continue
            start = snapshots.get((caller, start_suffix), {})
            end = snapshots.get((caller, end_suffix), {})
            # Union of all (upstream, counter) keys seen in either endpoint.
            keys = set(start) | set(end)
            for upstream, counter in keys:
                delta = end.get((upstream, counter), 0) - start.get((upstream, counter), 0)
                if delta == 0:
                    continue
                phase_data.setdefault((caller, upstream), {})[counter] = delta
        if phase_data:
            result[phase] = phase_data
    return result


def parse_run_retry_deltas(
    sidecar_dir: Path,
    services: List[str],
) -> Dict[Tuple[str, str], Dict[str, int]]:
    """
    Whole-run retry deltas, computed from `<svc>.pre.stats` (taken before
    warmup) and `<svc>.stats` (taken after cooldown). These two snapshots
    are written by run-experiment.sh on every run regardless of the
    `--phase-snapshots` flag, so this works on every run dir.

    Returns {(caller, upstream): {counter: delta}} where each entry is the
    total retries dispatched by `caller` to `upstream` over the entire
    run window. Used by the chain-amplification stacked-bar plot.
    """
    result: Dict[Tuple[str, str], Dict[str, int]] = {}
    if not sidecar_dir.exists():
        return result

    for caller in services:
        pre_file  = sidecar_dir / f"{caller}.pre.stats"
        post_file = sidecar_dir / f"{caller}.stats"
        if not pre_file.exists() or not post_file.exists():
            # Need both endpoints to compute a delta.
            continue
        pre  = _parse_retry_counters(pre_file)
        post = _parse_retry_counters(post_file)
        # Union of all (upstream, counter) keys seen in either snapshot.
        for upstream, counter in set(pre) | set(post):
            delta = post.get((upstream, counter), 0) - pre.get((upstream, counter), 0)
            if delta == 0:
                continue
            result.setdefault((caller, upstream), {})[counter] = delta
    return result


# Regex used by parse_istio_duration_buckets(). Compiled once. (`_re`
# is imported once at the top of the module.)
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
    Per-second client-observed latency percentiles (p50, p90, p95, p99).

    Returns {percentile_name -> pd.Series} where each series is indexed by
    bin (seconds since t_ref) and the value is that percentile of latencies
    in that bin (in milliseconds). Bins outside the observed data range
    stay NaN so matplotlib draws gaps.

    Counts ALL attempts (not just successful ones), so this surfaces both
    healthy-tail latency and failure-induced latency spikes uniformly.
    """
    empty = {"p50": pd.Series(dtype=float),
             "p90": pd.Series(dtype=float),
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
    p90 = grouped.quantile(0.90)
    p95 = grouped.quantile(0.95)
    p99 = grouped.quantile(0.99)

    n_bins = int((t_end - t_ref) // bin_sec) + 1
    return {
        "p50": p50.reindex(range(n_bins)),
        "p90": p90.reindex(range(n_bins)),
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


def retry_by_status_timeseries(
    df: pd.DataFrame, t_ref: float, t_end: float,
    bin_sec: float = 1.0,
) -> Dict[str, pd.Series]:
    """
    Per-second retry counts broken down by the HTTP status of each retry
    attempt. Only counts retries (attempt > 1). Returns a dict mapping
    status label ('429', '500', '502', '503', '504', '0', 'other') to a
    time series of counts.
    """
    empty: Dict[str, pd.Series] = {}
    if df.empty or "attempt" not in df.columns or "status" not in df.columns:
        return empty

    retries = df[
        (df["attempt"] > 1) &
        (df["timestamp"] >= t_ref) &
        (df["timestamp"] <= t_end)
    ].copy()
    if retries.empty:
        return empty

    retries["bin"] = ((retries["timestamp"] - t_ref) // bin_sec).astype(int)
    n_bins = int((t_end - t_ref) // bin_sec) + 1
    observed_min = int(retries["bin"].min())
    observed_max = int(retries["bin"].max())
    observed_range = range(max(0, observed_min), observed_max + 1)

    # Map status to string label; group uncommon ones as "other".
    known = {"429", "500", "502", "503", "504", "0", "-1", "302", "200"}
    retries["status_label"] = retries["status"].astype(str).apply(
        lambda s: s if s in known else "other"
    )
    # Treat -1 (client drop) as "0" (connection failure) for display.
    # Treat 302 and 200 as "success" (retries that succeeded).
    retries["status_label"] = retries["status_label"].replace(
        {"-1": "0", "302": "success", "200": "success"}
    )

    result: Dict[str, pd.Series] = {}
    for label, grp in retries.groupby("status_label"):
        counts = grp.groupby("bin").size().astype(float)
        counts = counts.reindex(observed_range, fill_value=0.0)
        result[str(label)] = counts.reindex(range(n_bins))
    return result


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


def recovery_time_sec(success_rate: pd.Series, fault_end_bin: int,
                      target_pct: float = 95.0) -> Optional[float]:
    """
    Seconds from fault_end until success rate first reaches target_pct%.
    Returns None if it never recovers within the series.

    Uses an absolute success-rate threshold (default 95%) rather than a
    relative fraction of pre-fault goodput. This is simpler, more
    intuitive, and independent of the offered load level.
    """
    if success_rate.empty:
        return None
    post = success_rate.loc[fault_end_bin:]
    hit = post[post >= target_pct]
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

    # X-axis bounds come from experiment.json durations (the *intended*
    # schedule). The fault-band shading uses _fault_band_x() which prefers
    # the per-policy actual fault timestamps when they're available.
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total_measured_duration = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

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
    Per-second client-observed latency percentiles over time, one figure
    per (percentile, y-axis scale) pair. Each figure compares all policies
    on the same axes. Useful for spotting:

      - tail-latency growth as load builds (compare p99 across policies)
      - mesh-retry-induced latency plateaus (p99 climbing toward client timeout)
      - the moment a fault trigger fires (sudden latency spike)
      - whether recovery is monotone or oscillating

    Each percentile is rendered twice — once on log y (good for spanning
    healthy ms to fault-window seconds in one plot) and once on linear y
    (good for reading absolute differences between policies in the
    fault/recovery window). The "log" view is the better default for
    spotting the metastable plateau; the "linear" view is the better
    default for measuring how much one policy beats another.

    `out_path` is treated as the base path. Files written:
        <stem>-p50-log.pdf,    <stem>-p50-linear.pdf
        <stem>-p90-log.pdf,    <stem>-p90-linear.pdf
        <stem>-p95-log.pdf,    <stem>-p95-linear.pdf
        <stem>-p99-log.pdf,    <stem>-p99-linear.pdf
    """
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    if not ordered:
        return

    # Scenario coordinates (same as the other timeseries plots).
    # Fault band x-coordinates come from _fault_band_x() which prefers
    # the per-policy actual fault timestamps over the intended ones.
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    percentiles = ("p50", "p90", "p95", "p99")
    out_path = Path(out_path)

    def _render(pct: str, yscale: str) -> None:
        """Render one percentile/yscale combo and write the file."""
        fig, ax = plt.subplots(figsize=(7.2, 3.6))

        any_plotted = False
        for policy in ordered:
            d = runs[policy]
            lat_ts = d.get("client_latency_ts") or {}
            ts = lat_ts.get(pct)
            if ts is None or ts.empty:
                continue
            ax.plot(
                ts.index, ts.values,
                label=POLICY_LABELS.get(policy, policy),
                color=POLICY_COLORS.get(policy),
                linewidth=1.8,
            )
            any_plotted = True

        if not any_plotted:
            plt.close(fig)
            return

        # Fault region shading. axvspan must be added BEFORE the "fault"
        # label so we can read the final ylim — but on log scale ylim is
        # data-driven and stable; on linear scale we set it after plot too.
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

        ax.set_yscale(yscale)
        ax.set_ylabel(f"Client latency {pct} (ms)")
        ax.set_xlabel("Time (s)")
        if total > 0:
            ax.set_xlim(0, total)
        # On log scale we want major+minor gridlines (the "which='both'"
        # path); on linear scale matplotlib's default major-only grid is
        # the cleaner look.
        if yscale == "log":
            ax.grid(True, which="both", alpha=0.25, linewidth=0.5)
        else:
            ax.grid(True, alpha=0.25, linewidth=0.5)
        ax.legend(loc="upper right", frameon=True, fancybox=False,
                  edgecolor="#888888", framealpha=0.95, fontsize=10)

        pct_path = out_path.with_name(
            f"{out_path.stem}-{pct}-{yscale}{out_path.suffix}"
        )
        fig.tight_layout()
        fig.savefig(pct_path, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {pct_path}")

    for pct in percentiles:
        _render(pct, "log")
        _render(pct, "linear")


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

    # Scenario coordinates. Fault band x-coordinates come from
    # _fault_band_x() which prefers per-policy actual fault timestamps.
    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec    = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

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
    fills = [POLICY_COLORS_FILL.get(p, "gray") for p in ordered]
    edges = [POLICY_COLORS.get(p, "gray") for p in ordered]

    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    bars = ax.bar(labels, values, color=fills, edgecolor="none")
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
# Per-phase per-service retry plots
# ---------------------------------------------------------------------------
#
# These read `phase_retry_deltas` from each policy's runs[] entry, which is
# populated by parse_phase_retry_deltas() during process_policy(). Older
# runs that don't have the phase-boundary snapshots will silently produce
# only the phases they have data for (or be skipped entirely).
#
# Two plot styles:
#   plot_retries_by_upstream    — view (b): per-upstream aggregation,
#                                  one panel per phase, grouped bars by
#                                  policy. Best for "how much retry
#                                  pressure landed on each service".
#   plot_retries_caller_matrix  — view (a): caller × upstream heatmap,
#                                  one heatmap per (policy, phase). Best
#                                  for "which caller is generating the
#                                  retries to which upstream".
# A textual summary is also printed via print_retry_summary().

# Phases we draw, in order. We deliberately drop "warmup" and "cooldown"
# from the headline plots — they're just bookend phases and rarely have
# meaningful retry activity. They're still printed in the summary.
_RETRY_PLOT_PHASES = ("prefault", "fault", "recovery")


def _aggregate_retries_by_upstream(
    phase_data: Dict[Tuple[str, str], Dict[str, int]],
    counter: str = "upstream_rq_retry",
) -> Dict[str, int]:
    """Sum a counter across all callers for each upstream in one phase."""
    out: Dict[str, int] = {}
    for (caller, upstream), counters in phase_data.items():
        v = counters.get(counter, 0)
        if v:
            out[upstream] = out.get(upstream, 0) + v
    return out


def plot_retries_by_upstream(
    runs: Dict[str, dict],
    out_path: Path,
    experiment: dict,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    View (b): per-upstream aggregated retry counts during prefault/fault/
    recovery phases. One subplot per phase, x-axis = upstream service,
    grouped bars per policy.

    Saves a single PDF with three side-by-side panels.
    """
    _apply_paper_style()
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    # Per-phase aggregation: { phase: { policy: { upstream: count } } }
    per_phase: Dict[str, Dict[str, Dict[str, int]]] = {}
    for phase in _RETRY_PLOT_PHASES:
        per_phase[phase] = {}
        for policy in ordered_policies:
            phase_deltas = runs[policy].get("phase_retry_deltas") or {}
            phase_data = phase_deltas.get(phase) or {}
            per_phase[phase][policy] = _aggregate_retries_by_upstream(phase_data, counter)

    # Union of upstream services across all policies and all phases.
    # Sorted alphabetically for stable ordering.
    upstreams = set()
    for phase in _RETRY_PLOT_PHASES:
        for policy in ordered_policies:
            upstreams.update(per_phase[phase][policy].keys())
    upstreams = sorted(upstreams)
    if not upstreams:
        # No phase-snapshot data anywhere — older run, skip silently.
        return

    n_phases = len(_RETRY_PLOT_PHASES)
    fig, axes = plt.subplots(
        1, n_phases, figsize=(5.5 * n_phases, 4.2),
        sharey=False, squeeze=False,
    )

    n_policies = len(ordered_policies)
    bar_width = 0.8 / max(n_policies, 1)
    x_pos = np.arange(len(upstreams))

    for col, phase in enumerate(_RETRY_PLOT_PHASES):
        ax = axes[0][col]
        for i, policy in enumerate(ordered_policies):
            counts = per_phase[phase][policy]
            heights = [counts.get(u, 0) for u in upstreams]
            offset = (i - (n_policies - 1) / 2.0) * bar_width
            ax.bar(
                x_pos + offset, heights,
                width=bar_width,
                color=POLICY_COLORS_FILL.get(policy, "gray"),
                edgecolor="none",
                label=POLICY_LABELS.get(policy, policy),
            )
        ax.set_xticks(x_pos)
        ax.set_xticklabels(upstreams, rotation=45, ha="right", fontsize=9)
        ax.set_title(f"{phase}", fontsize=12, loc="left", pad=4)
        ax.set_ylabel(f"retries dispatched ({phase})")
        ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
        if col == n_phases - 1:
            ax.legend(loc="upper right", frameon=True, fancybox=False,
                      edgecolor="#888888", framealpha=0.95, fontsize=10)

    fig.suptitle(
        f"Per-upstream retry dispatches by phase ({counter})",
        fontsize=13, y=1.02,
    )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_retries_caller_matrix(
    runs: Dict[str, dict],
    out_dir: Path,
    experiment: dict,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    View (a): caller × upstream retry-count heatmaps. Saves one PDF per
    (policy × phase) pair into `out_dir`, named like:
        retries-matrix-<policy>-<phase>.pdf

    Phases plotted: prefault / fault / recovery (same as plot_retries_by_
    upstream — warmup/cooldown are skipped to keep the output set small).
    """
    _apply_paper_style()
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    for policy in ordered_policies:
        phase_deltas = runs[policy].get("phase_retry_deltas") or {}
        for phase in _RETRY_PLOT_PHASES:
            phase_data = phase_deltas.get(phase) or {}
            if not phase_data:
                continue

            callers = sorted({c for (c, _u) in phase_data.keys()})
            upstreams = sorted({u for (_c, u) in phase_data.keys()})
            if not callers or not upstreams:
                continue

            mat = np.zeros((len(callers), len(upstreams)), dtype=float)
            for (c, u), counters in phase_data.items():
                v = counters.get(counter, 0)
                if v <= 0:
                    continue
                mat[callers.index(c), upstreams.index(u)] = v

            if mat.max() == 0:
                continue

            fig, ax = plt.subplots(
                figsize=(0.6 * len(upstreams) + 3.0,
                         0.4 * len(callers) + 2.5)
            )
            im = ax.imshow(mat, aspect="auto", cmap="YlOrRd")
            ax.set_xticks(range(len(upstreams)))
            ax.set_xticklabels(upstreams, rotation=45, ha="right", fontsize=9)
            ax.set_yticks(range(len(callers)))
            ax.set_yticklabels(callers, fontsize=9)
            ax.set_xlabel("upstream service")
            ax.set_ylabel("caller (sidecar)")

            # Annotate each cell with its count, in white on dark cells
            # and black on light cells.
            vmax = mat.max()
            for i in range(len(callers)):
                for j in range(len(upstreams)):
                    v = mat[i, j]
                    if v == 0:
                        continue
                    color = "white" if v > vmax * 0.6 else "black"
                    ax.text(j, i, f"{int(v)}",
                            ha="center", va="center",
                            fontsize=8, color=color)

            cbar = fig.colorbar(im, ax=ax)
            cbar.set_label("retries", fontsize=9)
            ax.set_title(
                f"{POLICY_LABELS.get(policy, policy)} — {phase}",
                fontsize=12, loc="left", pad=4,
            )
            out_path = out_dir / f"retries-matrix-{policy}-{phase}.pdf"
            fig.tight_layout()
            fig.savefig(out_path, bbox_inches="tight")
            plt.close(fig)
            print(f"  wrote {out_path}")


# Service order for the chain-amplification stacked bar. Bottom-to-top
# follows the call graph from the client edge inward: frontend (closest
# to the client) at the bottom, then checkoutservice (one hop deeper),
# then the leaves above. The order is hardcoded so the chain story is
# visually consistent across runs and across policies; services not in
# the list are appended to the top in alphabetical order.
_CHAIN_SERVICE_ORDER = [
    "gateway",
    "frontend",
    "checkoutservice",
    "cartservice",
    "productcatalogservice",
    "currencyservice",
    "shippingservice",
    "paymentservice",
    "emailservice",
    "recommendationservice",
    "adservice",
]

# Distinct colors for the service segments. Picked to be visually
# distinguishable on a small bar; the order is paired with
# _CHAIN_SERVICE_ORDER above.
_CHAIN_SERVICE_COLORS = {
    "gateway":               "#555555",  # dark gray
    "frontend":              "#1f77b4",  # blue
    "checkoutservice":       "#ff7f0e",  # orange
    "cartservice":           "#2ca02c",  # green
    "productcatalogservice": "#d62728",  # red
    "currencyservice":       "#9467bd",  # purple
    "shippingservice":       "#8c564b",  # brown
    "paymentservice":        "#e377c2",  # pink
    "emailservice":          "#7f7f7f",  # gray
    "recommendationservice": "#bcbd22",  # olive
    "adservice":             "#17becf",  # teal
}


def plot_retries_stacked_by_caller(
    runs: Dict[str, dict],
    out_path: Path,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    Chain-amplification stacked bar plot.

    For each policy, draw one vertical bar whose total height is the total
    number of retries dispatched across the entire call graph during the
    run. The bar is segmented by *which caller* dispatched the retries,
    in chain order (frontend at the bottom, checkoutservice above, then
    leaves). Reading the bar bottom-to-top traces how the retry storm
    propagates inward from the client edge.

    Every service in `_CHAIN_SERVICE_ORDER` is drawn as a segment, even
    when its contribution is zero. Zero-height segments are invisible in
    the bars but still appear in the legend, so the reader can see the
    full call-graph shape and which layers contributed (vs. which didn't).
    Services that appear in the data but aren't in the chain order are
    appended at the top of the stack in alphabetical order.

    Data source: each policy's `run_retry_deltas` (computed in
    process_policy from the .pre.stats and .stats whole-run snapshots).
    The deltas are summed across all upstreams a caller talked to, so
    "frontend" includes frontend's retries to checkout AND to cart AND
    to productcatalog, etc.

    Saved as a single PDF. One vertical bar per policy.
    """
    _apply_paper_style()
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    # Sum retries per (policy, caller) across all upstreams the caller
    # touched. Result: { policy: { caller: total_retries } }.
    per_policy: Dict[str, Dict[str, int]] = {}
    for policy in ordered_policies:
        deltas = runs[policy].get("run_retry_deltas") or {}
        per_caller: Dict[str, int] = {}
        for (caller, _upstream), counters in deltas.items():
            v = counters.get(counter, 0)
            if v <= 0:
                continue
            per_caller[caller] = per_caller.get(caller, 0) + v
        per_policy[policy] = per_caller

    # Union of all callers that contributed retries in any policy.
    callers_seen = set()
    for d in per_policy.values():
        callers_seen.update(d.keys())

    # Build the legend order: every service in the canonical chain order
    # FIRST (so each bar has the same set of segments and the chain shape
    # is visible regardless of which services contributed), then any
    # unknown callers (services in the data but not in the canonical chain)
    # appended in alphabetical order at the top of the stack.
    chain_services = list(_CHAIN_SERVICE_ORDER)
    extras = sorted(c for c in callers_seen if c not in _CHAIN_SERVICE_ORDER)
    callers_in_chain_order = chain_services + extras

    if not callers_in_chain_order:
        return  # no chain configured and no data — give up

    # If no policy contributed any retries at all, don't draw an empty
    # figure with only zero-height segments.
    if not any(per_policy[p].get(c, 0) > 0
               for p in ordered_policies
               for c in callers_in_chain_order):
        return

    # Build the figure.
    fig, ax = plt.subplots(figsize=(1.4 * len(ordered_policies) + 2.6, 5.0))
    x = np.arange(len(ordered_policies))
    bottom = np.zeros(len(ordered_policies))

    for caller in callers_in_chain_order:
        heights = np.array([per_policy[p].get(caller, 0) for p in ordered_policies],
                           dtype=float)
        # Render every chain service even when its contribution is zero
        # — the legend keeps the full call graph visible and the bar
        # widths/colors stay consistent across runs.
        ax.bar(
            x, heights,
            bottom=bottom,
            color=_CHAIN_SERVICE_COLORS.get(caller, "lightgray"),
            edgecolor="white",
            linewidth=0.5,
            label=caller,
            width=0.65,
        )
        bottom += heights

    # Total label on top of each bar.
    totals = bottom  # bottom now equals the cumulative top of each bar
    if totals.max() > 0:
        ymax = totals.max()
        for i, total in enumerate(totals):
            if total <= 0:
                continue
            ax.text(
                x[i], total + ymax * 0.015,
                f"{int(total):,}",
                ha="center", va="bottom",
                fontsize=10,
            )

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in ordered_policies],
                       rotation=0, fontsize=11)
    ax.set_ylabel("Retries dispatched (sum across all hops)")
    ax.set_title("Chain amplification: retries by caller, stacked",
                 fontsize=12, loc="left", pad=4)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    # Legend in reverse order so the visual stack (top-to-bottom) matches
    # the legend (top-to-bottom). Without this, the bottom segment would
    # appear at the top of the legend, which reads unintuitively.
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(
        handles[::-1], labels[::-1],
        loc="upper right",
        frameon=True, fancybox=False,
        edgecolor="#888888", framealpha=0.95, fontsize=10,
        title="dispatched by",
    )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_retries_stacked_by_callee(
    runs: Dict[str, dict],
    out_path: Path,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    Complementary view to plot_retries_stacked_by_caller: shows retries
    *received by* each service (incoming), stacked by the receiving
    service. For each policy, one vertical bar whose total height is the
    total retries received across all services. Each colored segment is
    one upstream service that received retries from any caller.

    This answers "which services bore the retry pressure?" while the
    caller version answers "which services generated the retry pressure?"
    Together the two plots trace the full chain: caller → callee.
    """
    _apply_paper_style()
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    # Sum retries per (policy, upstream) across all callers that retried
    # to that upstream. Result: { policy: { upstream: total_retries } }.
    per_policy: Dict[str, Dict[str, int]] = {}
    for policy in ordered_policies:
        deltas = runs[policy].get("run_retry_deltas") or {}
        per_upstream: Dict[str, int] = {}
        for (_caller, upstream), counters in deltas.items():
            v = counters.get(counter, 0)
            if v <= 0:
                continue
            per_upstream[upstream] = per_upstream.get(upstream, 0) + v
        per_policy[policy] = per_upstream

    # Same chain-order rendering as the caller version.
    chain_services = list(_CHAIN_SERVICE_ORDER)
    extras = sorted(u for p in per_policy.values() for u in p
                    if u not in _CHAIN_SERVICE_ORDER)
    callees_in_chain_order = chain_services + list(dict.fromkeys(extras))

    if not any(per_policy[p].get(c, 0) > 0
               for p in ordered_policies
               for c in callees_in_chain_order):
        return

    fig, ax = plt.subplots(figsize=(1.4 * len(ordered_policies) + 2.6, 5.0))
    x = np.arange(len(ordered_policies))
    bottom = np.zeros(len(ordered_policies))

    for svc in callees_in_chain_order:
        heights = np.array([per_policy[p].get(svc, 0) for p in ordered_policies],
                           dtype=float)
        ax.bar(
            x, heights,
            bottom=bottom,
            color=_CHAIN_SERVICE_COLORS.get(svc, "lightgray"),
            edgecolor="white",
            linewidth=0.5,
            label=svc,
            width=0.65,
        )
        bottom += heights

    totals = bottom
    if totals.max() > 0:
        ymax = totals.max()
        for i, total in enumerate(totals):
            if total <= 0:
                continue
            ax.text(
                x[i], total + ymax * 0.015,
                f"{int(total):,}",
                ha="center", va="bottom",
                fontsize=10,
            )

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in ordered_policies],
                       rotation=0, fontsize=11)
    ax.set_ylabel("Retries received (sum across all callers)")
    ax.set_title("Chain amplification: retries by callee, stacked",
                 fontsize=12, loc="left", pad=4)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    # Only show services that received retries under at least one policy.
    handles, labels = ax.get_legend_handles_labels()
    filtered = [(h, l) for h, l in zip(handles, labels)
                if any(per_policy[p].get(l, 0) > 0 for p in ordered_policies)]
    if filtered:
        fh, fl = zip(*filtered)
        ax.legend(
            list(fh)[::-1], list(fl)[::-1],
            loc="upper right",
            frameon=True, fancybox=False,
            edgecolor="#888888", framealpha=0.95, fontsize=10,
            title="received by",
        )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_chain_retry(
    runs: Dict[str, dict],
    out_path: Path,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    Chain retry plot: x-axis is services in call-chain order, grouped bars
    show the number of retries *received by* each service for each policy.

    This makes it easy to see where in the chain retries concentrate and
    how each policy affects retry distribution across services.
    """
    _apply_paper_style()
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    # Build {policy: {callee: total_retries_received}}
    per_policy: Dict[str, Dict[str, int]] = {}
    for policy in ordered_policies:
        deltas = runs[policy].get("run_retry_deltas") or {}
        per_callee: Dict[str, int] = {}
        for (_caller, upstream), counters in deltas.items():
            v = counters.get(counter, 0)
            if v <= 0:
                continue
            per_callee[upstream] = per_callee.get(upstream, 0) + v
        per_policy[policy] = per_callee

    # Determine which services received retries under any policy, in chain order.
    all_callees = set()
    for p in per_policy.values():
        all_callees.update(k for k, v in p.items() if v > 0)
    if not all_callees:
        return

    chain_order = [s for s in _CHAIN_SERVICE_ORDER if s in all_callees]
    extras = sorted(s for s in all_callees if s not in _CHAIN_SERVICE_ORDER)
    services = chain_order + extras

    n_svc = len(services)
    n_pol = len(ordered_policies)
    bar_width = 0.8 / n_pol
    x = np.arange(n_svc)

    fig, ax = plt.subplots(figsize=(max(6, 1.5 * n_svc + 2), 5.0))

    for i, policy in enumerate(ordered_policies):
        heights = [per_policy[policy].get(svc, 0) for svc in services]
        ax.bar(
            x + i * bar_width - 0.4 + bar_width / 2,
            heights,
            width=bar_width,
            color=POLICY_COLORS_FILL.get(policy, "lightgray"),
            edgecolor="none",
            label=POLICY_LABELS.get(policy, policy),
        )

    # Add value labels on top of each bar (compact K/M suffixes to avoid overlap).
    def _fmt(v: float) -> str:
        if v >= 1_000_000:
            return f"{v/1e6:.1f}M"
        if v >= 1_000:
            return f"{v/1e3:.0f}K"
        return f"{v:.0f}"

    for i, policy in enumerate(ordered_policies):
        heights = [per_policy[policy].get(svc, 0) for svc in services]
        for j, h in enumerate(heights):
            if h > 0:
                ax.text(
                    x[j] + i * bar_width - 0.4 + bar_width / 2,
                    h, _fmt(h),
                    ha="center", va="bottom", fontsize=7,
                )

    ax.set_xticks(x)
    svc_short = {
        "gateway": "Gateway",
        "frontend": "Frontend",
        "cartservice": "Cart",
        "productcatalogservice": "ProductCatalog",
        "checkoutservice": "Checkout",
        "paymentservice": "Payment",
        "currencyservice": "Currency",
        "shippingservice": "Shipping",
        "emailservice": "Email",
        "recommendationservice": "Recommend.",
        "adservice": "Ad",
    }
    svc_labels = [svc_short.get(s, s) for s in services]
    ax.set_xticklabels(svc_labels, rotation=0, fontsize=11)
    ax.set_ylabel("Retries received")
    ax.set_title("Retries by service along the call chain",
                 fontsize=12, loc="left", pad=4)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    ax.legend(
        loc="upper left",
        frameon=True, fancybox=False,
        edgecolor="#888888", framealpha=0.95, fontsize=10,
    )
    # Leave headroom for the value labels above the tallest bar.
    ymax = max(per_policy[p].get(s, 0) for p in ordered_policies for s in services)
    if ymax > 0:
        ax.set_ylim(top=ymax * 1.15)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")

    # --- Log-scale version ---
    log_path = out_path.with_name(out_path.stem + "-log" + out_path.suffix)
    fig2, ax2 = plt.subplots(figsize=(max(6, 1.5 * n_svc + 2), 5.0))
    for i, policy in enumerate(ordered_policies):
        heights = [max(per_policy[policy].get(svc, 0), 0.1) for svc in services]
        ax2.bar(
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
                ax2.text(
                    x[j] + i * bar_width - 0.4 + bar_width / 2,
                    real_h, _fmt(real_h),
                    ha="center", va="bottom", fontsize=7,
                )
    ax2.set_yscale("log")
    ax2.set_xticks(x)
    ax2.set_xticklabels([svc_short.get(s, s) for s in services], rotation=0, fontsize=11)
    ax2.set_ylabel("Retries received (log scale)")
    ax2.set_title("Retries by service along the call chain",
                  fontsize=12, loc="left", pad=4)
    ax2.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    ax2.legend(
        loc="upper left",
        frameon=True, fancybox=False,
        edgecolor="#888888", framealpha=0.95, fontsize=10,
    )
    fig2.tight_layout()
    fig2.savefig(log_path, bbox_inches="tight")
    plt.close(fig2)
    print(f"  wrote {log_path}")


_RETRY_STATUS_COLORS = {
    "500":     "#d62728",   # red — application error
    "429":     "#9467bd",   # purple — arolla rejection
    "503":     "#ff7f0e",   # orange — service unavailable
    "504":     "#e377c2",   # pink — gateway timeout
    "502":     "#8c564b",   # brown — bad gateway
    "0":       "#7f7f7f",   # gray — connection failure / client timeout
    "success": "#2ca02c",   # green — retry that succeeded
    "other":   "#17becf",   # teal
}

_RETRY_STATUS_ORDER = ["500", "429", "503", "504", "502", "0", "success", "other"]


def plot_retry_status_ts(
    runs: Dict[str, dict],
    out_path: Path,
    experiment: dict,
) -> None:
    """
    Per-policy subplot: retry attempts per second, stacked by HTTP status.
    Shows how retry traffic decomposes (429 rejections vs 500 errors vs
    connection failures) over the experiment timeline.
    """
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    if not ordered:
        return

    n = len(ordered)
    fig, axes = plt.subplots(n, 1, figsize=(10, 3.0 * n), sharex=True, sharey=True)
    if n == 1:
        axes = [axes]

    for ax, policy in zip(axes, ordered):
        rbs = runs[policy].get("retry_by_status_ts") or {}
        if not rbs:
            ax.set_title(POLICY_LABELS.get(policy, policy), fontsize=11, loc="left")
            ax.set_ylabel("retries/s")
            continue

        # Stack in a fixed order so colors are consistent across policies.
        labels_present = [s for s in _RETRY_STATUS_ORDER if s in rbs]
        bottom = None
        for status_label in labels_present:
            series = rbs[status_label].fillna(0)
            color = _RETRY_STATUS_COLORS.get(status_label, "lightgray")
            display_label = {
                "success": "Retry succeeded",
                "0": "Conn. failure",
            }.get(status_label, f"HTTP {status_label}")
            if bottom is None:
                ax.fill_between(series.index, 0, series,
                                color=color, alpha=0.7, label=display_label,
                                linewidth=0.5)
                bottom = series.copy()
            else:
                ax.fill_between(series.index, bottom, bottom + series,
                                color=color, alpha=0.7, label=display_label,
                                linewidth=0.5)
                bottom = bottom + series

        # Fault band.
        fault_start = runs[policy].get("fault_start_bin")
        fault_end = runs[policy].get("fault_end_bin")
        if fault_start is not None and fault_end is not None:
            ax.axvspan(fault_start, fault_end, color="red", alpha=0.08)
            ax.axvline(fault_start, color="red", linewidth=0.6, linestyle="--", alpha=0.5)
            ax.axvline(fault_end, color="red", linewidth=0.6, linestyle="--", alpha=0.5)

        ax.set_title(POLICY_LABELS.get(policy, policy), fontsize=11, loc="left")
        ax.set_ylabel("retries/s")
        ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)

    axes[-1].set_xlabel("Time (s from warmup end)")

    # Shared legend from the last subplot that had data.
    handles, labels = [], []
    for ax in axes:
        h, l = ax.get_legend_handles_labels()
        if h:
            handles, labels = h, l
    if handles:
        fig.legend(handles, labels, loc="upper right",
                   frameon=True, fancybox=False,
                   edgecolor="#888888", framealpha=0.95, fontsize=9,
                   bbox_to_anchor=(0.98, 0.98))

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_chain_retry_by_phase(
    runs: Dict[str, dict],
    out_path: Path,
    counter: str = "upstream_rq_retry",
) -> None:
    """
    Per-policy subplot showing retries received by each callee service,
    broken down by phase (prefault / fault / recovery) if phase snapshots
    are available. Falls back to whole-run totals when phase data is missing.

    Layout: one subplot per policy, each with grouped bars (one group per
    callee service, one bar per phase).
    """
    _apply_paper_style()
    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]
    if not ordered:
        return

    phases = ["prefault", "fault", "recovery"]
    phase_colors = {
        "prefault": "#2ca02c",   # green
        "fault":    "#d62728",   # red
        "recovery": "#1f77b4",   # blue
        "whole_run": "#555555",  # gray (fallback)
    }

    # Collect data: {policy: {phase: {callee: retries}}}
    policy_phase_data: Dict[str, Dict[str, Dict[str, int]]] = {}
    has_phase_data = False
    for policy in ordered:
        phase_deltas = runs[policy].get("phase_retry_deltas") or {}
        run_deltas = runs[policy].get("run_retry_deltas") or {}

        if any(phase_deltas.get(ph) for ph in phases):
            has_phase_data = True
            per_phase: Dict[str, Dict[str, int]] = {}
            for ph in phases:
                pd_data = phase_deltas.get(ph) or {}
                per_callee: Dict[str, int] = {}
                for (_caller, upstream), counters in pd_data.items():
                    v = counters.get(counter, 0)
                    if v > 0:
                        per_callee[upstream] = per_callee.get(upstream, 0) + v
                per_phase[ph] = per_callee
            policy_phase_data[policy] = per_phase
        else:
            # Fallback: whole-run totals as a single "whole_run" phase.
            per_callee: Dict[str, int] = {}
            for (_caller, upstream), counters in run_deltas.items():
                v = counters.get(counter, 0)
                if v > 0:
                    per_callee[upstream] = per_callee.get(upstream, 0) + v
            policy_phase_data[policy] = {"whole_run": per_callee}

    # Determine callee services (in chain order) that appear in any policy.
    all_callees = set()
    for ppd in policy_phase_data.values():
        for phase_data in ppd.values():
            all_callees.update(k for k, v in phase_data.items() if v > 0)
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

    active_phases = phases if has_phase_data else ["whole_run"]
    n_phases = len(active_phases)
    n_pol = len(ordered)

    fig, axes = plt.subplots(n_pol, 1,
                             figsize=(max(6, 1.8 * len(services) + 2), 3.5 * n_pol),
                             sharex=True, sharey=True)
    if n_pol == 1:
        axes = [axes]

    x = np.arange(len(services))
    bar_width = 0.8 / n_phases

    for ax, policy in zip(axes, ordered):
        ppd = policy_phase_data.get(policy, {})
        for i, phase in enumerate(active_phases):
            phase_data = ppd.get(phase, {})
            heights = [phase_data.get(svc, 0) for svc in services]
            ax.bar(
                x + i * bar_width - 0.4 + bar_width / 2,
                heights,
                width=bar_width,
                color=phase_colors.get(phase, "#999999"),
                edgecolor="white",
                linewidth=0.5,
                label=phase if policy == ordered[0] else None,
            )
            # Value labels.
            for j, h in enumerate(heights):
                if h > 0:
                    label_str = f"{h/1e3:.0f}K" if h >= 1000 else f"{h:.0f}"
                    ax.text(
                        x[j] + i * bar_width - 0.4 + bar_width / 2,
                        h, label_str,
                        ha="center", va="bottom", fontsize=6,
                    )

        ax.set_title(POLICY_LABELS.get(policy, policy), fontsize=11, loc="left")
        ax.set_ylabel("retries received")
        ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)

    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([svc_short.get(s, s) for s in services],
                             rotation=0, fontsize=11)

    # Shared legend from first subplot.
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper right",
                   frameon=True, fancybox=False,
                   edgecolor="#888888", framealpha=0.95, fontsize=9,
                   bbox_to_anchor=(0.98, 0.98))

    # Headroom for value labels.
    ymax = max(
        ppd_phase.get(svc, 0)
        for ppd in policy_phase_data.values()
        for ppd_phase in ppd.values()
        for svc in services
    )
    if ymax > 0:
        for ax in axes:
            ax.set_ylim(top=ymax * 1.15)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def print_retry_summary(runs: Dict[str, dict]) -> None:
    """
    Textual per-phase per-policy retry summary, printed to stdout.
    Mirrors the data plotted by plot_retries_by_upstream so a quick
    glance at the analyzer's output tells you the same story without
    opening a PDF.
    """
    ordered_policies = [p for p in POLICY_ORDER if p in runs] + \
                       [p for p in runs if p not in POLICY_ORDER]
    if not ordered_policies:
        return

    # Bail early if no policy has retry-delta data.
    if not any(runs[p].get("phase_retry_deltas") for p in ordered_policies):
        return

    print()
    print("=== Per-phase retries by upstream (sum across all callers) ===")
    print(
        f"{'phase':<10s} {'policy':<20s} "
        f"{'upstream':<28s} {'retries':>10s} {'overflow':>10s} "
        f"{'rsuccess':>10s} {'limit_hit':>10s}"
    )
    print("-" * 100)

    for phase in PHASE_SNAPSHOTS:
        phase_name = phase[0]
        any_row = False
        for policy in ordered_policies:
            phase_deltas = runs[policy].get("phase_retry_deltas") or {}
            phase_data = phase_deltas.get(phase_name) or {}
            if not phase_data:
                continue

            # Aggregate by upstream
            agg: Dict[str, Dict[str, int]] = {}
            for (_caller, upstream), counters in phase_data.items():
                d = agg.setdefault(upstream, {c: 0 for c in RETRY_COUNTERS})
                for c in RETRY_COUNTERS:
                    d[c] += counters.get(c, 0)

            # Sort by retry count desc and only show non-zero rows.
            for upstream, c in sorted(
                agg.items(), key=lambda kv: -kv[1]["upstream_rq_retry"]
            ):
                if c["upstream_rq_retry"] == 0 and c["upstream_rq_retry_overflow"] == 0:
                    continue
                print(
                    f"{phase_name:<10s} "
                    f"{POLICY_LABELS.get(policy, policy):<20s} "
                    f"{upstream:<28s} "
                    f"{c['upstream_rq_retry']:>10d} "
                    f"{c['upstream_rq_retry_overflow']:>10d} "
                    f"{c['upstream_rq_retry_success']:>10d} "
                    f"{c['upstream_rq_retry_limit_exceeded']:>10d}"
                )
                any_row = True
        if any_row:
            print()


# ---------------------------------------------------------------------------
# Fairness plots (§6.4)
# ---------------------------------------------------------------------------

# Per-tenant color palette — warm/cool tones distinguishable by name.
# Auto-assigned for unknown tenants via TENANT_FALLBACK_COLORS.
TENANT_COLORS = {
    "client1": "#E07B7B",   # salmon
    "client2": "#70AD47",   # green
    "client3": "#5B9BD5",   # blue
    "client4": "#E6913E",   # orange
    "client5": "#9B59B6",   # purple
    "client6": "#1ABC9C",   # teal
}

TENANT_LABELS = {
    "client1": "Client 1: 3 retries, no backoff",
    "client2": "Client 2: 3 retries, no backoff",
    "client3": "Client 3: 3 retries, no backoff",
    "client4": "Client 4: 10 retries, no backoff",
    "client5": "Client 5: 10 retries, no backoff",
    "client6": "Client 6: 10 retries, no backoff",
}

# Fallback colors for tenants not in the map above.
TENANT_FALLBACK_COLORS = [
    "#9B59B6", "#3498DB", "#E67E22", "#1ABC9C", "#E74C3C", "#2ECC71",
]


def _tenant_color(tenant: str, idx: int = 0) -> str:
    return TENANT_COLORS.get(tenant, TENANT_FALLBACK_COLORS[idx % len(TENANT_FALLBACK_COLORS)])


def _tenant_label(tenant: str) -> str:
    return TENANT_LABELS.get(tenant, tenant)


def plot_fairness_retry_share(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
) -> None:
    """
    Bar chart: per-tenant retry share (%) for each policy.

    Retry share = (retries admitted for tenant) / (total retries admitted)
    across the fault window. A dashed line marks the ideal equal share.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    # Discover tenants from the first policy that has data.
    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    # For each policy, compute per-tenant retry share.
    # Preferred source: server-side per-tenant Envoy stats from arolla-fairness-record
    # (whole-run counters, diffed pre vs post). Fallback: client-side CSV inference.
    policy_tenant_shares: Dict[str, Dict[str, float]] = {}
    for policy in ordered:
        d = runs[policy]
        tenant_stats = d.get("tenant_arolla_stats", {})

        if tenant_stats:
            # Server-side stats available — use them (most accurate).
            total_admitted = sum(ts["admitted"] for ts in tenant_stats.values())
            if total_admitted == 0:
                policy_tenant_shares[policy] = {t: 0.0 for t in all_tenants}
            else:
                shares = {}
                for tenant in all_tenants:
                    # Match tenant profile name to sanitized stat name
                    # (e.g. "tenant-conservative" → "tenant_conservative").
                    safe = tenant.replace("-", "_")
                    count = tenant_stats.get(safe, {}).get("admitted", 0)
                    shares[tenant] = count / total_admitted * 100.0
                policy_tenant_shares[policy] = shares
        else:
            # Fallback: infer from client-side CSV (status != 429 and != 0).
            df = d.get("df")
            if df is None or df.empty or "profile" not in df.columns:
                continue
            t_ref = d["t_ref"]
            fault_start = d["fault_start_bin"]
            fault_end = d["fault_end_bin"]

            df_ts = df.copy()
            df_ts["bin"] = ((df_ts["timestamp"] - t_ref)).astype(int)
            fault_retries = df_ts[
                (df_ts["bin"] >= fault_start) &
                (df_ts["bin"] < fault_end) &
                (df_ts["is_retry"] == True)
            ]
            admitted = fault_retries[
                (fault_retries["status"] > 0) & (fault_retries["status"] != 429)
            ]

            total_admitted = len(admitted)
            if total_admitted == 0:
                policy_tenant_shares[policy] = {t: 0.0 for t in all_tenants}
                continue

            shares = {}
            for tenant in all_tenants:
                count = len(admitted[admitted["profile"] == tenant])
                shares[tenant] = count / total_admitted * 100.0
            policy_tenant_shares[policy] = shares

    if not policy_tenant_shares:
        return

    # Plot grouped bar chart (like the uploaded reference figure).
    n_policies = len(policy_tenant_shares)
    n_tenants = len(all_tenants)
    bar_width = 0.8 / n_tenants
    x = np.arange(n_policies)

    fig, ax = plt.subplots(figsize=(max(6.4, n_policies * 2.0), 4.0))

    for i, tenant in enumerate(all_tenants):
        offsets = x + (i - n_tenants / 2 + 0.5) * bar_width
        values = [
            policy_tenant_shares[p].get(tenant, 0.0)
            for p in policy_tenant_shares
        ]
        color = _tenant_color(tenant, i)
        label = _tenant_label(tenant)
        ax.bar(offsets, values, bar_width * 0.9, label=label, color=color,
               alpha=0.85, edgecolor="white", linewidth=0.5)

    # Ideal equal share line.
    ideal = 100.0 / n_tenants if n_tenants > 0 else 0
    ax.axhline(ideal, color="#C83232", linestyle="--", linewidth=1.5,
               alpha=0.7, label=f"Equal share ({ideal:.0f}%)")

    ax.set_xticks(x)
    ax.set_xticklabels([
        POLICY_LABELS.get(p, p) for p in policy_tenant_shares
    ])
    ax.set_ylabel("Retry Share (%)")
    ax.set_ylim(0, max(55, ax.get_ylim()[1] * 1.05))
    ax.legend(loc="upper right", frameon=True, fancybox=False,
              edgecolor="#888888", framealpha=0.95)
    fig.tight_layout()
    out_path = out_dir / "retry-share.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_fairness_admission_rate(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
) -> None:
    """
    Bar chart: per-tenant retry admission rate (%) for each policy.

    Admission rate = admitted retries / total retries attempted (per tenant).
    This shows how likely each tenant's retry is to be admitted — the true
    fairness signal, unlike admission *share* which is dominated by offered volume.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    policy_tenant_rates: Dict[str, Dict[str, float]] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        t_ref = d["t_ref"]
        fault_start = d["fault_start_bin"]
        fault_end = d["fault_end_bin"]

        df_ts = df.copy()
        df_ts["bin"] = ((df_ts["timestamp"] - t_ref)).astype(int)
        fault_retries = df_ts[
            (df_ts["bin"] >= fault_start) &
            (df_ts["bin"] < fault_end) &
            (df_ts["is_retry"] == True)
        ]

        rates: Dict[str, float] = {}
        for tenant in all_tenants:
            tenant_retries = fault_retries[fault_retries["profile"] == tenant]
            # Admitted = reached backend (not 429 and not network error).
            admitted = tenant_retries[
                (tenant_retries["status"] > 0) & (tenant_retries["status"] != 429)
            ]
            total = len(tenant_retries)
            rates[tenant] = (len(admitted) / total * 100.0) if total > 0 else 0.0
        policy_tenant_rates[policy] = rates

    if not policy_tenant_rates:
        return

    n_policies = len(policy_tenant_rates)
    n_tenants = len(all_tenants)
    bar_width = 0.8 / n_tenants
    x = np.arange(n_policies)

    fig, ax = plt.subplots(figsize=(max(6.4, n_policies * 2.0), 4.0))

    for i, tenant in enumerate(all_tenants):
        offsets = x + (i - n_tenants / 2 + 0.5) * bar_width
        values = [policy_tenant_rates[p].get(tenant, 0.0) for p in policy_tenant_rates]
        color = _tenant_color(tenant, i)
        label = _tenant_label(tenant)
        ax.bar(offsets, values, bar_width * 0.9, label=label, color=color,
               alpha=0.85, edgecolor="white", linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policy_tenant_rates])
    ax.set_ylabel("Retry admission rate (%)")
    ax.set_ylim(0, max(105, ax.get_ylim()[1] * 1.05))
    ax.legend(loc="best", frameon=True, fancybox=False,
              edgecolor="#888888", framealpha=0.95, fontsize=9)
    fig.tight_layout()
    out_path = out_dir / "admission-rate.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_fairness_polite_vs_aggressive(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
    *, polite_threshold: int = 4,
) -> None:
    """
    Two bar charts comparing polite vs aggressive client treatment:

    1. polite-vs-aggressive-rate.pdf — admission RATE ratio
         (polite_admitted / polite_retries) / (aggressive_admitted / aggressive_retries)
       Normalizes for volume; each bar is "how much more likely a polite retry
       is to be admitted than an aggressive retry, per-retry".

    2. polite-vs-aggressive-count.pdf — admission COUNT ratio
         polite_admitted / aggressive_admitted
       Raw count ratio. Even without fairness this reflects volume; with
       fairness the ratio rises (polite captures more admissions despite
       sending less volume).

    A client is "polite" if its max observed attempt is <= polite_threshold
    (default 4 → up to 3 retries); otherwise "aggressive".
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    # Classify tenants by max observed attempt (across all policies).
    tenant_max_attempt: Dict[str, int] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        for tenant, sub in df.groupby("profile"):
            m = int(sub["attempt"].max())
            prev = tenant_max_attempt.get(tenant, 0)
            if m > prev:
                tenant_max_attempt[tenant] = m
    if not tenant_max_attempt:
        return
    polite_set = {t for t, m in tenant_max_attempt.items() if m <= polite_threshold}
    agg_set = set(tenant_max_attempt.keys()) - polite_set
    if not polite_set or not agg_set:
        return

    # Per-policy: admission counts and retry counts for each group (fault window).
    results: Dict[str, Dict[str, int]] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        t_ref = d["t_ref"]
        fault_start = d["fault_start_bin"]
        fault_end = d["fault_end_bin"]

        df_ts = df.copy()
        df_ts["bin"] = ((df_ts["timestamp"] - t_ref)).astype(int)
        fault_retries = df_ts[
            (df_ts["bin"] >= fault_start) &
            (df_ts["bin"] < fault_end) &
            (df_ts["is_retry"] == True)
        ]

        def counts_for(group: set) -> tuple:
            g = fault_retries[fault_retries["profile"].isin(group)]
            admitted = g[(g["status"] > 0) & (g["status"] != 429)]
            return len(admitted), len(g)

        p_adm, p_ret = counts_for(polite_set)
        a_adm, a_ret = counts_for(agg_set)
        results[policy] = {
            "polite_adm": p_adm, "polite_ret": p_ret,
            "agg_adm":    a_adm, "agg_ret":    a_ret,
        }

    if not results:
        return

    # Shared helper to render a single-metric bar chart.
    def _render(ratios: List[float], ylabel: str, filename: str) -> None:
        n_policies = len(results)
        x = np.arange(n_policies)
        colors = [POLICY_COLORS.get(p, "#555555") for p in results]

        fig, ax = plt.subplots(figsize=(max(6.4, n_policies * 1.5), 4.0))
        ax.bar(x, ratios, 0.6, color=colors, alpha=0.85,
               edgecolor="white", linewidth=0.5)

        ax.axhline(1.0, color="#888888", linestyle="--", linewidth=1.0,
                   alpha=0.8, label="1× (parity)")

        finite = [r for r in ratios if not (np.isnan(r) or np.isinf(r))]
        ymax = max(finite + [1.0]) if finite else 1.0
        for i, r in enumerate(ratios):
            label = "—" if (np.isnan(r) or np.isinf(r)) else f"{r:.2f}×"
            y = r if not (np.isnan(r) or np.isinf(r)) else 0
            ax.text(x[i], y + ymax * 0.03, label, ha="center", va="bottom",
                    fontsize=11, fontweight="bold", color="#333333")

        ax.set_xticks(x)
        ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in results])
        ax.set_ylabel(ylabel)
        ax.set_ylim(0, ymax * 1.15)
        ax.legend(loc="best", frameon=True, fancybox=False,
                  edgecolor="#888888", framealpha=0.95, fontsize=9)
        fig.tight_layout()
        out_path = out_dir / filename
        fig.savefig(out_path, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {out_path}")

    # Rate ratio: (polite admitted/retries) / (aggressive admitted/retries).
    rate_ratios = []
    for policy in results:
        r = results[policy]
        p_rate = r["polite_adm"] / r["polite_ret"] if r["polite_ret"] > 0 else 0.0
        a_rate = r["agg_adm"] / r["agg_ret"] if r["agg_ret"] > 0 else 0.0
        rate_ratios.append(p_rate / a_rate if a_rate > 0 else float("nan"))
    _render(
        rate_ratios,
        ylabel=f"Polite / Aggressive admission rate\n(polite = ≤{polite_threshold-1} retries)",
        filename="polite-vs-aggressive-rate.pdf",
    )

    # Count ratio: polite admitted / aggressive admitted.
    count_ratios = []
    for policy in results:
        r = results[policy]
        count_ratios.append(
            r["polite_adm"] / r["agg_adm"] if r["agg_adm"] > 0 else float("nan")
        )
    _render(
        count_ratios,
        ylabel=f"Polite admitted / Aggressive admitted\n(polite = ≤{polite_threshold-1} retries)",
        filename="polite-vs-aggressive-count.pdf",
    )


def plot_fairness_share_over_demand(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
    *, polite_threshold: int = 4,
) -> None:
    """
    Grouped bar chart: over/under-representation per group, per policy.

    For each group (polite / aggressive) and each policy:
      ratio = admitted_share / volume_share

    Where:
      admitted_share = group_admitted / total_admitted
      volume_share   = group_retries  / total_retries

    Interpretation:
      > 1  → group is over-represented in admissions (policy favors it)
      = 1  → proportional (admission tracks volume — no differentiation)
      < 1  → group is under-represented

    This metric is RPS-independent: it works regardless of whether polite
    and aggressive clients have equal or wildly different offered volumes.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    tenant_max_attempt: Dict[str, int] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        for tenant, sub in df.groupby("profile"):
            m = int(sub["attempt"].max())
            prev = tenant_max_attempt.get(tenant, 0)
            if m > prev:
                tenant_max_attempt[tenant] = m
    if not tenant_max_attempt:
        return
    polite_set = {t for t, m in tenant_max_attempt.items() if m <= polite_threshold}
    agg_set = set(tenant_max_attempt.keys()) - polite_set
    if not polite_set or not agg_set:
        return

    results: Dict[str, Dict[str, float]] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        t_ref = d["t_ref"]
        fault_start = d["fault_start_bin"]
        fault_end = d["fault_end_bin"]

        df_ts = df.copy()
        df_ts["bin"] = ((df_ts["timestamp"] - t_ref)).astype(int)
        fault_retries = df_ts[
            (df_ts["bin"] >= fault_start) &
            (df_ts["bin"] < fault_end) &
            (df_ts["is_retry"] == True)
        ]
        admitted = fault_retries[
            (fault_retries["status"] > 0) & (fault_retries["status"] != 429)
        ]
        total_ret = len(fault_retries)
        total_adm = len(admitted)

        def ratio_for(group: set) -> float:
            g_ret = len(fault_retries[fault_retries["profile"].isin(group)])
            g_adm = len(admitted[admitted["profile"].isin(group)])
            if total_ret == 0 or total_adm == 0 or g_ret == 0:
                return float("nan")
            vol_share = g_ret / total_ret
            adm_share = g_adm / total_adm
            return adm_share / vol_share if vol_share > 0 else float("nan")

        results[policy] = {
            "polite": ratio_for(polite_set),
            "aggressive": ratio_for(agg_set),
        }

    if not results:
        return

    n_policies = len(results)
    x = np.arange(n_policies)
    bar_width = 0.35

    fig, ax = plt.subplots(figsize=(max(6.4, n_policies * 2.0), 4.0))

    polite_vals = [results[p]["polite"] for p in results]
    agg_vals = [results[p]["aggressive"] for p in results]

    ax.bar(x - bar_width/2, polite_vals, bar_width,
           label=f"Polite (≤{polite_threshold-1} retries)",
           color="#5B9BD5", alpha=0.85, edgecolor="white", linewidth=0.5)
    ax.bar(x + bar_width/2, agg_vals, bar_width,
           label=f"Aggressive (>{polite_threshold-1} retries)",
           color="#FF6B6B", alpha=0.85, edgecolor="white", linewidth=0.5)

    ax.axhline(1.0, color="#555555", linestyle="--", linewidth=1.2,
               alpha=0.8, label="Proportional (1×)")

    finite = [v for v in polite_vals + agg_vals if not (np.isnan(v) or np.isinf(v))]
    ymax = max(finite + [1.0]) if finite else 1.0

    for i, policy in enumerate(results):
        p = polite_vals[i]
        a = agg_vals[i]
        for xp, val in [(x[i] - bar_width/2, p), (x[i] + bar_width/2, a)]:
            label = "—" if (np.isnan(val) or np.isinf(val)) else f"{val:.2f}×"
            y = 0 if (np.isnan(val) or np.isinf(val)) else val
            ax.text(xp, y + ymax * 0.03, label, ha="center", va="bottom",
                    fontsize=9, fontweight="bold", color="#333333")

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in results])
    ax.set_ylabel("Admitted share / Volume share")
    ax.set_ylim(0, ymax * 1.15)
    ax.legend(loc="best", frameon=True, fancybox=False,
              edgecolor="#888888", framealpha=0.95, fontsize=9)
    fig.tight_layout()
    out_path = out_dir / "share-over-demand.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_fairness_success_rate(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
) -> None:
    """
    Per-tenant success rate over time, one subplot per policy.

    Each subplot shows success rate (%) for each tenant as a separate line,
    with fault region shaded.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    # Discover tenants.
    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    n_policies = len(ordered)
    fig, axes = plt.subplots(n_policies, 1, figsize=(7.0, 3.0 * n_policies),
                             sharex=True, squeeze=False)
    axes = axes.flatten()

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total_duration = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    for idx, policy in enumerate(ordered):
        ax = axes[idx]
        d = runs[policy]
        df = d.get("df")
        t_ref = d.get("t_ref", 0)
        t_end = d.get("t_cooldown_end", 0)

        if df is None or df.empty or "profile" not in df.columns:
            ax.set_title(POLICY_LABELS.get(policy, policy))
            continue

        for tenant in all_tenants:
            tenant_df = df[df["profile"] == tenant]
            sr = success_rate_timeseries(tenant_df, t_ref, t_end,
                                         bin_sec=1.0, smooth_win=5)
            if sr.empty:
                continue
            color = _tenant_color(tenant, all_tenants.index(tenant))
            label = _tenant_label(tenant)
            ax.plot(sr.index, sr.values, label=label, color=color, linewidth=1.5)

        # Fault band.
        if fault_sec > 0:
            ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                       alpha=0.55, zorder=0)
            ax.text(
                (fault_start_x + fault_end_x) / 2.0, 98, "fault",
                ha="center", va="top",
                fontsize=10, fontstyle="italic", color="#555555",
            )

        ax.set_title(POLICY_LABELS.get(policy, policy))
        ax.set_ylim(-5, 105)
        ax.set_ylabel("Success rate (%)")
        if total_duration > 0:
            ax.set_xlim(0, total_duration)
        if idx == 0:
            ax.legend(loc="lower right", frameon=True, fancybox=False,
                      edgecolor="#888888", framealpha=0.95, fontsize=9)

    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    out_path = out_dir / "success-rate-per-tenant.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_fairness_goodput_per_tenant(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
) -> None:
    """
    Per-tenant goodput (successful requests/s) over time, one subplot per policy.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    n_policies = len(ordered)
    fig, axes = plt.subplots(n_policies, 1, figsize=(7.0, 3.0 * n_policies),
                             sharex=True, squeeze=False)
    axes = axes.flatten()

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total_duration = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    for idx, policy in enumerate(ordered):
        ax = axes[idx]
        d = runs[policy]
        df = d.get("df")
        t_ref = d.get("t_ref", 0)
        t_end = d.get("t_cooldown_end", 0)

        if df is None or df.empty or "profile" not in df.columns:
            ax.set_title(POLICY_LABELS.get(policy, policy))
            continue

        for tenant in all_tenants:
            tenant_df = df[df["profile"] == tenant]
            gp = goodput_timeseries(tenant_df, t_ref, t_end, bin_sec=1.0)
            if gp.empty:
                continue
            color = _tenant_color(tenant, all_tenants.index(tenant))
            label = _tenant_label(tenant)
            ax.plot(gp.index, gp.values, label=label, color=color, linewidth=1.5)

        if fault_sec > 0:
            ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                       alpha=0.55, zorder=0)
            ymax = ax.get_ylim()[1]
            ax.text(
                (fault_start_x + fault_end_x) / 2.0,
                ymax - 0.04 * ymax, "fault",
                ha="center", va="top",
                fontsize=10, fontstyle="italic", color="#555555",
            )

        ax.set_title(POLICY_LABELS.get(policy, policy))
        ax.set_ylabel("Goodput (req/s)")
        if total_duration > 0:
            ax.set_xlim(0, total_duration)
        if idx == 0:
            ax.legend(loc="lower right", frameon=True, fancybox=False,
                      edgecolor="#888888", framealpha=0.95, fontsize=9)

    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    out_path = out_dir / "goodput-per-tenant.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def plot_fairness_latency_per_tenant(
    runs: Dict[str, dict], out_dir: Path, experiment: dict,
) -> None:
    """
    Per-tenant p50 latency (ms) over time, one subplot per policy.
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    n_policies = len(ordered)
    fig, axes = plt.subplots(n_policies, 1, figsize=(7.0, 3.0 * n_policies),
                             sharex=True, squeeze=False)
    axes = axes.flatten()

    prefault_sec = float(experiment.get("prefault_sec", 0))
    fault_sec = float(experiment.get("fault_sec", 0))
    recovery_sec = float(experiment.get("recovery_sec", 0))
    cooldown_sec = float(experiment.get("cooldown_sec", 0))
    total_duration = prefault_sec + fault_sec + recovery_sec + cooldown_sec
    fault_start_x, fault_end_x = _fault_band_x(experiment)

    for idx, policy in enumerate(ordered):
        ax = axes[idx]
        d = runs[policy]
        df = d.get("df")
        t_ref = d.get("t_ref", 0)
        t_end = d.get("t_cooldown_end", 0)

        if df is None or df.empty or "profile" not in df.columns:
            ax.set_title(POLICY_LABELS.get(policy, policy))
            continue

        for tenant in all_tenants:
            tenant_df = df[df["profile"] == tenant]
            lat = client_latency_timeseries(tenant_df, t_ref, t_end, bin_sec=1.0)
            p50 = lat.get("p50")
            if p50 is None or p50.empty:
                continue
            color = _tenant_color(tenant, all_tenants.index(tenant))
            label = _tenant_label(tenant)
            ax.plot(p50.index, p50.values, label=label, color=color, linewidth=1.5)

        if fault_sec > 0:
            ax.axvspan(fault_start_x, fault_end_x, color="lightgray",
                       alpha=0.55, zorder=0)
            ymax = ax.get_ylim()[1]
            ax.text(
                (fault_start_x + fault_end_x) / 2.0,
                ymax - 0.04 * ymax, "fault",
                ha="center", va="top",
                fontsize=10, fontstyle="italic", color="#555555",
            )

        ax.set_title(POLICY_LABELS.get(policy, policy))
        ax.set_ylabel("Latency p50 (ms)")
        ax.set_yscale("log")
        if total_duration > 0:
            ax.set_xlim(0, total_duration)
        if idx == 0:
            ax.legend(loc="upper right", frameon=True, fancybox=False,
                      edgecolor="#888888", framealpha=0.95, fontsize=9)

    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    out_path = out_dir / "latency-per-tenant.pdf"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


def _fairness_bar_chart(
    runs: Dict[str, dict],
    out_dir: Path,
    experiment: dict,
    *,
    phase: str,
    metric: str,
    ylabel: str,
    filename: str,
    ylim: Optional[tuple] = None,
) -> None:
    """
    Grouped bar chart: one group per policy, one bar per tenant.

    `metric` is one of:
      - "goodput": successful requests/s (ok count / phase duration)
      - "first_attempt_sr": first-attempt success rate (%)
    `phase` is "fault" or "recovery".
    """
    _apply_paper_style()

    ordered = [p for p in POLICY_ORDER if p in runs] + \
              [p for p in runs if p not in POLICY_ORDER]

    all_tenants: List[str] = []
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is not None and "profile" in df.columns:
            all_tenants = sorted(df["profile"].unique().tolist())
            break
    if not all_tenants:
        return

    # Compute per-policy per-tenant values.
    policy_values: Dict[str, Dict[str, float]] = {}
    for policy in ordered:
        d = runs[policy]
        df = d.get("df")
        if df is None or df.empty or "profile" not in df.columns:
            continue
        t_ref = d["t_ref"]
        fault_start = d["fault_start_bin"]
        fault_end = d["fault_end_bin"]
        t_cooldown = d["t_cooldown_end"]

        if phase == "fault":
            ts_start = t_ref + fault_start
            ts_end = t_ref + fault_end
        else:  # recovery
            ts_start = t_ref + fault_end
            ts_end = t_cooldown

        duration = ts_end - ts_start
        if duration <= 0:
            continue

        phase_df = df[(df["timestamp"] >= ts_start) & (df["timestamp"] < ts_end)]

        vals = {}
        for tenant in all_tenants:
            sub = phase_df[phase_df["profile"] == tenant]
            if metric == "goodput":
                ok_count = sub["ok"].sum() if not sub.empty else 0
                vals[tenant] = ok_count / duration
            elif metric == "first_attempt_sr":
                first = sub[~sub["is_retry"]]
                if len(first) > 0:
                    vals[tenant] = first["ok"].sum() / len(first) * 100.0
                else:
                    vals[tenant] = 0.0
        policy_values[policy] = vals

    if not policy_values:
        return

    n_policies = len(policy_values)
    n_tenants = len(all_tenants)
    bar_width = 0.8 / n_tenants
    x = np.arange(n_policies)

    fig, ax = plt.subplots(figsize=(max(6.4, n_policies * 2.0), 4.0))

    for i, tenant in enumerate(all_tenants):
        offsets = x + (i - n_tenants / 2 + 0.5) * bar_width
        values = [
            policy_values[p].get(tenant, 0.0) for p in policy_values
        ]
        color = _tenant_color(tenant, i)
        label = _tenant_label(tenant)
        ax.bar(offsets, values, bar_width * 0.9, label=label, color=color,
               alpha=0.85, edgecolor="white", linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policy_values])
    ax.set_ylabel(ylabel)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.legend(loc="best", frameon=True, fancybox=False,
              edgecolor="#888888", framealpha=0.95, fontsize=9)
    fig.tight_layout()
    out_path = out_dir / filename
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_and_compute_basics(policy_dir: Path) -> Optional[dict]:
    """Load client CSV + timeline; compute core per-second bins and scalars."""
    timeline = load_timeline(policy_dir)
    if not timeline:
        print(f"[warn] {policy_dir.name}: no timeline.json", file=sys.stderr)
        return None

    df = load_client_csv(policy_dir / "client-metrics")
    if df.empty:
        print(f"[warn] {policy_dir.name}: no client CSV data", file=sys.stderr)
        return None

    t_prefault_end = float(timeline["t_prefault_end"])
    t_warmup_end = float(timeline["t_warmup_end"])
    t_cooldown_end = float(timeline["t_cooldown_end"])

    t_fault_intended_start = float(timeline["t_fault_start"])
    t_fault_intended_end   = float(timeline["t_fault_end"])
    t_fault_actual_start = float(timeline.get("t_fault_actual_start", 0) or 0)
    t_fault_actual_end   = float(timeline.get("t_fault_actual_end",   0) or 0)
    t_fault_start = t_fault_actual_start if t_fault_actual_start > 0 else t_fault_intended_start
    t_fault_end   = t_fault_actual_end   if t_fault_actual_end   > 0 else t_fault_intended_end

    t_ref = t_warmup_end

    goodput = goodput_timeseries(df, t_ref, t_cooldown_end, bin_sec=1.0)
    success_rate = success_rate_timeseries(
        df, t_ref, t_cooldown_end, bin_sec=1.0, smooth_win=5,
    )
    amp = amplification(df, t_fault_start, t_fault_end)
    reff = retry_efficiency_pct(df, t_fault_start, t_fault_end)

    pre_start_bin   = 0
    pre_end_bin     = round(t_prefault_end - t_ref)
    fault_start_bin = round(t_fault_start  - t_ref)
    fault_end_bin   = round(t_fault_end    - t_ref)

    recovery = recovery_time_sec(success_rate, fault_end_bin)
    avg_goodput_fault = float(goodput.loc[fault_start_bin:fault_end_bin - 1].mean()) \
        if not goodput.empty else 0.0
    avg_goodput_pre = float(goodput.loc[pre_start_bin:pre_end_bin - 1].mean()) \
        if not goodput.empty else 0.0

    prefault_sr_pct = float(success_rate.loc[pre_start_bin:pre_end_bin - 1].mean()) \
        if not success_rate.empty else float("nan")
    if np.isnan(prefault_sr_pct):
        prefault_sr_pct = 0.0
    gp_smooth = goodput.rolling(window=5, min_periods=1, center=True).mean()
    sustained = per_spike_recovery(
        timeline,
        success_rate.to_dict(),
        gp_smooth.to_dict(),
        prefault_sr_pct=prefault_sr_pct,
        prefault_goodput=avg_goodput_pre,
        t_ref=t_ref,
        window_sec=45,
        sr_frac=0.95,
        goodput_frac=0.90,
        min_delay_sec=0,
    )

    return {
        "policy_dir": policy_dir,
        "timeline": timeline,
        "df": df,
        "t_ref": t_ref,
        "t_cooldown_end": t_cooldown_end,
        "t_fault_start": t_fault_start,
        "t_fault_end": t_fault_end,
        "goodput": goodput,
        "success_rate": success_rate,
        "pre_start_bin": pre_start_bin,
        "pre_end_bin": pre_end_bin,
        "fault_start_bin": fault_start_bin,
        "fault_end_bin": fault_end_bin,
        "amp": amp,
        "reff": reff,
        "recovery": recovery,
        "avg_goodput_fault": avg_goodput_fault,
        "avg_goodput_pre": avg_goodput_pre,
        "sustained": sustained,
    }


def process_policy_summary(policy_dir: Path, experiment: dict | None = None) -> Optional[dict]:
    """Client-CSV-only path for matrix/scalar summaries (no sidecar parsing)."""
    del experiment  # reserved for API compatibility with process_policy
    b = _load_and_compute_basics(policy_dir)
    if b is None:
        return None
    return {
        "amplification": b["amp"],
        "recovery_sec": b["recovery"] if b["recovery"] is not None else float("nan"),
        "sustained_recovery": b["sustained"],
    }


def process_policy(policy_dir: Path, experiment: dict) -> Optional[dict]:
    b = _load_and_compute_basics(policy_dir)
    if b is None:
        return None

    policy_dir = b["policy_dir"]
    timeline = b["timeline"]
    df = b["df"]
    t_ref = b["t_ref"]
    t_cooldown_end = b["t_cooldown_end"]
    t_fault_start = b["t_fault_start"]
    t_fault_end = b["t_fault_end"]
    goodput = b["goodput"]
    success_rate = b["success_rate"]
    fault_start_bin = b["fault_start_bin"]
    fault_end_bin = b["fault_end_bin"]
    amp = b["amp"]
    reff = b["reff"]
    recovery = b["recovery"]
    avg_goodput_fault = b["avg_goodput_fault"]
    avg_goodput_pre = b["avg_goodput_pre"]
    sustained = b["sustained"]

    rates = request_rate_timeseries(df, t_ref, t_cooldown_end, bin_sec=1.0)
    retry_by_status_ts = retry_by_status_timeseries(df, t_ref, t_cooldown_end, bin_sec=1.0)
    client_latency_ts = client_latency_timeseries(
        df, t_ref, t_cooldown_end, bin_sec=1.0,
    )

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

    # Enrich with Arolla sidecar counters (diffed: post minus pre).
    # We diff to scope to this run only and to avoid double-counting stale
    # counters from prior experiments that persist in .pre.stats snapshots.
    sidecar_dir = policy_dir / "sidecar-stats"
    arolla_admitted = 0.0
    arolla_rejected = 0.0
    if sidecar_dir.exists():
        for stats_file in sorted(sidecar_dir.glob("*.stats")):
            # Only post-run stats files (<svc>.stats), skip phase snapshots
            # like <svc>.pre.stats, <svc>.warmup_end.stats, etc.
            if "." in stats_file.stem:
                continue
            svc_name = stats_file.stem
            pre_file = sidecar_dir / f"{svc_name}.pre.stats"
            post_parsed = parse_envoy_stats(stats_file)
            pre_parsed = parse_envoy_stats(pre_file) if pre_file.exists() else {}
            for name, val in post_parsed.items():
                delta = val - pre_parsed.get(name, 0.0)
                if delta <= 0:
                    continue
                if "arolla_retries_admitted_total" in name:
                    arolla_admitted += delta
                elif "arolla_retries_rejected_total" in name:
                    arolla_rejected += delta

    # Per-tenant Arolla stats (from arolla-fairness-record filter).
    # Stats names: arolla_tenant_<safe_name>_admitted_total / _rejected_total
    # Diffed: post minus pre to scope to this run only.
    tenant_arolla_stats: Dict[str, Dict[str, float]] = {}
    if sidecar_dir.exists():
        for stats_file in sorted(sidecar_dir.glob("*.stats")):
            if "." in stats_file.stem:
                continue
            svc_name = stats_file.stem
            pre_file = sidecar_dir / f"{svc_name}.pre.stats"
            post_parsed = parse_envoy_stats(stats_file)
            pre_parsed = parse_envoy_stats(pre_file) if pre_file.exists() else {}
            for name, val in post_parsed.items():
                # Envoy prefixes wasm custom metrics with "wasmcustom.".
                clean = name.replace("wasmcustom.", "")
                if "arolla_tenant_" not in clean:
                    continue
                delta = val - pre_parsed.get(name, 0.0)
                if delta <= 0:
                    continue
                # Parse tenant name and metric type from stat name.
                # Format: arolla_tenant_<safe_name>_admitted_total
                #      or arolla_tenant_<safe_name>_rejected_total
                suffix = clean.replace("arolla_tenant_", "")
                if suffix.endswith("_admitted_total"):
                    tenant = suffix[:-len("_admitted_total")]
                    if tenant == "__default__":
                        continue
                    tenant_arolla_stats.setdefault(tenant, {"admitted": 0, "rejected": 0})
                    tenant_arolla_stats[tenant]["admitted"] += delta
                elif suffix.endswith("_rejected_total"):
                    tenant = suffix[:-len("_rejected_total")]
                    if tenant == "__default__":
                        continue
                    tenant_arolla_stats.setdefault(tenant, {"admitted": 0, "rejected": 0})
                    tenant_arolla_stats[tenant]["rejected"] += delta

    # Per-phase retry deltas, sourced from the .pre / .warmup_end /
    # .fault_start / .fault_end / .recovery_end / "" snapshots written by
    # run-experiment.sh's dump_sidecar_stats. Older runs only have .pre and
    # "" snapshots; the parser tolerates this and just returns whichever
    # phases it can compute.
    services_in_run = discover_services_from_sidecar_dir(sidecar_dir)
    phase_retry_deltas = parse_phase_retry_deltas(sidecar_dir, services_in_run)

    # Whole-run retry deltas (.pre.stats vs .stats), used by the chain-
    # amplification stacked-bar plot. Always available because both
    # endpoints are taken regardless of --phase-snapshots.
    run_retry_deltas = parse_run_retry_deltas(sidecar_dir, services_in_run)

    return {
        "goodput": goodput,
        "success_rate": success_rate,
        "rates": rates,
        "retry_by_status_ts": retry_by_status_ts,
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
        "sustained_recovery": sustained,
        "arolla_admitted": arolla_admitted,
        "arolla_rejected": arolla_rejected,
        "phase_retry_deltas": phase_retry_deltas,
        "run_retry_deltas": run_retry_deltas,
        "tenant_arolla_stats": tenant_arolla_stats,
        # Raw data for per-tenant fairness plots.
        "df": df,
        "t_ref": t_ref,
        "t_cooldown_end": t_cooldown_end,
    }


def _spike_sec(sustained: dict, idx: int) -> float:
    """Per-spike sustained-recovery seconds (NaN if missing / never recovered)."""
    vals = (sustained or {}).get("per_spike_sec", [])
    if idx < len(vals) and vals[idx] is not None:
        return float(vals[idx])
    return float("nan")


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
            "sustained_recovery_sec": _spike_sec(d["sustained_recovery"], 0),
            "sustained_recovery_spike2_sec": _spike_sec(d["sustained_recovery"], 1),
            "sustained_recovery_label": d["sustained_recovery"]["label"],
            "num_spikes": d["sustained_recovery"]["num_spikes"],
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

    # ---- per-phase per-service retry summary (silently no-op on old runs) ----
    print_retry_summary(runs)

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
             title="Time to reach 95% success rate",
             out_path=plots_dir / "recovery-time.pdf",
             value_fmt="{:.1f}")
    # Per-phase per-service retry plots (view (b) aggregated + view (a)
    # caller×upstream matrices). Both silently do nothing on runs that
    # only have .pre and "" snapshots.
    plot_retries_by_upstream(
        runs, plots_dir / "retries-by-upstream.pdf", experiment,
    )
    plot_retries_caller_matrix(runs, plots_dir, experiment)
    # Chain-amplification stacked bar (one bar per policy, segments by
    # caller in chain order). Always available — uses .pre.stats and
    # .stats whole-run snapshots, not the per-phase ones.
    plot_retries_stacked_by_caller(
        runs, plots_dir / "retries-stacked-by-caller.pdf",
    )
    plot_retries_stacked_by_callee(
        runs, plots_dir / "retries-stacked-by-callee.pdf",
    )
    plot_chain_retry(
        runs, plots_dir / "chain-retry.pdf",
    )
    plot_retry_status_ts(
        runs, plots_dir / "retry-status-ts.pdf", experiment,
    )
    plot_chain_retry_by_phase(
        runs, plots_dir / "chain-retry-by-phase.pdf",
    )

    # ---- Fairness plots (§6.4) ----
    # Only generated when multiple client profiles (tenants) are present.
    has_tenants = any(
        d.get("df") is not None and "profile" in d["df"].columns
        and d["df"]["profile"].nunique() > 1
        for d in runs.values()
    )
    if has_tenants:
        fairness_dir = plots_dir / "fairness"
        fairness_dir.mkdir(exist_ok=True)
        print(f"\nfairness plots:")
        plot_fairness_retry_share(runs, fairness_dir, experiment)
        plot_fairness_admission_rate(runs, fairness_dir, experiment)
        plot_fairness_polite_vs_aggressive(runs, fairness_dir, experiment)
        plot_fairness_share_over_demand(runs, fairness_dir, experiment)
        plot_fairness_success_rate(runs, fairness_dir, experiment)
        plot_fairness_goodput_per_tenant(runs, fairness_dir, experiment)
        plot_fairness_latency_per_tenant(runs, fairness_dir, experiment)
        # Aggregated bar charts: per-tenant goodput and first-attempt SR
        for phase in ("fault", "recovery"):
            _fairness_bar_chart(
                runs, fairness_dir, experiment,
                phase=phase, metric="goodput",
                ylabel="Goodput (ok req/s)",
                filename=f"goodput-bar-{phase}.pdf",
            )
            _fairness_bar_chart(
                runs, fairness_dir, experiment,
                phase=phase, metric="first_attempt_sr",
                ylabel="First-attempt success rate (%)",
                filename=f"first-attempt-sr-bar-{phase}.pdf",
                ylim=(0, 105),
            )


# ---------------------------------------------------------------------------
# Sweep-level plots (across multiple run directories)
# ---------------------------------------------------------------------------

def plot_recovery_vs_sweep(
    sweep_root: Path,
    out_path: Path,
    x_label: str = "Load (req/s)",
    format_x: str = "auto",
) -> None:
    """
    Plot recovery time vs swept parameter for each policy.

    sweep_root: directory containing one subdirectory per parameter value,
                each with a summary.csv (e.g. combined/600/, combined/800/, ...).
    format_x:   "auto" formats >=1000 as "1.0k", "raw" uses the value as-is.
    """
    _apply_paper_style()

    NEVER_Y = 70
    PLOT_MAX = 78
    # Small vertical offsets so overlapping "never" markers are distinguishable.
    arrow_offsets = {
        "no-control":         -2.0,
        "circuit-breaker":    -0.7,
        "envoy-retry-budget":  0.7,
        "arolla":              2.0,
    }

    data: Dict[str, Dict[float, Optional[float]]] = {}
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
    if not x_values:
        return

    ordered = [p for p in POLICY_ORDER if p in data] + \
              [p for p in data if p not in POLICY_ORDER]

    fig, ax = plt.subplots(figsize=(6, 4))

    # Shaded "no recovery" band.
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
                    marker=marker, markersize=7, linewidth=2, zorder=4)
        else:
            ax.plot([], [], color=color, label=label,
                    marker=marker, markersize=7, linewidth=2)

        if never_xs:
            offset = arrow_offsets.get(pol, 0)
            ax.scatter(never_xs, [NEVER_Y + offset] * len(never_xs),
                       marker=marker, s=80, color=color, zorder=5)
            if rec_xs:
                ax.plot([rec_xs[-1], min(never_xs)],
                        [rec_ys[-1], NEVER_Y + offset],
                        color=color, linestyle="--", linewidth=1.5,
                        alpha=0.5, zorder=3)

    ax.text(x_values[0], NEVER_Y, "no recovery", va="center", fontsize=11,
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
    ax.legend(loc="center left", frameon=True, fancybox=False,
              edgecolor="#cccccc", framealpha=0.95,
              bbox_to_anchor=(0.0, 0.55))

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


if __name__ == "__main__":
    main()
