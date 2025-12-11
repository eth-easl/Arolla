from caller import ClientConfig, ClientRuntime
from core import TimeInterval, s_to_ns, ms_to_ns
from fault_events import FaultEventsTracker
from faults import LoadSpike, LatencyInjection, PartialFailure
from policies.load_limiter import NoLoadLimiter
from policies.retry import FixedBackoffRetryPolicy
from policies.timeout import StaticTimeout
from service import ServiceConfig, ServiceRuntime
from simulator import Simulator
from workload import Workload

if __name__ == "__main__":
    sim = Simulator(seed=43232)

    # Create fault events tracker
    fault_tracker = FaultEventsTracker()

    # Service: 20ms median, lognormal sigma 0.5, 16 workers, queue cap 200
    svc_cfg = ServiceConfig(
        name="svc-A",
        latency_median=ms_to_ns(20),
        latency_lognorm_sigma=0.5,
        workers=16,
        queue_capacity=20,
        partial_failures=[
            PartialFailure(
                duration=TimeInterval(begin=s_to_ns(50), end=s_to_ns(70)), p_fail=0.5
            ),
        ],
        retry=FixedBackoffRetryPolicy(
            max_attempts=2,
            delay=ms_to_ns(100),
        ),
        load_limiter=NoLoadLimiter(),
        timeout=StaticTimeout(
            global_timeout=None, attempt_timeout=ms_to_ns(50)
        )
    )

    svc = ServiceRuntime(cfg=svc_cfg).bind()

    # Register service fault events
    svc_cfg.register_fault_events(fault_tracker)

    # Client + retry: 2 retries with 5ms fixed backoff (total 3 attempts), 150ms total deadline
    cli_cfg = ClientConfig()
    client = ClientRuntime(cfg=cli_cfg, service=svc)

    # Workload: 300 rps for 12s, with a 2x spike between [4s, 6s)
    wl = Workload(
        base_rps=300.0,
        duration_s=300,
        load_spikes=[
            LoadSpike(
                duration=TimeInterval(begin=s_to_ns(20), end=s_to_ns(40)),
                rps_multiplier=2.0,
            ),
        ],
    )

    # Register workload fault events
    wl.register_fault_events(fault_tracker)

    # Hook arrivals to client
    wl.drive(sim, lambda s: client.start_request(s))

    sim.run(until=s_to_ns(wl.duration_s))
    sim.run()  # drain outstanding requests

    # Print summary
    m = client.metrics()

    m.export_csv(path="output.csv", granularity_s=1.0)

    # Export fault events
    fault_tracker.export_json("fault_events.json")
