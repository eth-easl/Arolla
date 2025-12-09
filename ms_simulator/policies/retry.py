import random
from dataclasses import dataclass
from typing import Optional, Tuple
from core import TimeDuration, TimePoint
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
    # _prev: TimeDuration = 0

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