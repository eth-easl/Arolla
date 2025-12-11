"""Core type definitions for ms_simulator."""

from enum import Enum, auto

# Time types
TimePoint = int  # nanoseconds
TimeDuration = int  # nanoseconds

# Time conversion constants
S_TO_NS: TimeDuration = 1_000_000_000
MS_TO_NS: TimeDuration = 1_000_000

# Minimum service time to avoid zero-latency requests
MIN_SERVICE_TIME_NS: TimeDuration = 100_000  # 0.1ms


class DropReason(Enum):
    """Reasons why a request was dropped"""
    NONE = 0
    QUEUE_FULL = auto()
    DEADLINE = auto()
    SERVER_FAILURE = auto()
    CIRCUIT_OPEN = auto()
