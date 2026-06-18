from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.faults.events import FaultEventsTracker
from simulator.faults.injection import LatencyInjection, PartialFailure
from simulator.middleware.base import MiddlewareChain
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryPolicy
from simulator.policies.timeout import Timeout

from .service_attempts import _ServiceAttemptMixin
from .service_dependencies import _ServiceDependencyMixin
from .service_middleware import _ServiceMiddlewareMixin
from .service_timing import _ServiceTimingMixin

# RL extension: live telemetry + runtime controls consumed by the RL environments.
# These are inert unless an RL env enables the live buffer or mutates a policy.
from simulator.metrics.live_buffer import LiveMetricsBuffer
from simulator.policies.istio_retry_budget import IstioRetryBudget
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.retry_controls import GlobalRetryBudget, LimiterRetryBudgetPolicy


@dataclass(frozen=True)
class ServiceConfig:
    name: str

    latency_median: TimeDuration
    latency_lognorm_sigma: float
    workers: int
    queue_capacity: Optional[int] = None
    latency_injections: List[LatencyInjection] = field(default_factory=list)
    partial_failures: List[PartialFailure] = field(default_factory=list)

    load_limiter: Optional[LoadLimiter] = None
    timeout: Optional[Timeout] = None
    retry: Optional[RetryPolicy] = None

    def register_fault_events(self, tracker: FaultEventsTracker):
        for latency_inj in self.latency_injections:
            tracker.add_latency_injection(
                latency_inj.duration, latency_inj.add_latency, latency_inj.multiplier
            )
        for partial_failure in self.partial_failures:
            tracker.add_partial_failure(
                partial_failure.duration, partial_failure.p_fail
            )


@dataclass(order=True)
class QItem:
    enqueued_at: TimePoint
    seq: int
    start: Callable[[], None] = field(compare=False)
    # RL: track whether the queued attempt is a retry so the service can expose
    # retry concurrency to Istio-style budgets.
    is_retry: bool = field(default=False, compare=False)


@dataclass
class _SrvRetryCtx:
    attempt: int
    global_deadline: Optional[TimePoint]
    on_attempt_done: Callable[
        [bool, TimeDuration, DropReason, int, TimePoint, Optional[TimePoint]], None
    ]
    on_root_done: Callable[[], None]
    retry_budget_remaining: Optional[List[int]] = None  # Arolla Level 2: shared mutable [B]
    tenant_id: Optional[str] = None
    # RL: a caller-supplied (client-managed) retry stays a "retry" for its whole
    # service-side lifecycle, even though its local attempt counter starts at 1.
    external_is_retry: bool = False


@dataclass
class ServiceRuntime(
    _ServiceMiddlewareMixin,
    _ServiceAttemptMixin,
    _ServiceDependencyMixin,
    _ServiceTimingMixin,
):
    cfg: ServiceConfig
    in_flight: int = 0
    # RL: retry concurrency counters used to drive Istio-style retry budgets.
    in_flight_retries: int = 0
    queued_retries: int = 0
    queue: List[QItem] = field(default_factory=list)
    _seq: int = 0

    dependencies: List["ServiceRuntime"] = field(default_factory=list)
    dependency_optionality: List[bool] = field(default_factory=list)
    dependency_call_pattern: str = "sequential"

    _middleware_chain: Optional[MiddlewareChain] = field(
        default=None, init=False, repr=False
    )
    _rng: Optional[random.Random] = field(default=None, init=False, repr=False)

    _events: List[tuple] = field(default_factory=list, init=False, repr=False)
    _record_events: bool = field(default=False, init=False, repr=False)

    # RL: per-service live metrics buffer for real-time aggregation (off by default).
    _live_buffer: Optional[LiveMetricsBuffer] = field(default=None, init=False, repr=False)

    @property
    def dependency(self) -> Optional["ServiceRuntime"]:
        return self.dependencies[0] if self.dependencies else None

    def bind(self, seed: Optional[int] = None, record_events: bool = False):
        from collections import defaultdict

        self.in_flight = 0
        self.in_flight_retries = 0
        self.queued_retries = 0
        self.queue.clear()
        self._seq = 0
        self._middleware_chain = self._build_middleware_chain()
        self._record_events = record_events
        self._events = []
        # RL: live buffer starts disabled; RL envs call enable_live_buffer() after bind.
        self._live_buffer = None
        self._rng = random.Random(seed if seed is not None else 0)
        # Pre-queue admission counters (used by _ServiceAttemptMixin.submit_request)
        self._admission_requested = defaultdict(int)
        self._admission_admitted = defaultdict(int)
        return self

    @property
    def events(self) -> List[tuple]:
        """(timestamp_ns, latency_ns, success, drop_reason, queue_size, attempt_num, is_retry)."""
        return self._events

    def get_admission_stats(self) -> dict:
        """Get per-tenant retry admission stats from pre-queue admission check.

        Returns dict mapping tenant_id -> {'requested': N, 'admitted': M},
        or empty dict if no load limiter is configured.
        """
        if not hasattr(self, '_admission_requested'):
            return {}
        result = {}
        for tenant in self._admission_requested:
            result[tenant] = {
                'requested': self._admission_requested[tenant],
                'admitted': self._admission_admitted.get(tenant, 0),
            }
        return result

    # ------------------------------------------------------------------
    # RL extension: live telemetry
    # ------------------------------------------------------------------
    @property
    def live_buffer(self) -> Optional[LiveMetricsBuffer]:
        return self._live_buffer

    def enable_live_buffer(self) -> None:
        """Activate the live metrics buffer (used by RL envs). No-op if already active."""
        if self._live_buffer is None:
            self._live_buffer = LiveMetricsBuffer()

    def _record_live_metrics(
        self,
        timestamp_ns: TimePoint,
        latency_ns: TimeDuration,
        success: bool,
        drop_reason: DropReason,
        queue_size: int,
        attempt_num: int,
        is_retry: bool,
    ) -> None:
        """Record a finished or admission-rejected attempt into the live buffer.

        This is RL-only telemetry and is independent of the static ``_events``
        log consumed by the metrics collector.
        """
        if self._live_buffer is not None:
            self._live_buffer.record_event(
                timestamp_ns,
                latency_ns,
                success,
                drop_reason,
                queue_size,
                attempt_num,
                is_retry,
            )

    def _refresh_retry_budget_runtime_state(self) -> None:
        """Expose current service concurrency to Istio-style retry budgets."""
        limiter = self.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            limiter.update_runtime_state(
                active_requests=self.in_flight,
                pending_requests=len(self.queue),
                active_retries=self.in_flight_retries + self.queued_retries,
            )

    # ------------------------------------------------------------------
    # RL extension: runtime policy controls (mutated by RL envs between steps)
    # ------------------------------------------------------------------
    def update_retry_config(
        self,
        max_attempts: Optional[int] = None,
        delay_ns: Optional[TimeDuration] = None,
        budget_ratio: Optional[float] = None,
        budget_max_retries: Optional[int] = None,
    ):
        """Swap policy objects in the middleware chain at runtime."""
        if self._middleware_chain is None:
            return

        from simulator.middleware.load_limiter import LoadLimiterMiddleware
        from simulator.middleware.retry import RetryMiddleware

        for mw in self._middleware_chain.middlewares:
            if isinstance(mw, RetryMiddleware) and (max_attempts is not None or delay_ns is not None):
                old = mw.policy
                if isinstance(old, FixedBackoffRetryPolicy):
                    mw.policy = FixedBackoffRetryPolicy(
                        max_attempts=max_attempts if max_attempts is not None else old.max_attempts,
                        delay=delay_ns if delay_ns is not None else old.delay,
                    )

            if isinstance(mw, LoadLimiterMiddleware) and (budget_ratio is not None or budget_max_retries is not None):
                old = mw.limiter
                if isinstance(old, LimiterRetryBudgetPolicy):
                    mw.limiter = LimiterRetryBudgetPolicy(
                        budget_ratio=budget_ratio if budget_ratio is not None else old.budget_ratio,
                        max_retries=budget_max_retries if budget_max_retries is not None else old.max_retries,
                    )

    def update_token_bucket(self, refill_rate=None, bucket_capacity=None):
        """Update the token bucket parameters at runtime."""
        limiter = self.cfg.load_limiter
        if isinstance(limiter, GlobalRetryBudget):
            if refill_rate is not None:
                limiter.refill_rate = refill_rate
            if bucket_capacity is not None:
                limiter.max_tokens = bucket_capacity
                limiter._tokens = min(limiter._tokens, float(bucket_capacity))

    def update_istio_retry_budget(self, percent=None, min_retry_concurrency=None):
        """Update Istio-style retry budget parameters at runtime."""
        limiter = self.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            limiter.update_params(
                percent=percent,
                min_retry_concurrency=min_retry_concurrency,
            )
