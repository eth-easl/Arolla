from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional, Tuple

from simulator.core.types import TimeDuration, TimePoint
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryContext, RetryPolicy


@dataclass
class RetryBudgetPolicy(RetryPolicy):
    """
    Client-side retry budget (token bucket) that wraps an inner retry policy.
    """

    inner: RetryPolicy
    budget_ratio: float = 0.1
    min_retries_per_sec: int = 10
    max_retries: int = 30

    _tokens: float = 0
    _max_tokens: float = 0
    retry_cost: int = 100
    success_reward: int = 0
    _last_refill_time: int = field(default=0, init=False)

    def __post_init__(self):
        self.retry_cost = 100
        self.success_reward = int(self.budget_ratio * self.retry_cost)
        self._max_tokens = self.retry_cost * self.max_retries
        self._tokens = self._max_tokens

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        now = context.now if context.now is not None else 0
        if self._last_refill_time > 0 and now > self._last_refill_time:
            elapsed_s = (now - self._last_refill_time) / 1e9
            refill = elapsed_s * self.min_retries_per_sec * self.retry_cost
            if refill > 0:
                self._tokens = min(self._max_tokens, self._tokens + refill)

        self._last_refill_time = now
        if self._tokens < self.retry_cost:
            return False, 0

        should, delay = self.inner.next_delay(context)
        if should:
            self._tokens -= self.retry_cost
        return should, delay

    def record_attempt(self, context: RetryContext, success: bool):
        self.inner.record_attempt(context, success)
        if success and context.attempt == 1:
            self._tokens = min(self._max_tokens, self._tokens + self.success_reward)

    def applies_pre_queue_admission(self, is_retry: bool) -> bool:
        # Preserve historical behavior if this is used in a server-side slot.
        return is_retry


class CircuitBreakerState(Enum):
    CLOSED = 1
    OPEN = 2
    HALF_OPEN = 3


@dataclass
class CircuitBreakerRequest:
    timepoint: int
    is_ok: bool


@dataclass
class RetryCircuitBreakerPolicy(RetryPolicy):
    """
    Window-based retry gate. Blocks retries when recent failure ratio is high.
    """

    inner: Optional[RetryPolicy]
    failure_rate_threshold: float
    window_duration: TimeDuration
    min_window_size: int
    wait_duration_in_open_state: TimeDuration

    requests_window: Deque[CircuitBreakerRequest] = None
    window_failed_req_count: int = 0

    def __init__(
        self,
        inner: Optional[RetryPolicy],
        failure_rate_threshold: float,
        window_duration: TimeDuration,
        min_window_size: Optional[int] = None,
        wait_duration_in_open_state: TimeDuration = 0,
        min_requests: Optional[int] = None,
    ):
        self.inner = inner
        self.failure_rate_threshold = failure_rate_threshold
        self.window_duration = window_duration

        resolved_min_window = min_window_size if min_window_size is not None else min_requests
        if resolved_min_window is None:
            raise ValueError("RetryCircuitBreakerPolicy requires min_window_size or min_requests")
        self.min_window_size = resolved_min_window
        self.min_requests = resolved_min_window  # backward-compat alias
        self.wait_duration_in_open_state = wait_duration_in_open_state
        self.__post_init__()

    def __post_init__(self):
        self.requests_window = deque()
        self.window_failed_req_count = 0

    def _is_failure_threshold_reached(self) -> bool:
        if len(self.requests_window) < self.min_window_size:
            return False
        return (self.window_failed_req_count / len(self.requests_window)) >= self.failure_rate_threshold

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        now = context.now if context.now is not None else 0

        while self.requests_window and (now - self.requests_window[0].timepoint > self.window_duration):
            popped = self.requests_window.popleft()
            if not popped.is_ok:
                self.window_failed_req_count -= 1

        if self._is_failure_threshold_reached():
            return False, 0

        if self.inner is None:
            return True, 0
        return self.inner.next_delay(context)

    def add_result(self, success: bool, now: Optional[int] = None) -> None:
        """Record a result for LoadLimiter compatibility (used by LoadLimiterMiddleware)."""
        ts = now if now is not None else 0
        self.requests_window.append(CircuitBreakerRequest(ts, success))
        if not success:
            self.window_failed_req_count += 1

        while self.requests_window and (ts - self.requests_window[0].timepoint > self.window_duration):
            popped = self.requests_window.popleft()
            if not popped.is_ok:
                self.window_failed_req_count -= 1

    def record_attempt(self, context: RetryContext, success: bool):
        if self.inner is not None:
            self.inner.record_attempt(context, success)

        now = context.now if context.now is not None else 0
        self.add_result(success, now)


@dataclass
class TimeBasedCircuitBreakerPolicy(RetryPolicy):
    """
    Stateful retry circuit breaker with CLOSED/OPEN/HALF_OPEN transitions.
    """

    inner: RetryPolicy
    failure_rate_threshold: float
    window_duration: TimeDuration
    min_window_size: int
    wait_duration_in_open_state: TimeDuration

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    last_open_time: int = 0
    requests_window: Deque[CircuitBreakerRequest] = None
    window_failed_req_count: int = 0
    permitted_half_open_calls: int = 100

    def __init__(
        self,
        inner: RetryPolicy,
        failure_rate_threshold: float,
        window_duration: TimeDuration,
        min_window_size: int,
        wait_duration_in_open_state: TimeDuration,
    ):
        self.inner = inner
        self.failure_rate_threshold = failure_rate_threshold
        self.window_duration = window_duration
        self.min_window_size = min_window_size
        self.wait_duration_in_open_state = wait_duration_in_open_state
        self.permitted_half_open_calls = min_window_size
        self.__post_init__()

    def __post_init__(self):
        self.requests_window = deque()

    def _reset_window(self):
        self.requests_window.clear()
        self.window_failed_req_count = 0

    def _set_state(self, state: CircuitBreakerState, now: int):
        self.state = state
        if state == CircuitBreakerState.OPEN:
            self.last_open_time = now
        self._reset_window()

    def _is_failure_threshold_reached(self) -> bool:
        return (
            len(self.requests_window) >= self.min_window_size
            and (self.window_failed_req_count / len(self.requests_window)) >= self.failure_rate_threshold
        )

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        now = context.now if context.now is not None else 0

        if self.state == CircuitBreakerState.CLOSED:
            if self._is_failure_threshold_reached():
                self._set_state(CircuitBreakerState.OPEN, now)
                return False, 0
        elif self.state == CircuitBreakerState.OPEN:
            if (now - self.last_open_time) < self.wait_duration_in_open_state:
                return False, 0
            self._set_state(CircuitBreakerState.HALF_OPEN, now)
        elif self.state == CircuitBreakerState.HALF_OPEN:
            if len(self.requests_window) >= self.permitted_half_open_calls:
                if self._is_failure_threshold_reached():
                    self._set_state(CircuitBreakerState.OPEN, now)
                    return False, 0
                self._set_state(CircuitBreakerState.CLOSED, now)

        return self.inner.next_delay(context)

    def record_attempt(self, context: RetryContext, success: bool):
        self.inner.record_attempt(context, success)
        now = context.now if context.now is not None else 0

        self.requests_window.append(CircuitBreakerRequest(now, success))
        if not success:
            self.window_failed_req_count += 1

        while self.requests_window and (now - self.requests_window[0].timepoint > self.window_duration):
            popped = self.requests_window.popleft()
            if not popped.is_ok:
                self.window_failed_req_count -= 1


class CBState(Enum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


@dataclass
class CountBasedCircuitBreakerPolicy(LoadLimiter):
    failure_threshold_ratio: Tuple[int, int]
    success_threshold_ratio: Tuple[int, int]
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
            return False, 0
        return True, 0


@dataclass
class LimiterTimeBasedCircuitBreakerPolicy(LoadLimiter):
    failure_threshold_rate: float
    success_threshold_rate: float
    min_requests: int
    window_duration: TimeDuration
    half_open_delay: TimeDuration

    _window: Deque[Tuple[TimePoint, bool]] = field(default_factory=deque)
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
                if total >= self.min_requests and rate < (1 - self.success_threshold_rate):
                    self._state = CBState.CLOSED
                    self._opened_at = None

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if self._state == CBState.OPEN:
            return False, 0
        return True, 0


@dataclass
class LimiterRetryBudgetPolicy(LoadLimiter):
    budget_ratio: float
    max_retries: int

    _max_tokens: int = field(init=False)
    _success_award: int = field(init=False)
    _retry_cost: int = field(init=False)
    _tokens: float = field(init=False)

    def __post_init__(self):
        self._retry_cost = 100
        self._success_award = int(self._retry_cost * self.budget_ratio)
        self._max_tokens = self._retry_cost * self.max_retries
        self._tokens = self._max_tokens

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        if success:
            self._tokens = min(self._max_tokens, self._tokens + self._success_award)

    def can_retry(self) -> bool:
        return self._tokens >= self._retry_cost

    def get_token_balance(self) -> float:
        return self._tokens

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if self.can_retry():
            self._tokens -= self._retry_cost
            return True, 0
        return False, 0


@dataclass
class GlobalRetryBudget(LoadLimiter):
    max_tokens: int
    refill_rate: int
    period: TimeDuration

    _tokens: float = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = float(self.max_tokens)

    def _refill(self, now: TimePoint):
        if self._last_refill is None:
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed > 0:
            tokens_to_add = (elapsed / self.period) * self.refill_rate
            self._tokens = min(float(self.max_tokens), self._tokens + tokens_to_add)
            self._last_refill = now

    def request_ticket(self, now: TimePoint) -> bool:
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def balance(self) -> float:
        return self._tokens

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        pass

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            return False, 0
        if self.request_ticket(context.now):
            return True, 0
        return False, 0

    def applies_pre_queue_admission(self, is_retry: bool) -> bool:
        return is_retry


@dataclass
class AIMDGlobalRetryBudget(LoadLimiter):
    min_rps: int
    max_rps: int
    initial_rps: int
    additive_step: int
    decrease_factor: float
    failure_threshold: float
    window_duration: TimeDuration
    max_burst: int

    _tokens: float = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)
    _current_rps: float = field(init=False)

    _window_start: Optional[TimePoint] = field(default=None, init=False)
    _window_requests: int = 0
    _window_failures: int = 0

    def __post_init__(self):
        self._current_rps = float(self.initial_rps)
        self._tokens = float(self.max_burst)
        self._window_requests = 0
        self._window_failures = 0

    def _update_policy(self, now: TimePoint):
        if self._window_start is None:
            self._window_start = now
            return
        if now - self._window_start >= self.window_duration:
            if self._window_requests > 0:
                failure_rate = self._window_failures / self._window_requests
                if failure_rate > self.failure_threshold:
                    self._current_rps = max(float(self.min_rps), self._current_rps * self.decrease_factor)
                else:
                    self._current_rps = min(float(self.max_rps), self._current_rps + self.additive_step)
            self._window_start = now
            self._window_requests = 0
            self._window_failures = 0

    def _refill(self, now: TimePoint):
        self._update_policy(now)
        if self._last_refill is None:
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed > 0:
            tokens_to_add = (elapsed / 1_000_000_000.0) * self._current_rps
            self._tokens = min(float(self.max_burst), self._tokens + tokens_to_add)
            self._last_refill = now

    def request_ticket(self, now: TimePoint) -> bool:
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        if now is None:
            return
        if self._window_start is None:
            self._window_start = now
        self._update_policy(now)
        self._window_requests += 1
        if not success:
            self._window_failures += 1

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            return False, 0
        if self.request_ticket(context.now):
            return True, 0
        return False, 0

    def applies_pre_queue_admission(self, is_retry: bool) -> bool:
        return is_retry
