from caller import ClientConfig, ClientRuntime
from core import TimeInterval, s_to_ns, ms_to_ns
from faults import LoadSpike, LatencyInjection, PartialFailure
from policies.retry import ExponentialBackoffRetryPolicy
from policies.load_limiter import TimeBasedCircuitBreakerPolicy
from policies.timeout import StaticTimeout
from service import ServiceConfig, ServiceRuntime
from simulator import Simulator
from workload import Workload

if __name__ == "__main__":
    sim = Simulator(seed=77432)

    # Service with prolonged failure periods
    svc_cfg = ServiceConfig(
        name="svc-cb-time",
        latency_median=ms_to_ns(40),
        latency_lognorm_sigma=0.9,
        workers=12,
        queue_capacity=20,
        latency_injections=[
            LatencyInjection(
                duration=TimeInterval(begin=s_to_ns(20), end=s_to_ns(60)),
                add_latency=ms_to_ns(80),
            ),
        ],
        partial_failures=[
            PartialFailure(
                duration=TimeInterval(begin=s_to_ns(35), end=s_to_ns(75)), p_fail=0.7
            ),
        ],

        retry=ExponentialBackoffRetryPolicy(
            max_attempts=4,
            initial_delay=ms_to_ns(25),
            max_delay=ms_to_ns(400),
        ),
        load_limiter=TimeBasedCircuitBreakerPolicy(
            failure_threshold_rate=0.8,
            success_threshold_rate=0.8,
            min_requests=10,
            window_duration=s_to_ns(5),
            half_open_delay=s_to_ns(3),
        ),
        timeout=StaticTimeout(
            global_timeout=None,
            attempt_timeout=ms_to_ns(150),
        ),
    )

    svc = ServiceRuntime(cfg=svc_cfg).bind()

    cli_cfg = ClientConfig(

    )
    client = ClientRuntime(cfg=cli_cfg, service=svc)

    wl = Workload(
        base_rps=250.0,
        duration_s=100,
        load_spikes=[
            LoadSpike(
                duration=TimeInterval(begin=s_to_ns(40), end=s_to_ns(50)),
                rps_multiplier=1.8,
            ),
        ],
    )

    wl.drive(sim, lambda s: client.start_request(s))

    sim.run(until=s_to_ns(wl.duration_s))
    sim.run()

    m = client.metrics()
    m.export_csv(path="circuit_breaker_time_output.csv", granularity_s=1.0)
