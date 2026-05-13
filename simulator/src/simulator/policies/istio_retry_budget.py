from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from simulator.core.types import TimeDuration, TimePoint
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryContext


@dataclass
class IstioRetryBudget(LoadLimiter):
    """Server-side retry budget with Istio/Envoy-style concurrency semantics.

    Istio's retry budget limits concurrent retries to a percentage of active and
    pending requests, while preserving a small minimum retry concurrency:

        max(min_retry_concurrency, percent * (active + pending))

    The service runtime refreshes the active/pending counters before asking this
    limiter whether a retry should be admitted.
    """

    percent: float = 20.0
    min_retry_concurrency: int = 3

    active_requests: int = 0
    pending_requests: int = 0
    active_retries: int = 0

    def update_params(
        self,
        percent: Optional[float] = None,
        min_retry_concurrency: Optional[int] = None,
    ) -> None:
        if percent is not None:
            self.percent = max(0.0, float(percent))
        if min_retry_concurrency is not None:
            self.min_retry_concurrency = max(0, int(min_retry_concurrency))

    def update_runtime_state(
        self,
        *,
        active_requests: int,
        pending_requests: int,
        active_retries: int,
    ) -> None:
        self.active_requests = max(0, int(active_requests))
        self.pending_requests = max(0, int(pending_requests))
        self.active_retries = max(0, int(active_retries))

    @property
    def concurrency_limit(self) -> float:
        total_active_or_pending = self.active_requests + self.pending_requests
        percent_limit = (self.percent / 100.0) * total_active_or_pending
        return max(float(self.min_retry_concurrency), percent_limit)

    @property
    def utilization(self) -> float:
        limit = self.concurrency_limit
        return float(self.active_retries / limit) if limit > 0 else 0.0

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        # This limiter is concurrency-based, so completed attempts do not refill
        # tokens or alter a moving window.
        pass

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        return self.active_retries < self.concurrency_limit, 0
