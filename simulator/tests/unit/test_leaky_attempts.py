"""
Test the attempt limit of the leaky rate limiter.

As with other backoff policies, max_attempts includes the initial attempt.
For example, max_attempts: 3 allows one initial attempt and two retries.
"""

from simulator.policies.load_limiter import LeakyRateLimiterPolicy
from simulator.policies.retry import FixedBackoffRetryPolicy, RetryContext
from simulator.utils.time import s_to_ns


def attempts_made(policy):
    """Total attempts of a request whose every attempt fails."""
    attempt = 1
    while policy.next_delay(RetryContext(attempt=attempt, now=0))[0]:
        attempt += 1
    return attempt


def test_it_makes_max_attempts_attempts():
    """Test that a request makes exactly `max_attempts` attempts"""
    for max_attempts in (1, 2, 3, 7):
        limiter = LeakyRateLimiterPolicy(max_requests=1000, max_attempts=max_attempts, period=s_to_ns(1))
        assert attempts_made(limiter) == max_attempts


def test_it_agrees_with_the_backoff_policies():
    """Test that the leaky limiter and a fixed backoff make the same number of attempts"""
    for max_attempts in (1, 3, 5):
        limiter = LeakyRateLimiterPolicy(max_requests=1000, max_attempts=max_attempts, period=s_to_ns(1))
        backoff = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0)
        assert attempts_made(limiter) == attempts_made(backoff)


def test_a_denied_retry_reserves_no_slot():
    """Test that a denied retry does not use a slot"""
    limiter = LeakyRateLimiterPolicy(max_requests=10, max_attempts=2, period=s_to_ns(1))
    assert limiter.next_delay(RetryContext(attempt=1, now=0)) == (True, 0)
    assert limiter.next_delay(RetryContext(attempt=2, now=0)) == (False, 0)
    # The next request is spaced after the ONE slot that was handed out.
    assert limiter.next_delay(RetryContext(attempt=1, now=0)) == (True, s_to_ns(1) // 10)
