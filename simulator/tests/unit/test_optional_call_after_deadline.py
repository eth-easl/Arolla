"""
Test optional dependency calls that complete after the caller's deadline.

The caller tolerates failures of optional dependencies but must still
respect its own deadline. Queued calls may report a timeout only when
a worker becomes available, even if their deadline has already passed.

Example: slow has one worker and a service time of 100 ms. Front has
a service time of 1 ms, an attempt timeout of 50 ms, and calls slow
as an optional dependency.

     0 ms  A direct call occupies slow worker until 100 ms.
    10 ms  A request arrives at Front. Its dependency call queues at slow.
           Front deadline is 60 ms.
   100 ms  Slow dequeues the call and reports a timeout.
           Front own deadline has expired, so it reports a timeout
           and releases its worker, despite the dependency being optional.
"""

import pytest

from simulator.core.engine import Simulator
from simulator.core.types import DropReason
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns


def make_service(name, median_ms, workers, deps=(), call_pattern="sequential", timeout_ms=None):
    """A service with a fixed service time whose dependencies are all optional."""
    cfg = ServiceConfig(
        name=name,
        latency_median=ms_to_ns(median_ms),
        latency_lognorm_sigma=0.0,
        workers=workers,
        timeout=StaticTimeout(attempt_timeout=ms_to_ns(timeout_ms)) if timeout_ms else None,
    )
    return ServiceRuntime(
        cfg=cfg,
        dependencies=list(deps),
        dependency_optionality=[True] * len(deps),
        dependency_call_pattern=call_pattern,
    ).bind(seed=1)


def is_ms(timestamp_ns, expected_ms):
    """Whether a timestamp is `expected_ms` (service times carry a few ns of rounding)."""
    return abs(timestamp_ns - ms_to_ns(expected_ms)) < ms_to_ns(0.01)


@pytest.mark.parametrize("call_pattern", ["sequential", "parallel"])
def test_the_attempt_times_out_when_the_late_answer_arrives(call_pattern):
    """Test that the attempt times out when the late answer arrives.

    The example at the top of this file: `front` reports a timeout at 100 ms.
    """
    sim = Simulator(seed=1)
    slow = make_service("slow", median_ms=100, workers=1)
    front = make_service("front", median_ms=1, workers=10, deps=[slow],
                         call_pattern=call_pattern, timeout_ms=50)

    attempts = []

    def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
        attempts.append((sim.timestep, success, drop_reason))

    def request():
        front.submit_request(sim, on_attempt_done=on_attempt_done, on_root_done=lambda: None)

    sim.schedule(0, lambda: slow.submit_call(sim, lambda result: None))
    sim.schedule(ms_to_ns(10), request)
    sim.run()

    assert len(attempts) == 1
    answered_at, success, reason = attempts[0]
    assert is_ms(answered_at, 100)
    assert not success
    assert reason == DropReason.DEADLINE
    assert front.in_flight == 0
