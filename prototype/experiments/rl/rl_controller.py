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
# works because both files sit in the same rl/ subdirectory
# (and inside the container both files land at /app/).
from rl_obs_schema import (  # noqa: E402
    LATENCY_HISTOGRAM_EDGES_S,
    LATENCY_HISTOGRAM_NUM_BUCKETS,
)
from rl_obs_envoy import (  # noqa: E402
    ENVOY_GAUGE_KEYS,
    aggregate_istio_snapshots,
    aggregate_pod_snapshots,
    compose_metrics_from_envoy,
    deadline_rate_from_envoy,
    delta_counters,
    delta_istio_snapshots,
    median_latency_from_envoy,
    p95_from_envoy,
    parse_envoy_stats_json,
    parse_istio_requests_total,
    per_profile_delta,
)


# Feature order must match the training environment exactly. Slot 7
# (formerly named ``queue_utilization``) is the caller-side
# budget_reject_rate per obs.md: fraction of retries the Envoy retry
# budget rejected in the window. Rename is purely cosmetic — the slot
# remains the 8th element of the vector and the model can't tell the
# difference. Downstream consumers (bench_decision_diff.py) must stay
# in sync with this list.
OBSERVATION_FIELDS = [
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
    vecnormalize_stats_path: Path | None
    decision_interval_sec: float
    observation_window_sec: float
    # Length of the *delta* window used to compute features #11
    # (delta_success_agg) and #12 (delta_window_load_amplification).
    # In the simulator this is `--delta-window-s`; defaults to the
    # observation window for back-compat with v1..v4 models that were
    # trained with delta_window == obs_window.
    delta_window_sec: float
    # Per-attempt request deadline in milliseconds. Used as the divisor
    # for #7 p95_latency_pressure and (rows path only) as the deadline
    # threshold for #10. Must match the training environment's
    # `attempt_ms` — see Phase 0 coordination note in obs.md.
    attempt_timeout_ms: float
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
    vecnormalize_stats_path: Path | None = None
    if raw_model:
        candidate = Path(raw_model)
        config_dir = path.parent
        # Resolve relative to the config directory and the in-cluster
        # ConfigMap layout (`/etc/rl/{model.zip,config.yaml}`).
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
        else:
            # Look for VecNormalize stats next to the resolved model.zip
            # (sibling file). Models trained without VecNormalize won't
            # have one — that's fine, controller falls back to raw
            # observations with a warning at load time.
            #
            # An explicit ``vecnormalize_stats_path`` YAML field wins;
            # otherwise we accept either the canonical name
            # (``vecnormalize_stats.pkl``) or the labmate's
            # per-model convention (``<model-stem>-normalize.pkl``,
            # ``vecnormalize.pkl``). The in-cluster ConfigMap layout
            # also exposes the file under the canonical name.
            raw_stats = doc.get("vecnormalize_stats_path")
            stats_candidates: list[Path] = []
            if raw_stats:
                explicit = Path(raw_stats)
                if explicit.is_absolute():
                    stats_candidates.append(explicit)
                else:
                    stats_candidates.append((script_dir / explicit).resolve())
                    stats_candidates.append((config_dir / explicit).resolve())
            for stem_form in (
                "vecnormalize_stats.pkl",
                "vecnormalize.pkl",
                f"{model_path.stem}-normalize.pkl",
            ):
                stats_candidates.append(model_path.parent / stem_form)
                stats_candidates.append(config_dir / stem_form)
            for stats in stats_candidates:
                if stats.exists():
                    vecnormalize_stats_path = stats
                    break

    obs_window = float(ctl.get("observation_window_sec", 10))
    return ControllerConfig(
        mode=str(doc.get("mode", ctl.get("mode", "shadow_stub"))),
        model_path=model_path,
        vecnormalize_stats_path=vecnormalize_stats_path,
        decision_interval_sec=float(ctl.get("decision_interval_sec", 5)),
        observation_window_sec=obs_window,
        # Default = obs_window for back-compat with v1..v4 models trained
        # without a separate delta window. The simulator's training script
        # uses `--delta-window-s` for this knob; the YAML field mirrors it.
        delta_window_sec=float(ctl.get("delta_window_sec", obs_window)),
        # Default 3000 ms matches production loader profiles
        # (timeout_s: 3.0). Override in the YAML to match training when a
        # retrained model uses a different attempt deadline.
        attempt_timeout_ms=float(ctl.get("attempt_timeout_ms", 3000.0)),
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


def load_vecnormalize(stats_path: Path, model: Any) -> Any | None:
    """Restore the running mean/var that VecNormalize tracked at training.

    Returns ``None`` on failure or when SB3 / gymnasium is unavailable.
    The training script wraps a single env in DummyVecEnv → VecNormalize;
    inference needs the same wrapper shape, so we reconstruct a tiny
    DummyVecEnv whose obs/action spaces match the model and then
    ``VecNormalize.load`` overlays the saved statistics. The dummy env's
    ``step`` and ``reset`` are never called — only ``normalize_obs`` is.
    """
    try:
        import gymnasium as gym  # noqa: PLC0415
        from stable_baselines3.common.vec_env import (  # noqa: PLC0415
            DummyVecEnv,
            VecNormalize,
        )
    except ImportError:
        print(
            "[rl_controller] gymnasium / stable_baselines3 not installed; "
            "cannot apply VecNormalize at inference",
            file=sys.stderr,
        )
        return None

    class _SpacesShim(gym.Env):
        """Minimal env exposing the spaces VecNormalize needs to bind."""

        observation_space = model.observation_space
        action_space = model.action_space

        def reset(self, *, seed=None, options=None):  # noqa: D401
            del seed, options
            return self.observation_space.sample(), {}

        def step(self, action):  # noqa: D401
            del action
            return (
                self.observation_space.sample(),
                0.0,
                True,
                False,
                {},
            )

    try:
        venv = DummyVecEnv([lambda: _SpacesShim()])
        vec_norm = VecNormalize.load(str(stats_path), venv)
        vec_norm.training = False
        vec_norm.norm_reward = False
        print(
            f"[rl_controller] loaded VecNormalize stats from {stats_path}",
            file=sys.stderr,
        )
        return vec_norm
    except Exception as exc:  # noqa: BLE001
        print(
            f"[rl_controller] VecNormalize load failed: {exc}",
            file=sys.stderr,
        )
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


# ---------------------------------------------------------------------------
# Observation fetch — HTTP path (loader /window and /buckets endpoints)
#
# The HTTP path fans out across `loader_ports` (one per loader shard), reuses
# a single requests.Session so HTTP keep-alive + connection pooling apply,
# and feeds the same `parse_client_rows` consumer for the rows transport.
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
    a stale read (degenerate metrics → controller's startup hold-off
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
# Buckets path — pre-aggregated loader-side observation transport
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
    delta_metrics: dict[str, Any] | None,
    attempt_timeout_ms: float = 3000.0,
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
    # p95 latency pressure: divide by configured attempt deadline (ms),
    # no clamp. See `build_metrics` for rationale.
    latency_pressure = (p95_lat * 1000.0) / max(attempt_timeout_ms, 1e-9)

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

    obs, sources = build_observation(metrics, current, previous, delta_metrics)
    sources = dict(sources)
    sources["obs_transport"] = "http_buckets"
    return obs, sources, metrics


# ---------------------------------------------------------------------------
# Envoy /stats path — production-shape observation transport
#
# Each tick the controller:
#
#   1. Reads the cached caller-pod IP list (refreshed in a background
#      thread every 30 s — `_PodCache` below).
#   2. Fetches `http://<podIP>:15020/stats/prometheus?filter=upstream_rq.*<callee>&usedonly`
#      from every pod in parallel via a ThreadPoolExecutor. The loader
#      buckets path collapses N caller pods into one fan-in at the
#      loader; here we have to talk to each pod, so parallel fan-out is
#      load-bearing. Port 15020 is pilot-agent's pod-IP-reachable
#      Prometheus merge endpoint (admin port 15000 is bound to localhost
#      by Istio and not externally reachable from another pod).
#   3. Parses each pod's Prom output via `rl_obs_envoy.parse_envoy_stats_prom`,
#      element-wise sums them (`aggregate_pod_snapshots`), then takes the
#      per-window delta against the previous-tick snapshot.
#   4. Composes the same `metrics` dict the rows / buckets paths produce
#      (`compose_metrics_from_envoy`) and runs it through the shared
#      `build_observation` so the 18-feature vector is emitted identically.
#
# Sidecar CPU is reserved cluster-wide via
# `manifests/istio/sidecar-cpu-reservation.yaml` (mesh default 100 m, up
# from Istio's 10 m). That's what gives us the p95 ≤ 200 ms tail-latency
# floor; without the bump the sidecar can be cgroup-throttled when its
# pod's application container saturates the pod's CPU budget, which
# pushes /stats latency into the seconds.
# ---------------------------------------------------------------------------


_HTTP_EXECUTOR: Any = None
_HTTP_EXECUTOR_SIZE = 4


def _ensure_bearer_token_alias() -> None:
    """Workaround for kubernetes-client>=36 where ``load_incluster_config``
    stores the service-account bearer token under ``api_key['authorization']``
    while the generated ``CoreV1Api.list_namespaced_pod_with_http_info`` (and
    every other op) declares ``auth_settings=['BearerToken']``. Because
    ``Configuration.auth_settings()`` only registers ``BearerToken`` when the
    key is present in ``api_key`` under that exact name, every API call ends
    up unauthenticated (apiserver sees ``system:anonymous``) and returns 403.
    Copying the token into the ``BearerToken`` slot makes ``auth_settings()``
    register it and the generated code picks it up.

    Idempotent and version-tolerant: a no-op if the alias is already in place
    or if the python kubernetes client isn't installed. Always safe to call
    immediately after ``load_incluster_config()`` or ``load_kube_config()``.
    """
    try:
        from kubernetes import client  # noqa: PLC0415
    except ImportError:
        return
    cfg = client.Configuration.get_default_copy()
    if not cfg.api_key:
        return
    bearer = cfg.api_key.get("authorization") or cfg.api_key.get("Authorization")
    if bearer and not cfg.api_key.get("BearerToken"):
        cfg.api_key["BearerToken"] = bearer
        client.Configuration.set_default(cfg)


def _get_http_executor() -> Any:
    """Cached ThreadPoolExecutor for the parallel /stats fan-out.

    4 workers covers cart's two callers (frontend, checkoutservice)
    in parallel and leaves slack for any future sweep that asks the
    controller to poll a third or fourth caller. Each fetch is an HTTP
    GET against an in-cluster pod IP, so threads block on socket I/O
    rather than the GIL.
    """
    global _HTTP_EXECUTOR
    if _HTTP_EXECUTOR is not None:
        return _HTTP_EXECUTOR
    try:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    except ImportError:
        return None
    _HTTP_EXECUTOR = ThreadPoolExecutor(max_workers=_HTTP_EXECUTOR_SIZE)
    return _HTTP_EXECUTOR


class _PodCache:
    """Background-refreshed cache of caller-pod IPs.

    The kubernetes List API is the slowest call in the controller's
    toolbox (typically 50–150 ms; serial dependency on apiserver
    pagination + RBAC checks). At the 2 s decision cadence, doing a List
    every tick burns a third of the latency budget on something that
    changes only when a pod is rescheduled. We refresh in a daemon
    thread every 30 s and invalidate on demand so a fresh pod IP is
    picked up within one tick of any pod restart.
    """

    def __init__(
        self,
        namespace: str,
        labels: list[str],
        refresh_sec: float = 30.0,
    ) -> None:
        self._namespace = namespace
        self._labels = labels
        self._refresh_sec = float(refresh_sec)
        self._lock = threading.Lock()
        self._ips: list[str] = []
        self._last_error: str | None = None
        self._last_refresh = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Synchronous first refresh so the very first tick has something
        # to fetch. If discovery fails the controller logs and continues
        # to the next tick (which auto-retries).
        self._refresh_once()
        t = threading.Thread(
            target=self._loop, name="pod-cache", daemon=True,
        )
        t.start()
        self._thread = t

    def stop(self) -> None:
        self._stop.set()

    def get(self) -> list[str]:
        with self._lock:
            return list(self._ips)

    def invalidate(self) -> None:
        """Force a refresh on the next loop iteration.

        Called by the fetch path when a connect error suggests the pod
        IP set is stale. We don't refresh inline because a List API
        call would block the tick; instead we shorten the daemon's next
        wake-up.
        """
        with self._lock:
            self._last_refresh = 0.0

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                age = time.time() - self._last_refresh
            if age >= self._refresh_sec:
                self._refresh_once()
            self._stop.wait(min(2.0, self._refresh_sec))

    def _refresh_once(self) -> None:
        try:
            from kubernetes import client, config  # noqa: PLC0415
        except ImportError:
            with self._lock:
                self._last_error = "kubernetes-client-not-installed"
                self._last_refresh = time.time()
            return
        try:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            _ensure_bearer_token_alias()
            v1 = client.CoreV1Api()
            label_selector = f"app in ({','.join(self._labels)})"
            resp = v1.list_namespaced_pod(
                self._namespace,
                label_selector=label_selector,
                field_selector="status.phase=Running",
                _request_timeout=5,
            )
            ips: list[str] = []
            for pod in resp.items:
                ip = getattr(pod.status, "pod_ip", None)
                if ip:
                    ips.append(str(ip))
            with self._lock:
                self._ips = ips
                self._last_error = None
                self._last_refresh = time.time()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"{exc.__class__.__name__}: {exc}"
                self._last_refresh = time.time()


def http_fetch_envoy_stats(
    pod_ips: list[str],
    callee_match: str,
    *,
    timeout: float = 2.0,
    admin_port: int = 15020,
) -> tuple[dict[str, Any], int]:
    """Fan-out fetch + element-wise aggregate of
    ``/stats/prometheus?filter=upstream_rq.*<callee>&usedonly`` across
    every caller-sidecar pod.

    Returns ``(aggregated, n_ok)`` where ``aggregated`` is the dict from
    :func:`rl_obs_envoy.aggregate_pod_snapshots` (counter sums + summed
    histogram buckets across all responding pods) and ``n_ok`` is the
    number of pods that returned a parseable response.

    A pod that times out or returns a non-200 / unparseable body is
    dropped from the aggregate; the controller relies on having ≥ 1
    responding pod (typically 2 for cart's frontend + checkoutservice)
    to compose a meaningful observation. If every pod fails the aggregate
    is all-zero and the obs-source-fail-loud logic in the main loop
    surfaces the regression after 5 consecutive empty windows.

    Why port 15020 (pilot-agent merge endpoint) and not 15000 (Envoy
    admin): Istio binds the Envoy admin listener to 127.0.0.1 only and
    the bind address is hard-coded in the agent's bootstrap template
    (not overridable via standard mesh config). A pod-to-pod request to
    ``<podIP>:15000`` hits Envoy's inbound capture and returns 503.
    pilot-agent runs its own listener on 15020 that proxies
    ``/stats/prometheus`` through to Envoy admin internally and is
    bound 0.0.0.0 by design (for the readiness probe). The endpoint
    accepts Envoy's ``?filter=<regex>&usedonly`` query so we can shrink
    the response to ≤ 15 KB without changing any sidecar config.
    """
    session = _get_http_session()
    executor = _get_http_executor()
    if session is None or executor is None or not pod_ips:
        return ({}, 0)

    def fetch_one(ip: str) -> dict[str, Any] | None:
        # Envoy admin filter — matches the raw stat name shape
        # ``cluster.outbound|<port>||<svc>.<ns>.svc.cluster.local;.upstream_rq*``.
        # Prefixing with ``cluster.outbound`` keeps the response tiny
        # (~20 KB) and rules out unrelated `internal_upstream_rq` /
        # `external_upstream_rq` from other listeners.
        url = (
            f"http://{ip}:{admin_port}/stats/prometheus"
            f"?filter=cluster.outbound.*{callee_match}.*upstream_rq"
            f"&usedonly"
        )
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                return None
            return parse_envoy_stats_json(resp.text, callee=callee_match)
        except Exception:
            return None

    snapshots: list[dict[str, Any]] = []
    for snap in executor.map(fetch_one, pod_ips):
        if snap is not None:
            snapshots.append(snap)

    if not snapshots:
        return ({}, 0)
    return (aggregate_pod_snapshots(snapshots), len(snapshots))


def http_fetch_istio_requests(
    pod_ips: list[str],
    workload: str | None = None,
    callee: str | None = None,
    *,
    timeout: float = 2.0,
    admin_port: int = 15020,
) -> tuple[dict[str, Any], int]:
    """Fan-out fetch of ``istio_requests_total`` from each caller pod's
    sidecar, aggregated into the two-key snapshot
    (``per_profile`` + ``callee_aggregate``) consumed by
    :func:`compose_metrics_from_envoy`.

    Companion to :func:`http_fetch_envoy_stats`: the raw Envoy
    ``cluster.outbound.*upstream_rq_*`` family does NOT carry the
    ``rl_profile`` label (the Istio Telemetry CR only labels the
    ``istio_requests_total`` family) and classifies success/failure
    by HTTP response code only (which miscounts gRPC application
    errors wrapped in HTTP 200). This second fetch unlocks two sets
    of features:

      - Per-profile success accounting for slot #1
        (``min_client_success``) and slot #5
        (``retry_fairness_gap``). PATH C limitation documented in
        :func:`parse_istio_requests_total`'s docstring — boils down
        to "for single-profile workloads this is identical to the
        aggregate, which is what we want".

      - gRPC-aware callee aggregate (``{total, success,
        server_failures}``) that overrides slots #0 and #8 in
        :func:`compose_metrics_from_envoy`. Cart's gRPC failures
        (status 14 wrapped in HTTP 200) are correctly counted as
        failures here whereas Envoy's HTTP-class counters miss them.

    ``workload`` (optional) restricts per-profile rows to a specific
    ``destination_workload`` (typically ``"frontend"`` since that's
    where the gateway tags propagate). ``callee`` (required for the
    aggregate view) is the ``destination_workload`` label value to
    aggregate cart-side gRPC stats on, e.g. ``"cartservice"``.

    Returns ``(aggregated, n_ok)`` where ``aggregated`` is the
    two-key snapshot from :func:`aggregate_istio_snapshots`. An
    empty / no-callee result is the safe signal back to
    :func:`compose_observation_from_envoy` to fall back to
    aggregate-derived stubs.
    """
    session = _get_http_session()
    executor = _get_http_executor()
    if session is None or executor is None or not pod_ips:
        return ({"per_profile": {}}, 0)

    def fetch_one(ip: str) -> dict[str, Any] | None:
        # ``istio_requests_total`` is emitted by the Wasm stats filter
        # bundled with Istio's bootstrap. The filter regex matches the
        # bare metric name (Prometheus regex over stat name without
        # labels), so we don't need to repeat the cluster filter here.
        # ``usedonly`` drops zero-valued series — only ones with at
        # least one request in the lifetime of the sidecar — which
        # keeps the response under 30 KB even on a busy cluster.
        url = (
            f"http://{ip}:{admin_port}/stats/prometheus"
            f"?filter=istio_requests_total"
            f"&usedonly"
        )
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                return None
            return parse_istio_requests_total(
                resp.text, workload=workload, callee=callee,
            )
        except Exception:
            return None

    snapshots: list[dict[str, Any]] = []
    for snap in executor.map(fetch_one, pod_ips):
        if snap is not None:
            snapshots.append(snap)

    if not snapshots:
        return ({"per_profile": {}}, 0)
    return (aggregate_istio_snapshots(snapshots), len(snapshots))


def compose_observation_from_envoy(
    snapshot: dict[str, Any],
    prev_snapshot: dict[str, Any] | None,
    window_sec: float,
    current: RetryBudget,
    previous: RetryBudget,
    delta_metrics: dict[str, Any] | None,
    attempt_timeout_ms: float = 3000.0,
    istio_snapshot: dict[str, Any] | None = None,
    prev_istio_snapshot: dict[str, Any] | None = None,
) -> tuple[list[float], dict[str, str], dict[str, Any]]:
    """Build the 18-feature observation vector from an Envoy /stats snapshot.

    Mirrors :func:`compose_observation_from_buckets`'s shape; the only
    difference is where the per-window counter delta + histogram delta
    come from (Envoy cumulative counters vs loader 1-second buckets).

    ``istio_snapshot`` / ``prev_istio_snapshot`` are the current and
    previous two-key snapshots from :func:`http_fetch_istio_requests`
    (``per_profile`` + ``callee_aggregate``), delta'd here so both
    views reflect a single window. The per-profile slice powers
    features #1 (min_client_success) and #5 (retry_fairness_gap); the
    callee_aggregate slice overrides #0 (success_rate_agg) and #8
    (server_fail_rate) with gRPC-aware values. When absent the
    function falls back to HTTP-only Envoy counters for #0/#8 and to
    aggregate-based stubs for #1/#5, so callers without the Telemetry
    CR rolled out still get a valid 18-vector.
    """
    counter_delta = delta_counters(snapshot, prev_snapshot)
    # Envoy 1.27+ exposes a per-flush ``interval`` p95 in the histogram
    # block; ``p95_from_envoy`` reads it directly (no bucket
    # interpolation) and falls back to the cumulative value if Envoy
    # hasn't flushed yet (first tick). See rl_obs_envoy.py for the full
    # rationale.
    p95_seconds = p95_from_envoy(snapshot, prev_snapshot)
    # p50 of the same histogram, used by compose_metrics_from_envoy
    # as the mean-retry-latency proxy in the Little's-law estimate for
    # #12 (budget_utilization). Falls through to 0 on cold start, in
    # which case the gauge-based path (also currently 0) is used and
    # the slot reports 0 with a clear quality label.
    median_latency_seconds = median_latency_from_envoy(snapshot, prev_snapshot)
    # Per-window deadline rate via the histogram CDF at the configured
    # attempt timeout. Replaces Envoy's ``upstream_rq_timeout`` counter
    # which is dead in this stack (no per-try-timeout on the route).
    deadline_rate = deadline_rate_from_envoy(
        snapshot,
        prev_snapshot,
        attempt_timeout_ms / 1000.0,
    )
    # Gauges are scrape-time snapshots; pulled straight through (no
    # delta semantics) so #13/#14 can read live concurrency. Empty dict
    # falls back to the buckets-style approximation in build_observation.
    gauges = {k: int(snapshot.get(k, 0) or 0) for k in ENVOY_GAUGE_KEYS}
    # Per-profile deltas from labelled Envoy cluster.outbound counters.
    # On this stack the Telemetry CR labels istio_requests_total only,
    # not cluster.outbound, so this is typically empty — left in for
    # future stacks that add a per-profile dimension upstream.
    pp_delta = per_profile_delta(snapshot, prev_snapshot)
    # Per-window delta of the istio_requests_total aggregate snapshot.
    # Drives the per-profile success-rate features AND the gRPC-aware
    # success / server-failure overrides for slots #0 and #8.
    istio_delta = delta_istio_snapshots(
        istio_snapshot or {"per_profile": {}}, prev_istio_snapshot,
    )
    pp_summary = istio_delta.get("per_profile") or {}
    callee_aggregate = istio_delta.get("callee_aggregate")
    metrics = compose_metrics_from_envoy(
        counter_delta,
        p95_seconds,
        window_sec,
        attempt_timeout_ms=attempt_timeout_ms,
        gauges=gauges,
        percent=current.percent,
        min_retry_concurrency=current.min_retry_concurrency,
        per_profile_delta=pp_delta if pp_delta else None,
        deadline_rate_override=deadline_rate,
        median_latency_seconds=median_latency_seconds,
        per_profile_summary=pp_summary if pp_summary else None,
        callee_aggregate=callee_aggregate,
    )
    obs, sources = build_observation(
        metrics, current, previous, delta_metrics,
    )
    sources = dict(sources)
    sources["obs_transport"] = "http_envoy_stats"
    quality = metrics.get("quality") or {}
    # Propagate the per-feature source labels so the per-tick
    # rl-observations.jsonl gets ``deadline_rate=envoy_histogram_cdf``
    # vs ``envoy_upstream_rq_timeout`` etc., which is how we audit
    # whether the new derivations are firing in production.
    for key in (
        "success_rate_agg",
        "min_client_success",
        "server_fail_rate",
        "deadline_rate",
        "budget_utilization",
        "retry_fairness_gap",
    ):
        if key in quality and quality[key]:
            sources[key] = quality[key]
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


def build_metrics(
    rows: list[dict[str, Any]],
    window_sec: float,
    attempt_timeout_ms: float = 3000.0,
) -> dict[str, Any]:
    finals = final_attempts(rows)
    total_attempts = len(rows)
    total_requests = max(len(finals), 1)
    retries = sum(1 for row in rows if row["_is_retry"])
    retry_successes = sum(1 for row in rows if row["_is_retry"] and row["_ok"])
    success_requests = sum(1 for row in finals.values() if row["_ok"])
    server_failures = sum(1 for row in rows if row["_status"] >= 500)
    # Deadline detection used to key off status==0 (transport timeout
    # surfaces as no HTTP response), but that misses Envoy-side timeouts
    # that produce a non-zero status code and disagrees with the buckets
    # path. Switching to latency-vs-deadline aligns all three transports
    # and matches the simulator's deadline-miss semantics.
    deadline_threshold_s = attempt_timeout_ms / 1000.0
    deadline_failures = sum(
        1 for row in rows if row["_latency_s"] >= deadline_threshold_s
    )
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
    # p95 latency pressure: divide by the configured attempt deadline.
    # Per obs.md the clamp is dropped — the simulator emits the raw
    # ratio (which can exceed 1.0 when finals include retries that ran
    # almost to deadline) and VecNormalize handles the distribution.
    latency_pressure = (percentile(latencies, 95) * 1000.0) / max(
        attempt_timeout_ms, 1e-9,
    )

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
    delta_metrics: dict[str, Any] | None,
) -> tuple[list[float], dict[str, str]]:
    """Assemble the 18-feature observation vector from per-window metrics.

    ``delta_metrics`` carries the *previous delta-window's* metrics so
    #11 / #12 compute deltas over the configured ``delta_window_sec``
    rather than tick-to-tick over the obs window — mirrors the
    simulator's ``_delta_features`` path.

    ``metrics`` may carry caller-side budget gauges already
    (``budget_reject_rate``, ``budget_utilization``,
    ``retry_pressure_vs_limit``, ``concurrency_limit``) when the Envoy
    transport populated them. Buckets / rows transports leave them
    unset and we fall back to the rate-form approximation that v1..v4
    models were trained with.
    """
    source: dict[str, str] = {}

    delta_success = 0.0
    delta_amp = 0.0
    if delta_metrics:
        delta_success = (
            metrics["success_rate_agg"] - delta_metrics["success_rate_agg"]
        )
        delta_amp = (
            metrics["window_load_amplification"]
            - delta_metrics["window_load_amplification"]
        )

    if "budget_reject_rate" in metrics:
        budget_reject_rate = float(metrics["budget_reject_rate"])
        source["budget_reject_rate"] = "envoy_retry_overflow_delta"
    else:
        budget_reject_rate = 0.0
        source["budget_reject_rate"] = "unavailable_stub_zero"

    if "budget_utilization" in metrics and "retry_pressure_vs_limit" in metrics:
        budget_utilization = float(metrics["budget_utilization"])
        retry_pressure = float(metrics["retry_pressure_vs_limit"])
        source["budget_utilization"] = "envoy_gauge_concurrency"
        source["retry_pressure_vs_limit"] = "envoy_gauge_concurrency"
    else:
        # Rate-form approximation used by v1..v4. Allowed retry RPS is the
        # configured budget applied to the observed request rate.
        allowed_retry_rps = max(
            float(current.min_retry_concurrency),
            metrics["request_rps"] * float(current.percent) / 100.0,
            1e-9,
        )
        retry_pressure = metrics["retry_rps"] / allowed_retry_rps
        budget_utilization = min(retry_pressure, 1.0)
        source["budget_utilization"] = "approximated_from_window_retry_rps"
        source["retry_pressure_vs_limit"] = "approximated_from_window_retry_rps"

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
        "budget_reject_rate": budget_reject_rate,
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
    vec_norm: Any | None = None,
) -> tuple[RetryBudget, tuple[int, int]]:
    """Run deterministic inference with the loaded PPO model.

    ``vec_norm`` is a ``VecNormalize`` wrapper whose running statistics
    were restored from training. When present, the obs vector is
    standardised (zero mean, unit variance) before ``model.predict``
    so inference sees the same distribution the policy was trained on.
    Skipping this step is the historical default — and the source of the
    obs-distribution mismatch obs.md calls out.
    """
    try:
        import numpy as np  # noqa: PLC0415
        obs_arr = np.array(obs, dtype=np.float32).reshape(1, -1)
    except ImportError:
        import array as _a
        obs_arr = _a.array("f", obs)

    if vec_norm is not None:
        try:
            obs_arr = vec_norm.normalize_obs(obs_arr)
        except Exception as exc:  # noqa: BLE001
            # Defensive: a malformed stats file shouldn't crash the
            # controller mid-experiment; log once and continue with the
            # raw obs (degrades to v1..v4 behavior).
            print(
                f"[rl_controller] normalize_obs failed: {exc}; "
                "falling back to raw observations for this tick",
                file=sys.stderr,
            )

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
# tick. We try in-cluster config first; kubeconfig is a dev-only fallback.
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

    _ensure_bearer_token_alias()
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
    parser.add_argument(
        "--loader-url-template", required=True,
        help=(
            "Loader obs endpoint, e.g. 'http://10.10.1.6:{port}/window'. "
            "Combined with --loader-ports, the controller fans out across "
            "shards per tick and assembles a single observation window."
        ),
    )
    parser.add_argument(
        "--loader-ports", required=True,
        help=(
            "Comma-separated shard ports for --loader-url-template (e.g. "
            "8765,8766,8767,8768)."
        ),
    )
    parser.add_argument(
        "--obs-mode",
        choices=("auto", "rows", "buckets", "envoy"),
        default="auto",
        help=(
            "How the controller fetches observations. "
            "`rows`: GET /window on the loader, per-attempt rows. "
            "`buckets`: GET /buckets on the loader, pre-aggregated. "
            "`envoy`: GET /stats on caller-pod sidecars, production-shape "
            "(no loader involvement on the obs path). "
            "`auto`: try envoy → buckets → rows; degrade to the next path "
            "only if the active one returns nothing on five ticks in a row."
        ),
    )
    # Envoy /stats source (only consulted when --obs-mode is `envoy`
    # or when `auto` falls back to envoy).
    parser.add_argument(
        "--envoy-callee", default="cartservice",
        help=(
            "Callee cluster substring to filter on inside Envoy stat "
            "names (e.g. `cartservice` matches "
            "`cluster.outbound|7070||cartservice.<ns>.svc.cluster.local`). "
            "Default: cartservice (Online Boutique's faulting service)."
        ),
    )
    parser.add_argument(
        "--envoy-caller-labels", default="frontend,checkoutservice",
        help=(
            "Comma-separated `app=<label>` values for the caller pods "
            "whose Envoy sidecars expose retry counters for the callee. "
            "Default: frontend,checkoutservice (cart's two callers in "
            "Online Boutique)."
        ),
    )
    parser.add_argument(
        "--envoy-pod-discovery-namespace", default="",
        help=(
            "Namespace to list caller pods in. Defaults to --namespace, "
            "which is correct for the standard single-namespace deploy."
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
            "to `kubectl patch` per tick (measurement baseline only)."
        ),
    )
    args = parser.parse_args()

    try:
        loader_ports = [int(p) for p in args.loader_ports.split(",") if p.strip()]
    except ValueError:
        print(
            f"[rl_controller] bad --loader-ports={args.loader_ports!r}",
            file=sys.stderr,
        )
        return 2
    if not loader_ports:
        print("[rl_controller] --loader-ports must list at least one port", file=sys.stderr)
        return 2

    # Resolve the observation transport. The in-cluster Job always passes
    # loader URL/ports; envoy mode can cascade to buckets/rows over HTTP.
    use_http_obs = True
    obs_mode = args.obs_mode
    # `use_envoy_obs` is the boolean for "we will or might use the envoy
    # /stats source on at least one tick" — controls whether to spin up
    # the pod-discovery cache.
    use_envoy_obs = obs_mode in ("envoy", "auto")
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

    # Bring up the caller-pod IP cache when envoy mode is in the
    # resolution set (either pinned or `auto`).
    pod_cache: _PodCache | None = None
    if use_envoy_obs:
        envoy_ns = (
            args.envoy_pod_discovery_namespace or args.namespace
        )
        labels = [
            s.strip() for s in args.envoy_caller_labels.split(",") if s.strip()
        ]
        if not labels:
            print(
                "[rl_controller] --envoy-caller-labels resolved to empty list; "
                "aborting envoy mode",
                file=sys.stderr,
            )
            return 2
        pod_cache = _PodCache(
            namespace=envoy_ns, labels=labels, refresh_sec=30.0,
        )
        pod_cache.start()
        print(
            f"[rl_controller] envoy obs: namespace={envoy_ns} "
            f"labels={labels} callee={args.envoy_callee} "
            f"initial_pods={len(pod_cache.get())} "
            f"discovery_error={pod_cache.last_error()}",
            file=sys.stderr,
        )

    cfg = load_config(Path(args.config))
    apply_enabled = not args.shadow

    # Load the PPO model when the config points to one.
    model = None
    vec_norm: Any | None = None
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
            # VecNormalize restores the running mean/var the training
            # script tracked across episodes. When the stats file is
            # missing (older v1..v4 runs that didn't ship one), we run
            # raw — same as before this phase — but loud-warn so the
            # operator knows the inference distribution is mismatched.
            if cfg.vecnormalize_stats_path is not None:
                vec_norm = load_vecnormalize(
                    cfg.vecnormalize_stats_path, model,
                )
            else:
                print(
                    "[rl_controller] WARNING: no vecnormalize_stats.pkl "
                    "found next to model.zip; observations passed raw to "
                    "the model (distribution may not match training)",
                    file=sys.stderr,
                )
    else:
        if cfg.mode == "rl_ppo":
            print(
                "[rl_controller] mode=rl_ppo but no model_path in config – "
                "falling back to shadow_stub",
                file=sys.stderr,
            )
        effective_mode = "shadow_stub"
        apply_enabled = False

    if obs_mode == "envoy":
        obs_path_label = (
            f"envoy(initial_pods={len(pod_cache.get()) if pod_cache else 0}, "
            f"mode={obs_mode})"
        )
    else:
        obs_path_label = f"http({len(loader_ports)} shards, mode={obs_mode})"
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
    # Time-indexed history of per-tick metrics. Each tick we pick the
    # snapshot closest to `now - delta_window_sec` ago so #11/#12 reflect
    # change over the configured delta window (matches sim training).
    # Older entries (beyond 2× delta window) are trimmed below.
    metrics_history: list[tuple[float, dict[str, Any]]] = []
    start_ts = time.time()
    tick_index = 0
    # Runtime state for the obs source. `effective_obs_mode` tracks what
    # we are actually using (`obs_mode` is the operator's intent — `auto`
    # cascades envoy → buckets → rows on first failure of the active
    # source). `consecutive_empty_*` counts let the controller fail loud
    # if the active source silently produces zero data, rather than
    # producing all-zero observations.
    if obs_mode == "envoy":
        effective_obs_mode = "envoy"
    elif obs_mode == "auto":
        effective_obs_mode = "envoy" if use_envoy_obs else "buckets"
    elif obs_mode == "buckets":
        effective_obs_mode = "buckets"
    else:
        effective_obs_mode = "rows"
    consecutive_empty_buckets = 0
    consecutive_empty_envoy = 0
    # Envoy per-tick state. Envoy reports cumulative counters; we keep
    # the previous-tick snapshot so `delta_counters` can report per-
    # window deltas. The histogram p95 already comes pre-windowed from
    # Envoy's tdigest ``interval`` field, so it doesn't need a per-tick
    # diff (see rl_obs_envoy.p95_from_envoy). First-tick delta is the
    # cumulative value since process start, which the model tolerates
    # because the startup hold-off prevents that tick from ever
    # causing a patch.
    prev_envoy_snapshot: dict[str, Any] | None = None
    # Companion previous snapshot for the istio_requests_total fan-out
    # that backs the per-profile success-rate features (#1, #5) AND
    # the gRPC-aware aggregate overrides for #0 / #8. Lives on the
    # same per-tick lifecycle: store on a successful Envoy tick,
    # diff on the next.
    prev_istio_snapshot: dict[str, Any] | None = None
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

            # Pick the historical snapshot closest to `delta_window_sec`
            # ago (largest timestamp <= now - delta_window_sec). When the
            # delta window equals the obs window the snapshot is from a
            # window ago, matching the v1..v4 "diff against previous
            # tick" contract whenever decision_interval == obs_window.
            # During warmup the history is too short and we pass None,
            # which build_observation treats as zero delta.
            delta_target_ts = now - cfg.delta_window_sec
            delta_metrics: dict[str, Any] | None = None
            for ts, snap in reversed(metrics_history):
                if ts <= delta_target_ts:
                    delta_metrics = snap
                    break

            # ---- Obs fetch + parse + build ----
            # Envoy path: parallel /stats fan-out across the caller-pod
            # sidecars; per-tick delta against the previous snapshot;
            # single pass through `compose_observation_from_envoy`.
            # Buckets path: one /buckets call per shard, no row-level
            # work, single linear pass over ~40 buckets in
            # `compose_observation_from_buckets`.
            # Rows path: /window or SSH-cat → parse_client_rows
            # → build_metrics → build_observation, unchanged.
            if effective_obs_mode == "envoy":
                t = time.perf_counter()
                pod_ips = pod_cache.get() if pod_cache else []
                snapshot, n_ok = http_fetch_envoy_stats(
                    pod_ips,
                    args.envoy_callee,
                )
                # Per-profile + gRPC-aware aggregate from istio_requests_total
                # (separate filter; same pods, same admin port).
                # ``per_profile`` view (reporter=destination, real
                # rl_profile) drives slots #1 / #5. ``callee_aggregate``
                # view (reporter=source, destination_workload=cart)
                # drives the gRPC-aware overrides for slots #0 / #8 —
                # correctly classifying gRPC application errors that
                # Envoy's HTTP-only counters miss. Failure to fetch
                # (Telemetry CR not applied, no caller pods, etc.)
                # yields an empty snapshot and the controller falls
                # back to the aggregate-derived stubs.
                istio_snapshot, _istio_n_ok = http_fetch_istio_requests(
                    pod_ips,
                    callee=args.envoy_callee,
                )
                timing.obs_fetch_ms = (time.perf_counter() - t) * 1000.0

                # When `auto` and every pod failed (no IPs discovered yet, or
                # all sidecars are unreachable) cascade down. Pinned `envoy`
                # mode keeps trying — the consecutive-empty guard below
                # surfaces a sustained failure.
                if n_ok == 0 and not pod_ips and obs_mode == "auto":
                    print(
                        "[rl_controller] envoy obs: no caller pods discovered "
                        "yet; cascading to /buckets for this tick "
                        f"(discovery_error={pod_cache.last_error() if pod_cache else 'no-cache'})",
                        file=sys.stderr,
                    )
                    effective_obs_mode = "buckets" if use_http_obs else "rows"
                elif n_ok == 0 and obs_mode == "auto":
                    print(
                        "[rl_controller] envoy obs: 0 of "
                        f"{len(pod_ips)} caller pods responded; "
                        "cascading to /buckets for this controller lifetime",
                        file=sys.stderr,
                    )
                    if pod_cache is not None:
                        pod_cache.invalidate()
                    effective_obs_mode = "buckets" if use_http_obs else "rows"
                else:
                    t = time.perf_counter()
                    timing.obs_parse_ms = (time.perf_counter() - t) * 1000.0
                    timing.rows = int(snapshot.get("upstream_rq_total", 0))

                    t = time.perf_counter()
                    observation, sources, metrics = (
                        compose_observation_from_envoy(
                            snapshot,
                            prev_envoy_snapshot,
                            cfg.observation_window_sec,
                            current,
                            previous_budget,
                            delta_metrics,
                            attempt_timeout_ms=cfg.attempt_timeout_ms,
                            istio_snapshot=istio_snapshot,
                            prev_istio_snapshot=prev_istio_snapshot,
                        )
                    )
                    sources["envoy_pods_responded"] = str(n_ok)
                    pp = (istio_snapshot or {}).get("per_profile") or {}
                    if pp:
                        sources["istio_profiles_observed"] = str(len(pp))
                    if (istio_snapshot or {}).get("callee_aggregate"):
                        sources["istio_callee_aggregate"] = "present"
                    timing.obs_build_ms = (time.perf_counter() - t) * 1000.0
                    prev_envoy_snapshot = snapshot
                    prev_istio_snapshot = istio_snapshot or None

                    # Fail-loud guard for the envoy path. We tolerate one
                    # connect-error tick (pod restarted) but bail noisily
                    # if every fetch fails for 5 ticks in a row, which is
                    # 10 s of policy-blind decisions. The guard fires
                    # whether the operator pinned `envoy` or `auto` cascaded
                    # to it; the message points to the right knob.
                    counter_total = int(snapshot.get("upstream_rq_total", 0))
                    if counter_total == 0:
                        consecutive_empty_envoy += 1
                    else:
                        consecutive_empty_envoy = 0
                    if consecutive_empty_envoy >= 5:
                        print(
                            f"[rl_controller] obs_mode=envoy returned 0 "
                            f"upstream_rq_total for 5 ticks (pods={pod_ips}, "
                            f"discovery_error={pod_cache.last_error() if pod_cache else None}); "
                            "either the cluster is idle or the sidecars "
                            "are not exposing /stats — falling back to /buckets",
                            file=sys.stderr,
                        )
                        if obs_mode == "auto":
                            effective_obs_mode = (
                                "buckets" if use_http_obs else "rows"
                            )
                            consecutive_empty_envoy = 0

            if use_http_obs and effective_obs_mode == "buckets":
                t = time.perf_counter()
                buckets, ok_shards = http_fetch_buckets(
                    buckets_url_template, loader_ports, since_ts,
                )
                timing.obs_fetch_ms = (time.perf_counter() - t) * 1000.0

                # /buckets returns 404 on loaders that pre-date the
                # pre-aggregated transport. In `auto` mode the controller
                # silently degrades to /window on the
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
                            delta_metrics,
                            attempt_timeout_ms=cfg.attempt_timeout_ms,
                        )
                    )
                    timing.obs_build_ms = (time.perf_counter() - t) * 1000.0

                    # Buckets-path fail-loud guard. If the controller
                    # silently produced zero-attempt observations for many
                    # consecutive ticks (an early-iteration regression that
                    # was bug-prone enough to need its own guard), surface
                    # it.
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
            # Skip when envoy mode owns this tick.
            if effective_obs_mode != "envoy" and effective_obs_mode == "rows":
                t = time.perf_counter()
                raw = http_fetch_window(
                    args.loader_url_template, loader_ports, since_ts,
                )
                timing.obs_fetch_ms = (time.perf_counter() - t) * 1000.0

                t = time.perf_counter()
                rows = parse_client_rows(raw, since_ts=since_ts)
                timing.obs_parse_ms = (time.perf_counter() - t) * 1000.0
                timing.rows = len(rows)

                t = time.perf_counter()
                metrics = build_metrics(
                    rows,
                    cfg.observation_window_sec,
                    attempt_timeout_ms=cfg.attempt_timeout_ms,
                )
                observation, sources = build_observation(
                    metrics, current, previous_budget, delta_metrics,
                )
                timing.obs_build_ms = (time.perf_counter() - t) * 1000.0

            # Policy: real RL inference or stub depending on what is available.
            pct_idx: int | None = None
            mrc_idx: int | None = None
            t = time.perf_counter()
            if model is not None:
                selected, (pct_idx, mrc_idx) = rl_policy(
                    observation, model, cfg, vec_norm=vec_norm,
                )
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
            metrics_history.append((now, metrics))
            # Keep at most 2× delta window of history (plus a small slack)
            # so the lookup stays O(short). Anything older than that can't
            # affect the next delta computation.
            trim_cutoff = now - (2.0 * cfg.delta_window_sec + 5.0)
            while metrics_history and metrics_history[0][0] < trim_cutoff:
                metrics_history.pop(0)

            deadline = time.time() + cfg.decision_interval_sec
            while not STOP and time.time() < deadline:
                time.sleep(min(0.25, deadline - time.time()))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
