"""Core simulation engine and data structures."""

# Re-export for backward compatibility and convenience
from .types import (
    TimePoint,
    TimeDuration,
    DropReason,
    S_TO_NS,
    MS_TO_NS,
    MIN_SERVICE_TIME_NS,
)

from .models import (
    TimeInterval,
    Request,
    RootRequest,
    Summary,
)

__all__ = [
    # Types
    "TimePoint",
    "TimeDuration",
    "DropReason",
    "S_TO_NS",
    "MS_TO_NS",
    "MIN_SERVICE_TIME_NS",
    
    # Models
    "TimeInterval",
    "Request",
    "RootRequest",
    "Summary",
]
