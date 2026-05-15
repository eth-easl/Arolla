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
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


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
    `t_loop_start` is `time.time()` at the top of the iteration so a tick
    can be cross-referenced to the rl-observations.jsonl `timestamp`
    field. One record per tick — the tally script in bench_summarize.py
    aggregates p50/p95/p99 across runs."""

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
        # Resolve relative to the script directory (where run-experiment.sh lives).
        script_dir = Path(__file__).parent
        model_path = (script_dir / candidate).resolve()
        if not model_path.exists():
            print(
                f"[rl_controller] WARNING: model_path '{model_path}' not found, "
                "falling back to shadow_stub",
                file=sys.stderr,
            )
            model_path = None

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
# Kubernetes client (Phase 1.1: persistent CustomObjectsApi -> no per-tick
# kubectl subprocess). Lazy singleton — the TLS handshake and config load
# happen the first time we need to patch, then the HTTP/2 PATCH stream is
# reused for every later tick. The Mac-side controller uses a kubeconfig
# file (KUBECONFIG=~/.kube/config-emulab); falling back to the kubectl
# subprocess keeps the script runnable on hosts without the python
# kubernetes client installed.
# ---------------------------------------------------------------------------

_K8S_API: Any = None
_K8S_API_BACKEND: str = "uninitialised"


def _get_k8s_custom_objects_api() -> Any:
    """Return a cached CustomObjectsApi or None if the python client is
    unavailable / no usable kubeconfig is in scope. The first call initialises
    config; subsequent calls return the same client."""
    global _K8S_API, _K8S_API_BACKEND
    if _K8S_API is not None or _K8S_API_BACKEND == "kube_config":
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
    kubernetes client is unavailable (legacy hosts without the dependency),
    or when --legacy-patch-kubectl forces it for the Phase-0 baseline."""
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
    parser.add_argument("--client-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-opts", default="")
    parser.add_argument("--remote-metrics-dir", default="/tmp/online-boutique-clients/metrics")
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Observe and log decisions but do not patch the DestinationRule.",
    )
    # Phase-separation flags. Phase 0 baseline runs with both set so the
    # tick-latency table is comparable to the pre-plan-12 controller. The
    # Phase 1 sweep leaves them off (= use the new fast paths).
    parser.add_argument(
        "--legacy-patch-kubectl", action="store_true",
        help=(
            "Bypass the persistent kubernetes Python client and shell out "
            "to `kubectl patch` per tick (Phase 0 measurement baseline)."
        ),
    )
    parser.add_argument(
        "--legacy-full-cat", action="store_true",
        help=(
            "Skip the server-side awk pre-filter in ssh_cat_metrics and "
            "transfer every CSV byte each tick (Phase 0 measurement baseline)."
        ),
    )
    args = parser.parse_args()

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

    print(
        f"[rl_controller] mode={effective_mode}  apply={apply_enabled}  "
        f"model={'loaded' if model else 'none'}  "
        f"obs_window={cfg.observation_window_sec}s  "
        f"startup_hold_off={cfg.startup_hold_off_sec}s  "
        f"obs_dim={len(OBSERVATION_FIELDS)}",
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

    with observations_path.open("a") as obs_f, \
            decisions_path.open("a", newline="") as dec_f, \
            timings_path.open("a") as tim_f:
        writer = csv.writer(dec_f)
        while not STOP:
            tick_index += 1
            timing = TickTiming(tick_index=tick_index, t_loop_start=time.time())
            tick_t0 = time.perf_counter()
            now = timing.t_loop_start

            since_ts = now - cfg.observation_window_sec
            t = time.perf_counter()
            raw = ssh_cat_metrics(
                args.client_host, args.ssh_user, args.ssh_opts,
                args.remote_metrics_dir,
                # Phase 0 baseline: --legacy-full-cat disables the awk
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
