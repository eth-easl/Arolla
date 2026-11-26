"""
Test deadline propagation (propagate_deadline).

By default, the service knows only the overall request deadline, not the
client attempt timeout. With propagate_deadline enabled, the service
stops processing the call when the client stops waiting. The client
records the same outcome in both cases.

Tests cover:
- Deadline propagation from clients to services
- Deadline propagation from services to their dependencies, enabled and disabled
- YAML configuration
"""

import pytest

from simulator.config.loader import ConfigLoader
from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, ns_to_ms

DEADLINE, FAILURE, OK = DropReason.DEADLINE, DropReason.SERVER_FAILURE, DropReason.NONE


def make_service(median_ms=20, fail_until_ms=0, max_attempts=None):
    failures = [PartialFailure(duration=TimeInterval(begin=0, end=ms_to_ns(fail_until_ms)), p_fail=1.0)]
    retry = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0) if max_attempts else None
    cfg = ServiceConfig(
        name="svc", latency_median=ms_to_ns(median_ms), latency_lognorm_sigma=0.0, workers=1,
        partial_failures=failures if fail_until_ms else [], retry=retry,
    )
    return ServiceRuntime(cfg=cfg).bind(seed=1, record_events=True)


def run_one_request(service, propagate_deadline, max_attempts=None, global_ms=None):
    """One request from a client that waits 15 ms for each attempt."""
    timeout = StaticTimeout(
        attempt_timeout=ms_to_ns(15), global_timeout=ms_to_ns(global_ms) if global_ms else None
    )
    retry = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0) if max_attempts else None
    client = ClientRuntime(
        cfg=ClientConfig(retry=retry, timeout=timeout, propagate_deadline=propagate_deadline),
        service=service,
    )
    sim = Simulator(seed=1)
    client.start_request(sim)
    sim.run()
    (root,) = client.roots
    return [(round(ns_to_ms(a.interval.end)), a.drop_reason) for a in root.attempts]


def service_attempts(service):
    """(end in ms, drop reason) of every attempt the service made."""
    return [(round(ns_to_ms(event[0])), event[3]) for event in service.events]


def test_by_default_the_service_finishes_a_call_the_client_gave_up_on():
    """Test that by default the service finishes a call the client gave up on"""
    service = make_service(median_ms=20)
    assert run_one_request(service, propagate_deadline=False) == [(15, DEADLINE)]
    assert service_attempts(service) == [(20, OK)]


def test_with_the_deadline_the_service_stops_when_the_client_stops_waiting():
    """Test that with `propagate_deadline` the service stops when the client stops waiting"""
    service = make_service(median_ms=20)
    assert run_one_request(service, propagate_deadline=True) == [(15, DEADLINE)]
    assert service_attempts(service) == [(15, DEADLINE)]
    assert service.in_flight == 0


def test_the_service_stops_retrying_once_the_client_has_given_up():
    """Test that the service stops retrying once the client has given up.

    The service takes 10 ms, fails, and retries up to three times. By default it makes
    all three attempts. With the deadline, its second attempt is cut at 15 ms and there
    is no third.
    """
    by_default = make_service(median_ms=10, fail_until_ms=100, max_attempts=3)
    run_one_request(by_default, propagate_deadline=False)
    assert service_attempts(by_default) == [(10, FAILURE), (20, FAILURE), (30, FAILURE)]

    told = make_service(median_ms=10, fail_until_ms=100, max_attempts=3)
    run_one_request(told, propagate_deadline=True)
    assert service_attempts(told) == [(10, FAILURE), (15, DEADLINE)]


def test_the_clients_retry_does_not_wait_behind_the_call_it_replaces():
    """Test that the client's retry does not wait behind the call it replaces.

    One worker, 20 ms of work, a client that retries once. By default the retry sent at
    15 ms waits until the first call is done at 20 ms. With the deadline, the service has
    dropped the first call at 15 ms and starts the retry at once.
    """
    by_default = make_service(median_ms=20)
    assert run_one_request(by_default, False, max_attempts=2) == [(15, DEADLINE), (30, DEADLINE)]
    assert service_attempts(by_default) == [(20, OK), (40, OK)]

    told = make_service(median_ms=20)
    assert run_one_request(told, True, max_attempts=2) == [(15, DEADLINE), (30, DEADLINE)]
    assert service_attempts(told) == [(15, DEADLINE), (30, DEADLINE)]


def test_an_earlier_deadline_of_the_whole_request_still_applies():
    """Test that an earlier deadline of the whole request still applies"""
    service = make_service(median_ms=20)
    assert run_one_request(service, propagate_deadline=True, global_ms=10) == [(10, DEADLINE)]
    assert service_attempts(service) == [(10, DEADLINE)]


# A service passes its own attempt deadline on to its dependencies, unless it is given
# `propagate_deadline: false`. Below, service A gives up on an attempt after 15 ms.


def make_caller(deps, propagate_deadline):
    cfg = ServiceConfig(
        name="a", latency_median=ms_to_ns(5), latency_lognorm_sigma=0.0, workers=1,
        timeout=StaticTimeout(attempt_timeout=ms_to_ns(15)),
    )
    return ServiceRuntime(cfg=cfg, dependencies=deps, propagate_deadline=propagate_deadline).bind(
        seed=1, record_events=True
    )


def call(target):
    """Send one call; returns (when it was answered in ms, drop reason)."""
    sim = Simulator(seed=1)
    answers = []
    target.submit_call(sim, lambda r: answers.append((round(ns_to_ms(sim.timestep)), r.drop_reason)))
    sim.run()
    (answer,) = answers
    return answer


def test_by_default_a_dependency_stops_at_the_deadline_of_its_caller():
    """Test that by default a dependency stops at the deadline of its caller"""
    b = make_service(median_ms=20)
    assert call(make_caller([b], propagate_deadline=True)) == (15, DEADLINE)
    assert service_attempts(b) == [(15, DEADLINE)]


def test_without_propagation_the_dependency_finishes_after_its_caller_gave_up():
    """Test that without propagation the dependency finishes after its caller gave up"""
    b = make_service(median_ms=20)
    a = make_caller([b], propagate_deadline=False)
    assert call(a) == (15, DEADLINE)
    assert service_attempts(a) == [(15, DEADLINE)]
    assert service_attempts(b) == [(20, OK)]
    assert a.in_flight == 0


def test_without_propagation_the_dependency_also_keeps_retrying():
    """Test that without propagation the dependency also keeps retrying.

    B takes 10 ms, fails, and retries up to three times.
    """
    b = make_service(median_ms=10, fail_until_ms=100, max_attempts=3)
    assert call(make_caller([b], propagate_deadline=True)) == (15, DEADLINE)
    assert service_attempts(b) == [(10, FAILURE), (15, DEADLINE)]

    b = make_service(median_ms=10, fail_until_ms=100, max_attempts=3)
    assert call(make_caller([b], propagate_deadline=False)) == (15, DEADLINE)
    assert service_attempts(b) == [(10, FAILURE), (20, FAILURE), (30, FAILURE)]


def test_after_giving_up_the_service_does_not_call_its_next_dependency():
    """Test that a service that gave up does not call its next dependency.

    A calls B and then C. B answers at 20 ms, after A gave up: C is never called.
    """
    b, c = make_service(median_ms=20), make_service(median_ms=1)
    assert call(make_caller([b, c], propagate_deadline=False)) == (15, DEADLINE)
    assert service_attempts(b) == [(20, OK)]
    assert service_attempts(c) == []


def test_an_answer_in_time_is_used_as_usual():
    """Test that an answer that arrives in time is used as usual.

    B answers after 5 ms, then A does its own 5 ms of work.
    """
    b = make_service(median_ms=5)
    assert call(make_caller([b], propagate_deadline=False)) == (10, OK)


CONFIG = """
name: deadline_propagation
services:
  - name: db
    latency: {median_ms: 1}
    workers: 1
  - name: api
    latency: {median_ms: 1}
    workers: 1
    dependencies: [{service: db}]
    propagate_deadline: false
clients:
  - name: plain
    workload: {base_rps: 10, duration_s: 1}
    timeout: {attempt_ms: 15}
  - name: told
    workload: {base_rps: 10, duration_s: 1}
    timeout: {attempt_ms: 15}
    propagate_deadline: true
"""


def test_the_yaml_key_reaches_the_client_and_the_service():
    """Test that the YAML key reaches the client and the service"""
    _, clients, _, _, services = ConfigLoader.build_simulation(ConfigLoader.load_from_string(CONFIG))
    assert [client.cfg.propagate_deadline for client in clients] == [False, True]
    assert services["api"].propagate_deadline is False
    assert services["db"].propagate_deadline is True


def test_the_key_is_refused_on_a_client_without_an_attempt_timeout():
    """Test that `propagate_deadline` is refused on a client without an attempt timeout"""
    with pytest.raises(ValueError, match="needs timeout.attempt_ms"):
        ConfigLoader.load_from_string(CONFIG.replace("attempt_ms: 15", "global_ms: 100"))
