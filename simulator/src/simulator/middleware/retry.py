"""
Retry middleware - extracts retry logic from ServiceRuntime.

This middleware handles retry decisions based on a RetryPolicy,
decoupling retry logic from the service implementation.
"""

from typing import Callable
from simulator.middleware.base import Middleware, AttemptContext
from simulator.policies.retry import RetryPolicy, RetryContext
from simulator.core.engine import Simulator


class RetryMiddleware(Middleware):
    """
    Handles retry logic based on a RetryPolicy.
    
    This middleware:
    1. Checks if the attempt was successful
    2. If not, consults the retry policy to decide whether to retry
    3. Respects global deadlines
    4. Sets should_retry and retry_delay in the context
    """
    
    def __init__(self, policy: RetryPolicy):
        """
        Create retry middleware.
        
        Args:
            policy: The retry policy to use for retry decisions
        """
        self.policy = policy
    
    def process_attempt(
        self, 
        ctx: AttemptContext, 
        next_fn: Callable[[AttemptContext], None]
    ) -> None:
        """Process attempt and decide whether to retry"""
        
        # If successful, no retry needed - pass through
        if ctx.is_successful:
            next_fn(ctx)
            return
        
        # Consult retry policy
        retry_ctx = RetryContext(attempt=ctx.attempt_number, now=ctx.end_time)
        should_retry, delay = self.policy.next_delay(retry_ctx)
        
        # Check global deadline constraint
        if should_retry and ctx.global_deadline is not None:
            next_start = ctx.end_time + delay
            if next_start >= ctx.global_deadline:
                should_retry = False
        
        # Update context
        if should_retry:
            ctx.should_retry = True
            ctx.retry_delay = delay
            ctx.metadata['retry_reason'] = 'policy_allowed'
        else:
            ctx.should_retry = False
            ctx.metadata['retry_reason'] = 'policy_denied' if not should_retry else 'deadline_exceeded'
        
        # Pass to next middleware
        next_fn(ctx)


class NoRetryMiddleware(Middleware):
    """
    Middleware that explicitly disables retries.
    Useful for services that should never retry.
    """
    
    def process_attempt(self, ctx: AttemptContext, next_fn: Callable) -> None:
        """Disable retries"""
        ctx.should_retry = False
        ctx.metadata['retry_reason'] = 'retries_disabled'
        next_fn(ctx)
