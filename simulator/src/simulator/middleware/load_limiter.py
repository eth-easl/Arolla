"""
Load limiter middleware - extracts circuit breaker, rate limiter, and retry budget logic.

This middleware handles admission control and retry limiting based on
LoadLimiter policies (circuit breakers, rate limiters, retry budgets).
"""

from collections import defaultdict
from typing import Callable, Dict
from simulator.middleware.base import Middleware, AttemptContext
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryContext


class LoadLimiterMiddleware(Middleware):
    """
    Handles load limiting (circuit breaking, rate limiting, retry budgets).

    This middleware:
    1. Records attempt results for state tracking (e.g., circuit breaker state)
    2. Decides whether to allow retries based on limiter state
    3. Can override retry decisions from RetryMiddleware
    4. Tracks per-tenant retry admission stats (requested vs admitted)

    Note: This middleware should typically run AFTER RetryMiddleware in the chain,
    so it can veto retry decisions based on system health.
    """

    def __init__(self, limiter: LoadLimiter):
        """
        Create load limiter middleware.

        Args:
            limiter: The load limiter policy (circuit breaker, rate limiter, etc.)
        """
        self.limiter = limiter
        # Per-tenant retry admission tracking
        self._retry_requested: Dict[str, int] = defaultdict(int)
        self._retry_admitted: Dict[str, int] = defaultdict(int)

    def process_attempt(
        self,
        ctx: AttemptContext,
        next_fn: Callable[[AttemptContext], None]
    ) -> None:
        """Process attempt and apply load limiting"""

        # Record result for state tracking (circuit breakers, budgets, etc.)
        if hasattr(self.limiter, 'add_result'):
            self.limiter.add_result(
                ctx.is_successful, ctx.end_time,
                tenant_id=ctx.tenant_id,
                is_retry=(ctx.attempt_number > 1),
            )

        # If successful or retry already denied by upstream middleware,
        # pass through without consuming limiter tokens
        if ctx.is_successful or not ctx.should_retry:
            next_fn(ctx)
            return

        # Client wants to retry — track the request
        tenant = ctx.tenant_id or '__global__'
        self._retry_requested[tenant] += 1

        # Check if limiter allows retry (may consume a token from budget)
        retry_ctx = RetryContext(
            attempt=ctx.attempt_number, now=ctx.end_time, tenant_id=ctx.tenant_id,
        )
        limiter_allows_retry, limiter_delay = self.limiter.next_delay(retry_ctx)

        if not limiter_allows_retry:
            # Limiter blocks retry (e.g., circuit breaker open, budget exhausted)
            ctx.should_retry = False
            ctx.metadata['limiter_blocked'] = True
            ctx.metadata['limiter_reason'] = self._get_limiter_reason()
        else:
            # Limiter admits the retry
            self._retry_admitted[tenant] += 1
            if limiter_delay > ctx.retry_delay:
                # Limiter allows retry but with longer delay (e.g., rate limiting)
                ctx.retry_delay = limiter_delay
                ctx.metadata['limiter_delay_applied'] = True

        # Pass to next middleware
        next_fn(ctx)

    def get_admission_stats(self) -> Dict[str, Dict[str, int]]:
        """Return per-tenant retry admission stats.

        Returns:
            Dict mapping tenant_id -> {'requested': N, 'admitted': M}
        """
        tenants = set(self._retry_requested) | set(self._retry_admitted)
        return {
            tenant: {
                'requested': self._retry_requested[tenant],
                'admitted': self._retry_admitted[tenant],
            }
            for tenant in tenants
        }
    
    def _get_limiter_reason(self) -> str:
        """Get human-readable reason for limiter blocking retry"""
        limiter_type = type(self.limiter).__name__
        
        # Check circuit breaker state
        if hasattr(self.limiter, 'get_state'):
            state = self.limiter.get_state()
            return f"{limiter_type}_state_{state.name}"
        
        # Check retry budget
        if hasattr(self.limiter, 'can_retry'):
            if not self.limiter.can_retry():
                return f"{limiter_type}_budget_exhausted"
        
        return f"{limiter_type}_blocked"


class CompositeLoadLimiterMiddleware(Middleware):
    """
    Combines multiple load limiters (e.g., circuit breaker + retry budget).
    
    All limiters must allow retry for the attempt to proceed.
    """
    
    def __init__(self, limiters: list[LoadLimiter]):
        """
        Create composite load limiter middleware.
        
        Args:
            limiters: List of load limiters to apply (all must allow retry)
        """
        self.limiters = limiters
        self.middlewares = [LoadLimiterMiddleware(limiter) for limiter in limiters]
    
    def process_attempt(self, ctx: AttemptContext, next_fn: Callable) -> None:
        """Apply all limiters in sequence"""
        # Build chain of limiter middlewares
        handler = next_fn
        for middleware in reversed(self.middlewares):
            current_handler = handler
            handler = lambda c, m=middleware, h=current_handler: m.process_attempt(c, h)

        handler(ctx)


class EndToEndRetryBudgetMiddleware(Middleware):
    """
    Arolla Level 2: End-to-end per-request retry budget.

    Blocks retries when the shared budget (stamped at ingress) is exhausted.
    This bounds total retries across all hops to B, preventing chain amplification.
    No-op when retry_budget_remaining is None (budget not configured).
    """

    def process_attempt(
        self,
        ctx: AttemptContext,
        next_fn: Callable[[AttemptContext], None],
    ) -> None:
        if ctx.is_successful or not ctx.should_retry:
            next_fn(ctx)
            return

        if ctx.retry_budget_remaining is not None and ctx.retry_budget_remaining <= 0:
            ctx.should_retry = False
            ctx.metadata['limiter_blocked'] = True
            ctx.metadata['limiter_reason'] = 'e2e_budget_exhausted'

        next_fn(ctx)
