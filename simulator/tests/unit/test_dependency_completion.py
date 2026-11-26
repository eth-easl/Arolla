"""
Test how services handle dependency call results.

The caller receives one result per dependency call, reflecting the final
attempt after all retries. For example, a failed attempt followed by a
successful retry produces one successful call.

Tests cover:
- Sequential dependencies
- Parallel dependencies
- Requests that complete without an attempt
- Stacks of retrying services configured through YAML
"""

from simulator.config.loader import ConfigLoader
from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.retry_controls import GlobalRetryBudget
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, s_to_ns


# ============================================================================
# Helpers
# ============================================================================

def make_service(name, deps=None, optionality=None, call_pattern="sequential",
                 median_ms=10, fail_until_ms=None, max_attempts=None, timeout=None,
                 load_limiter=None, seed=1):
    """A service whose attempts fail (p_fail = 1) until `fail_until_ms`."""
    failures = []
    if fail_until_ms is not None:
        failures.append(PartialFailure(
            duration=TimeInterval(begin=0, end=ms_to_ns(fail_until_ms)), p_fail=1.0,
        ))
    retry = None
    if max_attempts is not None:
        retry = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0)
    cfg = ServiceConfig(
        name=name,
        latency_median=ms_to_ns(median_ms),
        latency_lognorm_sigma=0.0,
        workers=10,
        partial_failures=failures,
        retry=retry,
        timeout=timeout,
        load_limiter=load_limiter,
    )
    deps = deps or []
    return ServiceRuntime(
        cfg=cfg,
        dependencies=deps,
        dependency_optionality=optionality or [False] * len(deps),
        dependency_call_pattern=call_pattern,
    ).bind(seed=seed, record_events=True)


def at_ms(timestamp_ns, expected_ms):
    """Whether a timestamp is `expected_ms` (service times carry a few ns of rounding)."""
    return abs(timestamp_ns - ms_to_ns(expected_ms)) < ms_to_ns(0.01)


def submit(sim, service, is_retry=False):
    """Submit one request; returns the list of attempts reported and the root-done count."""
    attempts = []
    root_done = []

    def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
        attempts.append((sim.timestep, success, drop_reason))

    service.submit_request(
        sim,
        on_attempt_done=on_attempt_done,
        on_root_done=lambda: root_done.append(sim.timestep),
        is_retry=is_retry,
    )
    return attempts, root_done


# ============================================================================
# Sequential dependencies
# ============================================================================

class TestSequentialDependency:
    def test_caller_waits_for_the_dependency_to_finish_retrying(self):
        """Test that the caller waits for the dependency to finish retrying.

        D fails its first attempt and succeeds on its retry: A succeeds, once.
        """
        sim = Simulator(seed=1)
        # D's attempts end at 10 ms (inside the fault) and at 20 ms (after it).
        svc_d = make_service("D", fail_until_ms=15, max_attempts=3)
        svc_a = make_service("A", deps=[svc_d])

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert len(svc_d.events) == 2
        assert [(ok, reason) for _, ok, reason in attempts] == [(True, DropReason.NONE)]
        assert len(root_done) == 1
        # 2 x 10 ms at D, then A's own 10 ms.
        assert at_ms(attempts[0][0], 30)
        assert svc_a.in_flight == 0 and svc_d.in_flight == 0

    def test_caller_fails_once_when_the_dependency_gives_up(self):
        """Test that the caller fails once when the dependency gives up.

        D fails all three attempts: A reports one failure, after the third.
        """
        sim = Simulator(seed=1)
        svc_d = make_service("D", fail_until_ms=10_000, max_attempts=3)
        svc_a = make_service("A", deps=[svc_d])

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert len(svc_d.events) == 3
        assert [(ok, reason) for _, ok, reason in attempts] == [(False, DropReason.SERVER_FAILURE)]
        assert at_ms(attempts[0][0], 30)
        assert len(root_done) == 1
        assert svc_a.in_flight == 0 and svc_d.in_flight == 0

    def test_the_next_dependency_is_called_once(self):
        """Test that the next dependency is called once.

        A calls D then E. D's retry must not make A call E a second time.
        """
        sim = Simulator(seed=1)
        svc_d = make_service("D", fail_until_ms=15, max_attempts=3)
        svc_e = make_service("E")
        svc_a = make_service("A", deps=[svc_d, svc_e])

        attempts, _ = submit(sim, svc_a)
        sim.run()

        assert len(svc_e.events) == 1
        assert [(ok, reason) for _, ok, reason in attempts] == [(True, DropReason.NONE)]
        assert svc_a.in_flight == 0

    def test_caller_retries_see_one_result_per_attempt(self):
        """Test that each attempt of a retrying caller sees one result.

        A retries too: each of A's attempts is one complete request to D.
        """
        sim = Simulator(seed=1)
        svc_d = make_service("D", fail_until_ms=10_000, max_attempts=2)
        svc_a = make_service("A", deps=[svc_d], max_attempts=3)

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert [reason for _, _, reason in attempts] == [DropReason.SERVER_FAILURE] * 3
        assert len(svc_d.events) == 3 * 2
        assert len(root_done) == 1
        assert svc_a.in_flight == 0 and svc_d.in_flight == 0


# ============================================================================
# Parallel dependencies
# ============================================================================

class TestParallelDependency:
    def test_caller_waits_for_every_dependency_to_finish_retrying(self):
        """Test that the caller waits for every dependency to finish retrying.

        B recovers on its retry while C is already done: A succeeds, once.
        """
        sim = Simulator(seed=1)
        svc_b = make_service("B", fail_until_ms=15, max_attempts=3)
        svc_c = make_service("C", median_ms=12)
        svc_a = make_service("A", deps=[svc_b, svc_c], call_pattern="parallel")

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert [(ok, reason) for _, ok, reason in attempts] == [(True, DropReason.NONE)]
        assert at_ms(attempts[0][0], 30)
        assert len(root_done) == 1
        assert svc_a.in_flight == 0

    def test_caller_fails_once_when_one_dependency_gives_up(self):
        """Test that the caller fails once when one of its dependencies gives up"""
        sim = Simulator(seed=1)
        svc_b = make_service("B", fail_until_ms=10_000, max_attempts=3)
        svc_c = make_service("C")
        svc_a = make_service("A", deps=[svc_b, svc_c], call_pattern="parallel")

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert [(ok, reason) for _, ok, reason in attempts] == [(False, DropReason.SERVER_FAILURE)]
        assert at_ms(attempts[0][0], 30)
        assert len(root_done) == 1
        assert svc_a.in_flight == 0


# ============================================================================
# Requests that end without an attempt
# ============================================================================

class TestRequestsThatEndWithoutAnAttempt:
    def test_dependency_called_at_the_deadline_fails_the_caller(self):
        """Test that a dependency called at the deadline fails the caller.

        A (15 ms per attempt) calls B, optional and too slow, then C. B is cut
        at A's deadline, so C is called when no time is left and makes no
        attempt. A must fail with a deadline, not wait forever.
        """
        sim = Simulator(seed=1)
        svc_b = make_service("B", median_ms=50)
        svc_c = make_service("C")
        svc_a = make_service(
            "A", deps=[svc_b, svc_c], optionality=[True, False],
            timeout=StaticTimeout(attempt_timeout=ms_to_ns(15)),
        )

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        assert len(svc_c.events) == 0
        assert attempts == [(ms_to_ns(15), False, DropReason.DEADLINE)]
        assert len(root_done) == 1
        assert svc_a.in_flight == 0

    def test_a_request_refused_at_admission_is_finished(self):
        """Test that a request refused at admission is finished.

        A refused retry reports its attempt AND the end of the request.
        """
        sim = Simulator(seed=1)
        empty_budget = GlobalRetryBudget(max_tokens=0, refill_rate=0, period=s_to_ns(1))
        svc = make_service("S", load_limiter=empty_budget)

        attempts, root_done = submit(sim, svc, is_retry=True)
        sim.run()

        assert attempts == [(0, False, DropReason.SERVER_FAILURE)]
        assert len(root_done) == 1
        assert svc.in_flight == 0

    def test_caller_of_a_refused_dependency_call_completes(self):
        """Test that the caller of a refused dependency call completes.

        A's retry reaches a dependency that refuses retries: the retry fails, A ends.
        """
        sim = Simulator(seed=1)
        empty_budget = GlobalRetryBudget(max_tokens=0, refill_rate=0, period=s_to_ns(1))
        svc_d = make_service("D", fail_until_ms=10_000, load_limiter=empty_budget)
        svc_a = make_service("A", deps=[svc_d], max_attempts=2)

        attempts, root_done = submit(sim, svc_a)
        sim.run()

        # First attempt: D is called and fails (10 ms). Second attempt is a
        # retry, which D refuses on arrival.
        assert [(ok, reason) for _, ok, reason in attempts] == [
            (False, DropReason.SERVER_FAILURE), (False, DropReason.SERVER_FAILURE),
        ]
        assert len(svc_d.events) == 1
        assert len(root_done) == 1
        assert svc_a.in_flight == 0


# ============================================================================
# A retry stack built from YAML
# ============================================================================

RETRY_STACK = """
name: retry_stack
seed: 7
services:
  - name: db
    latency: {median_ms: 50, lognorm_sigma: 0.5}
    workers: 10
    queue_capacity: 150
    partial_failures:
      - {type: partial_failure, start_s: 1, end_s: 4, p_fail: 0.7}
  - name: app
    latency: {median_ms: 10, lognorm_sigma: 0.2}
    workers: 8
    queue_capacity: 150
    timeout: {global_ms: 1500, attempt_ms: 550}
    dependencies: [{service: db}]
    retry: {type: fixed, max_attempts: 7, delay_ms: 0}
  - name: gw
    latency: {median_ms: 2, lognorm_sigma: 0.1}
    workers: 8
    queue_capacity: 150
    timeout: {global_ms: 2500, attempt_ms: 850}
    dependencies: [{service: app}]
    retry: {type: fixed, max_attempts: 7, delay_ms: 0}
clients:
  - name: user
    target_service: gw
    workload: {base_rps: 100, duration_s: 5}
    timeout: {global_ms: 3000, attempt_ms: 1000}
"""


class TestRetryStack:
    def test_workers_are_released_once_and_attempts_stay_bounded(self):
        """Test that workers are released once and attempts stay bounded.

        Retries at two layers: no service ends with a negative `in_flight`, and no
        layer makes more than `max_attempts` attempts per call it received.
        """
        cfg = ConfigLoader.load_from_string(RETRY_STACK)
        sim, clients, workloads, _, services = ConfigLoader.build_simulation(cfg)
        for client, workload in zip(clients, workloads):
            workload.drive(sim, lambda s, c=client: c.start_request(s))
        sim.run(until=s_to_ns(5))
        sim.run()

        requests = len(clients[0].roots)
        assert requests > 400
        for service in services.values():
            assert service.in_flight == 0, service.cfg.name
            assert len(service.queue) == 0, service.cfg.name

        gw_attempts = len(services["gw"].events)
        app_attempts = len(services["app"].events)
        assert requests <= gw_attempts <= 7 * requests
        # Every gateway attempt that reached its dependency is one call to app.
        assert app_attempts <= 7 * gw_attempts
        assert len(services["db"].events) <= app_attempts
