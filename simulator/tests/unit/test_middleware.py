"""
Basic tests for middleware pattern.

Run with: python -m pytest tests/test_middleware.py
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator.middleware.base import Middleware, AttemptContext, MiddlewareChain
from simulator.middleware.retry import RetryMiddleware, NoRetryMiddleware
from simulator.middleware.load_limiter import LoadLimiterMiddleware
from simulator.policies.retry import FixedBackoffRetryPolicy, NoRetryPolicy
from simulator.policies.retry_controls import LimiterRetryBudgetPolicy
from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimePoint
from simulator.utils.time import ms_to_ns


def test_middleware_chain_execution_order():
    """Test that middleware executes in correct order"""
    calls = []
    
    class LoggingMiddleware(Middleware):
        def __init__(self, name):
            self.name = name
        
        def process_attempt(self, ctx, next_fn):
            calls.append(f"{self.name}_before")
            next_fn(ctx)
            calls.append(f"{self.name}_after")
    
    # Build chain
    chain = MiddlewareChain([
        LoggingMiddleware("A"),
        LoggingMiddleware("B"),
    ])
    
    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.NONE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    chain.execute(ctx, lambda c: calls.append("final"))
    
    # Should execute in order: A_before, B_before, final, B_after, A_after
    assert calls == ["A_before", "B_before", "final", "B_after", "A_after"]
    print("✓ Middleware chain execution order correct")


def test_retry_middleware_allows_retry():
    """Test that RetryMiddleware allows retry on failure"""
    policy = FixedBackoffRetryPolicy(max_attempts=3, delay=ms_to_ns(100))
    middleware = RetryMiddleware(policy)
    
    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.SERVER_FAILURE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    middleware.process_attempt(ctx, lambda c: None)
    
    assert ctx.should_retry == True
    assert ctx.retry_delay == ms_to_ns(100)
    print("✓ RetryMiddleware allows retry on failure")


def test_retry_middleware_no_retry_on_success():
    """Test that RetryMiddleware doesn't retry on success"""
    policy = FixedBackoffRetryPolicy(max_attempts=3, delay=ms_to_ns(100))
    middleware = RetryMiddleware(policy)
    
    ctx = AttemptContext(
        attempt_number=1,
        success=True,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.NONE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    middleware.process_attempt(ctx, lambda c: None)
    
    assert ctx.should_retry == False
    print("✓ RetryMiddleware doesn't retry on success")


def test_load_limiter_blocks_retry():
    """Test that LoadLimiterMiddleware can block retries"""
    # Create retry budget with no budget
    budget = LimiterRetryBudgetPolicy(budget_ratio=0.1, max_retries=10)
    budget._tokens = 0  # Exhaust budget
    
    middleware = LoadLimiterMiddleware(budget)
    
    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.SERVER_FAILURE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    # Set initial retry intent
    ctx.should_retry = True
    ctx.retry_delay = ms_to_ns(100)
    
    middleware.process_attempt(ctx, lambda c: None)
    
    # Limiter should block retry
    assert ctx.should_retry == False
    assert ctx.metadata.get('limiter_blocked') == True
    print("✓ LoadLimiterMiddleware blocks retry when budget exhausted")


def test_retry_and_load_limiter_composition():
    """Test that RetryMiddleware and LoadLimiterMiddleware compose correctly"""
    retry_policy = FixedBackoffRetryPolicy(max_attempts=3, delay=ms_to_ns(100))
    budget = LimiterRetryBudgetPolicy(budget_ratio=0.1, max_retries=10)
    budget._tokens = 200  # Have budget
    
    chain = MiddlewareChain([
        RetryMiddleware(retry_policy),
        LoadLimiterMiddleware(budget),
    ])
    
    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.SERVER_FAILURE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    chain.execute(ctx, lambda c: None)
    
    # Both should allow retry
    assert ctx.should_retry == True
    assert ctx.retry_delay == ms_to_ns(100)
    print("✓ RetryMiddleware and LoadLimiterMiddleware compose correctly")


def test_no_retry_middleware():
    """Test that NoRetryMiddleware blocks all retries"""
    middleware = NoRetryMiddleware()
    
    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.SERVER_FAILURE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test",
    )
    
    middleware.process_attempt(ctx, lambda c: None)
    
    assert ctx.should_retry == False
    print("✓ NoRetryMiddleware blocks all retries")


if __name__ == "__main__":
    print("Running middleware tests...\n")
    test_middleware_chain_execution_order()
    test_retry_middleware_allows_retry()
    test_retry_middleware_no_retry_on_success()
    test_load_limiter_blocks_retry()
    test_retry_and_load_limiter_composition()
    test_no_retry_middleware()
    print("\n✅ All tests passed!")
