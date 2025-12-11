"""
Metrics middleware - decouples metrics collection from service logic.

This middleware records attempt information for metrics without
blocking the request flow or requiring services to expose internals.
"""

from typing import Protocol, Optional
from simulator.middleware.base import Middleware, AttemptContext
from simulator.core.models import Request, RootRequest, TimeInterval


class MetricsSink(Protocol):
    """
    Interface for metrics collection backends.
    
    Implementations can write to CSV, databases, streaming systems, etc.
    """
    
    def record_attempt(
        self, 
        ctx: AttemptContext,
        root_request: Optional[RootRequest] = None
    ) -> None:
        """Record an attempt for metrics"""
        ...
    
    def close(self) -> None:
        """Close the metrics sink"""
        ...


class InMemoryMetricsSink:
    """
    In-memory metrics sink that collects attempts in a list.
    Compatible with existing Metrics class.
    """
    
    def __init__(self):
        self.roots: list[RootRequest] = []
        self.attempts_total: int = 0
        self._current_roots: dict[int, RootRequest] = {}  # root_id -> RootRequest
    
    def record_attempt(
        self, 
        ctx: AttemptContext,
        root_request: Optional[RootRequest] = None
    ) -> None:
        """Record attempt in memory"""
        self.attempts_total += 1
        
        # Create Request object from context
        req = Request(
            service=ctx.service_name,
            interval=TimeInterval(begin=ctx.begin_time, end=ctx.end_time),
            deadline=ctx.attempt_deadline,
            success=ctx.success,
            drop_reason=ctx.drop_reason,
            queue_size_at_end=ctx.queue_size,
        )
        
        # Add to root request if provided
        if root_request is not None:
            root_request.add_attempt(req)
    
    def add_root(self, root: RootRequest) -> None:
        """Add completed root request"""
        self.roots.append(root)
    
    def close(self) -> None:
        """No-op for in-memory sink"""
        pass


class MetricsMiddleware(Middleware):
    """
    Collects metrics without blocking request flow.
    
    This middleware records attempt information to a MetricsSink,
    allowing metrics collection to be decoupled from service logic.
    """
    
    def __init__(self, sink: MetricsSink):
        """
        Create metrics middleware.
        
        Args:
            sink: The metrics sink to write to
        """
        self.sink = sink
    
    def process_attempt(
        self, 
        ctx: AttemptContext, 
        next_fn: callable
    ) -> None:
        """Record metrics and pass through"""
        
        # Record attempt
        self.sink.record_attempt(ctx)
        
        # Pass through to next middleware
        next_fn(ctx)


class StreamingCSVSink:
    """
    Streaming CSV sink that writes metrics incrementally.
    Avoids OOM for long simulations.
    """
    
    def __init__(self, path: str):
        """
        Create streaming CSV sink.
        
        Args:
            path: Path to CSV file to write
        """
        import csv
        self.path = path
        self.file = open(path, 'w', newline='')
        self.writer = csv.DictWriter(self.file, fieldnames=[
            'timestamp_ns',
            'service',
            'attempt_number',
            'success',
            'latency_ns',
            'service_time_ns',
            'drop_reason',
            'queue_size',
            'had_deadline',
        ])
        self.writer.writeheader()
    
    def record_attempt(
        self, 
        ctx: AttemptContext,
        root_request: Optional[RootRequest] = None
    ) -> None:
        """Write attempt to CSV"""
        self.writer.writerow({
            'timestamp_ns': ctx.end_time,
            'service': ctx.service_name,
            'attempt_number': ctx.attempt_number,
            'success': ctx.success,
            'latency_ns': ctx.latency,
            'service_time_ns': ctx.service_time,
            'drop_reason': ctx.drop_reason.name,
            'queue_size': ctx.queue_size,
            'had_deadline': ctx.attempt_deadline is not None,
        })
        
        # Flush periodically to avoid buffering issues
        if ctx.attempt_number % 100 == 0:
            self.file.flush()
    
    def close(self) -> None:
        """Close CSV file"""
        self.file.close()
