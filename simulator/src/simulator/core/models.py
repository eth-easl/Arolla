"""Core data models for ms_simulator."""

from dataclasses import dataclass, field
from typing import List, Optional

from .types import TimePoint, TimeDuration, DropReason


@dataclass
class TimeInterval:
    """
    Represents a half-open time interval [begin, end).
    
    The interval includes the begin time but excludes the end time,
    following standard interval notation.
    
    Attributes:
        begin: Start time of the interval (inclusive)
        end: End time of the interval (exclusive)
    
    Raises:
        ValueError: If end < begin (invalid interval)
    """
    begin: TimePoint
    end: TimePoint

    def __post_init__(self):
        """Validate that the interval is well-formed"""
        if self.end < self.begin:
            raise ValueError(
                f"Invalid time interval: end ({self.end}) < begin ({self.begin})"
            )

    def duration(self) -> TimeDuration:
        """Calculate the duration of this interval"""
        return self.end - self.begin

    def contains(self, timepoint: TimePoint) -> bool:
        """Check if a timepoint falls within this interval (half-open)"""
        return self.begin <= timepoint < self.end


@dataclass
class Request:
    """
    Represents a single request attempt in the simulation.
    
    A request attempt captures the lifecycle of one try at processing a request,
    including timing information, success/failure status, and the reason for any
    failure. Multiple attempts may be made for a single root request due to retries.
    
    Attributes:
        service: Name of the service that handled this attempt
        interval: Time span from request start to completion
        deadline: Optional deadline for this attempt (None if no deadline)
        success: Whether the attempt succeeded (True) or failed (False)
        drop_reason: Reason for failure if not successful (NONE if successful)
        queue_size_at_end: Queue depth when attempt completed (for metrics)
    """
    service: str
    interval: TimeInterval
    deadline: Optional[TimePoint] = None
    success: bool = False
    drop_reason: DropReason = DropReason.NONE
    queue_size_at_end: Optional[int] = None


@dataclass
class RootRequest:
    """
    Represents a client request that may consist of multiple attempts.
    
    A root request is the top-level request from a client's perspective. It may
    involve multiple attempts (retries) before succeeding or being abandoned.
    The root request tracks all attempts and maintains the global deadline.
    
    Attributes:
        attempts: List of all attempts made for this request (in chronological order)
        global_deadline: Optional deadline for the entire request across all attempts
        done: Whether the request has been completed (successfully or not)
    
    Methods:
        add_attempt: Add a new attempt to this request
        attempt_count: Get the total number of attempts made
    """
    attempts: List[Request] = field(default_factory=list)
    global_deadline: Optional[TimePoint] = None
    done: bool = False

    def add_attempt(self, attempt: Request):
        """Add a new attempt to this request"""
        self.attempts.append(attempt)

    def attempt_count(self) -> int:
        """Get the total number of attempts made for this request"""
        return len(self.attempts)


@dataclass
class Summary:
    """Summary statistics for simulation results"""
    total: int
    succeeded: int
    dropped_queue: int
    dropped_deadline: int
    dropped_server_failure: int
    p50: float
    p90: float
    p95: float
    p99: float
    p99_9: float
    p99_99: float
    mean: float
    max: float
    retries_per_root: float
    attempts_total: int
