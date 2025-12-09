from dataclasses import dataclass
from typing import Tuple

from simulator.core.types import TimeDuration
from simulator.policies.retry import RetryContext

from abc import ABC, abstractmethod

@dataclass(kw_only=True)
class LoadLimiter(ABC):
    """
    Base class for load limiting policies (circuit breakers, rate limiters, retry budgets).
    
    NOTE: With the middleware pattern, LoadLimiters no longer own retry logic.
    They only decide whether to ALLOW a retry, not the retry delay itself.
    The retry delay comes from RetryPolicy middleware.
    """
    
    @abstractmethod
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        """
        Decide whether to allow retry and optional additional delay.
        
        Returns:
            (should_allow, additional_delay): If should_allow is False, retry is blocked.
                                              If True, additional_delay can add to retry delay.
        """
        pass

class NoLoadLimiter(LoadLimiter):
    """Load limiter that allows all retries"""
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        return True, 0  # Allow retry with no additional delay

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional, Tuple

from simulator.core.types import TimeDuration, TimePoint


class CBState(Enum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2  # Probing stage not implemented properly


@dataclass
class CountBasedCircuitBreakerPolicy(LoadLimiter):
    # e.g. if you have 5 failures out of 10 calls, the circuit opens
    failure_threshold_ratio: Tuple[int, int]  # (failures, total)
    # e.g. if you have 3 successes out of 5 calls, the circuit closes
    success_threshold_ratio: Tuple[int, int]  # (successes, total)
    half_open_delay: TimeDuration

    _state: CBState = CBState.CLOSED
    _closed_window: Deque[bool] = field(init=False)
    _open_window: Deque[bool] = field(init=False)
    _opened_at: Optional[TimePoint] = None

    def __post_init__(self):
        f, ftot = self.failure_threshold_ratio
        s, stot = self.success_threshold_ratio
        assert 0 <= f <= ftot and ftot > 0
        assert 0 <= s <= stot and stot > 0
        # Note: deques with maxlen will drop old entries when new ones are added
        self._closed_window = deque(maxlen=ftot)
        self._open_window = deque(maxlen=stot)

    def get_state(self) -> CBState:
        return self._state

    def add_result(self, success: bool, now: TimePoint) -> None:
        if self._state == CBState.CLOSED:
            self._closed_window.append(success)
            if len(self._closed_window) == self._closed_window.maxlen:
                failures = sum(1 for ok in self._closed_window if not ok)
                if failures >= self.failure_threshold_ratio[0]:
                    self._state = CBState.OPEN
                    self._open_window.clear()
                    if now is not None:
                        self._opened_at = now
        elif self._state == CBState.OPEN:
            self._open_window.append(success)
            # Only evaluate closing after cooldown (if any)
            if (
                self._opened_at is None
                or self.half_open_delay == 0
                or now - self._opened_at >= self.half_open_delay
            ):
                if len(self._open_window) == self._open_window.maxlen:
                    successes = sum(1 for ok in self._open_window if ok)
                    if successes >= self.success_threshold_ratio[0]:
                        self._state = CBState.CLOSED
                        self._closed_window.clear()
                        self._opened_at = None

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if self._state == CBState.OPEN:
            return False, 0  # Circuit open, block retry
        return True, 0  # Circuit closed, allow retry


@dataclass
class TimeBasedCircuitBreakerPolicy(LoadLimiter):
    failure_threshold_rate: float  # percentage of failures to open the circuit
    success_threshold_rate: float  # percentage of successes to close the circuit
    min_requests: int  # Minimum requests in the window to evaluate
    window_duration: TimeDuration
    half_open_delay: TimeDuration

    _window: Deque[Tuple[TimePoint, bool]] = field(
        default_factory=deque
    )  # (timestamp, success)
    _state: CBState = CBState.CLOSED
    _opened_at: Optional[TimePoint] = None

    def _evict_old(self, now: TimePoint) -> None:
        while self._window and (now - self._window[0][0]) > self.window_duration:
            self._window.popleft()

    def get_state(self) -> CBState:
        return self._state

    def add_result(self, success: bool, now: TimePoint) -> None:
        self._evict_old(now)
        self._window.append((now, success))

        total = len(self._window)
        failures = sum(1 for _, ok in self._window if not ok)
        rate = failures / total if total > 0 else 0.0

        if self._state == CBState.CLOSED:
            if total >= self.min_requests and rate > self.failure_threshold_rate:
                self._state = CBState.OPEN
                self._opened_at = now
        elif self._state == CBState.OPEN:
            if (
                self._opened_at is None
                or self.half_open_delay == 0
                or now - self._opened_at >= self.half_open_delay
            ):
                if total >= self.min_requests and rate < (
                    1 - self.success_threshold_rate
                ):
                    self._state = CBState.CLOSED
                    self._opened_at = None

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if self._state == CBState.OPEN:
            return False, 0  # Circuit open, block retry
        return True, 0  # Circuit closed, allow retry


# By leaky we mean the leaky bucket as a queue version.
@dataclass
class LeakyRateLimiterPolicy(LoadLimiter):
    max_requests: int  # number of max requests in the period
    max_attempts: int  # number of max attempts
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
        delay = self.add_request(context.now)
        return True, delay


@dataclass
class RetryBudgetPolicy(LoadLimiter):
    budget_ratio: (
        float  # retries can be at most this ratio of successes (e.g., 0.1 for 10%)
    )
    max_retries: int  # maximum consecutive retries before throttling

    # Derived token values, calculated from budget_ratio and max_retries
    _max_tokens: int = field(init=False)
    _success_award: int = field(init=False)
    _retry_cost: int = field(init=False)

    _tokens: float = field(init=False)

    def __post_init__(self):
        # Derive token values from budget_ratio and max_retries
        # If budget_ratio = 0.1 (10%), then 10 successes should allow 1 retry
        # So success_award = 10, retry_cost = 100
        self._retry_cost = 100  # Base cost for a retry
        self._success_award = int(self._retry_cost * self.budget_ratio)
        self._max_tokens = self._retry_cost * self.max_retries

        self._tokens = self._max_tokens  # start with full budget

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        # Add tokens for a successful request
        if success:
            self._tokens = min(self._max_tokens, self._tokens + self._success_award)

    def can_retry(self) -> bool:
        return self._tokens >= self._retry_cost

    def get_token_balance(self) -> float:
        return self._tokens

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if self.can_retry():
            self._tokens -= self._retry_cost
            return True, 0  # Budget available, allow retry
        return False, 0  # Budget exhausted, block retry


# Watch out for float vs int issues with tokens
@dataclass
class BurstyRateLimiterPolicy(LoadLimiter):
    max_requests: int  # maximum requests in the bucket
    refill_rate: int  # requests per nanosecond to refill bucket
    period: TimeDuration  # time period for refill calculations

    _tokens: int = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = self.max_requests

    def _refill_tokens(self, now: TimePoint) -> None:
        if self._last_refill is None:
            self._last_refill = now
            return

        time_elapsed = now - self._last_refill
        tokens_to_add = int(time_elapsed * self.refill_rate)  # funky rounding happening
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
            return True, 0  # Tokens available, allow retry
        return False, 0  # No tokens, block retry


@dataclass
class FixedWindowBurstyLimiterPolicy(LoadLimiter):
    max_requests: int  # maximum executions per period
    period: TimeDuration

    _tokens: int = field(init=False)
    _window_start: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = self.max_requests  # start with full tokens

    def _get_current_window_start(self, now: TimePoint) -> TimePoint:
        return (now // self.period) * self.period

    def _refill_tokens(self, now: TimePoint) -> None:
        current_window_start = self._get_current_window_start(now)

        if self._window_start is None or current_window_start > self._window_start:
            # new window, reset tokens
            self._window_start = current_window_start
            self._tokens = self.max_requests

    def can_retry(self, now: TimePoint) -> bool:
        self._refill_tokens(now)
        return self._tokens > 0

    def get_next_refill_time(self, now: TimePoint) -> TimePoint:
        current_window_start = self._get_current_window_start(now)
        return current_window_start + self.period

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            raise ValueError("FixedWindowBurstyLimiterPolicy requires context.now to be set")

        self._refill_tokens(context.now)

        if self.can_retry(context.now):
            self._tokens -= 1
            return True, 0  # Tokens available, allow retry
        return False, 0  # No tokens, block retry


# class HedgePolicy:

# class TimeoutPolicy:

