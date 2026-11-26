"""
Test parallel dependency calls with dependency_join: fail_fast.

By default, a service waits for all parallel dependency calls to complete
before reporting a failure. With fail_fast, it reports failure as soon as
a required dependency call fails. Outstanding calls continue running,
but the caller no longer waits for them.

The tests use service A with two dependencies: one fails after 10 ms,
the other succeeds after 100 ms.
"""

import pytest

from simulator.config.loader import ConfigLoader
from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, ns_to_ms, s_to_ns

ALWAYS = [PartialFailure(duration=TimeInterval(begin=0, end=s_to_ns(10)), p_fail=1.0)]


def service(name, median_ms, fails=False, timeout_ms=None, workers=10, queue_capacity=None,
            deps=(), optional=(), fail_fast=False):
    cfg = ServiceConfig(
        name=name, latency_median=ms_to_ns(median_ms), latency_lognorm_sigma=0.0, workers=workers,
        queue_capacity=queue_capacity, partial_failures=ALWAYS if fails else [],
        timeout=StaticTimeout(attempt_timeout=ms_to_ns(timeout_ms)) if timeout_ms else None,
    )
    return ServiceRuntime(
        cfg=cfg, dependencies=list(deps),
        dependency_optionality=[dep in optional for dep in deps],
        dependency_call_pattern="parallel", dependency_fail_fast=fail_fast,
    ).bind(seed=1, record_events=True)


def call(target, sim=None):
    """Send one call; returns (when it was answered in ms, success, drop reason)."""
    sim = sim or Simulator(seed=1)
    answers = []
    target.submit_call(sim, lambda r: answers.append((round(ns_to_ms(sim.timestep)), r.success, r.drop_reason)))
    sim.run()
    (answer,) = answers
    return answer


def test_by_default_the_service_waits_for_the_slow_call():
    """Test that by default the service waits for the slow call before it reports a failure"""
    a = service("a", 10, deps=[service("fast", 10, fails=True), service("slow", 100)])
    assert call(a) == (100, False, DropReason.SERVER_FAILURE)


def test_with_fail_fast_it_gives_up_when_the_first_call_fails():
    """Test that with `fail_fast` the service gives up when the first call fails"""
    slow = service("slow", 100)
    a = service("a", 10, deps=[service("fast", 10, fails=True), slow], fail_fast=True)
    assert call(a) == (10, False, DropReason.SERVER_FAILURE)
    # The slow call was not stopped: it finished its work at 100 ms.
    assert [(round(ns_to_ms(e[0])), e[2]) for e in slow.events] == [(100, True)]
    assert a.in_flight == 0


def test_a_failed_optional_call_does_not_end_the_attempt():
    """Test that a failed optional call does not end the attempt.

    A waits for the slow call, then does its own 10 ms of work.
    """
    fast = service("fast", 10, fails=True)
    a = service("a", 10, deps=[fast, service("slow", 100)], optional=[fast], fail_fast=True)
    assert call(a) == (110, True, DropReason.NONE)


def test_the_reason_is_the_one_of_the_first_failed_call():
    """Test that with `fail_fast` the drop reason is the one of the first failed call.

    One call fails after 10 ms, another times out after 50 ms. By default the service
    reports the more serious of the two (the timeout). With fail_fast it reports the first.
    """
    def deps():
        return [service("fails", 10, fails=True), service("times_out", 100, timeout_ms=50)]

    assert call(service("a", 10, deps=deps())) == (50, False, DropReason.DEADLINE)
    assert call(service("a", 10, deps=deps(), fail_fast=True)) == (10, False, DropReason.SERVER_FAILURE)


def test_a_call_refused_at_once_does_not_stop_the_other_calls_from_being_sent():
    """Test that a call refused at once does not stop the other calls from being sent.

    The first dependency has one worker, which is busy, and no queue: it refuses the
    call on the spot. The second dependency is still called.
    """
    sim = Simulator(seed=1)
    full = service("full", 100, workers=1, queue_capacity=0)
    full.submit_call(sim, lambda result: None)  # keeps the only worker busy
    slow = service("slow", 100)
    a = service("a", 10, deps=[full, slow], fail_fast=True)
    assert call(a, sim) == (0, False, DropReason.QUEUE_FULL)
    assert len(slow.events) == 1


CONFIG = """
name: fail_fast
services:
  - name: b
    latency: {median_ms: 1}
    workers: 1
  - name: a
    latency: {median_ms: 1}
    workers: 1
    dependencies: [{service: b}]
    dependency_call_pattern: parallel
    dependency_join: fail_fast
workload: {base_rps: 10, duration_s: 1}
"""


def test_the_yaml_key_reaches_the_service():
    """Test that the YAML key reaches the service"""
    *_, services = ConfigLoader.build_simulation(ConfigLoader.load_from_string(CONFIG))
    assert services["a"].dependency_fail_fast is True
    assert services["b"].dependency_fail_fast is False


def test_fail_fast_is_refused_for_sequential_dependencies():
    """Test that `fail_fast` is refused for sequential dependencies"""
    with pytest.raises(ValueError, match="needs dependency_call_pattern 'parallel'"):
        ConfigLoader.load_from_string(CONFIG.replace("dependency_call_pattern: parallel", ""))
