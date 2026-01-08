import random
from dataclasses import dataclass
from collections import deque
from typing import Optional, Tuple, Deque
from simulator.core.types import TimeDuration, TimePoint
from enum import Enum
from abc import ABC, abstractmethod

@dataclass
class RetryContext:
    attempt: int  # attempt starts at 1 (the original request is attempt 1)
    now: Optional[TimePoint] = None

class RetryPolicy(ABC):
    @abstractmethod
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        pass

    def record_attempt(self, context: RetryContext, success: bool):
        """Optional hook for stateful policies to track success/failure."""
        pass

@dataclass
class FixedBackoffRetryPolicy(RetryPolicy):
    max_attempts: int
    delay: TimeDuration

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.attempt < self.max_attempts:
            return True, self.delay
        return False, 0


@dataclass
class NoRetryPolicy(RetryPolicy):
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        return False, 0


@dataclass
class ExponentialBackoffRetryPolicy(RetryPolicy):
    max_attempts: int
    initial_delay: TimeDuration
    max_delay: TimeDuration

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.attempt < self.max_attempts:
            delay = min(
                self.initial_delay * (2 ** (context.attempt - 1)), self.max_delay
            )
            return True, delay
        return False, 0


class JitterMode(Enum):
    FULL = 0
    EQUAL = 1
    DECORRELATED = 2


@dataclass
class ExponentialBackoffWithJitterRetryPolicy(RetryPolicy):
    max_attempts: int
    initial_delay: TimeDuration
    max_delay: TimeDuration
    rng: random.Random  # pass in the simulator's rng
    jitter_mode: JitterMode = JitterMode.FULL

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.attempt < self.max_attempts:
            exp_delay = min(
                self.initial_delay * (2 ** (context.attempt - 1)), self.max_delay
            )
            if self.jitter_mode == JitterMode.FULL:
                delay = self.rng.uniform(0, exp_delay)
            elif self.jitter_mode == JitterMode.EQUAL:
                delay = exp_delay / 2 + self.rng.uniform(0, exp_delay / 2)
            elif self.jitter_mode == JitterMode.DECORRELATED:
                raise NotImplementedError("Decorrelated jitter not implemented")
            else:
                raise ValueError("Unknown jitter mode")
            return True, int(delay)
        return False, 0


@dataclass
class RetryBudgetPolicy(RetryPolicy):
    """
    Implements a client-side retry budget (token bucket).
    Ref: jirfag/simulations (RetryBudgetStrategy)
    """
    inner: RetryPolicy
    budget_ratio: float = 0.1
    min_retries_per_sec: int = 10 # Not used yet, reference uses 30 as capacity multiplier
    max_retries: int = 30 # Default to 30 to match reference
    
    # Internal state
    _tokens: float = 0
    _max_tokens: float = 0
    retry_cost: int = 100
    success_reward: int = 0

    def __post_init__(self):
        # Jirfag: tokens_to_sub = 100, tokens_to_add = int(ratio * 100)
        # Max tokens = tokens_to_sub * max_retries (Reference uses 30)
        self.retry_cost = 100
        self.success_reward = int(self.budget_ratio * self.retry_cost)
        self._max_tokens = self.retry_cost * self.max_retries
        self._tokens = self._max_tokens # Start full
        self._last_refill_time = 0

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        # Refill based on time
        now = context.now if context.now is not None else 0
        if self._last_refill_time > 0 and now > self._last_refill_time:
             # Elapsed seconds
             elapsed_s = (now - self._last_refill_time) / 1e9
             refill = elapsed_s * self.min_retries_per_sec * self.retry_cost
             if refill > 0:
                 self._tokens = min(self._max_tokens, self._tokens + refill)
        
        self._last_refill_time = now

        if self._tokens < self.retry_cost:
             return False, 0
        
        should, delay = self.inner.next_delay(context)
        if should:
            # We are allowing a retry. Deduct cost.
            self._tokens -= self.retry_cost
        return should, delay

    def record_attempt(self, context: RetryContext, success: bool):
        # Pass through to inner
        self.inner.record_attempt(context, success)

        if success:
            # Only refill budget for successful INITIAL calls.
            # Successful retries do not refill the budget (they consume it).
            if context.attempt == 1:
                self._tokens = min(self._max_tokens, self._tokens + self.success_reward)


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
    Implements a stateless, window-based circuit breaker (Retry Circuit Breaker).
    Ref Logic: "On success or failure, it updates statistics... If failure rate > threshold, don't retry."
    """ 
    inner: RetryPolicy
    failure_rate_threshold: float
    window_duration: TimeDuration
    min_requests: int
    wait_duration_in_open_state: TimeDuration 
    
    # Internal State
    requests_window: Deque[CircuitBreakerRequest] = None
    window_failed_req_count: int = 0

    def __init__(self, inner: RetryPolicy, failure_rate_threshold: float, window_duration: TimeDuration, 
                 min_window_size: int, wait_duration_in_open_state: TimeDuration):
        self.inner = inner
        self.failure_rate_threshold = failure_rate_threshold
        self.window_duration = window_duration
        self.min_window_size = min_window_size
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
        
        # Prune old
        while self.requests_window and (now - self.requests_window[0].timepoint > self.window_duration):
            popped = self.requests_window.popleft()
            if not popped.is_ok:
                self.window_failed_req_count -= 1

        # Check threshold
        if self._is_failure_threshold_reached():
             return False, 0
        
        return self.inner.next_delay(context)

    def record_attempt(self, context: RetryContext, success: bool):
        self.inner.record_attempt(context, success)
        
        now = context.now if context.now is not None else 0
        
        # Add to window
        self.requests_window.append(CircuitBreakerRequest(now, success))
        if not success:
            self.window_failed_req_count += 1
            
        # Prune old
        while self.requests_window and (now - self.requests_window[0].timepoint > self.window_duration):
            popped = self.requests_window.popleft()
            if not popped.is_ok:
                self.window_failed_req_count -= 1


@dataclass
class TimeBasedCircuitBreakerPolicy(RetryPolicy):
    """
    Implements a time-based circuit breaker with states (CLOSED, OPEN, HALF_OPEN).
    Matches reference implementation logic.
    """
    inner: RetryPolicy
    failure_rate_threshold: float
    window_duration: TimeDuration
    min_window_size: int
    wait_duration_in_open_state: TimeDuration
    
    # Internal State
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    last_open_time: int = 0
    requests_window: Deque[CircuitBreakerRequest] = None
    window_failed_req_count: int = 0
    permitted_half_open_calls: int = 100 

    def __init__(self, inner: RetryPolicy, failure_rate_threshold: float, window_duration: TimeDuration, 
                 min_window_size: int, wait_duration_in_open_state: TimeDuration):
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
        
        # State Machine Logic
        if self.state == CircuitBreakerState.CLOSED:
            if self._is_failure_threshold_reached():
                self._set_state(CircuitBreakerState.OPEN, now)
                return False, 0
            
        elif self.state == CircuitBreakerState.OPEN:
             if (now - self.last_open_time) < self.wait_duration_in_open_state:
                 return False, 0
             self._set_state(CircuitBreakerState.HALF_OPEN, now)
             
        elif self.state == CircuitBreakerState.HALF_OPEN:
            if len(self.requests_window) < self.permitted_half_open_calls:
                 pass 
            else: 
                if self._is_failure_threshold_reached():
                    self._set_state(CircuitBreakerState.OPEN, now)
                    return False, 0
                else:
                    self._set_state(CircuitBreakerState.CLOSED, now)
                    pass

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