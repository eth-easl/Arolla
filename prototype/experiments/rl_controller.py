#!/usr/bin/env python3
"""RL retry-budget controller for prototype experiments.

Observation contract: 18-feature float32 vector matching
ISTIO_OBSERVATION_SPACE_ANALYSIS.md exactly (the original training contract).

Action contract: MultiDiscrete([5,5])
  percent ∈ [5,10,20,30,50], minRetryConcurrency ∈ [1,2,3,5,8]

The controller has three operating modes:
  rl_ppo       Load the trained PPO model and apply its decisions to Istio.
  shadow_stub  Build observations and log what would be applied, never patches.
  (fallback)   If model loading fails, degrades to shadow_stub with a warning.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

# Shared schema with the loader (prototype/clients/online-boutique/traffic_gen.py).
# Both sides import from rl_obs_schema.py so the histogram edges and the
# WindowBucket field order are defined in exactly one place. The bare import
# works because the file sits next to rl_controller.py in the experiments
# directory (and inside the container both files land at /app/).
from rl_obs_schema import (  # noqa: E402
    LATENCY_HISTOGRAM_EDGES_S,
    LATENCY_HISTOGRAM_NUM_BUCKETS,
)


# Feature order must match the training environment exactly.
OBSERVATION_FIELDS = [
    "success_rate_agg",
    "min_client_success",
    "retry_ratio",
    "window_load_amplification",
    "window_retry_efficiency",
    "retry_fairness_gap",
    "p95_latency_pressure",
    "queue_utilization",
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

STOP = False


def handle_signal(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


@dataclass
class RetryBudget:
    percent: float
    min_retry_concurrency: int


@dataclass
class TickTiming:
    """Per-tick latency breakdown written to rl-timings.jsonl.

    All ms fields are wall-clock deltas measured with time.perf_counter().
    `t_loop_start` is `time.time()` at the top of the iteration so a tick can
    be cross-referenced to the rl-observations.jsonl `timestamp` field.

    `xds_apply_ms` is populated only when --xds-probe is on; in normal runs
    it stays None (the field is still emitted for schema stability)."""

    tick_index: int
    t_loop_start: float
    obs_fetch_ms: float = 0.0
    obs_parse_ms: float = 0.0
    obs_build_ms: float = 0.0
    inference_ms: float = 0.0
    patch_ms: float = 0.0
    patch_attempted: bool = False
    patch_succeeded: bool = False
    total_ms: float = 0.0
    rows: int = 0
    xds_apply_ms: float | None = None


@dataclass
class ControllerConfig:
    mode: str
    model_path: Path | None
    decision_interval_sec: float
    observation_window_sec: float
    # Suppress patches for this many seconds after startup so the model's
    # first decision is based on a real, populated observation window
    # instead of empty/degenerate metrics (which would otherwise bias the
    # PPO policy toward its most defensive action).
    startup_hold_off_sec: float
    default_budget: RetryBudget
    percent_actions: list[float]
    min_retry_actions: list[int]


def load_config(path: Path) -> ControllerConfig:
    doc = yaml.safe_load(path.read_text())
    ctl = doc["controller"]
    defaults = ctl["default_retry_budget"]

    raw_model = doc.get("model_path")
    model_path: Path | None = None
    if raw_model:
        candidate = Path(raw_model)
        config_dir = path.parent
        # Resolve relative to the script directory (where run-experiment.sh
        # lives) — the original laptop-side path. The in-cluster Job also
        # has to handle the ConfigMap layout (`/etc/rl/{model.zip,config.yaml}`),
        # where the model sits next to the config under a fixed name. Try
        # several locations in order before giving up.
        script_dir = Path(__file__).parent
        search = []
        if candidate.is_absolute():
            search.append(candidate)
        else:
            search.append((script_dir / candidate).resolve())
            search.append((config_dir / candidate).resolve())
            # In-cluster ConfigMap: model lands at <config_dir>/model.zip
            # because `--from-file=model.zip=<path>` rewrites the key.
            search.append(config_dir / "model.zip")
        for resolved in search:
            if resolved.exists():
                model_path = resolved
                break
        if model_path is None:
            tried = ", ".join(str(p) for p in search)
            print(
                f"[rl_controller] WARNING: model_path '{raw_model}' not found "
                f"(tried: {tried}); falling back to shadow_stub",
                file=sys.stderr,
            )

    obs_window = float(ctl.get("observation_window_sec", 10))
    return ControllerConfig(
        mode=str(doc.get("mode", ctl.get("mode", "shadow_stub"))),
        model_path=model_path,
        decision_interval_sec=float(ctl.get("decision_interval_sec", 5)),
        observation_window_sec=obs_window,
        # Default = obs_window + 20s. The +20s covers warmup + load ramp so
        # the first decision sees a fully-populated window of real traffic.
        startup_hold_off_sec=float(
            ctl.get("startup_hold_off_sec", obs_window + 20.0)
        ),
        default_budget=RetryBudget(
            percent=float(defaults.get("percent", 20.0)),
            min_retry_concurrency=int(defaults.get("minRetryConcurrency", 3)),
        ),
        percent_actions=[float(v) for v in ctl["action_maps"]["percent"]],
        min_retry_actions=[int(v) for v in ctl["action_maps"]["minRetryConcurrency"]],
    )


def load_rl_model(path: Path) -> Any | None:
    """Load a trained SB3 PPO model from a zip file.  Returns None on failure."""
    try:
        from stable_baselines3 import PPO  # noqa: PLC0415
    except ImportError:
        print(
            "[rl_controller] stable_baselines3 not installed; "
            "run: pip install stable-baselines3 torch",
            file=sys.stderr,
        )
        return None
    try:
        model = PPO.load(str(path), device="cpu")
        print(
            f"[rl_controller] loaded PPO model from {path}  "
            f"(obs={model.observation_space.shape}, act={model.action_space})",
            file=sys.stderr,
        )
        return model
    except Exception as exc:
        print(f"[rl_controller] model load failed: {exc}", file=sys.stderr)
        return None


def clamp_to_actions(value: float | int, actions: list[float | int]) -> float | int:
    if not actions:
        return value
    return min(actions, key=lambda candidate: abs(float(candidate) - float(value)))


def run_cmd(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


def ssh_cat_metrics(
    client_host: str,
    ssh_user: str,
    ssh_opts: str,
    remote_metrics_dir: str,
    timeout: float = 8.0,
    since_ts: float | None = None,
) -> str:
    """Fetch client_attempts*.csv rows newer than `since_ts` from CLIENT_HOST.

    Phase 1.2: when `since_ts` is provided, the remote shell now runs an awk
    pre-filter so only rows whose timestamp (column 1) is ≥ since_ts are sent
    over the wire. At 1.6k RPS × 2 s decision interval that is ~3 KB per tick
    instead of multiple MB, which both shrinks the SCP-style copy and keeps
    parse_client_rows from re-doing the timestamp filter on millions of stale
    rows. Falls back to a full cat when since_ts is None (legacy callers)."""
    target = f"{ssh_user}@{client_host}"
    cmd = ["ssh"]
    if ssh_opts:
        cmd.extend(ssh_opts.split())
    if since_ts is None:
        remote = f"cat {remote_metrics_dir}/client_attempts*.csv 2>/dev/null || true"
    else:
        # `awk -F,` with `$1+0 >= s` does a numeric compare on the timestamp
        # column. The header line is dropped because parse_client_rows
        # already skips it; the `2>/dev/null || true` keeps the call quiet
        # when no shard files exist yet (e.g. first tick after start).
        # Pass since_ts via -v to avoid quoting headaches in the SSH command.
        remote = (
            f"awk -F, -v s={since_ts:.6f} '$1+0 >= s' "
            f"{remote_metrics_dir}/client_attempts*.csv 2>/dev/null || true"
        )
    cmd.extend([target, remote])
    try:
        proc = run_cmd(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


# ---------------------------------------------------------------------------
# Observation fetch — HTTP path (in-cluster) and SSH-cat path (legacy laptop)
#
# The HTTP path fans out across `loader_ports` (one per loader shard), reuses
# a single requests.Session so HTTP keep-alive + connection pooling apply,
# and feeds the same `parse_client_rows` consumer as the SSH path. That
# keeps the `build_metrics → build_observation` chain identical regardless
# of which transport is in use, which is what makes the decision-drift
# acceptance criterion in plan-12 §1 enforceable.
# ---------------------------------------------------------------------------

_HTTP_SESSION: Any = None


def _get_http_session() -> Any:
    """Cached requests.Session — one TCP/TLS pool reused across all ticks."""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        return None
    _HTTP_SESSION = requests.Session()
    return _HTTP_SESSION


def http_fetch_window(
    url_template: str,
    ports: list[int],
    since_ts: float,
    timeout: float = 4.0,
) -> str:
    """Pull recent attempt rows from every loader shard and re-serialise as
    CSV-ish text so `parse_client_rows` doesn't change.

    Returns "" on transport failure. The caller treats that the same way as
    a stale SSH read (degenerate metrics → controller's startup hold-off
    keeps it from acting on garbage)."""
    session = _get_http_session()
    if session is None:
        return ""
    out_lines: list[str] = []
    for port in ports:
        url = url_template.format(port=port) + f"?since={since_ts:.6f}"
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                continue
            doc = resp.json()
        except Exception:
            continue
        for row in doc.get("rows", []):
            out_lines.append(
                f"{row.get('timestamp', 0):.6f},"
                f"{row.get('profile', '')},"
                f"{row.get('worker', '')},"
                f"{row.get('request_id', '')},"
                f"{row.get('request_type', '')},"
                f"{row.get('method', '')},"
                f"{row.get('path', '')},"
                f"{row.get('attempt', 0)},"
                f"{row.get('is_retry', 0)},"
                f"{row.get('status', 0)},"
                f"{row.get('ok', 0)},"
                f"{row.get('latency_s', 0):.6f}"
            )
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def parse_client_rows(raw: str, since_ts: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line or line.startswith("timestamp,"):
            continue
        try:
            row = next(csv.DictReader([line], fieldnames=[
                "timestamp", "profile", "worker", "request_id", "request_type",
                "method", "path", "attempt", "is_retry", "status", "ok", "latency_s",
            ]))
            ts = float(row["timestamp"])
            if ts < since_ts:
                continue
            row["_timestamp"] = ts
            row["_attempt"] = int(float(row.get("attempt") or 0))
            row["_is_retry"] = int(float(row.get("is_retry") or 0))
            row["_status"] = int(float(row.get("status") or 0))
            row["_ok"] = int(float(row.get("ok") or 0))
            row["_latency_s"] = float(row.get("latency_s") or 0.0)
            rows.append(row)
        except (ValueError, StopIteration):
            continue
    return rows


# ---------------------------------------------------------------------------
# Buckets path (Phase 3) — pre-aggregated observation transport
#
# The loader pre-aggregates per-attempt counters into 1-second
# (shard, profile) buckets and serves them at /buckets. Per-tick payload
# drops from ~10 k JSON rows to ~40 buckets (~5 KB total), and the
# controller's compose step becomes a single linear pass over the bucket
# list + an O(22) histogram p95 reconstruction.
#
# Layout of `WindowBucket` is deliberately frozen here so a future schema
# change doesn't silently break the loader/controller contract; both ends
# pull the histogram edges out of `rl_obs_schema.py`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowBucket:
    """Decoded mirror of the loader's WindowBucket (rl_obs_schema.WindowBucket).

    Kept as a frozen dataclass so `compose_observation_from_buckets` can rely
    on the field order matching what the loader's `dataclasses.asdict`
    produced. The `latency_hist` is a tuple here (no mutation post-fetch)
    even though the loader uses a list.
    """

    ts_sec: int
    shard_id: int
    profile: str
    attempts: int
    requests: int
    successes: int
    retries: int
    retry_successes: int
    server_failures: int
    deadline_failures: int
    latency_hist: tuple[int, ...]


def http_fetch_buckets(
    url_template: str,
    ports: list[int],
    since_ts: float,
    timeout: float = 4.0,
) -> tuple[list[WindowBucket], int]:
    """Pull pre-aggregated buckets from every loader shard.

    Returns ``(buckets, ok_shards)``. The shard count is used by the main
    loop to detect a silent rows/buckets transport regression: if 0 shards
    answer 200 OK for /buckets across several consecutive ticks, the
    controller logs a warning and (in `auto` mode) re-routes to /window.
    """
    session = _get_http_session()
    if session is None:
        return ([], 0)
    out: list[WindowBucket] = []
    ok_shards = 0
    for port in ports:
        url = url_template.format(port=port) + f"?since={since_ts:.6f}"
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                continue
            doc = resp.json()
        except Exception:
            continue
        ok_shards += 1
        for raw in doc.get("buckets", []):
            try:
                out.append(WindowBucket(
                    ts_sec=int(raw.get("ts_sec", 0)),
                    shard_id=int(raw.get("shard_id", 0)),
                    profile=str(raw.get("profile", "")),
                    attempts=int(raw.get("attempts", 0)),
                    requests=int(raw.get("requests", 0)),
                    successes=int(raw.get("successes", 0)),
                    retries=int(raw.get("retries", 0)),
                    retry_successes=int(raw.get("retry_successes", 0)),
                    server_failures=int(raw.get("server_failures", 0)),
                    deadline_failures=int(raw.get("deadline_failures", 0)),
                    latency_hist=tuple(
                        int(x) for x in raw.get("latency_hist", ())
                    ),
                ))
            except (TypeError, ValueError):
                # A malformed bucket on one shard should not poison the
                # whole tick; the controller is resilient to a partial
                # window for the same reason the rows path is.
                continue
    return (out, ok_shards)


def p95_from_histogram(
    hist: list[int] | tuple[int, ...],
    edges: tuple[float, ...] = LATENCY_HISTOGRAM_EDGES_S,
) -> float:
    """Reconstruct p95 latency from the aggregated histogram.

    Linear interpolation inside the bucket containing the 95th-percentile
    sample. The overflow bucket (index len(edges), unbounded above) maps
    to the last finite edge so a fully-saturated window clamps to the
    edge value — the consumer then divides by the 3 s attempt-timeout and
    clamps to [0, 1] (see `_bucket_compose_pressure`).
    """
    n = sum(hist)
    if n == 0:
        return 0.0
    target = 0.95 * n
    cum = 0
    for i, count in enumerate(hist):
        if count <= 0:
            continue
        if cum + count >= target:
            lo = edges[i - 1] if 0 < i <= len(edges) else 0.0
            if i < len(edges):
                hi = edges[i]
            else:
                hi = edges[-1]  # overflow bucket: clamp to last edge
            if hi == lo:
                return hi
            frac = (target - cum) / count
            return lo + (hi - lo) * frac
        cum += count
    return edges[-1]


def compose_observation_from_buckets(
    buckets: list[WindowBucket],
    window_sec: float,
    current: RetryBudget,
    previous: RetryBudget,
    previous_metrics: dict[str, Any] | None,
) -> tuple[list[float], dict[str, str], dict[str, Any]]:
    """Build the 18-feature observation vector from pre-aggregated buckets.

    Mirror of the rows path
    ``build_metrics → build_observation``, but every metric is computed
    from integer counter sums + one histogram pass. Pure Python with
    < 1 KOp per tick on typical workloads.
    """
    total_attempts = 0
    total_requests = 0
    total_successes = 0
    total_retries = 0
    total_retry_successes = 0
    total_server_failures = 0
    total_deadline_failures = 0
    hist = [0] * LATENCY_HISTOGRAM_NUM_BUCKETS

    per_profile: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "attempts": 0,
            "requests": 0,
            "successes": 0,
            "retries": 0,
        },
    )

    for b in buckets:
        total_attempts += b.attempts
        total_requests += b.requests
        total_successes += b.successes
        total_retries += b.retries
        total_retry_successes += b.retry_successes
        total_server_failures += b.server_failures
        total_deadline_failures += b.deadline_failures
        p = per_profile[b.profile]
        p["attempts"] += b.attempts
        p["requests"] += b.requests
        p["successes"] += b.successes
        p["retries"] += b.retries
        # Histogram width is frozen at LATENCY_HISTOGRAM_NUM_BUCKETS; a
        # mismatched bucket from a stale loader image gets clipped to that
        # width (anything past it is dropped). Loud failure would be
        # preferable but the loader version is not on the wire — drift is
        # caught later by bench_decision_diff --per-feature.
        for i, count in enumerate(b.latency_hist[:LATENCY_HISTOGRAM_NUM_BUCKETS]):
            hist[i] += count

    requests_denom = max(total_requests, 1)
    attempts_denom = max(total_attempts, 1)
    retries_denom = max(total_retries, 1)

    success_rate = total_successes / requests_denom
    profile_success_rates: list[float] = []
    request_share: dict[str, float] = {}
    retry_share: dict[str, float] = {}
    retry_total = max(sum(p["retries"] for p in per_profile.values()), 1)
    for name, p in per_profile.items():
        denom = max(p["requests"], 1)
        profile_success_rates.append(p["successes"] / denom)
        request_share[name] = p["requests"] / requests_denom
        retry_share[name] = p["retries"] / retry_total

    fairness_gap = 0.0
    if len(per_profile) > 1:
        names = set(request_share) | set(retry_share)
        fairness_gap = max(
            abs(retry_share.get(n, 0.0) - request_share.get(n, 0.0))
            for n in names
        )

    retry_ratio = total_retries / attempts_denom
    load_amplification = total_attempts / requests_denom
    retry_efficiency = (
        total_retry_successes / retries_denom if total_retries else 0.0
    )
    p95_lat = p95_from_histogram(hist)
    latency_pressure = min(p95_lat / 3.0, 1.0)

    # Pseudo-RPS derived from the configured window size; identical to the
    # rows path's `retry_rps`/`request_rps` so downstream pressure
    # computations match exactly.
    retry_rps = total_retries / max(window_sec, 1.0)
    request_rps = total_requests / max(window_sec, 1.0)

    metrics = {
        # Same keys the rows path's `build_metrics` returns, so the
        # downstream JSONL ingestion (`rl-observations.jsonl`) is
        # byte-compatible.
        "rows": total_attempts,
        "requests": total_requests,
        "window_sec": window_sec,
        "success_rate_agg": success_rate,
        "min_client_success": (
            min(profile_success_rates) if profile_success_rates else success_rate
        ),
        "retry_ratio": retry_ratio,
        "window_load_amplification": load_amplification,
        "window_retry_efficiency": retry_efficiency,
        "retry_fairness_gap": fairness_gap,
        "p95_latency_pressure": latency_pressure,
        "server_fail_rate": total_server_failures / attempts_denom,
        "deadline_rate": total_deadline_failures / attempts_denom,
        "retry_rps": retry_rps,
        "request_rps": request_rps,
        "quality": {
            "client_metrics": "live_buckets" if buckets else "empty_window",
            "retry_fairness_gap": (
                "per_profile" if len(per_profile) > 1 else "single_client_profile"
            ),
        },
    }

    obs, sources = build_observation(metrics, current, previous, previous_metrics)
    sources = dict(sources)
    sources["obs_transport"] = "http_buckets"
    return obs, sources, metrics


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[int(rank)]
    return values[lower] * (upper - rank) + values[upper] * (rank - lower)


def final_attempts(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finals: dict[str, dict[str, Any]] = {}
    for row in rows:
        req_id = str(row.get("request_id", ""))
        if not req_id:
            continue
        prev = finals.get(req_id)
        if prev is None or row["_attempt"] >= prev["_attempt"]:
            finals[req_id] = row
    return finals


def build_metrics(rows: list[dict[str, Any]], window_sec: float) -> dict[str, Any]:
    finals = final_attempts(rows)
    total_attempts = len(rows)
    total_requests = max(len(finals), 1)
    retries = sum(1 for row in rows if row["_is_retry"])
    retry_successes = sum(1 for row in rows if row["_is_retry"] and row["_ok"])
    success_requests = sum(1 for row in finals.values() if row["_ok"])
    server_failures = sum(1 for row in rows if row["_status"] >= 500)
    deadline_failures = sum(1 for row in rows if row["_status"] == 0)
    latencies = [row["_latency_s"] for row in finals.values()]

    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in finals.values():
        by_profile[str(row.get("profile", ""))].append(row)

    profile_success = []
    for profile_rows in by_profile.values():
        denom = max(len(profile_rows), 1)
        profile_success.append(sum(1 for row in profile_rows if row["_ok"]) / denom)

    request_share = {p: len(v) / total_requests for p, v in by_profile.items()}
    retry_by_profile: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["_is_retry"]:
            retry_by_profile[str(row.get("profile", ""))] += 1
    retry_total = max(sum(retry_by_profile.values()), 1)
    retry_share = {p: c / retry_total for p, c in retry_by_profile.items()}
    fairness_gap = 0.0
    if len(by_profile) > 1:
        profiles = set(request_share) | set(retry_share)
        fairness_gap = max(
            abs(retry_share.get(p, 0.0) - request_share.get(p, 0.0)) for p in profiles
        )

    retry_ratio = retries / max(total_attempts, 1)
    load_amplification = total_attempts / total_requests
    retry_efficiency = retry_successes / max(retries, 1) if retries else 0.0
    success_rate = success_requests / total_requests
    # p95 latency pressure: divide by 3 s attempt-timeout and clamp to [0, 1].
    latency_pressure = min(percentile(latencies, 95) / 3.0, 1.0)

    return {
        "rows": len(rows),
        "requests": len(finals),
        "window_sec": window_sec,
        "success_rate_agg": success_rate,
        "min_client_success": min(profile_success) if profile_success else success_rate,
        "retry_ratio": retry_ratio,
        "window_load_amplification": load_amplification,
        "window_retry_efficiency": retry_efficiency,
        "retry_fairness_gap": fairness_gap,
        "p95_latency_pressure": latency_pressure,
        "server_fail_rate": server_failures / max(total_attempts, 1),
        "deadline_rate": deadline_failures / max(total_attempts, 1),
        "retry_rps": retries / max(window_sec, 1.0),
        "request_rps": len(finals) / max(window_sec, 1.0),
        "quality": {
            "client_metrics": "live_remote_csv" if rows else "empty_window",
            "retry_fairness_gap": (
                "per_profile" if len(by_profile) > 1 else "single_client_profile"
            ),
        },
    }


def build_observation(
    metrics: dict[str, Any],
    current: RetryBudget,
    previous: RetryBudget,
    previous_metrics: dict[str, Any] | None,
) -> tuple[list[float], dict[str, str]]:
    source = {
        "queue_utilization": "unavailable_approx_zero",
        "budget_utilization": "approximated_from_window_retry_rps",
        "retry_pressure_vs_limit": "approximated_from_window_retry_rps",
    }

    delta_success = 0.0
    delta_amp = 0.0
    if previous_metrics:
        delta_success = (
            metrics["success_rate_agg"] - previous_metrics["success_rate_agg"]
        )
        delta_amp = (
            metrics["window_load_amplification"]
            - previous_metrics["window_load_amplification"]
        )

    # Approximate budget saturation from the retry rate vs allowed budget.
    allowed_retry_rps = max(
        float(current.min_retry_concurrency),
        metrics["request_rps"] * float(current.percent) / 100.0,
        1e-9,
    )
    retry_pressure = metrics["retry_rps"] / allowed_retry_rps
    budget_utilization = min(retry_pressure, 1.0)

    # Normalisations match the training contract from ISTIO_OBSERVATION_SPACE_ANALYSIS.md:
    #   current_percent_norm              = percent / 100
    #   current_min_retry_concurrency_norm = mrc / 10
    obs = {
        "success_rate_agg": metrics["success_rate_agg"],
        "min_client_success": metrics["min_client_success"],
        "retry_ratio": metrics["retry_ratio"],
        "window_load_amplification": metrics["window_load_amplification"],
        "window_retry_efficiency": metrics["window_retry_efficiency"],
        "retry_fairness_gap": metrics["retry_fairness_gap"],
        "p95_latency_pressure": metrics["p95_latency_pressure"],
        "queue_utilization": 0.0,
        "server_fail_rate": metrics["server_fail_rate"],
        "deadline_rate": metrics["deadline_rate"],
        "delta_success_agg": delta_success,
        "delta_window_load_amplification": delta_amp,
        "budget_utilization": budget_utilization,
        "retry_pressure_vs_limit": retry_pressure,
        "current_percent_norm": current.percent / 100.0,
        "current_min_retry_concurrency_norm": current.min_retry_concurrency / 10.0,
        "previous_percent_norm": previous.percent / 100.0,
        "previous_min_retry_concurrency_norm": previous.min_retry_concurrency / 10.0,
    }
    return [float(obs[name]) for name in OBSERVATION_FIELDS], source


def rl_policy(
    obs: list[float],
    model: Any,
    cfg: ControllerConfig,
) -> tuple[RetryBudget, tuple[int, int]]:
    """Run deterministic inference with the loaded PPO model."""
    try:
        import numpy as np  # noqa: PLC0415
        obs_arr = np.array(obs, dtype=np.float32).reshape(1, -1)
    except ImportError:
        import array as _a
        obs_arr = _a.array("f", obs)

    action, _ = model.predict(obs_arr, deterministic=True)
    pct_idx = int(action.flat[0])
    mrc_idx = int(action.flat[1])
    pct_idx = max(0, min(pct_idx, len(cfg.percent_actions) - 1))
    mrc_idx = max(0, min(mrc_idx, len(cfg.min_retry_actions) - 1))
    return RetryBudget(
        percent=cfg.percent_actions[pct_idx],
        min_retry_concurrency=cfg.min_retry_actions[mrc_idx],
    ), (pct_idx, mrc_idx)


def stub_policy(current: RetryBudget, cfg: ControllerConfig) -> RetryBudget:
    return RetryBudget(
        percent=float(clamp_to_actions(current.percent, cfg.percent_actions)),
        min_retry_concurrency=int(
            clamp_to_actions(current.min_retry_concurrency, cfg.min_retry_actions)
        ),
    )


# ---------------------------------------------------------------------------
# Kubernetes client — persistent CustomObjectsApi (no per-tick kubectl
# subprocess), with in-cluster service-account tokens picked up via
# load_incluster_config() when the controller runs as a Job.
#
# Lazy singleton — the TLS handshake and config load happen the first time
# we need to patch, then the HTTP/2 PATCH stream is reused for every later
# tick. We try in-cluster config first so the same controller binary runs
# unmodified inside a Job; falling back to a kubeconfig file is the
# laptop-side path (KUBECONFIG=~/.kube/config-emulab).
# ---------------------------------------------------------------------------

_K8S_API: Any = None
_K8S_API_BACKEND: str = "uninitialised"


def _get_k8s_custom_objects_api() -> Any:
    """Return a cached CustomObjectsApi or None if the python client is
    unavailable / no usable kubeconfig is in scope. The first call initialises
    config; subsequent calls return the same client."""
    global _K8S_API, _K8S_API_BACKEND
    if _K8S_API is not None or _K8S_API_BACKEND in {"in_cluster", "kube_config"}:
        return _K8S_API
    try:
        from kubernetes import client, config  # noqa: PLC0415
    except ImportError:
        print(
            "[rl_controller] python kubernetes client not installed; "
            "falling back to kubectl subprocess for patches",
            file=sys.stderr,
        )
        _K8S_API_BACKEND = "kubectl_subprocess"
        return None

    try:
        config.load_incluster_config()
        _K8S_API_BACKEND = "in_cluster"
    except Exception:
        try:
            config.load_kube_config()
            _K8S_API_BACKEND = "kube_config"
        except Exception as exc:
            print(
                f"[rl_controller] could not load kube config "
                f"({exc.__class__.__name__}: {exc}); falling back to kubectl",
                file=sys.stderr,
            )
            _K8S_API_BACKEND = "kubectl_subprocess"
            return None

    _K8S_API = client.CustomObjectsApi()
    print(
        f"[rl_controller] kubernetes client ready (backend={_K8S_API_BACKEND})",
        file=sys.stderr,
    )
    return _K8S_API


def _patch_retry_budget_via_kubectl(namespace: str, budget: RetryBudget) -> bool:
    """Last-resort fallback: shell out to kubectl. Used only when the python
    kubernetes client is unavailable (legacy hosts without the dependency)."""
    payload = {
        "spec": {
            "trafficPolicy": {
                "retryBudget": {
                    "percent": budget.percent,
                    "minRetryConcurrency": budget.min_retry_concurrency,
                }
            }
        }
    }
    cmd = [
        "kubectl", "-n", namespace,
        "patch", "destinationrule", "arolla-baseline-retry-budget",
        "--type=merge", "-p", json.dumps(payload),
    ]
    proc = run_cmd(cmd, timeout=10)
    return proc.returncode == 0


def patch_retry_budget(
    namespace: str, budget: RetryBudget, *, force_kubectl: bool = False,
) -> bool:
    if force_kubectl:
        return _patch_retry_budget_via_kubectl(namespace, budget)
    api = _get_k8s_custom_objects_api()
    if api is None:
        return _patch_retry_budget_via_kubectl(namespace, budget)
    body = {
        "spec": {
            "trafficPolicy": {
                "retryBudget": {
                    "percent": budget.percent,
                    "minRetryConcurrency": budget.min_retry_concurrency,
                }
            }
        }
    }
    try:
        api.patch_namespaced_custom_object(
            group="networking.istio.io",
            version="v1",
            namespace=namespace,
            plural="destinationrules",
            name="arolla-baseline-retry-budget",
            body=body,
        )
        return True
    except Exception as exc:
        print(
            f"[rl_controller] patch_namespaced_custom_object failed: "
            f"{exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# xDS apply probe (optional one-off characterisation only)
#
# Polls one istio-proxy's /clusters?format=json every 100 ms after a patch
# and records the wall-clock time at which retry_budget.budget_percent
# first matches the just-patched value. Background thread; off by default.
# Don't run measurement sweeps with this on — the 100 ms kubectl-exec poll
# itself perturbs the patch latency we're trying to measure.
# ---------------------------------------------------------------------------


def kubectl_exec_clusters(namespace: str, pod: str) -> str | None:
    cmd = [
        "kubectl", "-n", namespace, "exec", pod, "-c", "istio-proxy", "--",
        "curl", "-s", "localhost:15000/clusters?format=json",
    ]
    try:
        proc = run_cmd(cmd, timeout=4.0)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def find_proxy_pod(namespace: str) -> str | None:
    cmd = [
        "kubectl", "-n", namespace, "get", "pod",
        "-l", "app=cartservice",
        "-o", "jsonpath={.items[0].metadata.name}",
    ]
    try:
        proc = run_cmd(cmd, timeout=4.0)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def extract_budget_percent(clusters_json: str) -> float | None:
    """Pluck the first cluster's retry_budget.budget_percent (Envoy schema).

    Envoy reports `retry_budget` per cluster under `cluster_statuses`. The
    field name varies slightly across versions; try a couple of shapes."""
    try:
        doc = json.loads(clusters_json)
    except (ValueError, TypeError):
        return None
    statuses = doc.get("cluster_statuses") or []
    for status in statuses:
        cfg = status.get("circuit_breakers") or status.get("retry_budget")
        if isinstance(cfg, dict):
            pct = cfg.get("budget_percent") or cfg.get("percent")
            if isinstance(pct, (int, float)):
                return float(pct)
    return None


def probe_xds_apply(
    namespace: str,
    target_percent: float,
    timeout_s: float = 5.0,
    poll_ms: int = 100,
) -> float | None:
    """Block (in a worker thread) until the proxy reports `target_percent`.

    Returns the elapsed milliseconds, or None on timeout / lookup failure.
    Caller is expected to invoke this from a daemon thread; there is no
    cancellation channel by design — the timeout is the bound."""
    pod = find_proxy_pod(namespace)
    if pod is None:
        return None
    deadline = time.perf_counter() + timeout_s
    started = time.perf_counter()
    poll_s = max(poll_ms, 1) / 1000.0
    while time.perf_counter() < deadline:
        raw = kubectl_exec_clusters(namespace, pod)
        if raw is not None:
            current = extract_budget_percent(raw)
            if current is not None and abs(current - target_percent) < 0.001:
                return (time.perf_counter() - started) * 1000.0
        time.sleep(poll_s)
    return None


def write_decision_header(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "mode",
            "apply",
            "decision",
            "pct_idx",
            "mrc_idx",
            "current_percent",
            "current_minRetryConcurrency",
            "selected_percent",
            "selected_minRetryConcurrency",
            "patched",
            "reason",
        ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--namespace", default="online-boutique")
    # Laptop-side SSH path: client-host + ssh-user are required.
    # In-cluster Job: pass --loader-url-template + --loader-ports and skip
    # the SSH args. We can't mark either set as required at parse time
    # because both paths share this script; we validate after parsing.
    parser.add_argument("--client-host", default="")
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--ssh-opts", default="")
    parser.add_argument("--remote-metrics-dir", default="/tmp/online-boutique-clients/metrics")
    parser.add_argument(
        "--loader-url-template", default="",
        help=(
            "In-cluster obs path. e.g. 'http://10.10.1.6:{port}/window'. "
            "Combined with --loader-ports, the controller fans out across "
            "shards per tick and assembles a single observation window."
        ),
    )
    parser.add_argument(
        "--loader-ports", default="",
        help=(
            "Comma-separated shard ports for --loader-url-template (e.g. "
            "8765,8766,8767,8768). Required when the URL template is set."
        ),
    )
    parser.add_argument(
        "--legacy-ssh-obs", action="store_true",
        help=(
            "Force the SSH-cat observation path even when --loader-url-template "
            "is provided. Used for side-by-side decision-drift checks against "
            "the in-cluster path."
        ),
    )
    parser.add_argument(
        "--obs-mode",
        choices=("auto", "rows", "buckets"),
        default="auto",
        help=(
            "How the controller fetches observations from the loader. "
            "`rows` (Phase 2 default): GET /window, per-attempt rows. "
            "`buckets` (Phase 3): GET /buckets, pre-aggregated counters. "
            "`auto`: try /buckets first, fall back to /window if every "
            "shard returns 404 / connection refused (loader < Phase 3). "
            "Only applies when --loader-url-template is set."
        ),
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Observe and log decisions but do not patch the DestinationRule.",
    )
    parser.add_argument(
        "--xds-probe",
        action="store_true",
        help=(
            "After each successful patch, spawn a background thread that "
            "polls one istio-proxy's /clusters every 100 ms and records the "
            "wall-clock time at which the new budget_percent becomes visible. "
            "Off by default — the polling itself perturbs kubectl-exec cost."
        ),
    )
    # Legacy controller fallbacks. Both are off by default so production
    # sweeps get the persistent kube client and the awk pre-filter; flip
    # them on only when reproducing the pre-optimisation baseline row.
    parser.add_argument(
        "--legacy-patch-kubectl", action="store_true",
        help=(
            "Bypass the persistent kubernetes Python client and shell out "
            "to `kubectl patch` per tick (legacy measurement baseline)."
        ),
    )
    parser.add_argument(
        "--legacy-full-cat", action="store_true",
        help=(
            "Skip the server-side awk pre-filter in ssh_cat_metrics and "
            "transfer every CSV byte each tick (legacy measurement baseline)."
        ),
    )
    args = parser.parse_args()

    # Resolve the observation transport. The HTTP path is the in-cluster
    # default; the SSH path is the laptop-side fallback. We pick exactly
    # one so the per-tick code is uniform.
    use_http_obs = bool(args.loader_url_template) and not args.legacy_ssh_obs
    loader_ports: list[int] = []
    if use_http_obs:
        if not args.loader_ports:
            print(
                "[rl_controller] --loader-url-template requires --loader-ports",
                file=sys.stderr,
            )
            return 2
        try:
            loader_ports = [int(p) for p in args.loader_ports.split(",") if p.strip()]
        except ValueError:
            print(
                f"[rl_controller] bad --loader-ports={args.loader_ports!r}",
                file=sys.stderr,
            )
            return 2
    else:
        # SSH path (laptop-side, or in-cluster with --legacy-ssh-obs).
        if not args.client_host or not args.ssh_user:
            print(
                "[rl_controller] SSH-obs path requires --client-host and --ssh-user "
                "(or pass --loader-url-template + --loader-ports for HTTP)",
                file=sys.stderr,
            )
            return 2

    # Derive the buckets URL template from the configured /window template.
    # The loader serves both endpoints from the same aiohttp app, so the
    # only difference is the path suffix; the controller stays oblivious
    # to whether the loader's port assignment is hard-coded or dynamic.
    obs_mode = args.obs_mode if use_http_obs else "rows"
    buckets_url_template = ""
    if use_http_obs:
        if args.loader_url_template.endswith("/window"):
            buckets_url_template = (
                args.loader_url_template[: -len("/window")] + "/buckets"
            )
        elif args.loader_url_template.endswith("/window/"):
            buckets_url_template = (
                args.loader_url_template[: -len("/window/")] + "/buckets"
            )
        else:
            # If the template doesn't end with /window we leave a /buckets
            # sibling derived at the path level. This keeps the contract
            # explicit: --loader-url-template names the rows endpoint, and
            # the buckets endpoint is always its sibling.
            buckets_url_template = args.loader_url_template + "/buckets"

    cfg = load_config(Path(args.config))
    apply_enabled = not args.shadow

    # Load the PPO model when the config points to one.
    model = None
    effective_mode = cfg.mode
    if cfg.model_path is not None:
        model = load_rl_model(cfg.model_path)
        if model is None:
            print(
                "[rl_controller] model unavailable – falling back to shadow_stub",
                file=sys.stderr,
            )
            effective_mode = "shadow_stub"
            apply_enabled = False
    else:
        if cfg.mode == "rl_ppo":
            print(
                "[rl_controller] mode=rl_ppo but no model_path in config – "
                "falling back to shadow_stub",
                file=sys.stderr,
            )
        effective_mode = "shadow_stub"
        apply_enabled = False

    if use_http_obs:
        obs_path_label = f"http({len(loader_ports)} shards, mode={obs_mode})"
    else:
        obs_path_label = "ssh_cat"
    print(
        f"[rl_controller] mode={effective_mode}  apply={apply_enabled}  "
        f"model={'loaded' if model else 'none'}  "
        f"obs_window={cfg.observation_window_sec}s  "
        f"startup_hold_off={cfg.startup_hold_off_sec}s  "
        f"obs_dim={len(OBSERVATION_FIELDS)}  "
        f"obs_transport={obs_path_label}",
        file=sys.stderr,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    observations_path = out_dir / "rl-observations.jsonl"
    decisions_path = out_dir / "rl-decisions.csv"
    timings_path = out_dir / "rl-timings.jsonl"
    write_decision_header(decisions_path)

    current = cfg.default_budget
    previous_budget = cfg.default_budget
    previous_metrics: dict[str, Any] | None = None
    start_ts = time.time()
    tick_index = 0
    # Buckets/rows runtime state. `effective_obs_mode` tracks what we are
    # actually using (`obs_mode` is the operator's intent — `auto` resolves
    # to `buckets` on first success and degrades to `rows` only if every
    # shard returns 0 ok_shards on its first call). `consecutive_empty`
    # counts ticks where every shard answered but returned zero buckets so
    # the controller can fail loud rather than silently producing
    # all-zero observations (plan-optimization-phase3 §8 risks row 4).
    effective_obs_mode = "buckets" if obs_mode in {"auto", "buckets"} else "rows"
    consecutive_empty_buckets = 0
    # Pending xDS-probe results land here keyed by the tick that triggered
    # the patch; they are flushed onto the *next* tick's timing record so
    # the apply-time is colocated with the patch event in the timeline.
    pending_xds_apply: dict[str, Any] = {"ms": None}
    pending_lock = threading.Lock()

    def _xds_probe_async(target_percent: float) -> None:
        ms = probe_xds_apply(args.namespace, target_percent)
        with pending_lock:
            pending_xds_apply["ms"] = ms

    with observations_path.open("a") as obs_f, \
            decisions_path.open("a", newline="") as dec_f, \
            timings_path.open("a") as tim_f:
        writer = csv.writer(dec_f)
        while not STOP:
            tick_index += 1
            timing = TickTiming(tick_index=tick_index, t_loop_start=time.time())
            tick_t0 = time.perf_counter()
            now = timing.t_loop_start

            # Drain any xDS-probe result from the previous patch onto this
            # tick's record. The probe runs in a background thread so the
            # main loop never blocks on it.
            with pending_lock:
                timing.xds_apply_ms = pending_xds_apply["ms"]
                pending_xds_apply["ms"] = None

            since_ts = now - cfg.observation_window_sec

            # ---- Obs fetch + parse + build ----
            # Buckets path (Phase 3): one /buckets call per shard, no
            # row-level work, single linear pass over ~40 buckets in
            # `compose_observation_from_buckets`.
            # Rows path (Phase 2): /window or SSH-cat → parse_client_rows
            # → build_metrics → build_observation, unchanged.
            if use_http_obs and effective_obs_mode == "buckets":
                t = time.perf_counter()
                buckets, ok_shards = http_fetch_buckets(
                    buckets_url_template, loader_ports, since_ts,
                )
                timing.obs_fetch_ms = (time.perf_counter() - t) * 1000.0

                # /buckets returns 404 on pre-Phase-3 loaders. In `auto`
                # mode the controller silently degrades to /window on the
                # first tick where every shard refuses /buckets; in
                # `buckets` mode (operator pinned it) we keep trying and
                # the warning below surfaces the regression.
                if ok_shards == 0 and obs_mode == "auto":
                    print(
                        "[rl_controller] /buckets unavailable on every shard; "
                        "falling back to /window for this controller lifetime",
                        file=sys.stderr,
                    )
                    effective_obs_mode = "rows"
                else:
                    t = time.perf_counter()
                    # No row-level parse for buckets — fetch already
                    # decoded JSON; the `obs_parse_ms` slot stays 0.
                    timing.obs_parse_ms = (time.perf_counter() - t) * 1000.0
                    timing.rows = sum(b.attempts for b in buckets)

                    t = time.perf_counter()
                    observation, sources, metrics = (
                        compose_observation_from_buckets(
                            buckets,
                            cfg.observation_window_sec,
                            current,
                            previous_budget,
                            previous_metrics,
                        )
                    )
                    timing.obs_build_ms = (time.perf_counter() - t) * 1000.0

                    # Buckets-path fail-loud guard. If the controller
                    # silently produced zero-attempt observations for many
                    # consecutive ticks (which Phase 2's smoke incident
                    # showed is bug-prone), surface it.
                    if not buckets:
                        consecutive_empty_buckets += 1
                    else:
                        consecutive_empty_buckets = 0
                    if consecutive_empty_buckets >= 5:
                        print(
                            "[rl_controller] obs_mode=buckets returned 0 "
                            "buckets for 5 ticks in a row; loader endpoint "
                            "is probably broken",
                            file=sys.stderr,
                        )

            # Rows path — either the operator pinned --obs-mode=rows, or
            # `auto` fell back to it because no shard served /buckets.
            if not use_http_obs or effective_obs_mode == "rows":
                t = time.perf_counter()
                if use_http_obs:
                    raw = http_fetch_window(
                        args.loader_url_template, loader_ports, since_ts,
                    )
                else:
                    raw = ssh_cat_metrics(
                        args.client_host, args.ssh_user, args.ssh_opts,
                        args.remote_metrics_dir,
                        # Legacy baseline: --legacy-full-cat disables the awk
                        # pre-filter so every byte is shipped each tick.
                        since_ts=None if args.legacy_full_cat else since_ts,
                    )
                timing.obs_fetch_ms = (time.perf_counter() - t) * 1000.0

                t = time.perf_counter()
                rows = parse_client_rows(raw, since_ts=since_ts)
                timing.obs_parse_ms = (time.perf_counter() - t) * 1000.0
                timing.rows = len(rows)

                t = time.perf_counter()
                metrics = build_metrics(rows, cfg.observation_window_sec)
                observation, sources = build_observation(
                    metrics, current, previous_budget, previous_metrics,
                )
                timing.obs_build_ms = (time.perf_counter() - t) * 1000.0

            # Policy: real RL inference or stub depending on what is available.
            pct_idx: int | None = None
            mrc_idx: int | None = None
            t = time.perf_counter()
            if model is not None:
                selected, (pct_idx, mrc_idx) = rl_policy(observation, model, cfg)
                decision_label = "rl_ppo"
            else:
                selected = stub_policy(current, cfg)
                decision_label = "shadow_stub"
            timing.inference_ms = (time.perf_counter() - t) * 1000.0

            unchanged = (
                float(selected.percent) == float(current.percent)
                and int(selected.min_retry_concurrency) == int(current.min_retry_concurrency)
            )
            in_hold_off = (now - start_ts) < cfg.startup_hold_off_sec
            patch_allowed = apply_enabled and not unchanged and not in_hold_off
            patched = False
            if not apply_enabled:
                reason = "shadow_mode"
            elif in_hold_off:
                reason = "startup_hold_off"
            elif unchanged:
                reason = "unchanged"
            else:
                reason = "pending_patch"

            if patch_allowed:
                t = time.perf_counter()
                patched = patch_retry_budget(
                    args.namespace, selected,
                    force_kubectl=args.legacy_patch_kubectl,
                )
                timing.patch_ms = (time.perf_counter() - t) * 1000.0
                timing.patch_attempted = True
                timing.patch_succeeded = patched
                reason = "patched" if patched else "patch_failed"
                if patched:
                    previous_budget = current
                    current = selected
                    if args.xds_probe:
                        threading.Thread(
                            target=_xds_probe_async,
                            args=(selected.percent,),
                            daemon=True,
                        ).start()

            timing.total_ms = (time.perf_counter() - tick_t0) * 1000.0

            obs_doc = {
                "timestamp": now,
                "tick_index": tick_index,
                "mode": effective_mode,
                "observation_fields": OBSERVATION_FIELDS,
                "observation": observation,
                "metrics": metrics,
                "sources": {**metrics.get("quality", {}), **sources},
                "current_budget": {
                    "percent": current.percent,
                    "minRetryConcurrency": current.min_retry_concurrency,
                },
                "previous_budget": {
                    "percent": previous_budget.percent,
                    "minRetryConcurrency": previous_budget.min_retry_concurrency,
                },
                "selected_budget": {
                    "percent": selected.percent,
                    "minRetryConcurrency": selected.min_retry_concurrency,
                },
                "pct_idx": pct_idx,
                "mrc_idx": mrc_idx,
            }
            obs_f.write(json.dumps(obs_doc, sort_keys=True) + "\n")
            obs_f.flush()
            writer.writerow([
                f"{now:.6f}",
                effective_mode,
                int(apply_enabled),
                decision_label,
                pct_idx if pct_idx is not None else "",
                mrc_idx if mrc_idx is not None else "",
                current.percent,
                current.min_retry_concurrency,
                selected.percent,
                selected.min_retry_concurrency,
                int(patched),
                reason,
            ])
            dec_f.flush()
            tim_f.write(json.dumps(asdict(timing), sort_keys=True) + "\n")
            tim_f.flush()
            previous_metrics = metrics

            deadline = time.time() + cfg.decision_interval_sec
            while not STOP and time.time() < deadline:
                time.sleep(min(0.25, deadline - time.time()))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
