from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from simulator.core.types import TimeDuration, TimePoint


@dataclass
class RetryContext:
    attempt: int  # attempt starts at 1 (the original request is attempt 1)
    now: Optional[TimePoint] = None
    tenant_id: Optional[str] = None


class RetryPolicy(ABC):
    @abstractmethod
    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        pass

    def record_attempt(self, context: RetryContext, success: bool):
        """Optional hook for stateful policies to track success/failure."""
        pass

    def applies_pre_queue_admission(self, is_retry: bool) -> bool:
        """
        Optional hook for server-side pre-queue admission checks.
        Retry policies normally do not participate in submission-time admission.
        """
        return False


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
            delay = min(self.initial_delay * (2 ** (context.attempt - 1)), self.max_delay)
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
    rng: random.Random
    jitter_mode: JitterMode = JitterMode.FULL

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.attempt < self.max_attempts:
            exp_delay = min(self.initial_delay * (2 ** (context.attempt - 1)), self.max_delay)
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

