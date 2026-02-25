"""
Middleware abstraction for composable request processing.

This module provides the core middleware pattern that allows policies
(retry, timeout, load limiting, etc.) to be composed in a clean,
decoupled way without embedding logic directly in ServiceRuntime.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Any, List
from functools import partial

from simulator.core.types import TimePoint, TimeDuration, DropReason


@dataclass
class AttemptContext:
    """
    Context passed through middleware chain for each request attempt.
    
    This encapsulates all information about a single attempt, including
    timing, success/failure, and metadata. Middleware can read from and
    write to this context to make decisions and communicate with other
    middleware in the chain.
    """
    # Immutable attempt information
    attempt_number: int  # 1-indexed (first attempt is 1)
    success: bool
    service_time: TimeDuration
    drop_reason: DropReason
    begin_time: TimePoint
    end_time: TimePoint
    attempt_deadline: Optional[TimePoint]
    global_deadline: Optional[TimePoint]
    queue_size: int
    service_name: str
    
    # SYSNAME: per-request state
    retry_budget_remaining: Optional[int] = None  # Level 2 end-to-end budget
    tenant_id: Optional[str] = None  # for per-tenant tracking

    # Mutable state for middleware to modify
    should_retry: bool = False
    retry_delay: TimeDuration = 0
    
    # Extensible metadata for custom middleware
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def latency(self) -> TimeDuration:
        """Total latency of this attempt"""
        return self.end_time - self.begin_time
    
    @property
    def is_successful(self) -> bool:
        """Whether attempt was successful (considering deadline)"""
        within_deadline = (
            self.attempt_deadline is None or 
            self.end_time < self.attempt_deadline
        )
        return self.success and within_deadline and self.drop_reason == DropReason.NONE


class Middleware(ABC):
    """
    Base class for request processing middleware.
    
    Middleware can intercept attempts to implement cross-cutting concerns
    like retry logic, circuit breaking, rate limiting, metrics collection,
    etc. Each middleware can:
    - Inspect the attempt context
    - Modify the context (e.g., set should_retry)
    - Call the next middleware in the chain
    - Short-circuit the chain by not calling next
    """
    
    @abstractmethod
    def process_attempt(
        self, 
        ctx: AttemptContext, 
        next_fn: Callable[[AttemptContext], None]
    ) -> None:
        """
        Process an attempt and optionally call the next middleware.
        
        Args:
            ctx: The attempt context with all attempt information
            next_fn: Function to call the next middleware in the chain
        """
        pass


class MiddlewareChain:
    """
    Builds and executes a chain of middleware.
    
    Middleware are executed in order, with each middleware deciding whether
    to call the next one. This implements the Chain of Responsibility pattern.
    """
    
    def __init__(self, middlewares: List[Middleware]):
        """
        Create a middleware chain.
        
        Args:
            middlewares: List of middleware to execute in order
        """
        self.middlewares = middlewares
    
    def execute(
        self, 
        ctx: AttemptContext, 
        final_handler: Callable[[AttemptContext], None]
    ) -> None:
        """
        Execute the middleware chain.
        
        Args:
            ctx: The attempt context to process
            final_handler: Function to call after all middleware complete
        """
        if not self.middlewares:
            final_handler(ctx)
            return
        
        # Build chain from right to left (last middleware wraps final handler)
        handler = final_handler
        for middleware in reversed(self.middlewares):
            # Capture current handler in closure
            handler = self._wrap_middleware(middleware, handler)
        
        # Execute the chain
        handler(ctx)
    
    @staticmethod
    def _wrap_middleware(
        middleware: Middleware, 
        next_handler: Callable[[AttemptContext], None]
    ) -> Callable[[AttemptContext], None]:
        """Wrap a middleware with the next handler"""
        def wrapped(ctx: AttemptContext) -> None:
            middleware.process_attempt(ctx, next_handler)
        return wrapped


class PassthroughMiddleware(Middleware):
    """
    Simple middleware that just passes through to the next handler.
    Useful for testing and as a base class.
    """
    
    def process_attempt(self, ctx: AttemptContext, next_fn: Callable) -> None:
        next_fn(ctx)


class LoggingMiddleware(Middleware):
    """
    Middleware that logs attempt information.
    Useful for debugging middleware chains.
    """
    
    def __init__(self, name: str = "LoggingMiddleware"):
        self.name = name
    
    def process_attempt(self, ctx: AttemptContext, next_fn: Callable) -> None:
        print(f"[{self.name}] Before: attempt={ctx.attempt_number}, "
              f"success={ctx.success}, latency={ctx.latency}")
        next_fn(ctx)
        print(f"[{self.name}] After: should_retry={ctx.should_retry}, "
              f"retry_delay={ctx.retry_delay}")
