"""Envoy /stats parser + 18-feature observation derivation.

Companion to ``rl_obs_schema.py`` (the loader-side bucket schema, kept
for the ``--obs-mode buckets`` regression-guard path). This module
implements the production-shape observation source: the controller's
observations come from each caller-pod's Envoy sidecar metrics endpoint
at ``http://<podIP>:15020/stats/prometheus`` rather than from the
load generator's aggregated buckets. The model itself is unchanged —
only the source of the 18 features is.

Motivation:

  - Loader-based observations are not a production architecture: a real
    deployment has clients and microservices, not a Python load
    generator.
  - The loader-buckets tail-latency floor (p95 5.8 s on S15/S22) is set
    by the loader's single asyncio event loop being saturated by
    per-attempt callbacks during a retry storm. Sidecar CPU is
    reservable (see ``manifests/istio/sidecar-cpu-reservation.yaml``)
    and decoupled from the application container's CPU, so the sidecar
    floor is genuinely set by how fast the controller can scrape /stats
    from N caller pods in parallel — expected p95 ≤ 200 ms.

Why the Prometheus endpoint (port 15020) and not the JSON admin endpoint
(port 15000): Istio binds Envoy's admin listener to 127.0.0.1 only and
that bind address is hard-coded in the agent's bootstrap template (not
overridable via standard mesh config). Pod-to-pod requests to
``<podIP>:15000`` therefore hit Envoy's inbound capture and return 503.
``pilot-agent`` runs on the istio-proxy container and exposes a merged
Prometheus endpoint at ``<podIP>:15020/stats/prometheus`` that includes
every Envoy stat in the bootstrap's ``proxyStatsMatcher`` inclusion
list (see ``manifests/istio/sidecar-cpu-reservation.yaml``). The
endpoint accepts ``?filter=<regex>&usedonly`` so a per-tick fetch for
``upstream_rq.*cartservice`` is ≤ 15 KB.

Design:

  - Caller sidecars only. The 18-feature vector needs ``retry`` and
    ``5xx-after-retry`` counts; both live exclusively on caller-side
    cluster.outbound.* counters. Cart's own (callee) sidecar sees each
    retry as a fresh inbound request and so cannot distinguish "first
    attempt" from "retry" — there is no ``upstream_rq_retry`` on the
    inbound side.
  - Cart's two callers in Online Boutique are ``frontend`` and
    ``checkoutservice``. Counters and histograms are summed element-wise
    across both; that matches what an admission policy would see.
  - Per-tick deltas: Envoy reports cumulative counters, so the controller
    keeps a snapshot of the previous tick and emits the per-window delta.
    Same logic Prometheus' ``rate()`` uses. The latency histogram is
    delta'd the same way — current bucket counts minus previous bucket
    counts gives a per-window distribution that p95 is interpolated from.
  - Per-profile features (``min_client_success`` and
    ``retry_fairness_gap``) require labelling each request with its
    originating profile, which Envoy /stats counters don't carry by
    default. They are stubbed here and only become real once the data
    path is extended with an ``x-rl-profile`` header + Istio Telemetry
    config. The 12 aggregate features still drive correct decisions for
    the cart-aggregate-fault scenario subset the policy is trained on.

This module is intentionally free of any heavy imports (no torch, no
SB3, no kubernetes client). It runs unit-testable on a developer laptop
against a canned ``testdata/envoy_stats_sample.prom`` and is the only
runtime file the controller needs in addition to ``rl_controller.py``
and ``rl_obs_schema.py``.
"""

from __future__ import annotations

import re
from typing import Any


# Counters we track on every caller sidecar. The first six map
# 1:1 to attempt outcomes; the last three drive the retry-derived
# features (``retry_ratio``, ``window_retry_efficiency``, budget rejects).
# Names follow Envoy's `cluster.upstream_rq*` convention as it appears in
# Prometheus output:
#
#   envoy_cluster_upstream_rq_total{cluster_name="outbound|7070||cartservice..."} N
#   envoy_cluster_upstream_rq{cluster_name="...",response_code_class="2xx"}      N
#   envoy_cluster_upstream_rq{cluster_name="...",response_code_class="5xx"}      N
#   envoy_cluster_upstream_rq_retry{cluster_name="..."}                          N
#
# The ``response_code_class``-labelled ``upstream_rq`` series stand in
# for the old ``upstream_rq_2xx`` / ``upstream_rq_5xx`` counters that the
# JSON admin endpoint exposed as flat scalars.
ENVOY_COUNTER_KEYS: tuple[str, ...] = (
    "upstream_rq_total",
    "upstream_rq_completed",
    "upstream_rq_2xx",
    "upstream_rq_5xx",
    "upstream_rq_timeout",
    "upstream_rq_retry",
    "upstream_rq_retry_success",
    "upstream_rq_retry_overflow",
)

# Histogram name fragment we look for. Envoy emits one per cluster:
# ``envoy_cluster_upstream_rq_time_bucket{cluster_name="...",le="X"}``.
ENVOY_HIST_KEY = "upstream_rq_time"


# -----------------------------------------------------------------------
# Parsing — Prometheus text format from pilot-agent's /stats/prometheus
# -----------------------------------------------------------------------
#
# Sample lines we care about:
#
#   envoy_cluster_upstream_rq_total{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local"} 172812
#   envoy_cluster_upstream_rq{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local",response_code="200"} 86617
#   envoy_cluster_upstream_rq{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local",response_code_class="2xx"} 86617
#   envoy_cluster_upstream_rq_retry{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local"} 4944
#   envoy_cluster_upstream_rq_time_bucket{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local",le="5"} 52851
#   envoy_cluster_upstream_rq_time_count{cluster_name="outbound|7070||cartservice.online-boutique.svc.cluster.local"} 86617
#
# Mapping back to the old admin-JSON counter names:
#
#   envoy_cluster_upstream_rq_total          → upstream_rq_total
#   envoy_cluster_upstream_rq_completed      → upstream_rq_completed
#   envoy_cluster_upstream_rq{response_code_class="2xx"} → upstream_rq_2xx
#   envoy_cluster_upstream_rq{response_code_class="5xx"} → upstream_rq_5xx
#   envoy_cluster_upstream_rq_timeout        → upstream_rq_timeout
#   envoy_cluster_upstream_rq_retry          → upstream_rq_retry
#   envoy_cluster_upstream_rq_retry_success  → upstream_rq_retry_success
#   envoy_cluster_upstream_rq_retry_overflow → upstream_rq_retry_overflow
#
# Histogram is captured as a tuple of (le_seconds, cumulative_count) so
# the per-window p95 can be derived from current-vs-previous deltas.
# Envoy's ``upstream_rq_time`` is in milliseconds; we keep the ``le``
# values in seconds to match the loader-side ``LATENCY_HISTOGRAM_EDGES_S``
# the controller already uses for the buckets path.

_ENVOY_CLUSTER_PREFIX = "envoy_cluster_"

# Matches a prom line like:
#   envoy_cluster_upstream_rq_total{cluster_name="outbound|7070||X..."} 123
#   envoy_cluster_upstream_rq{cluster_name="...",response_code_class="2xx"} 86617
# Capture groups: metric (after envoy_cluster_), label-block, value.
_PROM_LINE_RE = re.compile(
    r"^envoy_cluster_(?P<metric>[A-Za-z0-9_]+)"
    r"\{(?P<labels>[^}]*)\}\s+(?P<value>-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s*$"
)

# Label split — labels are comma-separated ``key="value"`` pairs and the
# value (cluster_name in particular) may contain commas, pipes, dots and
# even slashes, but not unescaped quotes (Envoy doesn't emit them). A
# simple split on ``",`` is robust.
_PROM_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a Prom label block (without surrounding ``{}``) into a dict."""
    out: dict[str, str] = {}
    for m in _PROM_LABEL_RE.finditer(raw):
        out[m.group(1)] = m.group(2)
    return out


def parse_envoy_stats_prom(
    raw: str,
    callee: str = "cartservice",
) -> dict[str, Any]:
    """Parse one pilot-agent /stats/prometheus response.

    Returns a dict with:

      - One entry per name in :data:`ENVOY_COUNTER_KEYS` (cumulative
        counter summed across response-code variants where applicable).
      - ``"upstream_rq_time_buckets"``: list of ``(le_seconds, count)``
        tuples ordered by ``le_seconds`` ascending, plus a trailing
        ``(float('inf'), total_count)`` from ``upstream_rq_time_count``
        for the overflow bucket. Empty list if Envoy hasn't observed any
        latency samples yet (cold start).

    Stat lines whose ``cluster_name`` label does not contain
    ``callee`` are dropped silently. ``cluster_name`` for outbound
    Istio clusters looks like ``outbound|<port>||<svc>.<ns>.svc.cluster.local``;
    matching on the callee substring is enough to single it out.

    The function is allocation-light — Prometheus text scanning is
    line-at-a-time with one regex match per relevant line, no JSON
    parser. On the live cluster the filtered fetch
    (``?filter=upstream_rq.*cartservice&usedonly``) returns ~12 KB which
    parses in ~1 ms.
    """
    counters: dict[str, int] = {k: 0 for k in ENVOY_COUNTER_KEYS}
    # ``le`` (string) → cumulative count. We resolve to seconds at the
    # end so the float ordering of "5" vs "50" doesn't bite us mid-parse.
    bucket_counts: dict[str, int] = {}
    rq_time_count: int = 0

    # Sub-name routing for the bare ``upstream_rq`` series, which the
    # Prom endpoint splits by ``response_code_class`` (or
    # ``response_code`` for individual codes). We sum only the
    # ``response_code_class`` variants; the ``response_code`` variants
    # would otherwise double-count, and the admin-JSON path's
    # ``upstream_rq_2xx`` / ``upstream_rq_5xx`` had the same class-level
    # granularity.

    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith(_ENVOY_CLUSTER_PREFIX):
            continue
        m = _PROM_LINE_RE.match(line)
        if m is None:
            continue
        metric = m.group("metric")
        # Skip the ``external_upstream_rq_*`` flavour. For an outbound
        # cluster Envoy emits *both* the canonical ``upstream_rq_*``
        # counters and a duplicate ``external_upstream_rq_*`` set with
        # identical values — the ``external_`` variant only matches
        # non-mesh upstreams, and for outbound clusters everything IS
        # non-mesh from the caller's POV. Summing both would double-
        # count every request. The same applies to the histogram series
        # ``external_upstream_rq_time_bucket``.
        if metric.startswith("external_upstream_rq") or metric.startswith(
            "internal_upstream_rq"
        ):
            continue
        labels = _parse_labels(m.group("labels"))
        cluster = labels.get("cluster_name", "")
        if callee not in cluster:
            continue

        try:
            value = float(m.group("value"))
        except ValueError:
            continue

        # Counter dispatch. ``metric`` is the bit after
        # ``envoy_cluster_`` — e.g. ``upstream_rq_total``,
        # ``upstream_rq`` (with code-class), ``upstream_rq_time_bucket``.
        if metric == "upstream_rq":
            cls = labels.get("response_code_class")
            if cls == "2xx":
                counters["upstream_rq_2xx"] += int(value)
            elif cls == "5xx":
                counters["upstream_rq_5xx"] += int(value)
            # ``response_code`` (per-code, e.g. "200", "503") is ignored
            # to avoid double-counting against the class-level variant.
            continue

        if metric == "upstream_rq_time_bucket":
            le = labels.get("le")
            if le is None:
                continue
            # Multiple pods or repeated entries: the *caller* aggregates
            # across pods via ``aggregate_pod_snapshots``, but a single
            # /stats fetch can also return multiple cluster entries that
            # match (e.g. when the same cluster has a stale duplicate);
            # sum to be safe.
            bucket_counts[le] = bucket_counts.get(le, 0) + int(value)
            continue

        if metric == "upstream_rq_time_count":
            rq_time_count += int(value)
            continue

        if metric == "upstream_rq_time_sum":
            # We don't expose the sum to the controller (p95 alone drives
            # the latency feature) but the parse is cheap and lets future
            # callers compute the mean if they want it.
            continue

        if metric in counters:
            counters[metric] += int(value)
            continue

    # Convert ``le`` strings to (float seconds, count) pairs, sorted
    # ascending. Envoy's ``upstream_rq_time`` is reported in ms, so the
    # ``le`` value (also in ms) is divided by 1000 to get seconds. The
    # ``+Inf`` bucket from ``upstream_rq_time_count`` is appended as the
    # overflow.
    buckets: list[tuple[float, int]] = []
    for le_str, cnt in bucket_counts.items():
        try:
            le_ms = float(le_str)
        except ValueError:
            continue
        buckets.append((le_ms / 1000.0, cnt))
    buckets.sort(key=lambda x: x[0])
    if rq_time_count and (not buckets or rq_time_count > buckets[-1][1]):
        buckets.append((float("inf"), rq_time_count))

    out: dict[str, Any] = dict(counters)
    out["upstream_rq_time_buckets"] = buckets
    out["upstream_rq_time_count"] = rq_time_count
    return out


# -----------------------------------------------------------------------
# Aggregation across multiple caller pods + delta vs previous snapshot
# -----------------------------------------------------------------------


def aggregate_pod_snapshots(
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Element-wise reduction of per-pod ``parse_envoy_stats_prom`` outputs.

    Counters are summed (every retry/2xx counts wherever it happens);
    histogram bucket counts are summed across pods on a per-``le`` basis
    so the controller sees the aggregate latency distribution across all
    callers, which is what a mesh-wide admission policy would see.

    An empty list returns a zeroed snapshot in the same shape so the
    downstream code path is uniform.
    """
    out: dict[str, Any] = {k: 0 for k in ENVOY_COUNTER_KEYS}
    out["upstream_rq_time_buckets"] = []
    out["upstream_rq_time_count"] = 0

    bucket_sums: dict[float, int] = {}
    total_count = 0
    for snap in snapshots:
        for key in ENVOY_COUNTER_KEYS:
            out[key] += int(snap.get(key, 0) or 0)
        for le, cnt in snap.get("upstream_rq_time_buckets") or []:
            bucket_sums[le] = bucket_sums.get(le, 0) + int(cnt)
        total_count += int(snap.get("upstream_rq_time_count", 0) or 0)
    if bucket_sums:
        out["upstream_rq_time_buckets"] = sorted(bucket_sums.items())
    out["upstream_rq_time_count"] = total_count
    return out


def delta_counters(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, int]:
    """Per-window delta on the counter set. ``max(0, ...)`` clamps so a
    sidecar that restarted between ticks (counters reset to 0) doesn't
    produce a negative count — the next tick's window will be incomplete
    but the tick after that recovers naturally.
    """
    out: dict[str, int] = {}
    for key in ENVOY_COUNTER_KEYS:
        cur = int(current.get(key, 0) or 0)
        prev = int((previous or {}).get(key, 0) or 0)
        out[key] = max(0, cur - prev)
    return out


def delta_hist(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[tuple[float, int]]:
    """Per-window cumulative histogram in (le_seconds, delta_count) form.

    Aligns the current and previous bucket lists by ``le`` and subtracts
    each pair (clamped to 0 to absorb pod restarts the same way
    :func:`delta_counters` does). Returns an empty list if either the
    current snapshot has no buckets or the alignment fails (e.g. Envoy
    versions disagree on the histogram edges — should never happen
    within one cluster).

    The result is still in *cumulative* form (each entry's count is
    the total observations ≤ that ``le`` in the window), which is what
    :func:`p95_from_envoy` expects.
    """
    cur_b = current.get("upstream_rq_time_buckets") or []
    if not cur_b:
        return []
    prev_b = dict((previous or {}).get("upstream_rq_time_buckets") or [])
    out: list[tuple[float, int]] = []
    for le, cur_cnt in cur_b:
        prev_cnt = int(prev_b.get(le, 0) or 0)
        out.append((le, max(0, int(cur_cnt) - prev_cnt)))
    return out


def p95_from_envoy(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> float:
    """Per-window p95 latency in seconds from an aggregated snapshot.

    Strategy:

      1. Compute the per-window cumulative histogram via :func:`delta_hist`.
      2. Linear interpolation inside the bucket that contains the 95th
         percentile sample. The overflow (``+Inf``) bucket maps to the
         last finite edge so a fully-saturated window clamps to that
         value — the consumer then divides by the 3 s attempt timeout
         and clamps to ``[0, 1]`` (same as the buckets path's
         ``_bucket_compose_pressure``).
      3. Returns ``0.0`` if the window observed no samples (the
         distribution is empty), which the controller's p95 feature
         downstream treats as "no pressure".
    """
    delta = delta_hist(current, previous)
    if not delta:
        return 0.0
    total = delta[-1][1] if delta else 0
    if total <= 0:
        return 0.0
    target = 0.95 * total
    prev_le = 0.0
    prev_cum = 0
    for le, cum in delta:
        if cum >= target:
            # Linear interpolation between (prev_le, prev_cum) and
            # (le, cum). The +Inf overflow bucket clamps to ``prev_le``
            # since "infinity" isn't a useful number.
            if le == float("inf"):
                return prev_le
            span_cum = cum - prev_cum
            if span_cum <= 0:
                return float(le)
            frac = (target - prev_cum) / span_cum
            return float(prev_le + (le - prev_le) * frac)
        prev_le = le if le != float("inf") else prev_le
        prev_cum = cum
    # All samples fall below the target (shouldn't happen since
    # target ≤ total = last cumulative), but return the last finite
    # edge as a defensive default.
    return prev_le


# -----------------------------------------------------------------------
# Build the 18-feature observation vector
# -----------------------------------------------------------------------


def compose_metrics_from_envoy(
    counter_delta: dict[str, int],
    p95_seconds: float,
    window_sec: float,
) -> dict[str, Any]:
    """Reconstruct the same per-tick ``metrics`` dict the rows / buckets
    paths produce.

    Mirrors ``rl_controller.compose_observation_from_buckets``'s metrics
    output so ``rl_controller.build_observation`` can run the rest of the
    pipeline unchanged. Per-profile features are stubbed: there's no way
    to derive them from Envoy /stats counters alone without a per-profile
    request label.

    ``p95_seconds`` is the per-window p95 latency in seconds, already
    extracted from the snapshot's histogram by :func:`p95_from_envoy`.
    Keeping it as a positional argument (instead of recomputing from
    the snapshot inside this function) makes the unit tests trivial and
    matches the controller's "build deltas, then compose" code shape
    for the rows / buckets paths.
    """
    total = max(int(counter_delta.get("upstream_rq_total", 0)), 1)
    completed = max(int(counter_delta.get("upstream_rq_completed", 0)), 1)
    successes = int(counter_delta.get("upstream_rq_2xx", 0))
    server_failures = int(counter_delta.get("upstream_rq_5xx", 0))
    timeouts = int(counter_delta.get("upstream_rq_timeout", 0))
    retries = int(counter_delta.get("upstream_rq_retry", 0))
    retry_successes = int(counter_delta.get("upstream_rq_retry_success", 0))

    success_rate = successes / completed
    retry_ratio = retries / total
    # `window_load_amplification` is attempts / requests in the rows path.
    # Envoy gives `total` (every attempt = first try + retries) and
    # `completed` (one per finalised request), so the same identity holds.
    load_amplification = (
        int(counter_delta.get("upstream_rq_total", 0)) / completed
    )
    retry_efficiency = retry_successes / max(retries, 1) if retries else 0.0
    latency_pressure = min(p95_seconds / 3.0, 1.0)

    # Pseudo-RPS, derived from the configured window. The buckets path
    # does the same: it doesn't have wall-clock per-attempt data, just
    # per-window aggregates.
    retry_rps = retries / max(window_sec, 1.0)
    request_rps = (
        int(counter_delta.get("upstream_rq_completed", 0)) / max(window_sec, 1.0)
    )

    return {
        "rows": int(counter_delta.get("upstream_rq_total", 0)),
        "requests": int(counter_delta.get("upstream_rq_completed", 0)),
        "window_sec": window_sec,
        "success_rate_agg": success_rate,
        # Stubbed: Envoy /stats can't break out per-profile success
        # without a request-label rewrite (x-rl-profile + Telemetry cfg).
        "min_client_success": success_rate,
        "retry_ratio": retry_ratio,
        "window_load_amplification": load_amplification,
        "window_retry_efficiency": retry_efficiency,
        # Stubbed for the same reason as min_client_success.
        "retry_fairness_gap": 0.0,
        "p95_latency_pressure": latency_pressure,
        "server_fail_rate": server_failures / total,
        "deadline_rate": timeouts / total,
        "retry_rps": retry_rps,
        "request_rps": request_rps,
        "quality": {
            # Distinct from "live_buckets" / "live_remote_csv" so the
            # controller's rl-observations.jsonl makes the obs source
            # explicit.
            "client_metrics": (
                "envoy_sidecar"
                if int(counter_delta.get("upstream_rq_total", 0)) > 0
                else "envoy_sidecar_empty_window"
            ),
            "retry_fairness_gap": "stubbed_no_per_profile_label",
        },
    }


# Back-compat alias: older callers (and the controller's import block)
# still reference ``parse_envoy_stats_json``. The pilot-agent endpoint
# emits Prometheus text, so the alias points at the new parser; the
# function name in the controller is irrelevant to the wire format.
parse_envoy_stats_json = parse_envoy_stats_prom


__all__ = [
    "ENVOY_COUNTER_KEYS",
    "ENVOY_HIST_KEY",
    "parse_envoy_stats_prom",
    "parse_envoy_stats_json",
    "aggregate_pod_snapshots",
    "delta_counters",
    "delta_hist",
    "p95_from_envoy",
    "compose_metrics_from_envoy",
]
