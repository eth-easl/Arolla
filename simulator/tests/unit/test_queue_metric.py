"""
Test the queue_avg_at_attempt_end metric.

The metric averages queue lengths recorded at the end of each attempt.
Local client timeouts record -1 instead of a queue length and are excluded
from the average. For example, values of 10, 30 and -1 yield an average of 20.
"""

import math

from simulator.core.engine import Simulator
from simulator.core.models import Request, RootRequest, TimeInterval
from simulator.core.types import DropReason
from simulator.metrics.collector import Metrics
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.timeout import StaticTimeout
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, s_to_ns


def attempt(end_s, queue_size, success=True):
    req = Request(service="svc", interval=TimeInterval(begin=s_to_ns(end_s) - 1000, end=s_to_ns(end_s)))
    req.success = success
    req.drop_reason = DropReason.NONE if success else DropReason.DEADLINE
    req.queue_size_at_end = queue_size
    return req


def queue_column(attempts):
    roots = []
    for a in attempts:
        root = RootRequest()
        root.add_attempt(a)
        root.done = True
        roots.append(root)
    frame = Metrics(roots=roots, attempts_total=len(attempts)).to_dataframe(granularity_s=1.0)
    return list(frame["queue_avg_at_attempt_end"])


class TestQueueAverage:
    def test_the_placeholder_is_not_a_measurement(self):
        """Test that the placeholder -1 is left out of the average"""
        column = queue_column([
            attempt(0.2, 10), attempt(0.4, 30), attempt(0.6, -1, success=False),
        ])
        assert column[0] == 20.0   # (10 + 30) / 2, not (10 + 30 - 1) / 3

    def test_a_second_with_only_timeouts_has_no_queue_average(self):
        """Test that a second with only local timeouts has no queue average (NaN)"""
        column = queue_column([
            attempt(0.5, 4),
            attempt(1.2, -1, success=False), attempt(1.7, -1, success=False),
        ])
        assert column[0] == 4.0
        assert math.isnan(column[1])

    def test_an_empty_queue_is_still_a_measurement(self):
        """Test that a queue length of 0 is counted in the average"""
        column = queue_column([attempt(0.2, 0), attempt(0.4, 6)])
        assert column[0] == 3.0

    def test_a_local_timeout_recorded_by_the_client_does_not_lower_the_average(self):
        """Test that local timeouts recorded by a real client do not lower the average.

        One worker, 40 ms of service, four requests at t = 0, 15 ms of patience:
        the last three time out locally while two or more are queued.
        """
        sim = Simulator(seed=1)
        svc = ServiceRuntime(cfg=ServiceConfig(
            name="svc", latency_median=ms_to_ns(40), latency_lognorm_sigma=0.0, workers=1,
        )).bind(seed=1)
        client = ClientRuntime(
            cfg=ClientConfig(
                retry=FixedBackoffRetryPolicy(max_attempts=1, delay=0),
                timeout=StaticTimeout(attempt_timeout=ms_to_ns(15)),
            ),
            service=svc,
        )
        for _ in range(4):
            client.start_request(sim)
        sim.run()

        sizes = [a.queue_size_at_end for r in client.roots for a in r.attempts]
        assert sizes.count(-1) == 4          # every call timed out before it was served
        frame = client.metrics().to_dataframe(granularity_s=1.0)
        assert math.isnan(frame["queue_avg_at_attempt_end"][0])
