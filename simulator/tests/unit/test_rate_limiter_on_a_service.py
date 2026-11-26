"""
Test service-side retry rate limiting.

When the rate limiter rejects a retry, the request fails without stopping
the simulation. For example, a fixed-window limiter configured at 6 retries
per second allows at most 24 retries over 4 seconds.
"""

import pytest

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns

SCENARIO = """
name: rate_limited_retries
seed: 5
services:
  - name: svc
    latency: {{median_ms: 5, lognorm_sigma: 0.2}}
    workers: 8
    retry: {{type: fixed, max_attempts: 4, delay_ms: 0}}
    rate_limiter: {limiter}
    partial_failures:
      - {{type: partial_failure, start_s: 0, end_s: 4, p_fail: 1.0}}
workload: {{base_rps: 50, duration_s: 4}}
"""


def run(limiter):
    cfg = ConfigLoader.load_from_string(SCENARIO.format(limiter=limiter))
    sim, clients, workloads, _, services = ConfigLoader.build_simulation(cfg)
    for client, workload in zip(clients, workloads):
        workload.drive(sim, lambda s, c=client: c.start_request(s))
    sim.run(until=s_to_ns(4))
    sim.run()
    return clients[0], services["svc"]


@pytest.mark.parametrize("limiter, retries_allowed", [
    # 5 tokens to start with, 2 more each second for 4 seconds.
    ("{type: bursty, max_requests: 5, period_ms: 1000, refill_rate: 2}", (5, 14)),
    # 6 retries in each of the four one-second windows.
    ("{type: fixed_window, max_requests: 6, period_ms: 1000}", (6, 24)),
])
def test_a_refused_retry_does_not_end_the_run(limiter, retries_allowed):
    """Test that a refused retry does not end the run"""
    client, service = run(limiter)

    requests = len(client.roots)
    assert requests > 150                       # the run went on to the end
    assert all(root.done for root in client.roots)
    retries = client.attempts_total - requests
    low, high = retries_allowed
    assert low <= retries <= high               # the limiter held the retries back ...
    assert retries < requests                   # ... to far fewer than the policy would make
    assert service.in_flight == 0


@pytest.mark.parametrize("kind", ["bursty", "fixed_window", "leaky"])
def test_a_zero_period_is_rejected_when_the_scenario_loads(kind):
    """Test that a limiter with `period_ms: 0` is refused when the scenario loads"""
    limiter = f"{{type: {kind}, max_requests: 5, period_ms: 0, refill_rate: 2}}"
    with pytest.raises(ValueError, match="period_ms"):
        ConfigLoader.load_from_string(SCENARIO.format(limiter=limiter))


def test_the_reason_names_the_limiter():
    """Test that each limiter names itself in the reason it gives for a refusal"""
    from simulator.policies.load_limiter import BurstyRateLimiterPolicy, LeakyRateLimiterPolicy
    from simulator.policies.retry import RetryContext
    from simulator.policies.retry_controls import (
        CountBasedCircuitBreakerPolicy,
        LimiterRetryBudgetPolicy,
        RetryCircuitBreakerPolicy,
    )

    bursty = BurstyRateLimiterPolicy(max_requests=1, refill_rate=1, period=s_to_ns(1))
    bursty.next_delay(RetryContext(attempt=2, now=0))            # takes the one token
    assert not bursty.next_delay(RetryContext(attempt=2, now=0))[0]
    assert bursty.refusal_reason() == "BurstyRateLimiterPolicy_budget_exhausted"

    budget = LimiterRetryBudgetPolicy(budget_ratio=0.1, max_retries=1)
    budget.next_delay(RetryContext(attempt=2, now=0))
    assert not budget.next_delay(RetryContext(attempt=2, now=0))[0]
    assert budget.refusal_reason() == "LimiterRetryBudgetPolicy_budget_exhausted"

    breaker = CountBasedCircuitBreakerPolicy(
        failure_threshold_ratio=(1, 1), success_threshold_ratio=(1, 1), half_open_delay=s_to_ns(60),
    )
    breaker.add_result(False, 0)                                 # opens it
    assert breaker.refusal_reason() == "CountBasedCircuitBreakerPolicy_state_OPEN"

    # Limiters with nothing more specific to say, including the retry policy
    # that a `retry_circuit_breaker` service uses as its limiter.
    leaky = LeakyRateLimiterPolicy(max_requests=1, max_attempts=1, period=s_to_ns(1))
    assert leaky.refusal_reason() == "LeakyRateLimiterPolicy_blocked"
    retry_breaker = RetryCircuitBreakerPolicy(
        inner=None, failure_rate_threshold=0.5, window_duration=s_to_ns(1), min_window_size=1,
    )
    assert retry_breaker.refusal_reason() == "RetryCircuitBreakerPolicy_blocked"
