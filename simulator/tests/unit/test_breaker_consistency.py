"""
Test failure thresholds for count-based, time-based and retry circuit
breakers on both clients and services.

A breaker opens when the failure rate in its window is greater than or
equal to failure_threshold. With a threshold of 0.15 and a window of
10 results, two failures trigger opening, but one does not.
A threshold of 1.0 requires all results in the window to be failures.
"""

import random

import pytest

from simulator.config.builders.policies import build_circuit_breaker, build_client_retry_policy
from simulator.config.schema import CircuitBreakerConfig, ClientConfigYAML, RetryConfig, WorkloadConfig
from simulator.policies.retry import RetryContext
from simulator.utils.time import ms_to_ns

BREAKERS = [
    ("service", "count_based"),
    ("service", "time_based"),
    ("service", "retry_circuit_breaker"),
    ("client", "time_based"),
    ("client", "retry_circuit_breaker"),
]


def breaker_config(kind, threshold, window):
    common = dict(type=kind, failure_threshold=threshold, half_open_delay_ms=1000)
    if kind == "count_based":
        return CircuitBreakerConfig(
            **common, success_threshold=0.5, failure_window_size=window, success_window_size=window
        )
    return CircuitBreakerConfig(**common, window_duration_ms=10_000, min_requests=window)


def refuses_a_retry(side, kind, threshold, window, failures):
    """Feed one full window with `failures` failures, then ask for a retry."""
    cfg = breaker_config(kind, threshold, window)
    if side == "service":
        breaker = build_circuit_breaker(cfg)
        record = lambda ok, t: breaker.add_result(ok, t)
    else:
        client = ClientConfigYAML(
            name="c",
            workload=WorkloadConfig(base_rps=1, duration_s=1),
            retry=RetryConfig(type="fixed", max_attempts=3, delay_ms=0),
            circuit_breaker=cfg,
        )
        breaker = build_client_retry_policy(client, random.Random(0))
        record = lambda ok, t: breaker.record_attempt(RetryContext(attempt=1, now=t), ok)

    results = [False] * failures + [True] * (window - failures)
    for i, ok in enumerate(results):
        record(ok, i * ms_to_ns(1))
    allowed, _ = breaker.next_delay(RetryContext(attempt=1, now=window * ms_to_ns(1)))
    return not allowed


@pytest.mark.parametrize("side, kind", BREAKERS)
@pytest.mark.parametrize("threshold, window, failures_below", [
    (0.1, 10, 0),
    (0.15, 10, 1),      # opens at 2 of 10, not 1
    (0.29, 100, 28),    # opens at 29 of 100, not 28
    (1.0, 10, 9),       # opens when every result failed
])
def test_every_breaker_opens_exactly_at_the_threshold(side, kind, threshold, window, failures_below):
    """Test that every breaker stays closed one failure below the threshold and opens at it"""
    assert not refuses_a_retry(side, kind, threshold, window, failures_below)
    assert refuses_a_retry(side, kind, threshold, window, failures_below + 1)
