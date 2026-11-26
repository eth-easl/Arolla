"""
Test the exact_arrivals workload option.

By default, the first request arrives at t = 0, and each interarrival
time is sampled using the rate at the previous arrival. Rate changes
therefore take effect only after the next arrival.

With exact_arrivals enabled, all arrival times are random, including
the first, and rate changes take effect at the specified spike boundaries.
"""

from simulator.config.loader import ConfigLoader
from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.faults.injection import LoadSpike
from simulator.runtime.workload import Workload
from simulator.utils.time import ms_to_ns, s_to_ns


def arrival_times(workload):
    sim = Simulator(seed=1)
    times = []
    workload.drive(sim, lambda s: times.append(s.timestep))
    sim.run()
    return times


def spike(start_s, end_s, multiplier):
    interval = TimeInterval(begin=s_to_ns(start_s), end=s_to_ns(end_s))
    return LoadSpike(duration=interval, rps_multiplier=multiplier)


def count_between(times, start_s, end_s):
    return sum(s_to_ns(start_s) <= t < s_to_ns(end_s) for t in times)


def test_by_default_the_first_request_is_sent_at_time_zero():
    """Test that by default the first request is sent at t = 0"""
    assert arrival_times(Workload(base_rps=100, duration_s=1, rng_seed=7))[0] == 0


def test_with_exact_arrivals_the_first_request_comes_at_a_random_time():
    """Test that with `exact_arrivals` the first request comes at a random time.

    At 100 requests per second the first request arrives after 10 ms on average.
    """
    firsts = [
        arrival_times(Workload(base_rps=100, duration_s=1, rng_seed=seed, exact_arrivals=True))[0]
        for seed in range(50)
    ]
    assert min(firsts) > 0
    assert len(set(firsts)) == 50
    assert ms_to_ns(5) < sum(firsts) / len(firsts) < ms_to_ns(15)


def test_without_a_load_spike_only_the_request_at_time_zero_is_missing():
    """Test that without a load spike both modes send the same requests, except at t = 0"""
    default = arrival_times(Workload(base_rps=200, duration_s=2, rng_seed=3))
    exact = arrival_times(Workload(base_rps=200, duration_s=2, rng_seed=3, exact_arrivals=True))
    assert exact == default[1:]


def test_a_load_spike_starts_and_ends_on_time():
    """Test that a load spike starts and ends on time.

    1 request per second, and 1000 per second from 10 s to 11 s: about 1000 requests
    arrive in that second, and about 10 in the 10 s after it.
    """
    workload = Workload(
        base_rps=1, duration_s=21, load_spikes=[spike(10, 11, 1000)], rng_seed=5, exact_arrivals=True
    )
    times = arrival_times(workload)
    assert 900 < count_between(times, 10, 11) < 1100
    assert count_between(times, 11, 21) < 30


def test_a_very_short_load_spike_still_gets_its_extra_requests():
    """Test that a very short load spike still gets its extra requests.

    1 request per second, and 1000 per second during 100 ms: about 100 requests arrive
    in those 100 ms. By default the spike is usually over before the next request, so the
    default never notices it.
    """
    load_spikes = [spike(10.0, 10.1, 1000)]
    exact = [
        count_between(arrival_times(Workload(1, 20, load_spikes, seed, exact_arrivals=True)), 10.0, 10.1)
        for seed in range(20)
    ]
    default = [
        count_between(arrival_times(Workload(1, 20, load_spikes, seed)), 10.0, 10.1)
        for seed in range(20)
    ]
    assert all(60 < count < 140 for count in exact)
    assert default.count(0) >= 15


def test_nothing_arrives_at_or_after_the_end():
    """Test that no request arrives at or after the end of the workload"""
    times = arrival_times(Workload(base_rps=500, duration_s=2, rng_seed=1, exact_arrivals=True))
    assert 850 < len(times) < 1150
    assert max(times) < s_to_ns(2)


def test_the_yaml_key_reaches_the_workload():
    """Test that the YAML key reaches the workload"""
    config = """
name: exact
services:
  - name: api
    latency: {median_ms: 1}
    workers: 1
clients:
  - name: plain
    workload: {base_rps: 10, duration_s: 1}
  - name: exact
    workload: {base_rps: 10, duration_s: 1, exact_arrivals: true}
"""
    _, _, workloads, _, _ = ConfigLoader.build_simulation(ConfigLoader.load_from_string(config))
    assert [workload.exact_arrivals for workload in workloads] == [False, True]
