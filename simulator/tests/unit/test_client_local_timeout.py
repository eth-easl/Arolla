"""
Test late responses after client-side attempt timeouts, with and without
client-side retries.

An attempt timeout records a failure on the client without cancelling
the request at the service. Any subsequent response is ignored.

For example, with a 15 ms timeout and a 20 ms service time, the client
records a timeout at 15 ms and ignores the successful response at 20 ms.
"""

from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns


def make_service(median_ms=20, fail_until_ms=None, max_attempts=None):
    failures = []
    if fail_until_ms is not None:
        failures.append(PartialFailure(
            duration=TimeInterval(begin=0, end=ms_to_ns(fail_until_ms)), p_fail=1.0,
        ))
    retry = None
    if max_attempts is not None:
        retry = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0)
    cfg = ServiceConfig(
        name="svc",
        latency_median=ms_to_ns(median_ms),
        latency_lognorm_sigma=0.0,
        workers=10,
        partial_failures=failures,
        retry=retry,
    )
    return ServiceRuntime(cfg=cfg).bind(seed=1, record_events=True)


def make_client(service, max_attempts=None, timeout_ms=15):
    retry = None
    if max_attempts is not None:
        retry = FixedBackoffRetryPolicy(max_attempts=max_attempts, delay=0)
    return ClientRuntime(
        cfg=ClientConfig(retry=retry, timeout=StaticTimeout(attempt_timeout=ms_to_ns(timeout_ms))),
        service=service,
    )


def run_one(client):
    sim = Simulator(seed=1)
    client.start_request(sim)
    sim.run()
    return sim


def at_ms(timestamp_ns, expected_ms):
    return abs(timestamp_ns - ms_to_ns(expected_ms)) < ms_to_ns(0.01)


class TestClientWithItsOwnPolicy:
    def test_a_late_success_does_not_complete_the_request(self):
        """Test that a late success does not complete the request.

        20 ms of service against a 15 ms timeout, two attempts. Both calls time
        out; the answers arrive at 20 and 35 ms and are not results.
        """
        svc = make_service(median_ms=20)
        client = make_client(svc, max_attempts=2)
        run_one(client)

        (root,) = client.roots
        assert [(a.success, a.drop_reason) for a in root.attempts] == [
            (False, DropReason.DEADLINE), (False, DropReason.DEADLINE),
        ]
        assert client.attempts_total == 2
        # The service did the work both times: a client timeout cancels nothing.
        assert len(svc.events) == 2
        assert svc.in_flight == 0

    def test_a_late_failure_does_not_trigger_a_second_decision(self):
        """Test that a late failure does not trigger a second retry decision.

        One timeout is one retry decision. With three attempts the calls start
        at 0, 15 and 30 ms; the failure that arrives at 20 ms for the first
        call must not start the third call early.
        """
        svc = make_service(median_ms=20, fail_until_ms=10_000)
        client = make_client(svc, max_attempts=3)
        run_one(client)

        (root,) = client.roots
        assert [a.drop_reason for a in root.attempts] == [DropReason.DEADLINE] * 3
        assert [a.interval.begin for a in root.attempts] == [0, ms_to_ns(15), ms_to_ns(30)]
        assert len(svc.events) == 3

    def test_an_answer_in_time_is_the_result(self):
        """Test that an answer that arrives before the timeout is the result of the call"""
        svc = make_service(median_ms=10)
        client = make_client(svc, max_attempts=2)
        run_one(client)

        (root,) = client.roots
        assert [(a.success, a.drop_reason) for a in root.attempts] == [(True, DropReason.NONE)]
        assert at_ms(root.attempts[0].interval.end, 10)
        assert client.attempts_total == 1

    def test_a_retry_that_answers_in_time_succeeds_after_a_timed_out_call(self):
        """Test that a retry that answers in time succeeds after a call that timed out.

        The first call is slow (a latency fault makes it 30 ms) and the service is
        fast again for the retry: [timeout, success].
        """
        from simulator.faults.injection import LatencyInjection

        cfg = ServiceConfig(
            name="svc",
            latency_median=ms_to_ns(5),
            latency_lognorm_sigma=0.0,
            workers=10,
            latency_injections=[LatencyInjection(
                duration=TimeInterval(begin=0, end=ms_to_ns(1)), add_latency=ms_to_ns(25),
            )],
        )
        svc = ServiceRuntime(cfg=cfg).bind(seed=1, record_events=True)
        client = make_client(svc, max_attempts=3)
        run_one(client)

        (root,) = client.roots
        # Call 1: 30 ms of service, timed out at 15. Call 2 (15 ms): 5 ms, answered at 20.
        assert [(a.success, a.drop_reason) for a in root.attempts] == [
            (False, DropReason.DEADLINE), (True, DropReason.NONE),
        ]
        assert at_ms(root.attempts[1].interval.end, 20)
        assert client.attempts_total == 2


class TestClientWithoutAPolicy:
    def test_a_late_success_is_not_counted(self):
        """Test that a late success is not counted for a client without a policy.

        One call, timed out at 15 ms. The service's answer at 20 ms ends the
        request, which stays a failure.
        """
        svc = make_service(median_ms=20)
        client = make_client(svc)
        run_one(client)

        (root,) = client.roots
        assert [(a.success, a.drop_reason) for a in root.attempts] == [(False, DropReason.DEADLINE)]
        assert root.done

    def test_a_success_on_the_services_retry_after_the_timeout_is_not_counted(self):
        """Test that a success on the service's own retry after the timeout is not counted.

        The service retries by itself: its first attempt fails at 20 ms and the
        second succeeds at 40 ms. The client timed out at 15 ms, so the request
        is a failure. The service still makes both attempts because we haven't
        implemented cancellation propagation yet.
        """
        svc = make_service(median_ms=20, fail_until_ms=25, max_attempts=2)
        client = make_client(svc)
        run_one(client)

        (root,) = client.roots
        assert [(a.success, a.drop_reason) for a in root.attempts] == [(False, DropReason.DEADLINE)]
        assert len(svc.events) == 2

    def test_the_timeout_also_covers_the_services_retry(self):
        """Test that the client's timeout also applies while the service retries.

        The service's first attempt fails at 10 ms, and the retry would
        succeed at 20 ms. The client's 15 ms timeout elapses during the retry and
        ends the request.
        """
        svc = make_service(median_ms=10, fail_until_ms=12, max_attempts=2)
        client = make_client(svc)
        run_one(client)

        (root,) = client.roots
        assert [(a.success, a.drop_reason) for a in root.attempts] == [
            (False, DropReason.SERVER_FAILURE), (False, DropReason.DEADLINE),
        ]
        assert at_ms(root.attempts[1].interval.end, 15)
