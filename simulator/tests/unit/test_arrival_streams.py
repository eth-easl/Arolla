"""
Test client arrival streams.

Clients targeting the same service have independent arrival streams, even
when they are replicas or share a workload configuration and rng_seed.

Clients targeting different services receive identical arrival streams,
allowing policies to be compared under the same traffic.
"""

import random

from simulator.config.builders.runtime import arrival_seed, build_workload
from simulator.config.loader import ConfigLoader
from simulator.config.schema import WorkloadConfig
from simulator.utils.time import s_to_ns

CONFIG = """
name: arrival_streams
seed: 1
services:
  - name: svc-a
    latency: {median_ms: 1, lognorm_sigma: 0.1}
    workers: 100
  - name: svc-b
    latency: {median_ms: 1, lognorm_sigma: 0.1}
    workers: 100
clients:
  - name: first
    target_service: svc-a
    replicas: 3
    workload: &load {base_rps: 50, duration_s: 4, rng_seed: 7}
  - name: second
    target_service: svc-a
    workload: *load
  - name: other-arm
    target_service: svc-b
    replicas: 3
    workload: *load
"""


def arrivals_per_client():
    """{client name: tuple of arrival instants} for CONFIG."""
    cfg = ConfigLoader.load_from_string(CONFIG)
    sim, clients, workloads, _, _ = ConfigLoader.build_simulation(cfg)
    seen = {client.cfg.name: [] for client in clients}
    for client, workload in zip(clients, workloads):
        workload.drive(sim, lambda s, c=client: seen[c.cfg.name].append(s.timestep))
    sim.run(until=s_to_ns(4))
    return {name: tuple(times) for name, times in seen.items()}


def shared_instants(a, b):
    """Arrival instants two clients have in common, the forced one at t = 0 aside."""
    return (set(a) & set(b)) - {0}


class TestArrivalStreams:
    def test_replicas_of_one_entry_arrive_independently(self):
        """Test that the replicas of one client entry send their requests at different times"""
        seen = arrivals_per_client()
        replicas = [seen["first.0"], seen["first.1"], seen["first.2"]]

        for times in replicas:
            assert 150 < len(times) < 250  # about 50 rps for 4 s each
        assert not shared_instants(replicas[0], replicas[1])
        assert not shared_instants(replicas[0], replicas[2])
        assert not shared_instants(replicas[1], replicas[2])

    def test_entries_on_the_same_service_arrive_independently(self):
        """Test that two client entries on the same service send requests at different times"""
        seen = arrivals_per_client()

        for replica in ("first.0", "first.1", "first.2"):
            assert not shared_instants(seen["second"], seen[replica])

    def test_arms_on_different_services_stay_paired(self):
        """Test that entries on different services get the same request times.

        Same workload block, different services: replica i of one entry sends its
        requests exactly when replica i of the other does.
        """
        seen = arrivals_per_client()

        for i in range(3):
            assert seen[f"other-arm.{i}"] == seen[f"first.{i}"]

    def test_the_first_client_of_a_service_keeps_the_configured_stream(self):
        """Test that the first client of a service uses the configured `rng_seed` as it is"""
        cfg = WorkloadConfig(base_rps=50, duration_s=4, rng_seed=7)

        assert arrival_seed(7) == 7
        assert arrival_seed(None) is None
        expected = random.Random(7)
        workload = build_workload(cfg)
        assert [workload._rng.random() for _ in range(5)] == [expected.random() for _ in range(5)]

    def test_each_position_and_replica_gets_its_own_stable_seed(self):
        """Test that every (position, replica) pair gets its own seed, the same on every run"""
        seeds = {
            (position, replica): arrival_seed(7, position, replica)
            for position in range(4)
            for replica in range(50)
        }
        assert len(set(seeds.values())) == len(seeds)
        assert seeds == {key: arrival_seed(7, *key) for key in seeds}
        # Another configured seed is another family of streams.
        assert arrival_seed(8, 1, 0) != arrival_seed(7, 1, 0)
        # No configured seed behaves like seed 0.
        assert arrival_seed(None, 1, 0) == arrival_seed(0, 1, 0)
