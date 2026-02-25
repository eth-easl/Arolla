from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Tuple

from simulator.core.types import TimeDuration, TimePoint
from simulator.policies.retry import RetryContext


@dataclass(kw_only=True)
class LoadLimiter(ABC):
    """
    Generic load limiting policies (non-retry-specific traffic shaping/admission).

    Retry-specific token buckets and circuit breakers live in `retry_controls.py`.
    """

    @abstractmethod
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        pass

    def applies_pre_queue_admission(self, is_retry: bool) -> bool:
        return False


class NoLoadLimiter(LoadLimiter):
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        return True, 0


@dataclass
class LeakyRateLimiterPolicy(LoadLimiter):
    max_requests: int
    max_attempts: int
    period: TimeDuration

    _next_at: Optional[TimePoint] = field(default=None, init=False)

    def add_request(self, now: TimePoint) -> TimeDuration:
        start_at = max(now, self._next_at or now)
        delay = max(0, start_at - now)
        self._next_at = round(start_at + self.period / self.max_requests)
        return delay

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            raise ValueError("LeakyRateLimiterPolicy requires context.now to be set")
        if context.attempt > self.max_attempts:
            return False, 0
        return True, self.add_request(context.now)


@dataclass
class BurstyRateLimiterPolicy(LoadLimiter):
    max_requests: int
    refill_rate: int
    period: TimeDuration

    _tokens: int = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = self.max_requests

    def _refill_tokens(self, now: TimePoint) -> None:
        if self._last_refill is None:
            self._last_refill = now
            return
        time_elapsed = now - self._last_refill
        if time_elapsed > 0 and self.period > 0:
            tokens_to_add = int((time_elapsed / self.period) * self.refill_rate)
            self._tokens = min(self.max_requests, self._tokens + tokens_to_add)
        self._last_refill = now

    def can_retry(self, now: TimePoint) -> bool:
        self._refill_tokens(now)
        return self._tokens > 0

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            raise ValueError("BurstyRateLimiterPolicy requires context.now to be set")
        if self.can_retry(context.now):
            self._tokens -= 1
            return True, 0
        return False, 0


@dataclass
class FixedWindowBurstyLimiterPolicy(LoadLimiter):
    max_requests: int
    period: TimeDuration

    _tokens: int = field(init=False)
    _window_start: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = self.max_requests

    def _get_current_window_start(self, now: TimePoint) -> TimePoint:
        return (now // self.period) * self.period

    def _refill_tokens(self, now: TimePoint) -> None:
        current_window_start = self._get_current_window_start(now)
        if self._window_start is None or current_window_start > self._window_start:
            self._window_start = current_window_start
            self._tokens = self.max_requests

    def can_retry(self, now: TimePoint) -> bool:
        self._refill_tokens(now)
        return self._tokens > 0

    def get_next_refill_time(self, now: TimePoint) -> TimePoint:
        return self._get_current_window_start(now) + self.period

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            raise ValueError("FixedWindowBurstyLimiterPolicy requires context.now to be set")
        if self.can_retry(context.now):
            self._tokens -= 1
            return True, 0
        return False, 0
