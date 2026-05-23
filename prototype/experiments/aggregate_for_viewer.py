#!/usr/bin/env python3
"""Aggregate raw CSV/JSON outputs into a single timeseries.json per scenario.

Usage:
  aggregate_for_viewer.py <scenario_dir>
  aggregate_for_viewer.py <sweep_dir> --all   # iterate all child scenarios

The output schema is documented in plan-b.md (Phase 2). Aligns every
policy's clocks on ``t_rel = t - t_fault_start``, so multiple policies'
curves can be overlaid in the comparison page.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

# Subdirectories under a scenario that aren't policy outputs.
POLICY_DIRS_TO_IGNORE = {"plots", "correlation-plots", "heatmap"}

# rl-decisions.csv ships under two schemas:
#   - the test fixture's compact schema (``percent`` / ``minRetryConcurrency``)
#   - the live controller's schema (``selected_percent`` /
#     ``selected_minRetryConcurrency`` — see prototype/experiments/rl_controller.py).
# We accept either by checking which columns exist.
RL_PERCENT_COLS = ("selected_percent", "percent")
RL_MRC_COLS = ("selected_minRetryConcurrency", "minRetryConcurrency")


def _read_timeline(policy_dir: Path) -> dict | None:
    p = policy_dir / "timeline.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _phase_offsets(tl: dict) -> dict:
    t0 = tl["t_fault_start"]
    return {
        "warmup_start_rel_s":   int(tl["t_start"]          - t0),
        "prefault_start_rel_s": int(tl["t_warmup_end"]     - t0),
        "fault_start_rel_s":    0,
        "fault_end_rel_s":      int(tl["t_fault_end"]      - t0),
        "recovery_end_rel_s":   int(tl["t_recovery_end"]   - t0),
        "cooldown_end_rel_s":   int(tl["t_cooldown_end"]   - t0),
    }


def _aggregate_attempts(policy_dir: Path, t_fault_start: float) -> dict:
    """Per-second buckets matching ``analyze.py`` exactly.

    ``goodput`` and ``total`` are binned by COMPLETION time (the
    ``timestamp`` column), as in ``analyze.goodput_timeseries`` /
    ``analyze.request_rate_timeseries`` / ``analyze.client_latency_timeseries``.
    ``goodput`` counts every ``ok==1`` row — including retry-successes — because
    the loadgen breaks the retry loop on the first ok, so each successful
    logical request contributes exactly one ok-row.
    ``success_rate`` is computed separately in ``_aggregate_success_rate`` from
    the final attempt per ``request_id`` (matches
    ``analyze.success_rate_timeseries``).
    """
    csv_paths = sorted((policy_dir / "client-metrics").glob("client_attempts.shard*.csv"))
    if not csv_paths:
        return {}
    df = pd.concat([pd.read_csv(p) for p in csv_paths], ignore_index=True)
    df["is_retry"] = df["is_retry"].astype(int)
    df["ok"] = df["ok"].astype(int)
    df["latency_s"] = df["latency_s"].astype(float)
    # Completion-time binning, matching analyze.py.  floor() keeps negative
    # offsets in the correct bucket; int() would lift -0.5 to bucket 0.
    df["t_rel"] = np.floor(df["timestamp"] - t_fault_start).astype(int)

    per_sec: dict[int, dict] = defaultdict(lambda: {
        "total": 0, "originals": 0, "ok": 0, "latencies": [],
    })
    for row in df.itertuples():
        b = per_sec[int(row.t_rel)]
        b["total"] += 1
        if row.is_retry == 0:
            b["originals"] += 1
        if row.ok == 1:
            b["ok"] += 1
        b["latencies"].append(float(row.latency_s))

    return per_sec


def _aggregate_success_rate(policy_dir: Path, t_fault_start: float) -> dict[int, dict[str, float]]:
    """Per-second success rate matching ``analyze.success_rate_timeseries``.

    Each logical request (grouped by ``request_id``) contributes ONE data
    point: the final attempt's ``ok`` value, binned by that attempt's
    completion timestamp.
    """
    csv_paths = sorted((policy_dir / "client-metrics").glob("client_attempts.shard*.csv"))
    if not csv_paths:
        return {}
    df = pd.concat([pd.read_csv(p) for p in csv_paths], ignore_index=True)
    if "request_id" not in df.columns:
        return {}
    df["ok"] = df["ok"].astype(int)
    last = (
        df.sort_values("timestamp")
          .groupby("request_id", as_index=False)
          .tail(1)
          .copy()
    )
    last["t_rel"] = np.floor(last["timestamp"] - t_fault_start).astype(int)
    out: dict[int, dict[str, float]] = {}
    for t_rel, sub in last.groupby("t_rel"):
        total = int(len(sub))
        ok = int(sub["ok"].sum())
        out[int(t_rel)] = {"sr_total": total, "sr_ok": ok}
    return out


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[f] * 1000.0
    return (s[f] + (s[c] - s[f]) * (k - f)) * 1000.0


def _aggregate_resources(policy_dir: Path, t_fault_start: float) -> dict[int, dict[str, float]]:
    p = policy_dir / "resource-usage.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    df["t_rel"] = np.floor(df["timestamp"] - t_fault_start).astype(int)
    out: dict[int, dict[str, float]] = defaultdict(dict)
    grouped = df.groupby(["t_rel", "scope", "container"]).agg(
        cpu=("cpu_mcores", "sum"), mem=("memory_mib", "sum")
    ).reset_index()
    for r in grouped.itertuples():
        if r.scope == "cartservice_pod" and r.container == "server":
            out[int(r.t_rel)]["cart_pod_cpu_mcores"] = float(r.cpu)
            out[int(r.t_rel)]["cart_pod_mem_mib"] = float(r.mem)
        elif r.scope == "cartservice_pod" and r.container == "istio-proxy":
            out[int(r.t_rel)]["cart_sidecar_cpu_mcores"] = float(r.cpu)
            out[int(r.t_rel)]["cart_sidecar_mem_mib"] = float(r.mem)
        elif r.scope == "rl_controller_pod":
            # Sum across containers (controller + any sidecars).
            out[int(r.t_rel)]["rl_pod_cpu_mcores"] = (
                out[int(r.t_rel)].get("rl_pod_cpu_mcores", 0.0) + float(r.cpu)
            )
            out[int(r.t_rel)]["rl_pod_mem_mib"] = (
                out[int(r.t_rel)].get("rl_pod_mem_mib", 0.0) + float(r.mem)
            )
    return out


def _pick_col(df: "pd.DataFrame", names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _aggregate_rl_actions(policy_dir: Path, t_fault_start: float) -> dict[int, dict[str, float]]:
    p = policy_dir / "rl-controller" / "rl-decisions.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    if df.empty:
        return {}
    pct_col = _pick_col(df, RL_PERCENT_COLS)
    mrc_col = _pick_col(df, RL_MRC_COLS)
    if pct_col is None and mrc_col is None:
        return {}
    df["t_rel"] = np.floor(df["timestamp"] - t_fault_start).astype(int)
    out: dict[int, dict[str, float]] = {}
    for row in df.itertuples(index=False):
        rec = row._asdict()
        entry: dict[str, float] = {}
        if pct_col is not None:
            entry["rl_action_percent"] = float(rec[pct_col])
        if mrc_col is not None:
            entry["rl_action_minRetryConcurrency"] = int(rec[mrc_col])
        out[int(rec["t_rel"])] = entry
    return out


def _smooth(keys: list[int], values: list[float | None], win: int = 5) -> list[float | None]:
    """Apply a centered rolling mean (window=*win* seconds) to *values*.

    Handles sparse time grids by reindexing onto a dense grid before smoothing
    and back-projecting the results.  NaN gaps are preserved across the output.
    The window size matches the ``smooth_win=5`` default in
    ``analyze.py:success_rate_timeseries``.
    """
    if not keys:
        return values
    lo, hi = keys[0], keys[-1]
    dense = pd.Series(dict(zip(keys, values)), dtype=float)
    dense = dense.reindex(range(lo, hi + 1))
    smoothed = dense.rolling(window=win, min_periods=1, center=True).mean()
    return [None if pd.isna(v) else float(v) for v in smoothed.loc[keys]]


def _forward_fill(seq: list[float | None]) -> list[float | None]:
    last: float | None = None
    out: list[float | None] = []
    for v in seq:
        if v is None:
            out.append(last)
        else:
            out.append(v)
            last = v
    return out


def _build_policy_series(policy_dir: Path) -> dict | None:
    tl = _read_timeline(policy_dir)
    if tl is None:
        return None
    t0 = float(tl["t_fault_start"])

    attempts    = _aggregate_attempts(policy_dir, t0)
    sr_buckets  = _aggregate_success_rate(policy_dir, t0)
    resources   = _aggregate_resources(policy_dir, t0)
    rl_actions  = _aggregate_rl_actions(policy_dir, t0)

    # Clip to the authoritative experiment window from timeline.json so that
    # stale resource samples from a previous run (sampler not cleanly stopped)
    # don't push t_rel_s[0] far into the past.
    t_lo = int(tl["t_start"] - t0)
    t_hi = int(tl["t_cooldown_end"] - t0)
    keys = sorted(
        k for k in (set(attempts) | set(sr_buckets) | set(resources) | set(rl_actions))
        if t_lo <= k <= t_hi
    )
    if not keys:
        return None

    # success_rate: only series we smooth — matches analyze.success_rate_timeseries
    # (final attempt per logical request, ok/total per bucket, 5s rolling mean).
    raw_success = [
        (sr_buckets[k]["sr_ok"] / sr_buckets[k]["sr_total"])
        if sr_buckets.get(k, {}).get("sr_total") else None
        for k in keys
    ]

    series = {
        "t_rel_s": keys,
        # Goodput / total / latency / amplification match analyze.py:
        #   * goodput_rps  = count(ok==1) per completion-time bucket
        #     (analyze.goodput_timeseries — includes retry-successes since the
        #     loadgen emits one ok-row per successful logical request)
        #   * total_rps    = count(all attempts) per completion-time bucket
        #   * latency_*_ms = quantile over all attempts in the bucket
        #     (analyze.client_latency_timeseries)
        # No smoothing on these (analyze.py doesn't smooth them either) — any
        # transient bursts visible here reflect real loadgen / system behavior.
        "goodput_rps":         [attempts.get(k, {}).get("ok", 0) for k in keys],
        "total_rps":           [attempts.get(k, {}).get("total", 0) for k in keys],
        "success_rate":        _smooth(keys, raw_success),
        "retry_amplification": [
            (attempts[k]["total"] / attempts[k]["originals"])
            if attempts.get(k, {}).get("originals") else None
            for k in keys
        ],
        "latency_p50_ms":      [_percentile(attempts.get(k, {}).get("latencies", []), 0.50) for k in keys],
        "latency_p90_ms":      [_percentile(attempts.get(k, {}).get("latencies", []), 0.90) for k in keys],
        "latency_p95_ms":      [_percentile(attempts.get(k, {}).get("latencies", []), 0.95) for k in keys],
        "latency_p99_ms":      [_percentile(attempts.get(k, {}).get("latencies", []), 0.99) for k in keys],
        "cart_pod_cpu_mcores":     [resources.get(k, {}).get("cart_pod_cpu_mcores")     for k in keys],
        "cart_sidecar_cpu_mcores": [resources.get(k, {}).get("cart_sidecar_cpu_mcores") for k in keys],
        "cart_pod_mem_mib":        [resources.get(k, {}).get("cart_pod_mem_mib")        for k in keys],
        "cart_sidecar_mem_mib":    [resources.get(k, {}).get("cart_sidecar_mem_mib")    for k in keys],
        "rl_pod_cpu_mcores":       [resources.get(k, {}).get("rl_pod_cpu_mcores")       for k in keys],
        "rl_pod_mem_mib":          [resources.get(k, {}).get("rl_pod_mem_mib")          for k in keys],
        "rl_action_percent":             _forward_fill(
            [rl_actions.get(k, {}).get("rl_action_percent")             for k in keys]
        ),
        "rl_action_minRetryConcurrency": _forward_fill(
            [rl_actions.get(k, {}).get("rl_action_minRetryConcurrency") for k in keys]
        ),
    }
    return series


def _discover_policies(scenario_dir: Path) -> list[Path]:
    out = []
    for child in sorted(scenario_dir.iterdir()):
        if not child.is_dir() or child.name in POLICY_DIRS_TO_IGNORE:
            continue
        if (child / "timeline.json").exists():
            out.append(child)
    return out


def build_timeseries(scenario_dir: Path) -> dict:
    scenario_dir = Path(scenario_dir).resolve()
    policy_dirs = _discover_policies(scenario_dir)

    phases_per_policy = []
    series: dict[str, dict] = {}
    for pd_dir in policy_dirs:
        tl = _read_timeline(pd_dir)
        if tl is None:
            continue
        phases_per_policy.append(_phase_offsets(tl))
        s = _build_policy_series(pd_dir)
        if s is not None:
            series[pd_dir.name] = s

    if not phases_per_policy:
        raise SystemExit(f"no timeline.json found under {scenario_dir}")

    def _median_field(name: str) -> int:
        return int(statistics.median(p[name] for p in phases_per_policy))

    phases = {k: _median_field(k) for k in phases_per_policy[0]}

    return {
        "scenario_label": scenario_dir.name,
        "policies": list(series.keys()),
        "phases": phases,
        "series": series,
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--all", action="store_true",
                    help="Treat path as a sweep dir; aggregate every scenario inside")
    args = ap.parse_args(argv)

    targets = (
        [c for c in args.path.iterdir() if c.is_dir() and c.name.startswith("rate_rps=")]
        if args.all else [args.path]
    )
    for scen_dir in targets:
        out = build_timeseries(scen_dir)
        dst = scen_dir / "timeseries.json"
        dst.write_text(json.dumps(out, indent=2))
        print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
