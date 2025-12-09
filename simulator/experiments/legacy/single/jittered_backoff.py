import random

from caller import ClientConfig, ClientRuntime
from core import TimeInterval, s_to_ns, ms_to_ns
from faults import LoadSpike, LatencyInjection, PartialFailure
from policies.load_limiter import NoLoadLimiter
from policies.retry import ExponentialBackoffWithJitterRetryPolicy, JitterMode
from service import ServiceConfig, ServiceRuntime
from simulator import Simulator
from policies.timeout import StaticTimeout
from workload import Workload

if __name__ == "__main__":
    sim = Simulator(seed=54321)

    svc_cfg = ServiceConfig(
        name="svc-jitter",
        latency_median=ms_to_ns(30),
        latency_lognorm_sigma=0.7,
        workers=12,
        queue_capacity=25,
        latency_injections=[
            LatencyInjection(
                duration=TimeInterval(begin=s_to_ns(15), end=s_to_ns(35)),
                add_latency=ms_to_ns(30),
            ),
            LatencyInjection(
                duration=TimeInterval(begin=s_to_ns(75), end=s_to_ns(90)),
                add_latency=ms_to_ns(40),
            ),
        ],
        partial_failures=[
            PartialFailure(
                duration=TimeInterval(begin=s_to_ns(45), end=s_to_ns(65)), p_fail=0.4
            ),
        ],

        retry=ExponentialBackoffWithJitterRetryPolicy(
            max_attempts=4,
            initial_delay=ms_to_ns(20),
            max_delay=ms_to_ns(800),
            rng=random.Random(42),
            jitter_mode=JitterMode.FULL,
        ),
        load_limiter=NoLoadLimiter(),
        timeout=StaticTimeout(
            global_timeout=None,
            attempt_timeout=ms_to_ns(100),
        ),
    )

    svc = ServiceRuntime(cfg=svc_cfg).bind()

    cli_cfg = ClientConfig(

    )
    client = ClientRuntime(cfg=cli_cfg, service=svc)

    wl = Workload(
        base_rps=400.0,
        duration_s=100,
        load_spikes=[
            LoadSpike(
                duration=TimeInterval(begin=s_to_ns(30), end=s_to_ns(50)),
                rps_multiplier=2.5,
            ),
        ],
    )

    wl.drive(sim, lambda s: client.start_request(s))

    sim.run(until=s_to_ns(wl.duration_s))
    sim.run()

    m = client.metrics()
    m.export_csv(path="jittered_backoff_output.csv", granularity_s=1.0)
