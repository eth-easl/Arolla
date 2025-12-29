import random
from dataclasses import dataclass
from typing import Optional, Tuple
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
    
    # Internal state
    _tokens: float = 0
    _max_tokens: float = 0
    retry_cost: int = 100
    success_reward: int = 0

    def __post_init__(self):
        # Jirfag: tokens_to_sub = 100, tokens_to_add = int(ratio * 100)
        # Max tokens = tokens_to_sub * 30
        self.retry_cost = 100
        self.success_reward = int(self.budget_ratio * self.retry_cost)
        self._max_tokens = self.retry_cost * 30
        self._tokens = self._max_tokens # Start full

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
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
            self._tokens = min(self._max_tokens, self._tokens + self.success_reward)


@dataclass
class CircuitBreakerPolicy(RetryPolicy):
    """
    Implements a client-side circuit breaker.
    Ref: jirfag/simulations (RetryCircuitBreakerStrategy)
    """
    inner: RetryPolicy
    failure_rate_threshold: float = 0.5
    window_size: int = 100
    
    _history: list = None # List of bool (is_failure)
    _failures: int = 0
    
    def __post_init__(self):
        self._history = []

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if not self._history:
             # Default closed (allow) if empty? Or strictly check?
             # Jirfag checks if len == 0 -> return False (safeguard? or strict concurrency control?)
             # "Specifically to token bucket, we don't allow to retry if we don't have any successfully finished requests."
             # Actually Jirfag says: "if len(events) == 0 ... return (False, 0)".
             # But simplistic impl might be permissive. Let's stick to permissive for now unless strictness requested.
             return self.inner.next_delay(context)
        
        rate = self._failures / len(self._history)
        if rate >= self.failure_rate_threshold:
            return False, 0 # Open (block)
            
        return self.inner.next_delay(context)

    def record_attempt(self, context: RetryContext, success: bool):
        self.inner.record_attempt(context, success)
        
        is_failure = not success
        self._history.append(is_failure)
        if is_failure:
            self._failures += 1
            
        if len(self._history) > self.window_size:
            removed = self._history.pop(0)
            if removed:
                self._failures -= 1