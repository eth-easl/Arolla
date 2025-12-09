"""Time conversion utilities."""

from ..core.types import TimeDuration, S_TO_NS, MS_TO_NS


def s_to_ns(s: float) -> TimeDuration:
    """Convert seconds to nanoseconds"""
    return int(round(s * S_TO_NS))


def ms_to_ns(ms: float) -> TimeDuration:
    """Convert milliseconds to nanoseconds"""
    return int(round(ms * MS_TO_NS))


def ns_to_ms(ns: TimeDuration) -> float:
    """Convert nanoseconds to milliseconds"""
    return ns / MS_TO_NS


def ns_to_s(ns: TimeDuration) -> float:
    """Convert nanoseconds to seconds"""
    return ns / S_TO_NS
