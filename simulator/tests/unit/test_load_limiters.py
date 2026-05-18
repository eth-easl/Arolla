"""
Unit tests for load limiter policies.

Tests circuit breakers, rate limiters, and retry budgets.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator.core.engine import Simulator
from simulator.core.types import TimePoint
from simulator.policies.load_limiter import (
    NoLoadLimiter,
    LeakyRateLimiterPolicy,
    BurstyRateLimiterPolicy,
    FixedWindowBurstyLimiterPolicy,
)
from simulator.policies.retry_controls import (
    CBState,
    CountBasedCircuitBreakerPolicy,
    LimiterTimeBasedCircuitBreakerPolicy as TimeBasedCircuitBreakerPolicy,
    LimiterRetryBudgetPolicy as RetryBudgetPolicy,
)
from simulator.policies.retry import RetryContext
from simulator.utils.time import ms_to_ns, s_to_ns


# ============================================================================
# Circuit Breaker Tests
# ============================================================================

def test_count_based_circuit_breaker_opens():
    """Test that count-based circuit breaker opens on failures"""
    # Open circuit if 5 out of 10 requests fail
    cb = CountBasedCircuitBreakerPolicy(
        failure_threshold_ratio=(5, 10),
        success_threshold_ratio=(3, 5),
        half_open_delay=ms_to_ns(1000)
    )
    
    assert cb.get_state() == CBState.CLOSED
    
    # Add 4 failures and 5 successes (not enough to open)
    for _ in range(4):
        cb.add_result(False, 0)
    for _ in range(5):
        cb.add_result(True, 0)
    
    assert cb.get_state() == CBState.CLOSED
    
    # Add 1 more failure (now 5 failures in window of 10)
    cb.add_result(False, 0)
    
    assert cb.get_state() == CBState.OPEN
    print("✓ Count-based circuit breaker opens on failures")


def test_count_based_circuit_breaker_blocks_when_open():
    """Test that open circuit breaker blocks retries"""
    cb = CountBasedCircuitBreakerPolicy(
        failure_threshold_ratio=(3, 5),
        success_threshold_ratio=(2, 3),
        half_open_delay=ms_to_ns(500)
    )
    
    # Trigger circuit to open (need to fill window of 5, with 3 failures)
    cb.add_result(False, 0)
    cb.add_result(False, 0)
    cb.add_result(True, 0)
    cb.add_result(True, 0)
    cb.add_result(False, 0)  # 3 failures out of 5
    
    assert cb.get_state() == CBState.OPEN
    
    # Should block retries
    ctx = RetryContext(attempt=1, now=0)
    should_allow, delay = cb.next_delay(ctx)
    assert should_allow == False
    assert delay == 0
    
    print("✓ Open circuit breaker blocks retries")


def test_count_based_circuit_breaker_closes():
    """Test that circuit breaker closes after successes"""
    cb = CountBasedCircuitBreakerPolicy(
        failure_threshold_ratio=(3, 5),
        success_threshold_ratio=(2, 3),
        half_open_delay=ms_to_ns(500)
    )
    
    # Open the circuit (3 failures out of 5)
    cb.add_result(False, 0)
    cb.add_result(False, 0)
    cb.add_result(True, 0)
    cb.add_result(True, 0)
    cb.add_result(False, 0)
    assert cb.get_state() == CBState.OPEN
    
    # Add successes to close it (need 2 out of 3 after half_open_delay)
    cb.add_result(True, ms_to_ns(600))  # After half_open_delay
    cb.add_result(True, ms_to_ns(700))
    cb.add_result(False, ms_to_ns(800))  # 2 successes out of 3
    
    assert cb.get_state() == CBState.CLOSED
    print("✓ Circuit breaker closes after successes")


def test_time_based_circuit_breaker():
    """Test time-based circuit breaker with sliding window"""
    cb = TimeBasedCircuitBreakerPolicy(
        failure_threshold_rate=0.5,  # Open if >50% failures
        success_threshold_rate=0.8,  # Close if >80% successes
        min_requests=5,
        window_duration=s_to_ns(10),
        half_open_delay=s_to_ns(1)
    )
    
    assert cb.get_state() == CBState.CLOSED
    
    # Add 3 failures and 2 successes (60% failure rate)
    now = 0
    for i in range(3):
        cb.add_result(False, now + i * s_to_ns(1))
    for i in range(2):
        cb.add_result(True, now + (i + 3) * s_to_ns(1))
    
    # Should open (5 requests, 60% failure rate > 50%)
    assert cb.get_state() == CBState.OPEN
    
    print("✓ Time-based circuit breaker works")


def test_time_based_circuit_breaker_window_eviction():
    """Test that time-based circuit breaker evicts old results"""
    cb = TimeBasedCircuitBreakerPolicy(
        failure_threshold_rate=0.5,
        success_threshold_rate=0.8,
        min_requests=3,
        window_duration=s_to_ns(5),
        half_open_delay=s_to_ns(1)
    )
    
    # Add failures at t=0
    for i in range(3):
        cb.add_result(False, i * s_to_ns(1))
    
    assert cb.get_state() == CBState.OPEN
    
    # Add successes at t=10 (old failures should be evicted)
    for i in range(3):
        cb.add_result(True, s_to_ns(10) + i * s_to_ns(1))
    
    # After half_open_delay, should close (100% success rate)
    cb.add_result(True, s_to_ns(15))
    assert cb.get_state() == CBState.CLOSED
    
    print("✓ Time-based circuit breaker evicts old results")


# ============================================================================
# Retry Budget Tests
# ============================================================================

def test_retry_budget_basic():
    """Test basic retry budget behavior"""
    budget = RetryBudgetPolicy(
        budget_ratio=0.1,  # 10% of successes
        max_retries=10
    )
    
    # Start with full budget
    assert budget.can_retry() == True
    
    # Consume budget
    ctx = RetryContext(attempt=1)
    should_allow, delay = budget.next_delay(ctx)
    assert should_allow == True
    
    # Budget should be reduced
    assert budget.can_retry() == True  # Still have budget
    
    print("✓ Retry budget basic behavior works")


def test_retry_budget_exhaustion():
    """Test that retry budget can be exhausted"""
    budget = RetryBudgetPolicy(
        budget_ratio=0.1,
        max_retries=2  # Small budget
    )
    
    # Exhaust budget by retrying without successes
    for _ in range(2):
        ctx = RetryContext(attempt=1)
        budget.next_delay(ctx)
    
    # Budget should be exhausted
    assert budget.can_retry() == False
    
    ctx = RetryContext(attempt=1)
    should_allow, delay = budget.next_delay(ctx)
    assert should_allow == False
    
    print("✓ Retry budget can be exhausted")


def test_retry_budget_replenishment():
    """Test that retry budget is replenished by successes"""
    budget = RetryBudgetPolicy(
        budget_ratio=0.1,  # 10% ratio means 10 successes = 1 retry
        max_retries=10
    )
    
    # Exhaust budget
    for _ in range(10):
        ctx = RetryContext(attempt=1)
        budget.next_delay(ctx)
    
    assert budget.can_retry() == False
    
    # Add successes to replenish (need 10 successes per retry at 0.1 ratio)
    # Add 50 successes to get 5 retries worth of budget
    for _ in range(50):
        budget.add_result(True)
    
    # Should have budget again
    assert budget.can_retry() == True
    
    print("✓ Retry budget is replenished by successes")


# ============================================================================
# Rate Limiter Tests
# ============================================================================

def test_leaky_rate_limiter():
    """Test leaky bucket rate limiter"""
    limiter = LeakyRateLimiterPolicy(
        max_requests=10,
        max_attempts=5,
        period=s_to_ns(1)
    )
    
    # First request should have no delay
    ctx = RetryContext(attempt=1, now=0)
    should_allow, delay = limiter.next_delay(ctx)
    assert should_allow == True
    assert delay == 0
    
    # Second request should have delay
    ctx = RetryContext(attempt=2, now=0)
    should_allow, delay = limiter.next_delay(ctx)
    assert should_allow == True
    assert delay > 0  # Should have some delay
    
    print("✓ Leaky rate limiter works")


def test_leaky_rate_limiter_max_attempts():
    """Test that leaky rate limiter respects max_attempts"""
    limiter = LeakyRateLimiterPolicy(
        max_requests=10,
        max_attempts=3,
        period=s_to_ns(1)
    )
    
    # Attempts 1-3 should be allowed
    for attempt in [1, 2, 3]:
        ctx = RetryContext(attempt=attempt, now=0)
        should_allow, delay = limiter.next_delay(ctx)
        assert should_allow == True
    
    # Attempt 4 should be blocked
    ctx = RetryContext(attempt=4, now=0)
    should_allow, delay = limiter.next_delay(ctx)
    assert should_allow == False
    
    print("✓ Leaky rate limiter respects max_attempts")


def test_bursty_rate_limiter():
    """Test bursty (token bucket) rate limiter"""
    limiter = BurstyRateLimiterPolicy(
        max_requests=10,
        refill_rate=1,
        period=s_to_ns(1)
    )
    
    # Should start with full bucket
    assert limiter.can_retry(0) == True
    
    # Consume all tokens
    for _ in range(10):
        ctx = RetryContext(attempt=1, now=0)
        limiter.next_delay(ctx)
    
    # Should be empty
    assert limiter.can_retry(0) == False
    
    # Should block retry
    ctx = RetryContext(attempt=1, now=0)
    should_allow, delay = limiter.next_delay(ctx)
    assert should_allow == False
    
    print("✓ Bursty rate limiter works")


def test_fixed_window_limiter():
    """Test fixed window rate limiter"""
    limiter = FixedWindowBurstyLimiterPolicy(
        max_requests=5,
        period=s_to_ns(10)
    )
    
    # Should start with full tokens
    assert limiter.can_retry(0) == True
    
    # Consume all tokens in first window
    for _ in range(5):
        ctx = RetryContext(attempt=1, now=s_to_ns(1))
        limiter.next_delay(ctx)
    
    # Should be exhausted
    assert limiter.can_retry(s_to_ns(1)) == False
    
    # Move to next window
    assert limiter.can_retry(s_to_ns(11)) == True
    
    print("✓ Fixed window rate limiter works")


def test_no_load_limiter():
    """Test that NoLoadLimiter always allows retries"""
    limiter = NoLoadLimiter()
    
    for attempt in range(1, 10):
        ctx = RetryContext(attempt=attempt)
        should_allow, delay = limiter.next_delay(ctx)
        assert should_allow == True
        assert delay == 0
    
    print("✓ NoLoadLimiter always allows retries")


if __name__ == "__main__":
    print("Running load limiter tests...\n")
    
    # Circuit breaker tests
    test_count_based_circuit_breaker_opens()
    test_count_based_circuit_breaker_blocks_when_open()
    test_count_based_circuit_breaker_closes()
    test_time_based_circuit_breaker()
    test_time_based_circuit_breaker_window_eviction()
    
    # Retry budget tests
    test_retry_budget_basic()
    test_retry_budget_exhaustion()
    test_retry_budget_replenishment()
    
    # Rate limiter tests
    test_leaky_rate_limiter()
    test_leaky_rate_limiter_max_attempts()
    test_bursty_rate_limiter()
    test_fixed_window_limiter()
    test_no_load_limiter()
    
    print("\n✅ All load limiter tests passed!")
