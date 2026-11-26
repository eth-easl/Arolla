"""
Test the local_work: first option.

By default, a service performs its local work after its dependency calls
complete. With local_work: first, it performs local work before calling
its dependencies. In both cases, the worker remains occupied throughout
the attempt.

The tests use service A with 10 ms of local work and a dependency B
with a service time of 20 ms.
"""

from simulator.config.loader import ConfigLoader
from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, ns_to_ms, s_to_ns

ALWAYS = [PartialFailure(duration=TimeInterval(begin=0, end=s_to_ns(10)), p_fail=1.0)]


def service(name, median_ms, deps=(), work_first=False, fails=False, timeout_ms=None, workers=10):
    cfg = ServiceConfig(
        name=name, latency_median=ms_to_ns(median_ms), latency_lognorm_sigma=0.0, workers=workers,
        partial_failures=ALWAYS if fails else [],
        timeout=StaticTimeout(attempt_timeout=ms_to_ns(timeout_ms)) if timeout_ms else None,
    )
    return ServiceRuntime(cfg=cfg, dependencies=list(deps), local_work_first=work_first).bind(
        seed=1, record_events=True
    )


def call(target, times=1):
    """Send calls at t = 0; returns (when it was answered in ms, success, drop reason) of each."""
    sim = Simulator(seed=1)
    answers = []
    for _ in range(times):
        target.submit_call(sim, lambda r: answers.append((round(ns_to_ms(sim.timestep)), r.success, r.drop_reason)))
    sim.run()
    return answers


def ended_at_ms(target):
    """When each attempt of the service ended."""
    return [round(ns_to_ms(event[0])) for event in target.events]


def test_by_default_the_dependency_is_called_first():
    """Test that by default the dependency is called first"""
    b = service("b", 20)
    assert call(service("a", 10, deps=[b])) == [(30, True, DropReason.NONE)]
    assert ended_at_ms(b) == [20]


def test_with_work_first_the_dependency_is_called_after_the_services_own_work():
    """Test that with `local_work: first` the dependency is called after the service's work"""
    b = service("b", 20)
    assert call(service("a", 10, deps=[b], work_first=True)) == [(30, True, DropReason.NONE)]
    assert ended_at_ms(b) == [30]


def test_if_the_work_fails_the_dependency_is_never_called():
    """Test that the dependency is never called if the service's own work fails.

    By default the dependency has already done its work when the service fails.
    """
    b = service("b", 20)
    assert call(service("a", 10, deps=[b], fails=True)) == [(30, False, DropReason.SERVER_FAILURE)]
    assert ended_at_ms(b) == [20]

    b = service("b", 20)
    assert call(service("a", 10, deps=[b], fails=True, work_first=True)) == [(10, False, DropReason.SERVER_FAILURE)]
    assert ended_at_ms(b) == []


def test_if_the_work_runs_past_the_deadline_the_dependency_is_never_called():
    """Test that the dependency is never called if the service's work runs past the deadline"""
    b = service("b", 20)
    a = service("a", 10, deps=[b], work_first=True, timeout_ms=5)
    assert call(a) == [(5, False, DropReason.DEADLINE)]
    assert ended_at_ms(b) == []


def test_the_worker_is_kept_until_the_dependency_has_answered():
    """Test that the service keeps its worker until the dependency has answered.

    One worker and two calls: the second starts when the first is answered, at 30 ms.
    """
    a = service("a", 10, deps=[service("b", 20)], work_first=True, workers=1)
    assert call(a, times=2) == [(30, True, DropReason.NONE), (60, True, DropReason.NONE)]


def test_the_yaml_key_reaches_the_service():
    """Test that the YAML key reaches the service"""
    config = """
name: work_first
services:
  - name: b
    latency: {median_ms: 1}
    workers: 1
  - name: a
    latency: {median_ms: 1}
    workers: 1
    dependencies: [{service: b}]
    local_work: first
workload: {base_rps: 10, duration_s: 1}
"""
    *_, services = ConfigLoader.build_simulation(ConfigLoader.load_from_string(config))
    assert services["a"].local_work_first is True
    assert services["b"].local_work_first is False
