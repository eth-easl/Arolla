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

# Gauges (scrape-time snapshots — not delta'd between ticks). Powering
# the caller-side concurrency limit derivation that feeds #13
# (budget_utilization) and #14 (retry_pressure_vs_limit). Envoy emits
# them as ``envoy_cluster_upstream_rq_active`` etc. — same naming shape
# as the counters but they're conceptually different and must NOT be
# fed through :func:`delta_counters` (which only iterates the counter
# tuple anyway).
ENVOY_GAUGE_KEYS: tuple[str, ...] = (
    "upstream_rq_active",
    "upstream_rq_pending_active",
    "upstream_rq_retry_active",
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
    gauges: dict[str, int] = {k: 0 for k in ENVOY_GAUGE_KEYS}
    # Per-``rl_profile`` counters, populated only when the line carries
    # the label (added by the Istio Telemetry CR at
    # manifests/istio/telemetry-rl-profile.yaml). When the CR is not
    # applied this stays empty and the caller falls back to the
    # aggregate values — which is what every pre-CR rollout sees.
    per_profile: dict[str, dict[str, int]] = {}
    # ``le`` (string) → cumulative count. We resolve to seconds at the
    # end so the float ordering of "5" vs "50" doesn't bite us mid-parse.
    bucket_counts: dict[str, int] = {}
    rq_time_count: int = 0

    def _bump_profile(key: str, value: int, label_value: str) -> None:
        if not label_value:
            return
        slot = per_profile.setdefault(
            label_value,
            {k: 0 for k in ENVOY_COUNTER_KEYS},
        )
        slot[key] += value

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

        # ``rl_profile`` is the per-loader-profile label set by the
        # Istio Telemetry CR (Phase 4A). When absent, per-profile
        # accumulation is a no-op for this line.
        profile_label = labels.get("rl_profile", "")

        # Counter dispatch. ``metric`` is the bit after
        # ``envoy_cluster_`` — e.g. ``upstream_rq_total``,
        # ``upstream_rq`` (with code-class), ``upstream_rq_time_bucket``.
        if metric == "upstream_rq":
            cls = labels.get("response_code_class")
            if cls == "2xx":
                counters["upstream_rq_2xx"] += int(value)
                _bump_profile("upstream_rq_2xx", int(value), profile_label)
            elif cls == "5xx":
                counters["upstream_rq_5xx"] += int(value)
                _bump_profile("upstream_rq_5xx", int(value), profile_label)
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
            _bump_profile(metric, int(value), profile_label)
            continue

        if metric in gauges:
            # Gauges sum across cluster duplicates the same way counters
            # do — Envoy occasionally surfaces a stale cluster row that
            # we'd otherwise miss. Aggregation across pods is handled
            # later in :func:`aggregate_pod_snapshots`.
            gauges[metric] += int(value)
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
    out.update(gauges)
    out["upstream_rq_time_buckets"] = buckets
    out["upstream_rq_time_count"] = rq_time_count
    out["per_profile"] = per_profile
    return out


# -----------------------------------------------------------------------
# istio_requests_total — per-profile attempt accounting
# -----------------------------------------------------------------------
#
# The Istio Telemetry CR ``rl-profile-dimension`` (see
# ``prototype/manifests/istio/telemetry-rl-profile.yaml``) extends the
# ``REQUEST_COUNT`` metric with an ``rl_profile`` dimension extracted
# from the ``x-rl-profile`` header. That dimension lives on
# ``istio_requests_total`` (the Istio extension family) and NOT on the
# raw ``envoy_cluster_upstream_rq_*`` counters that drive every other
# slot. We therefore fetch and parse istio_requests_total separately
# and use it ONLY to derive the per-profile-success-rate signals
# (#1 min_client_success, #5 retry_fairness_gap).
#
# Each line shape:
#
#   istio_requests_total{
#       reporter="destination",                 # see WHICH-ROWS below
#       source_workload="boutique-gateway-istio",
#       destination_workload="frontend",
#       response_code="200",                    # final HTTP code
#       grpc_response_status="0",               # final gRPC status
#       response_flags="-",                     # Envoy ALS flags
#       rl_profile="post-cart-stress-open",     # injected by the CR
#       ...
#   } 1234
#
# WHICH ROWS WE KEEP — and why we don't filter to ``cartservice``:
# the gateway sets ``x-rl-profile`` per loader profile, and the Telemetry
# CR materialises that header as the ``rl_profile`` label on
# ``istio_requests_total``. But the FRONTEND application doesn't forward
# the header to its downstream gRPC calls, so every ``reporter=source``
# row with ``destination=cartservice/checkoutservice/etc.`` carries
# ``rl_profile="unknown"``. The only rows that carry a real loader
# profile are at the FRONTEND sidecar with ``reporter="destination"``
# (gateway → frontend hop), where the gateway propagated the header
# correctly.
#
# That's actually the RIGHT signal for the simulator's
# ``min_client_success`` — the simulator measures success per CLIENT
# class (loader profile) end-to-end, not per-callee. So we filter to
# ``reporter="destination"`` and skip rows where the profile label is
# blank or ``"unknown"``. Source workloads / response_flags variants
# collapse into a single ``{total, success}`` per profile.
#
# Success classification (HTTP vs gRPC): when ``grpc_response_status``
# is non-empty we use it (gRPC 0 = OK, anything else is a failure even
# if the HTTP wrapper is 200 — Envoy frequently logs OK+UNAVAILABLE
# for aborted upstream chains). Otherwise we fall back to HTTP code:
# 2xx and 3xx are success (3xx covers the frontend's 302 redirects on
# normal homepage loads), everything else is a failure.

_ISTIO_REQUESTS_LINE_RE = re.compile(
    r"^istio_requests_total"
    r"\{(?P<labels>[^}]*)\}\s+(?P<value>-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s*$"
)


def parse_istio_requests_total(
    raw: str,
    workload: str | None = None,
    callee: str | None = None,
) -> dict[str, Any]:
    """Parse a ``/stats/prometheus?filter=istio_requests_total`` response
    into two complementary views:

      - ``"per_profile"``: ``{rl_profile: {"total": N, "success": M}}``
        from ``reporter="destination"`` rows with a real (non-``unknown``)
        ``rl_profile`` label. Feeds slot #1 (``min_client_success``) and
        slot #5 (``retry_fairness_gap``). PATH C LIMITATION: in the
        Online Boutique deploy the only rows that carry a real
        ``rl_profile`` are at the frontend pod's sidecar reporter=
        destination view (gateway → frontend hop). Frontend's gRPC
        client doesn't forward the ``x-rl-profile`` header to cart, so
        every reporter=source / cart-side reporter=destination row is
        labelled ``rl_profile="unknown"``. This means slot #1 measures
        the loader's END-TO-END page-load success per profile, not
        the per-CART-CALL success per profile that the simulator
        nominally trains on. For single-profile workloads this is
        irrelevant (``min == aggregate``); the proper fix for multi-
        profile workloads is to add header propagation in the frontend
        application (or via an EnvoyFilter+Wasm shim). Documented in
        v5-thinking.log § "PER-SLOT AUDIT — PATH C".

      - ``"callee_aggregate"``: ``{"total": N, "success": M,
        "server_failures": K}`` from ``reporter="source"`` rows where
        ``destination_workload == callee`` (e.g. ``"cartservice"``).
        This is the gRPC-aware aggregate view of cart calls from the
        caller-side. Used to override slot #0 (``success_rate_agg``)
        and slot #8 (``server_fail_rate``), which would otherwise rely
        on Envoy's HTTP-only ``upstream_rq_{2xx,5xx}`` counters and
        misclassify gRPC application errors (status 14 UNAVAILABLE
        wrapped in HTTP 200) as success.

        Classification (mirroring the simulator's attempt outcomes):
          - ``success`` ← ``grpc_response_status == "0"``, OR
            ``response_code`` starts with ``"2"`` / ``"3"`` when gRPC
            status is missing (HTTP-only paths).
          - ``server_failures`` ← any non-success row whose
            ``response_flags`` does NOT contain ``UO`` (Envoy's
            UpstreamOverflow flag, which marks budget-rejected
            retries) AND whose ``grpc_response_status`` is NOT ``4``
            (DEADLINE_EXCEEDED — accounted under slot #9 via the
            latency-histogram CDF, not slot #8). HTTP 5xx is always
            counted here regardless of response_flags since the
            simulator's SERVER_FAILURE class covers them.
          - Budget rejects (response_flags=``UO``) and gRPC deadlines
            (grpc_status=4) are intentionally NOT in ``server_failures``;
            they belong to slot #7 (``budget_reject_rate``, via the
            Envoy retry counter) and slot #9 (``deadline_rate``, via
            the histogram CDF).

        ``callee`` must be provided to enable this view; otherwise the
        key is omitted and callers fall back to the HTTP-only formula.

    ``workload`` (optional) further restricts per-profile rows to a
    specific ``destination_workload`` — extra insurance when the same
    sidecar fetch sees multiple workloads (rarely an issue since
    pilot-agent's stats endpoint is scoped to its own pod).

    Returns a dict with at most the two keys above. Empty subdicts
    are the safe signal to fall back to the aggregate-counter stubs.
    """
    per_profile: dict[str, dict[str, int]] = {}
    callee_total = 0
    callee_success = 0
    callee_server_failures = 0
    saw_callee_row = False
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith("istio_requests_total{"):
            continue
        m = _ISTIO_REQUESTS_LINE_RE.match(line)
        if m is None:
            continue
        labels = _parse_labels(m.group("labels"))
        try:
            value = int(float(m.group("value")))
        except ValueError:
            continue
        if value <= 0:
            continue
        reporter = labels.get("reporter", "")
        grpc_status = labels.get("grpc_response_status", "")
        http_code = labels.get("response_code", "")
        response_flags = labels.get("response_flags", "")
        if grpc_status:
            is_success = grpc_status == "0"
        else:
            is_success = (
                http_code.startswith("2") or http_code.startswith("3")
            ) if http_code else False

        # Per-profile view (slot 1 / 5): reporter=destination + real profile.
        if reporter == "destination":
            profile = labels.get("rl_profile", "")
            if profile and profile != "unknown":
                if workload is None or labels.get(
                    "destination_workload", ""
                ) == workload:
                    slot = per_profile.setdefault(
                        profile, {"total": 0, "success": 0}
                    )
                    slot["total"] += value
                    if is_success:
                        slot["success"] += value

        # Callee-aggregate view (slots 0 / 8): reporter=source from any
        # caller, destination_workload == callee. This captures every
        # call we made TO the callee with the gRPC outcome correctly
        # attributed — the same data the loader's per-attempt CSV
        # would yield, just collected from sidecar telemetry instead.
        if (
            callee is not None
            and reporter == "source"
            and labels.get("destination_workload", "") == callee
        ):
            saw_callee_row = True
            callee_total += value
            if is_success:
                callee_success += value
            else:
                # Non-success classification — exclude budget rejects
                # (slot 7) and gRPC deadlines (slot 9) from slot 8.
                is_budget_reject = "UO" in response_flags
                is_deadline = grpc_status == "4"
                if not (is_budget_reject or is_deadline):
                    callee_server_failures += value

    out: dict[str, Any] = {"per_profile": per_profile}
    if callee is not None and saw_callee_row:
        out["callee_aggregate"] = {
            "total": callee_total,
            "success": callee_success,
            "server_failures": callee_server_failures,
        }
    return out


def aggregate_istio_snapshots(
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Element-wise sum of per-pod ``parse_istio_requests_total`` outputs.

    Handles the new two-key shape: ``per_profile`` (dict of profiles)
    and ``callee_aggregate`` (single dict of {total, success,
    server_failures}). A profile that only appears in some pods
    inherits 0 from the rest. The callee aggregate sums across all
    responding pods so the controller sees mesh-wide cart-call
    counts. Empty input returns ``{"per_profile": {}}`` — no
    callee_aggregate key — so the caller falls back to the aggregate-
    counter stubs.
    """
    per_profile: dict[str, dict[str, int]] = {}
    callee_total = 0
    callee_success = 0
    callee_server_failures = 0
    saw_callee = False
    for snap in snapshots:
        if not snap:
            continue
        for prof, counts in (snap.get("per_profile") or {}).items():
            slot = per_profile.setdefault(prof, {"total": 0, "success": 0})
            slot["total"] += int(counts.get("total", 0) or 0)
            slot["success"] += int(counts.get("success", 0) or 0)
        ca = snap.get("callee_aggregate")
        if ca:
            saw_callee = True
            callee_total += int(ca.get("total", 0) or 0)
            callee_success += int(ca.get("success", 0) or 0)
            callee_server_failures += int(ca.get("server_failures", 0) or 0)
    out: dict[str, Any] = {"per_profile": per_profile}
    if saw_callee:
        out["callee_aggregate"] = {
            "total": callee_total,
            "success": callee_success,
            "server_failures": callee_server_failures,
        }
    return out


# Back-compat alias for the previous single-view aggregator. Callers
# that only need the per-profile slice can still use this name; the
# new aggregator returns the full two-key shape.
def aggregate_istio_per_profile(
    snapshots: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Deprecated: returns only the per-profile slice of
    :func:`aggregate_istio_snapshots`. Kept so older tests don't break."""
    return aggregate_istio_snapshots(snapshots).get("per_profile", {})


def delta_istio_snapshots(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Per-window delta of the aggregated istio snapshot (per-profile
    map AND the callee aggregate). Same ``max(0, ...)`` clamp as
    :func:`delta_counters` so a counter reset between ticks doesn't
    produce a negative window.

    Returns the same two-key shape as :func:`aggregate_istio_snapshots`,
    with empty subdicts (or missing keys) when nothing moved in the
    window. The caller treats those as "fall back to HTTP-only
    aggregates" / "fall back to per-profile stubs" respectively.
    """
    cur = current or {}
    prev = previous or {}
    out: dict[str, Any] = {}
    cur_pp = cur.get("per_profile") or {}
    prev_pp = prev.get("per_profile") or {}
    profile_delta: dict[str, dict[str, int]] = {}
    for prof, cur_counts in cur_pp.items():
        prev_counts = prev_pp.get(prof, {})
        total_d = max(
            0,
            int(cur_counts.get("total", 0) or 0)
            - int(prev_counts.get("total", 0) or 0),
        )
        success_d = max(
            0,
            int(cur_counts.get("success", 0) or 0)
            - int(prev_counts.get("success", 0) or 0),
        )
        if total_d > 0:
            profile_delta[prof] = {"total": total_d, "success": success_d}
    out["per_profile"] = profile_delta

    cur_ca = cur.get("callee_aggregate")
    if cur_ca:
        prev_ca = prev.get("callee_aggregate") or {}
        total_d = max(
            0,
            int(cur_ca.get("total", 0) or 0)
            - int(prev_ca.get("total", 0) or 0),
        )
        if total_d > 0:
            success_d = max(
                0,
                int(cur_ca.get("success", 0) or 0)
                - int(prev_ca.get("success", 0) or 0),
            )
            server_d = max(
                0,
                int(cur_ca.get("server_failures", 0) or 0)
                - int(prev_ca.get("server_failures", 0) or 0),
            )
            out["callee_aggregate"] = {
                "total": total_d,
                "success": success_d,
                "server_failures": server_d,
            }
    return out


# Back-compat alias for the previous per-profile-only delta helper.
def delta_istio_per_profile(
    current: dict[str, Any] | dict[str, dict[str, int]],
    previous: dict[str, Any] | None,
) -> dict[str, dict[str, int]]:
    """Deprecated: returns only the per-profile slice of
    :func:`delta_istio_snapshots`. Kept so older tests don't break."""
    # Tolerate both the old flat ``{profile: counts}`` shape and the
    # new two-key shape so existing call sites can be migrated lazily.
    if current and "per_profile" not in current:
        # Old shape: treat as per-profile dict directly.
        wrapped = {"per_profile": current}
        wrapped_prev = (
            {"per_profile": previous} if previous and "per_profile" not in previous
            else previous
        )
        return delta_istio_snapshots(wrapped, wrapped_prev).get("per_profile", {})
    return delta_istio_snapshots(current, previous).get("per_profile", {})


# -----------------------------------------------------------------------
# Aggregation across multiple caller pods + delta vs previous snapshot
# -----------------------------------------------------------------------


def aggregate_pod_snapshots(
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Element-wise reduction of per-pod ``parse_envoy_stats_prom`` outputs.

    Counters are summed (every retry/2xx counts wherever it happens);
    gauges (active / pending_active / retry_active) are also summed
    across pods because the controller's concurrency-limit feature
    needs the mesh-wide in-flight count, not a per-pod view. Histogram
    bucket counts are summed across pods on a per-``le`` basis so the
    controller sees the aggregate latency distribution across all
    callers, which is what a mesh-wide admission policy would see.

    An empty list returns a zeroed snapshot in the same shape so the
    downstream code path is uniform.
    """
    out: dict[str, Any] = {k: 0 for k in ENVOY_COUNTER_KEYS}
    out.update({k: 0 for k in ENVOY_GAUGE_KEYS})
    out["upstream_rq_time_buckets"] = []
    out["upstream_rq_time_count"] = 0
    per_profile_agg: dict[str, dict[str, int]] = {}

    bucket_sums: dict[float, int] = {}
    total_count = 0
    for snap in snapshots:
        for key in ENVOY_COUNTER_KEYS:
            out[key] += int(snap.get(key, 0) or 0)
        for key in ENVOY_GAUGE_KEYS:
            out[key] += int(snap.get(key, 0) or 0)
        for prof, prof_counts in (snap.get("per_profile") or {}).items():
            slot = per_profile_agg.setdefault(
                prof, {k: 0 for k in ENVOY_COUNTER_KEYS},
            )
            for k, v in prof_counts.items():
                slot[k] = slot.get(k, 0) + int(v or 0)
        for le, cnt in snap.get("upstream_rq_time_buckets") or []:
            bucket_sums[le] = bucket_sums.get(le, 0) + int(cnt)
        total_count += int(snap.get("upstream_rq_time_count", 0) or 0)
    if bucket_sums:
        out["upstream_rq_time_buckets"] = sorted(bucket_sums.items())
    out["upstream_rq_time_count"] = total_count
    out["per_profile"] = per_profile_agg
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


def per_profile_delta(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, dict[str, int]]:
    """Per-window delta of the per-``rl_profile`` counter dicts.

    Same ``max(0, cur - prev)`` clamp as :func:`delta_counters` to absorb
    sidecar restarts. Profiles present only in ``current`` use 0 as the
    previous baseline (their full counter is the window delta); profiles
    that disappear are dropped. Returns ``{}`` when the current snapshot
    has no per-profile data, which is the safe signal back to
    :func:`compose_metrics_from_envoy` to fall back to the aggregate
    counters for #2 / #6.
    """
    cur_pp = current.get("per_profile") or {}
    if not cur_pp:
        return {}
    prev_pp = (previous or {}).get("per_profile") or {}
    out: dict[str, dict[str, int]] = {}
    for prof, cur_counts in cur_pp.items():
        prev_counts = prev_pp.get(prof, {})
        slot: dict[str, int] = {}
        for k in ENVOY_COUNTER_KEYS:
            cur = int(cur_counts.get(k, 0) or 0)
            prev = int(prev_counts.get(k, 0) or 0)
            slot[k] = max(0, cur - prev)
        out[prof] = slot
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


def quantile_from_envoy(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    q: float,
) -> float:
    """Per-window q-quantile of attempt latency (seconds) from an
    aggregated snapshot.

    Implementation:

      1. Per-window cumulative histogram via :func:`delta_hist`.
      2. Locate the bucket containing the ``q * total``-th sample and
         linearly interpolate the latency between bucket edges.
      3. The ``+Inf`` overflow bucket clamps to the last finite edge so
         a fully-saturated window doesn't return infinity to downstream
         consumers (the controller divides p95 by the attempt timeout;
         dividing infinity is unhelpful).
      4. Returns ``0.0`` for empty windows (no samples observed); the
         controller treats that as "no pressure".

    ``q`` is the quantile in [0, 1]. ``0.5`` for median, ``0.95`` for
    p95, etc. Mostly internal — :func:`p95_from_envoy` and
    :func:`median_latency_from_envoy` are the public shortcuts.
    """
    if not 0.0 < q <= 1.0:
        raise ValueError(f"q must be in (0, 1], got {q}")
    delta = delta_hist(current, previous)
    if not delta:
        return 0.0
    total = delta[-1][1]
    if total <= 0:
        return 0.0
    target = q * total
    prev_le = 0.0
    prev_cum = 0
    for le, cum in delta:
        if cum >= target:
            if le == float("inf"):
                return prev_le
            span_cum = cum - prev_cum
            if span_cum <= 0:
                return float(le)
            frac = (target - prev_cum) / span_cum
            return float(prev_le + (le - prev_le) * frac)
        prev_le = le if le != float("inf") else prev_le
        prev_cum = cum
    # All samples ≤ last finite edge (target == total); return that edge.
    return prev_le


def p95_from_envoy(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> float:
    """Per-window p95 latency in seconds. Thin wrapper around
    :func:`quantile_from_envoy` for callers that only need the
    canonical tail-latency quantile."""
    return quantile_from_envoy(current, previous, 0.95)


def median_latency_from_envoy(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> float:
    """Per-window p50 latency in seconds. Used as a proxy for the mean
    latency in Little's-law estimates of in-flight attempts; the
    histogram doesn't expose a true mean (the ``upstream_rq_time_sum``
    counter is dropped during parsing), and median is more robust to
    long-tail outliers than the mean would be anyway.
    """
    return quantile_from_envoy(current, previous, 0.5)


def deadline_rate_from_envoy(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    threshold_seconds: float,
) -> float:
    """Per-window fraction of attempts whose latency met or exceeded
    ``threshold_seconds``.

    Why we don't use ``upstream_rq_timeout``: that counter only
    increments when Envoy's own per-try-timeout cancels the request,
    which requires a ``perTryTimeout`` on the route. The cart route
    in Online Boutique has no per-try-timeout, so application-level
    slow attempts (300–400 ms p95 during fault injection) never
    increment it and the counter sits at 0. The simulator's
    ``deadline_rate`` semantics are "fraction with latency ≥
    attempt_timeout_ms" — a histogram CDF read, not a counter — so
    we derive it here from the same ``upstream_rq_time_bucket``
    series that powers :func:`p95_from_envoy`.

    Returns ``0.0`` for empty windows. Returns up to ``1.0`` when
    every observed attempt was slower than ``threshold_seconds``
    (the threshold falls entirely below the histogram).
    """
    delta = delta_hist(current, previous)
    if not delta:
        return 0.0
    total = delta[-1][1]
    if total <= 0:
        return 0.0
    # Walk the cumulative histogram to find the count of attempts at
    # or below the threshold; everything else is "missed deadline".
    prev_le = 0.0
    prev_cum = 0
    cum_at_threshold = 0
    finalised = False
    for le, cum in delta:
        if le == float("inf"):
            # Reached the overflow bucket — any samples between
            # ``prev_le`` and infinity have unknown latency, but if
            # ``prev_le >= threshold_seconds`` they're all already
            # past the deadline. Otherwise treat them conservatively
            # as past-deadline too (better to flag a slow tail than
            # mask it).
            if not finalised:
                cum_at_threshold = prev_cum
                finalised = True
            break
        if le >= threshold_seconds and not finalised:
            span_cum = cum - prev_cum
            if span_cum <= 0 or le <= prev_le:
                cum_at_threshold = prev_cum
            else:
                frac = (threshold_seconds - prev_le) / (le - prev_le)
                cum_at_threshold = prev_cum + frac * span_cum
            finalised = True
        prev_le, prev_cum = le, cum
    if not finalised:
        # Threshold above every finite bucket edge → every observed
        # attempt completed within the deadline.
        return 0.0
    rate = 1.0 - (cum_at_threshold / total)
    if rate < 0.0:
        return 0.0
    if rate > 1.0:
        return 1.0
    return rate


# -----------------------------------------------------------------------
# Build the 18-feature observation vector
# -----------------------------------------------------------------------


def compose_metrics_from_envoy(
    counter_delta: dict[str, int],
    p95_seconds: float,
    window_sec: float,
    *,
    attempt_timeout_ms: float = 3000.0,
    gauges: dict[str, int] | None = None,
    percent: float = 0.0,
    min_retry_concurrency: int = 0,
    per_profile_delta: dict[str, dict[str, int]] | None = None,
    deadline_rate_override: float | None = None,
    median_latency_seconds: float | None = None,
    per_profile_summary: dict[str, dict[str, int]] | None = None,
    callee_aggregate: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Reconstruct the same per-tick ``metrics`` dict the rows / buckets
    paths produce.

    Mirrors ``rl_controller.compose_observation_from_buckets``'s metrics
    output so ``rl_controller.build_observation`` can run the rest of the
    pipeline unchanged.

    ``p95_seconds`` is the per-window p95 latency in seconds, already
    extracted from the snapshot's histogram by :func:`p95_from_envoy`.
    Keeping it as a positional argument (instead of recomputing from
    the snapshot inside this function) makes the unit tests trivial and
    matches the controller's "build deltas, then compose" code shape
    for the rows / buckets paths.

    ``attempt_timeout_ms`` divides the per-window p95 so the resulting
    feature matches what the training environment sees (sim's per-attempt
    deadline). The clamp at 1.0 is dropped — VecNormalize standardises
    the raw distribution.

    ``gauges`` is the scrape-time snapshot of the live concurrency
    gauges (``upstream_rq_active``, optionally
    ``upstream_rq_pending_active`` / ``upstream_rq_retry_active`` when
    Envoy publishes them). The first IS exposed on caller-side
    ``cluster.outbound.*`` stats; the latter two are NOT in current
    Envoy/Istio versions on this stack — verified empirically on the
    live frontend sidecar. Combined with ``percent`` and
    ``min_retry_concurrency`` they drive #12 (budget_utilization) and
    #13 (retry_pressure_vs_limit). When ``gauges`` is empty the function
    omits those keys and the rate-form fallback in
    ``rl_controller.build_observation`` kicks in.

    ``per_profile_delta`` carries per-``x-rl-profile`` counter deltas
    extracted from labelled raw Envoy counters
    (``envoy_cluster_upstream_rq{rl_profile=...}``). These do not exist
    in the standard Istio Telemetry CR output — the CR labels the
    ``istio_requests_total`` family, not the raw cluster.outbound
    family. Kept here for symmetry with the loader-buckets path; in
    the Envoy path we pass ``per_profile_summary`` instead.

    ``per_profile_summary`` is the per-``rl_profile`` aggregate from
    ``istio_requests_total`` (a separate fetch, see
    :func:`http_fetch_istio_requests` in ``rl_controller.py``). Each
    profile entry has at least ``{"total": <attempts>,
    "success": <2xx_attempts>}``. Used to derive
    :feature:`min_client_success` and a fairness-gap proxy when ≥ 2
    profiles are observed. When absent, the aggregate counter is
    reused for both (back-compat with stacks where the Telemetry CR
    isn't applied or the loaders don't set the ``x-rl-profile``
    header).

    ``deadline_rate_override``: when provided, replaces the
    counter-based ``timeouts / total`` computation. Pre-computed by
    the controller via :func:`deadline_rate_from_envoy` so this
    module stays unit-testable without histogram threading.

    ``median_latency_seconds``: the p50 of the per-window latency
    histogram. When non-zero it enables a Little's-law estimate of
    retry concurrency (retry_rps × mean_retry_latency) for slot 12;
    when zero the gauge-based formula is used (which on real Envoy
    deploys today is also ~0 because the retry-active gauge isn't
    published — the controller logs ``budget_utilization`` source as
    ``littles_law_estimate`` vs ``envoy_gauge_concurrency``).

    ``callee_aggregate``: per-window ``{"total", "success",
    "server_failures"}`` from :func:`parse_istio_requests_total`'s
    ``callee_aggregate`` view. When provided AND ``total > 0``, this
    overrides slot 0 (``success_rate_agg``) and slot 8
    (``server_fail_rate``) with the gRPC-aware versions, because the
    raw Envoy ``upstream_rq_{2xx,5xx}`` counters classify by HTTP
    code only and miss gRPC application errors (status 14 UNAVAILABLE
    wrapped in HTTP 200). Slot 10 (``delta_success_agg``) gets the
    same fix transparently via the slot-0 delta logic in
    ``build_observation``. When absent or empty the function falls
    back to the HTTP-only counters; the quality dict records which
    path fired (``istio_grpc_aware`` vs ``envoy_http_class``).

    NOTE: slot 4 (``window_retry_efficiency``) still uses the raw
    Envoy ``upstream_rq_retry_success`` counter — that's also HTTP-
    only, but ``istio_requests_total`` has no clean "this row was a
    retry" marker we can sum on, so a gRPC-aware fix would require
    additional sidecar instrumentation. Left as a known limitation;
    the bias is similar in shape (a few % overcounting) but the slot
    isn't a primary control input for the model.
    """
    total = max(int(counter_delta.get("upstream_rq_total", 0)), 1)
    completed = max(int(counter_delta.get("upstream_rq_completed", 0)), 1)
    successes = int(counter_delta.get("upstream_rq_2xx", 0))
    server_failures = int(counter_delta.get("upstream_rq_5xx", 0))
    timeouts = int(counter_delta.get("upstream_rq_timeout", 0))
    retries = int(counter_delta.get("upstream_rq_retry", 0))
    retry_successes = int(counter_delta.get("upstream_rq_retry_success", 0))
    retry_overflow = int(counter_delta.get("upstream_rq_retry_overflow", 0))

    # Per-attempt success: 2xx / completed (every completed attempt
    # contributes, retries included). Matches sim's per-attempt reward.
    # Default formula uses Envoy's HTTP-only counters; overridden below
    # by the gRPC-aware ``callee_aggregate`` view when available.
    success_rate = successes / completed
    success_rate_source = "envoy_http_class"
    server_fail_rate = server_failures / total
    server_fail_source = "envoy_http_class"

    if callee_aggregate:
        ca_total = int(callee_aggregate.get("total", 0) or 0)
        if ca_total > 0:
            ca_success = int(callee_aggregate.get("success", 0) or 0)
            ca_server_fail = int(
                callee_aggregate.get("server_failures", 0) or 0
            )
            success_rate = ca_success / ca_total
            success_rate_source = "istio_grpc_aware"
            server_fail_rate = ca_server_fail / ca_total
            server_fail_source = "istio_grpc_aware"

    retry_ratio = retries / total
    # Bug fix per obs.md #4: load_amplification = attempts /
    # first_attempts. ``total`` counts every attempt and ``retries``
    # counts the subset that were retries, so ``total - retries`` is
    # first attempts. The previous formula divided by ``completed``,
    # which is the number of *root* requests in the rows path but is
    # actually the number of *finalised attempts* in Envoy (which equals
    # total for a healthy cluster), pinning the ratio at ~1.0.
    raw_total = int(counter_delta.get("upstream_rq_total", 0))
    first_attempts = max(raw_total - retries, 1)
    load_amplification = raw_total / first_attempts
    retry_efficiency = retry_successes / max(retries, 1) if retries else 0.0
    # p95 / attempt_timeout, no clamp. Training distribution is unclipped
    # and VecNormalize takes care of the dynamic range.
    latency_pressure = (p95_seconds * 1000.0) / max(attempt_timeout_ms, 1e-9)

    # Pseudo-RPS, derived from the configured window. The buckets path
    # does the same: it doesn't have wall-clock per-attempt data, just
    # per-window aggregates.
    retry_rps = retries / max(window_sec, 1.0)
    request_rps = (
        int(counter_delta.get("upstream_rq_completed", 0)) / max(window_sec, 1.0)
    )

    # Per-profile per-attempt success and first-attempt-share, computed
    # from labelled counters when the Istio Telemetry CR is applied.
    # The caller-side "first attempts" denominator is ``total - retries``
    # — the same identity used for load_amplification — applied per
    # profile so loaders below their target RPS still report a fair
    # share. See obs.md feature #6 for rationale.
    #
    # ``per_profile_delta`` (raw Envoy cluster.outbound counters with
    # ``rl_profile`` label) takes precedence when present, because it
    # gives us exact first-attempt vs retry counts per profile and we
    # can compute the simulator's fairness_gap formula directly.
    # ``per_profile_summary`` (Istio requests_total per profile) is the
    # production reality on this stack — the Telemetry CR labels
    # istio_requests_total but not cluster.outbound, and Istio rows
    # don't carry a retry flag, so we approximate fairness from per-
    # profile success-rate inequality. Different formula, same target
    # phenomenon (one client class being treated worse than others).
    min_client_success = success_rate
    min_client_success_source = "clone_success_rate_agg"
    fairness_gap = 0.0
    fairness_source = "stubbed_no_per_profile_label"
    if per_profile_delta:
        profile_success_rates: list[float] = []
        profile_first_attempts: dict[str, int] = {}
        profile_retries: dict[str, int] = {}
        for prof, cd in per_profile_delta.items():
            prof_completed = max(int(cd.get("upstream_rq_completed", 0)), 1)
            prof_2xx = int(cd.get("upstream_rq_2xx", 0))
            profile_success_rates.append(prof_2xx / prof_completed)
            prof_total = int(cd.get("upstream_rq_total", 0))
            prof_retries = int(cd.get("upstream_rq_retry", 0))
            profile_first_attempts[prof] = max(prof_total - prof_retries, 0)
            profile_retries[prof] = prof_retries
        if profile_success_rates:
            min_client_success = min(profile_success_rates)
            min_client_success_source = "per_profile_envoy_cluster"
        if len(per_profile_delta) > 1:
            total_first_attempts = max(sum(profile_first_attempts.values()), 1)
            total_retries_profiles = max(sum(profile_retries.values()), 1)
            names = set(profile_first_attempts) | set(profile_retries)
            fairness_gap = 0.5 * sum(
                abs(
                    profile_retries.get(n, 0) / total_retries_profiles
                    - profile_first_attempts.get(n, 0) / total_first_attempts
                )
                for n in names
            )
        fairness_source = "per_profile_envoy_cluster"
    elif per_profile_summary:
        # Filter to profiles that actually moved traffic in the window;
        # an empty/cold profile with total=0 would contribute a spurious
        # 0.0 success rate.
        active = {
            prof: counts
            for prof, counts in per_profile_summary.items()
            if int(counts.get("total", 0)) > 0
        }
        if active:
            rates = {
                prof: int(c.get("success", 0)) / int(c.get("total", 1))
                for prof, c in active.items()
            }
            if len(rates) >= 2:
                # Multi-profile branch: take min across loader classes
                # and compute the spread. Note PATH C limitation in
                # :func:`parse_istio_requests_total`'s docstring — the
                # per-profile signal here is frontend-page-load success
                # per loader class, not per-cart-call success per class.
                # OK for single-profile (handled below); for multi-
                # profile this measures frontend-level fairness rather
                # than cart-level fairness.
                min_client_success = min(rates.values())
                min_client_success_source = (
                    "istio_requests_per_profile_min"
                )
                # Half the range — keeps the same [0, 0.5] bound the
                # simulator's TVD formula has, so VecNormalize sees a
                # comparable distribution magnitude even though the
                # underlying formula differs.
                fairness_gap = 0.5 * (max(rates.values()) - min(rates.values()))
                fairness_source = "istio_requests_per_profile_success_spread"
            else:
                # Single-profile branch: the simulator's
                # min_client_success exactly equals success_rate_agg
                # when there's only one client class. The per-profile
                # rate from istio_requests_total reporter=destination
                # measures gateway → frontend success (page-load),
                # which is a DIFFERENT scope from the cart-call
                # aggregate that drives slot 0. To keep slot 1 ==
                # slot 0 in the single-profile case (as it is in the
                # simulator), clone the aggregate value instead of
                # using the frontend-scoped per-profile rate.
                # Multi-profile case stays on the per-profile branch
                # above; the Path-C limitation only bites there and
                # is documented in the spec doc.
                min_client_success = success_rate
                min_client_success_source = (
                    "clone_success_rate_agg_single_profile"
                )
                fairness_source = "istio_requests_single_profile"

    # Deadline rate (#9). Envoy's ``upstream_rq_timeout`` counter only
    # increments when its own per-try-timeout fires; with no per-try-
    # timeout on the cart route it sits at 0 even when application-level
    # latency is well past attempt_timeout_ms. The histogram-derived
    # override (see :func:`deadline_rate_from_envoy`) is the real signal.
    if deadline_rate_override is not None:
        deadline_rate = float(deadline_rate_override)
        deadline_source = "envoy_histogram_cdf"
    else:
        deadline_rate = timeouts / total
        deadline_source = "envoy_upstream_rq_timeout"

    out: dict[str, Any] = {
        "rows": raw_total,
        "requests": int(counter_delta.get("upstream_rq_completed", 0)),
        "window_sec": window_sec,
        "success_rate_agg": success_rate,
        "min_client_success": min_client_success,
        "retry_ratio": retry_ratio,
        "window_load_amplification": load_amplification,
        "window_retry_efficiency": retry_efficiency,
        "retry_fairness_gap": fairness_gap,
        "p95_latency_pressure": latency_pressure,
        "server_fail_rate": server_fail_rate,
        "deadline_rate": deadline_rate,
        "retry_rps": retry_rps,
        "request_rps": request_rps,
    }

    # Caller-side budget concurrency (#12 / #13 / #7). Two derivation
    # paths, picked by which signal Envoy actually exposes:
    #
    #   1. ``upstream_rq_retry_active`` gauge present → use it directly
    #      (the simulator's source-of-truth equivalent). This branch is
    #      effectively dead today; verified on a live frontend sidecar,
    #      the cluster.outbound.* family doesn't publish that gauge.
    #
    #   2. Little's-law estimate: ``active_retries ≈ retry_rps × mean
    #      retry latency``. We don't observe per-attempt retry latencies
    #      so we use ``median_latency_seconds`` (p50 of all attempts) as
    #      a proxy. That's biased downwards in fault scenarios (retries
    #      tend to fall in the slow tail) but it's bounded and reactive,
    #      and VecNormalize standardises the magnitude at inference time.
    #
    # The concurrency-limit denominator follows Envoy's admission rule:
    # ``max(min_retry, percent/100 × inflight)``. ``inflight`` is the
    # ``upstream_rq_active`` gauge (verified exposed on cluster.outbound).
    # ``upstream_rq_pending_active`` is also added when published —
    # currently not published on this stack so it contributes 0.
    if gauges:
        inflight = (
            int(gauges.get("upstream_rq_active", 0))
            + int(gauges.get("upstream_rq_pending_active", 0))
        )
        concurrency_limit = max(
            float(min_retry_concurrency),
            (float(percent) / 100.0) * float(inflight),
        )
        retry_active_gauge = float(gauges.get("upstream_rq_retry_active", 0))
        if retry_active_gauge > 0:
            est_retry_active = retry_active_gauge
            budget_util_source = "envoy_gauge_concurrency"
        elif median_latency_seconds and median_latency_seconds > 0:
            est_retry_active = retry_rps * float(median_latency_seconds)
            budget_util_source = "littles_law_estimate"
        else:
            # No retry-active gauge AND no median latency (cold start /
            # empty histogram). Leave the slot at 0 so build_observation
            # marks it as approximated rather than silently lying.
            est_retry_active = 0.0
            budget_util_source = "no_retry_signal_available"
        out["concurrency_limit"] = concurrency_limit
        out["budget_utilization"] = min(
            1.0, est_retry_active / max(concurrency_limit, 1e-9)
        )
        out["retry_pressure_vs_limit"] = (
            (retries / max(window_sec, 1e-9)) / max(concurrency_limit, 1e-9)
        )
        retry_attempted = retries + retry_overflow
        out["budget_reject_rate"] = retry_overflow / max(retry_attempted, 1)
    else:
        budget_util_source = "no_envoy_gauges_available"

    out["quality"] = {
        "client_metrics": (
            "envoy_sidecar"
            if raw_total > 0
            else "envoy_sidecar_empty_window"
        ),
        "success_rate_agg": success_rate_source,
        "min_client_success": min_client_success_source,
        "server_fail_rate": server_fail_source,
        "retry_fairness_gap": fairness_source,
        "deadline_rate": deadline_source,
        "budget_utilization": budget_util_source,
    }
    return out


# Back-compat alias: older callers (and the controller's import block)
# still reference ``parse_envoy_stats_json``. The pilot-agent endpoint
# emits Prometheus text, so the alias points at the new parser; the
# function name in the controller is irrelevant to the wire format.
parse_envoy_stats_json = parse_envoy_stats_prom


__all__ = [
    "ENVOY_COUNTER_KEYS",
    "ENVOY_GAUGE_KEYS",
    "ENVOY_HIST_KEY",
    "parse_envoy_stats_prom",
    "parse_envoy_stats_json",
    "parse_istio_requests_total",
    "aggregate_pod_snapshots",
    "aggregate_istio_per_profile",
    "aggregate_istio_snapshots",
    "delta_istio_per_profile",
    "delta_istio_snapshots",
    "delta_counters",
    "delta_hist",
    "quantile_from_envoy",
    "p95_from_envoy",
    "median_latency_from_envoy",
    "deadline_rate_from_envoy",
    "per_profile_delta",
    "compose_metrics_from_envoy",
]
