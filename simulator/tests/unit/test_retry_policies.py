"""
Unit tests for retry policies.

Tests all retry policy implementations including fixed backoff,
exponential backoff, and jittered backoff.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from simulator.policies.retry import (
    RetryPolicy, RetryContext, NoRetryPolicy,
    FixedBackoffRetryPolicy, ExponentialBackoffRetryPolicy,
    ExponentialBackoffWithJitterRetryPolicy, JitterMode
)
from simulator.utils.time import ms_to_ns


def test_no_retry_policy():
    """Test that NoRetryPolicy never allows retries"""
    policy = NoRetryPolicy()
    
    for attempt in range(1, 10):
        ctx = RetryContext(attempt=attempt)
        should_retry, delay = policy.next_delay(ctx)
        assert should_retry == False
        assert delay == 0
    
    print("✓ NoRetryPolicy never allows retries")


def test_fixed_backoff_basic():
    """Test basic fixed backoff behavior"""
    policy = FixedBackoffRetryPolicy(max_attempts=3, delay=ms_to_ns(100))
    
    # Attempt 1: should retry with 100ms delay
    ctx = RetryContext(attempt=1)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    assert delay == ms_to_ns(100)
    
    # Attempt 2: should retry with 100ms delay
    ctx = RetryContext(attempt=2)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    assert delay == ms_to_ns(100)
    
    # Attempt 3: should NOT retry (reached max)
    ctx = RetryContext(attempt=3)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == False
    assert delay == 0
    
    print("✓ FixedBackoffRetryPolicy works correctly")


def test_fixed_backoff_single_attempt():
    """Test fixed backoff with max_attempts=1 (no retries)"""
    policy = FixedBackoffRetryPolicy(max_attempts=1, delay=ms_to_ns(50))
    
    ctx = RetryContext(attempt=1)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == False
    assert delay == 0
    
    print("✓ FixedBackoffRetryPolicy with max_attempts=1 works")


def test_exponential_backoff_basic():
    """Test exponential backoff delay progression"""
    policy = ExponentialBackoffRetryPolicy(
        max_attempts=5,
        initial_delay=ms_to_ns(10),
        max_delay=ms_to_ns(1000)
    )
    
    # Expected delays: 10, 20, 40, 80, ...
    expected_delays = [10, 20, 40, 80]
    
    for attempt, expected_ms in enumerate(expected_delays, start=1):
        ctx = RetryContext(attempt=attempt)
        should_retry, delay = policy.next_delay(ctx)
        assert should_retry == True
        assert delay == ms_to_ns(expected_ms), f"Attempt {attempt}: expected {expected_ms}ms, got {delay/1_000_000}ms"
    
    # Attempt 5: should NOT retry
    ctx = RetryContext(attempt=5)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == False
    
    print("✓ ExponentialBackoffRetryPolicy delays are correct")


def test_exponential_backoff_max_delay():
    """Test that exponential backoff respects max_delay"""
    policy = ExponentialBackoffRetryPolicy(
        max_attempts=10,
        initial_delay=ms_to_ns(100),
        max_delay=ms_to_ns(500)
    )
    
    # Delays: 100, 200, 400, 500 (capped), 500 (capped), ...
    ctx = RetryContext(attempt=1)
    _, delay = policy.next_delay(ctx)
    assert delay == ms_to_ns(100)
    
    ctx = RetryContext(attempt=2)
    _, delay = policy.next_delay(ctx)
    assert delay == ms_to_ns(200)
    
    ctx = RetryContext(attempt=3)
    _, delay = policy.next_delay(ctx)
    assert delay == ms_to_ns(400)
    
    # Should be capped at 500ms
    ctx = RetryContext(attempt=4)
    _, delay = policy.next_delay(ctx)
    assert delay == ms_to_ns(500)
    
    ctx = RetryContext(attempt=5)
    _, delay = policy.next_delay(ctx)
    assert delay == ms_to_ns(500)
    
    print("✓ ExponentialBackoffRetryPolicy respects max_delay")


def test_jittered_backoff_full_mode():
    """Test jittered backoff with full jitter mode"""
    rng = random.Random(42)  # Fixed seed for reproducibility
    policy = ExponentialBackoffWithJitterRetryPolicy(
        max_attempts=5,
        initial_delay=ms_to_ns(100),
        max_delay=ms_to_ns(1000),
        rng=rng,
        jitter_mode=JitterMode.FULL
    )
    
    # Full jitter: delay is uniform(0, exponential_delay)
    ctx = RetryContext(attempt=1)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    # Delay should be between 0 and 100ms
    assert 0 <= delay <= ms_to_ns(100)
    
    ctx = RetryContext(attempt=2)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    # Delay should be between 0 and 200ms
    assert 0 <= delay <= ms_to_ns(200)
    
    print("✓ JitteredBackoffRetryPolicy with FULL mode works")


def test_jittered_backoff_equal_mode():
    """Test jittered backoff with equal jitter mode"""
    rng = random.Random(42)
    policy = ExponentialBackoffWithJitterRetryPolicy(
        max_attempts=5,
        initial_delay=ms_to_ns(100),
        max_delay=ms_to_ns(1000),
        rng=rng,
        jitter_mode=JitterMode.EQUAL
    )
    
    # Equal jitter: delay is exp_delay/2 + uniform(0, exp_delay/2)
    ctx = RetryContext(attempt=1)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    # Delay should be between 50ms and 100ms
    assert ms_to_ns(50) <= delay <= ms_to_ns(100)
    
    ctx = RetryContext(attempt=2)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == True
    # Delay should be between 100ms and 200ms
    assert ms_to_ns(100) <= delay <= ms_to_ns(200)
    
    print("✓ JitteredBackoffRetryPolicy with EQUAL mode works")


def test_jittered_backoff_max_attempts():
    """Test that jittered backoff respects max_attempts"""
    rng = random.Random(42)
    policy = ExponentialBackoffWithJitterRetryPolicy(
        max_attempts=3,
        initial_delay=ms_to_ns(50),
        max_delay=ms_to_ns(500),
        rng=rng,
        jitter_mode=JitterMode.FULL
    )
    
    # Attempts 1 and 2 should allow retry
    for attempt in [1, 2]:
        ctx = RetryContext(attempt=attempt)
        should_retry, delay = policy.next_delay(ctx)
        assert should_retry == True
    
    # Attempt 3 should NOT allow retry
    ctx = RetryContext(attempt=3)
    should_retry, delay = policy.next_delay(ctx)
    assert should_retry == False
    assert delay == 0
    
    print("✓ JitteredBackoffRetryPolicy respects max_attempts")


def test_retry_context_with_now():
    """Test that RetryContext can include timestamp"""
    ctx = RetryContext(attempt=2, now=1000000000)
    assert ctx.attempt == 2
    assert ctx.now == 1000000000
    
    print("✓ RetryContext with timestamp works")


def test_all_policies_return_tuple():
    """Test that all policies return (bool, int) tuple"""
    policies = [
        NoRetryPolicy(),
        FixedBackoffRetryPolicy(max_attempts=2, delay=ms_to_ns(10)),
        ExponentialBackoffRetryPolicy(
            max_attempts=3,
            initial_delay=ms_to_ns(10),
            max_delay=ms_to_ns(100)
        ),
        ExponentialBackoffWithJitterRetryPolicy(
            max_attempts=3,
            initial_delay=ms_to_ns(10),
            max_delay=ms_to_ns(100),
            rng=random.Random(42),
            jitter_mode=JitterMode.FULL
        ),
    ]
    
    for policy in policies:
        ctx = RetryContext(attempt=1)
        result = policy.next_delay(ctx)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], int)
    
    print("✓ All policies return correct tuple format")


if __name__ == "__main__":
    print("Running retry policy tests...\n")
    test_no_retry_policy()
    test_fixed_backoff_basic()
    test_fixed_backoff_single_attempt()
    test_exponential_backoff_basic()
    test_exponential_backoff_max_delay()
    test_jittered_backoff_full_mode()
    test_jittered_backoff_equal_mode()
    test_jittered_backoff_max_attempts()
    test_retry_context_with_now()
    test_all_policies_return_tuple()
    print("\n✅ All retry policy tests passed!")
