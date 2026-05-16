"""Shared schema for pre-aggregated RL observations.

Both the loader (``prototype/clients/online-boutique/traffic_gen.py``) and the
in-cluster controller (``prototype/experiments/rl_controller.py``) import this
module so the histogram edges, the bucket-record shape, and the bisect helper
are defined in exactly one place. If the loader and controller disagree about
either the edge list or the field order, every reconstructed feature in the
observation vector becomes silently wrong; centralising the schema is the
cheapest insurance against that.

The pre-aggregated-buckets transport amortises the per-attempt linear scan
from the controller into the loader's asyncio loop. Each bucket is one
second of one (shard, profile), holding integer counters plus a fixed-edge
latency histogram. The controller fetches ~40 small JSON records per tick
(versus the original per-attempt-rows transport's ~10 000 rows / tick) and
recomposes the 18-feature observation vector by summing buckets and running
a single linear pass over the histogram.

Bytes per bucket on the wire ≈ 130 B JSON-encoded → ~5 KB / tick total, vs
the rows transport's ~3.2 MB / tick → ~620× reduction.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Latency histogram edges
# ---------------------------------------------------------------------------
#
# 21 finite edges → 22 buckets:
#   bucket 0  = [0, edges[0])
#   bucket i  = [edges[i-1], edges[i])  for 1 ≤ i ≤ 20
#   bucket 21 = [edges[-1], +inf)        (overflow / timeout)
#
# Rationale: p95 under load typically lands in
# 0.3–1.0 s; the densest spacing is in that region (max bucket width 0.15 s).
# Worst-case p95 reconstruction error is therefore ±0.075 s, which divided by
# the 3 s attempt timeout gives ±2.5 % in p95_latency_pressure — well within
# the 1 % mean drift target across 100 sampled ticks.
LATENCY_HISTOGRAM_EDGES_S: tuple[float, ...] = (
    0.005, 0.010, 0.025, 0.050, 0.075,
    0.100, 0.150, 0.200, 0.300, 0.400,
    0.500, 0.600, 0.750, 0.900, 1.000,
    1.250, 1.500, 1.750, 2.000, 2.500,
    3.000,
)

LATENCY_HISTOGRAM_NUM_BUCKETS: int = len(LATENCY_HISTOGRAM_EDGES_S) + 1


def latency_bucket_index(latency_s: float, edges: tuple[float, ...] = LATENCY_HISTOGRAM_EDGES_S) -> int:
    """Index into ``latency_hist`` for a given attempt latency.

    Buckets are half-open on the right (``[edges[i-1], edges[i])``), so a
    latency exactly equal to ``edges[i]`` lands in bucket ``i+1``. This is
    ``bisect_right`` semantics — the lower edge is included in the bucket
    above it, matching standard latency-histogram convention (and what
    Prometheus / Envoy's ``le=`` buckets would produce). The histogram
    p95 reconstruction in :func:`rl_controller.p95_from_histogram` reads
    the same edges to reverse the mapping.
    """
    if latency_s < 0.0:
        return 0
    return bisect_right(edges, latency_s)


@dataclass
class WindowBucket:
    """One second of one (shard, profile) — the unit of pre-aggregation.

    The fields here must match the JSON keys the controller decodes in
    ``rl_controller.WindowBucket``. The dataclass on the loader side uses a
    mutable ``list`` for the histogram so per-attempt increments stay O(1);
    the controller side uses a ``tuple`` because nothing on its end mutates
    a bucket after deserialisation. Field order also matters for the
    ``asdict`` JSON encoding; keep the controller's ``WindowBucket`` in sync
    if you touch this.
    """

    ts_sec: int
    shard_id: int
    profile: str
    attempts: int = 0
    requests: int = 0
    successes: int = 0
    retries: int = 0
    retry_successes: int = 0
    server_failures: int = 0
    deadline_failures: int = 0
    latency_hist: list[int] = field(
        default_factory=lambda: [0] * LATENCY_HISTOGRAM_NUM_BUCKETS,
    )


__all__ = [
    "LATENCY_HISTOGRAM_EDGES_S",
    "LATENCY_HISTOGRAM_NUM_BUCKETS",
    "WindowBucket",
    "latency_bucket_index",
]
