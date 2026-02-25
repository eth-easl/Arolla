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


@dataclass
class _SrvRetryCtx:
    attempt: int
    global_deadline: Optional[TimePoint]
    on_attempt_done: Callable[
        [bool, TimeDuration, DropReason, int, TimePoint, Optional[TimePoint]], None
    ]
    on_root_done: Callable[[], None]


@dataclass
class ServiceRuntime(
    _ServiceMiddlewareMixin,
    _ServiceAttemptMixin,
    _ServiceDependencyMixin,
    _ServiceTimingMixin,
):
    cfg: ServiceConfig
    in_flight: int = 0
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

    @property
    def dependency(self) -> Optional["ServiceRuntime"]:
        return self.dependencies[0] if self.dependencies else None

    def bind(self, seed: Optional[int] = None, record_events: bool = False):
        self.in_flight = 0
        self.queue.clear()
        self._seq = 0
        self._middleware_chain = self._build_middleware_chain()
        self._record_events = record_events
        self._events = []
        self._rng = random.Random(seed if seed is not None else 0)
        return self

    @property
    def events(self) -> List[tuple]:
        """(timestamp_ns, latency_ns, success, drop_reason, queue_size, attempt_num, is_retry)."""
        return self._events
