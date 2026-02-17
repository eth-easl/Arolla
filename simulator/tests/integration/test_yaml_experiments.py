"""
Integration tests for YAML experiment execution.

These tests verify that YAML configurations load correctly and
simulations run end-to-end with expected results.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import unittest
from pathlib import Path

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns


class TestYAMLExperiments(unittest.TestCase):
    """Test that YAML experiment files load and run correctly"""
    
    def setUp(self):
        """Find experiments directory"""
        self.experiments_dir = Path(__file__).parent.parent.parent / 'experiments' / 'yaml'
        self.assertTrue(self.experiments_dir.exists(), 
                       f"Experiments directory not found: {self.experiments_dir}")
    
    def test_default_yaml_loads(self):
        """Test that default.yaml loads without errors"""
        yaml_path = self.experiments_dir / 'default.yaml'
        self.assertTrue(yaml_path.exists(), f"default.yaml not found at {yaml_path}")
        
        # Should load without exceptions
        config = ConfigLoader.load_from_file(str(yaml_path))
        
        # Verify basic structure
        self.assertIsNotNone(config)
        self.assertGreater(len(config.services), 0, "Should have at least one service")
        self.assertIsNotNone(config.workload)
    
    def test_default_yaml_runs(self):
        """Test that default.yaml runs to completion"""
        yaml_path = self.experiments_dir / 'default.yaml'
        config = ConfigLoader.load_from_file(str(yaml_path))

        # Build simulation (returns lists of clients/workloads)
        sim, clients, workloads, tracker = ConfigLoader.build_simulation(config)

        # Run simulation
        for client, workload in zip(clients, workloads):
            workload.drive(sim, client.start_request)
        sim.run(until=s_to_ns(config.workload.duration_s))

        # Verify simulation ran
        client = clients[0]
        self.assertGreater(len(client.roots), 0, "Should have processed requests")

        # Get summary
        summary = client.metrics().summary()
        self.assertGreater(summary.total, 0)
        self.assertGreater(summary.succeeded, 0)

        success_rate = summary.succeeded / summary.total if summary.total > 0 else 0.0
        self.assertGreaterEqual(success_rate, 0.0)
        self.assertLessEqual(success_rate, 1.0)

        print(f"✓ default.yaml: {summary.total} requests, "
              f"{success_rate:.1%} success rate")
    
    def test_2_chain_yaml_runs(self):
        """Test that 2_chain.yaml (service dependencies) runs correctly"""
        yaml_path = self.experiments_dir / '2_chain.yaml'

        if not yaml_path.exists():
            self.skipTest(f"2_chain.yaml not found at {yaml_path}")

        config = ConfigLoader.load_from_file(str(yaml_path))

        # Should have 2 services
        self.assertEqual(len(config.services), 2, "Should have 2 services in chain")

        # Build and run
        sim, clients, workloads, tracker = ConfigLoader.build_simulation(config)
        for client, workload in zip(clients, workloads):
            workload.drive(sim, client.start_request)
        sim.run(until=s_to_ns(config.workload.duration_s))

        # Verify ran
        client = clients[0]
        self.assertGreater(len(client.roots), 0)
        summary = client.metrics().summary()
        success_rate = summary.succeeded / summary.total if summary.total > 0 else 0.0

        print(f"✓ 2_chain.yaml: {summary.total} requests, "
              f"{success_rate:.1%} success rate")
    
    def test_circuit_breaker_yaml_runs(self):
        """Test that circuit_breaker.yaml runs and circuit breaker activates"""
        yaml_path = self.experiments_dir / 'circuit_breaker.yaml'

        if not yaml_path.exists():
            self.skipTest(f"circuit_breaker.yaml not found at {yaml_path}")

        config = ConfigLoader.load_from_file(str(yaml_path))

        # Build and run
        sim, clients, workloads, tracker = ConfigLoader.build_simulation(config)
        for client, workload in zip(clients, workloads):
            workload.drive(sim, client.start_request)
        sim.run(until=s_to_ns(config.workload.duration_s))

        # Verify ran
        client = clients[0]
        self.assertGreater(len(client.roots), 0)
        summary = client.metrics().summary()
        success_rate = summary.succeeded / summary.total if summary.total > 0 else 0.0

        # With circuit breaker and failures, success rate should be < 100%
        self.assertLess(success_rate, 1.0,
                       "Circuit breaker should cause some failures")

        print(f"✓ circuit_breaker.yaml: {summary.total} requests, "
              f"{success_rate:.1%} success rate")
    
    def test_jittered_backoff_yaml_runs(self):
        """Test that jittered_backoff.yaml runs with fault injection"""
        yaml_path = self.experiments_dir / 'jittered_backoff.yaml'

        if not yaml_path.exists():
            self.skipTest(f"jittered_backoff.yaml not found at {yaml_path}")

        config = ConfigLoader.load_from_file(str(yaml_path))

        # Build and run
        sim, clients, workloads, tracker = ConfigLoader.build_simulation(config)
        for client, workload in zip(clients, workloads):
            workload.drive(sim, client.start_request)
        sim.run(until=s_to_ns(config.workload.duration_s))

        # Verify ran
        client = clients[0]
        self.assertGreater(len(client.roots), 0)
        summary = client.metrics().summary()
        success_rate = summary.succeeded / summary.total if summary.total > 0 else 0.0

        print(f"✓ jittered_backoff.yaml: {summary.total} requests, "
              f"{success_rate:.1%} success rate, "
              f"P99={summary.p99:.1f}ms")


class TestEndToEndScenarios(unittest.TestCase):
    """Test complete end-to-end simulation scenarios"""
    
    def test_high_load_scenario(self):
        """Test simulation under high load"""
        from simulator.core.engine import Simulator
        from simulator.runtime.service import ServiceRuntime, ServiceConfig
        from simulator.runtime.client import ClientRuntime, ClientConfig
        from simulator.runtime.workload import Workload
        from simulator.utils.time import ms_to_ns
        
        # Create high-load scenario
        sim = Simulator(seed=42)
        
        service_cfg = ServiceConfig(
            name="high-load-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.3,
            workers=5,  # Limited workers
            queue_capacity=10  # Small queue
        )
        service = ServiceRuntime(cfg=service_cfg).bind()
        
        client_cfg = ClientConfig()
        client = ClientRuntime(cfg=client_cfg, service=service)
        
        # High RPS workload
        workload = Workload(
            base_rps=500,  # High load
            duration_s=10,
            load_spikes=[],
            rng_seed=42
        )
        
        # Run
        workload.drive(sim, client.start_request)
        sim.run(until=s_to_ns(10))
        
        # Verify
        summary = client.metrics().summary()
        self.assertGreater(summary.total, 1000, "Should have many requests")
        
        # Under high load, some requests should be dropped
        self.assertGreater(summary.dropped_queue, 0, 
                          "Should have queue full drops under high load")
        
        print(f"✓ High load: {summary.total} requests, "
              f"{summary.dropped_queue} queue drops")
    
    def test_fault_injection_increases_latency(self):
        """Test that latency injection increases observed latency"""
        from simulator.core.engine import Simulator
        from simulator.runtime.service import ServiceRuntime, ServiceConfig
        from simulator.runtime.client import ClientRuntime, ClientConfig
        from simulator.runtime.workload import Workload
        from simulator.faults.injection import LatencyInjection
        from simulator.core.models import TimeInterval
        from simulator.utils.time import ms_to_ns, s_to_ns
        
        # Run 1: No fault injection
        sim1 = Simulator(seed=42)
        service_cfg1 = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(20),
            latency_lognorm_sigma=0.1,
            workers=10,
            queue_capacity=50
        )
        service1 = ServiceRuntime(cfg=service_cfg1).bind()
        client1 = ClientRuntime(cfg=ClientConfig(), service=service1)
        workload1 = Workload(base_rps=100, duration_s=5, rng_seed=42)
        
        workload1.drive(sim1, client1.start_request)
        sim1.run(until=s_to_ns(5))
        summary1 = client1.metrics().summary()
        
        # Run 2: With latency injection
        sim2 = Simulator(seed=42)
        service_cfg2 = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(20),
            latency_lognorm_sigma=0.1,
            workers=10,
            queue_capacity=50,
            latency_injections=[
                LatencyInjection(
                    duration=TimeInterval(begin=s_to_ns(1), end=s_to_ns(4)),
                    add_latency=ms_to_ns(50),  # Add 50ms
                    multiplier=1
                )
            ]
        )
        service2 = ServiceRuntime(cfg=service_cfg2).bind()
        client2 = ClientRuntime(cfg=ClientConfig(), service=service2)
        workload2 = Workload(base_rps=100, duration_s=5, rng_seed=42)
        
        workload2.drive(sim2, client2.start_request)
        sim2.run(until=s_to_ns(5))
        summary2 = client2.metrics().summary()
        
        # Verify latency increased
        self.assertGreater(summary2.mean, summary1.mean,
                          "Latency injection should increase mean latency")
        self.assertGreater(summary2.p99, summary1.p99,
                          "Latency injection should increase P99 latency")
        
        print(f"✓ Fault injection: baseline P99={summary1.p99:.1f}ms, "
              f"with injection P99={summary2.p99:.1f}ms")
    
    def test_retry_increases_success_rate(self):
        """Test that retry policy improves success rate under failures"""
        from simulator.core.engine import Simulator
        from simulator.runtime.service import ServiceRuntime, ServiceConfig
        from simulator.runtime.client import ClientRuntime, ClientConfig
        from simulator.runtime.workload import Workload
        from simulator.faults.injection import PartialFailure
        from simulator.core.models import TimeInterval
        from simulator.policies.retry import FixedBackoffRetryPolicy
        from simulator.utils.time import ms_to_ns, s_to_ns
        
        # Scenario: 30% failure rate
        partial_failure = PartialFailure(
            duration=TimeInterval(begin=s_to_ns(0), end=s_to_ns(10)),
            p_fail=0.3
        )
        
        # Run 1: No retry
        sim1 = Simulator(seed=42)
        service_cfg1 = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=10,
            partial_failures=[partial_failure],
            retry=None  # No retry
        )
        service1 = ServiceRuntime(cfg=service_cfg1).bind()
        client1 = ClientRuntime(cfg=ClientConfig(), service=service1)
        workload1 = Workload(base_rps=100, duration_s=5, rng_seed=42)
        
        workload1.drive(sim1, client1.start_request)
        sim1.run(until=s_to_ns(5))
        summary1 = client1.metrics().summary()
        success_rate1 = summary1.succeeded / summary1.total if summary1.total > 0 else 0.0
        
        # Run 2: With retry
        sim2 = Simulator(seed=42)
        service_cfg2 = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=10,
            partial_failures=[partial_failure],
            retry=FixedBackoffRetryPolicy(max_attempts=3, delay=ms_to_ns(10))
        )
        service2 = ServiceRuntime(cfg=service_cfg2).bind()
        client2 = ClientRuntime(cfg=ClientConfig(), service=service2)
        workload2 = Workload(base_rps=100, duration_s=5, rng_seed=42)
        
        workload2.drive(sim2, client2.start_request)
        sim2.run(until=s_to_ns(5))
        summary2 = client2.metrics().summary()
        success_rate2 = summary2.succeeded / summary2.total if summary2.total > 0 else 0.0
        
        # Verify retry improves success rate
        self.assertGreater(success_rate2, success_rate1,
                          "Retry should improve success rate")
        
        print(f"✓ Retry benefit: no retry={success_rate1:.1%}, "
              f"with retry={success_rate2:.1%}")


def run_integration_tests():
    """Run all integration tests"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    suite.addTests(loader.loadTestsFromTestCase(TestYAMLExperiments))
    suite.addTests(loader.loadTestsFromTestCase(TestEndToEndScenarios))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    import sys
    success = run_integration_tests()
    sys.exit(0 if success else 1)
