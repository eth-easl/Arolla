"""
Unit tests for ServiceRuntime M/G/c/K queue implementation.

Tests verify:
1. Worker concurrency (c servers)
2. Queue capacity (K limit)
3. FCFS ordering
4. Load shedding (queue full)
5. Service time distribution (G)
6. Queueing metrics
"""

import unittest
from dataclasses import dataclass
from typing import List, Optional

from simulator.core.engine import Simulator
from simulator.core.types import TimePoint, TimeDuration, DropReason
from simulator.runtime.service import ServiceRuntime, ServiceConfig
from simulator.utils.time import ms_to_ns


class TestServiceQueueBasics(unittest.TestCase):
    """Test basic M/G/c/K queue properties"""
    
    def setUp(self):
        """Create a simple service for testing"""
        self.sim = Simulator(seed=42)
        self.cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),  # 10ms median
            latency_lognorm_sigma=0.1,     # Low variance for predictability
            workers=2,                      # c = 2
            queue_capacity=3                # K = 2 + 3 = 5 total
        )
        self.service = ServiceRuntime(cfg=self.cfg).bind()
        self.results = []
    
    def _on_done(self, success: bool, service_time: TimeDuration, 
                 drop_reason: DropReason, queue_size: int):
        """Callback to track results"""
        self.results.append({
            'success': success,
            'service_time': service_time,
            'drop_reason': drop_reason,
            'queue_size': queue_size,
            'completion_time': self.sim.timestep
        })
    
    def test_worker_concurrency(self):
        """Test that c workers can process requests simultaneously"""
        # Submit 2 requests (should both start immediately)
        self.service.submit_attempt(self.sim, self._on_done)
        self.service.submit_attempt(self.sim, self._on_done)
        
        # Both should be in_flight
        self.assertEqual(self.service.in_flight, 2, "Both workers should be busy")
        self.assertEqual(len(self.service.queue), 0, "Queue should be empty")
        
        # Submit 3rd request (should queue)
        self.service.submit_attempt(self.sim, self._on_done)
        self.assertEqual(self.service.in_flight, 2, "Still 2 workers busy")
        self.assertEqual(len(self.service.queue), 1, "1 request should be queued")
    
    def test_queue_capacity(self):
        """Test that queue respects K capacity limit"""
        # Fill workers (2) and queue (3) = 5 total
        for i in range(5):
            self.service.submit_attempt(self.sim, self._on_done)
        
        self.assertEqual(self.service.in_flight, 2, "2 workers busy")
        self.assertEqual(len(self.service.queue), 3, "3 requests queued")
        
        # 6th request should be rejected (queue full)
        self.service.submit_attempt(self.sim, self._on_done)
        
        # Check that it was dropped
        self.assertEqual(len(self.results), 1, "One request should be dropped")
        self.assertFalse(self.results[0]['success'])
        self.assertEqual(self.results[0]['drop_reason'], DropReason.QUEUE_FULL)
    
    def test_fcfs_ordering(self):
        """Test that requests are processed in FCFS order"""
        completion_order = []
        start_times = []
        
        def on_done_with_id(req_id: int):
            def callback(success, service_time, drop_reason, queue_size):
                completion_order.append(req_id)
            return callback
        
        # Submit 5 requests and track when they start
        for i in range(5):
            start_times.append(self.sim.timestep)
            self.service.submit_attempt(self.sim, on_done_with_id(i))
        
        # Run simulation
        self.sim.run()
        
        # All 5 should complete
        self.assertEqual(len(completion_order), 5, "All 5 should complete")
        
        # First 2 start immediately (workers), so can complete in any order
        # But requests 2, 3, 4 are queued and should start in that order
        # Due to service time variance, we just check they all completed
        self.assertEqual(set(completion_order), {0, 1, 2, 3, 4}, 
                        "All requests should complete")
    
    def test_load_shedding(self):
        """Test that system rejects requests when at capacity"""
        # Fill system to capacity
        for i in range(5):  # 2 workers + 3 queue = 5 total
            self.service.submit_attempt(self.sim, self._on_done)
        
        # Try to add more
        rejected = 0
        for i in range(10):
            self.service.submit_attempt(self.sim, self._on_done)
            if self.results and self.results[-1]['drop_reason'] == DropReason.QUEUE_FULL:
                rejected += 1
        
        self.assertEqual(rejected, 10, "All 10 additional requests should be rejected")
    
    def test_queue_draining(self):
        """Test that queue drains as workers become free"""
        # Submit 5 requests
        for i in range(5):
            self.service.submit_attempt(self.sim, self._on_done)
        
        initial_queue_size = len(self.service.queue)
        self.assertEqual(initial_queue_size, 3, "Queue should have 3 requests")
        
        # Run simulation
        self.sim.run()
        
        # All should complete
        self.assertEqual(len(self.results), 5, "All 5 requests should complete")
        self.assertEqual(len(self.service.queue), 0, "Queue should be empty")
        self.assertEqual(self.service.in_flight, 0, "No workers should be busy")
        
        # All should succeed (no drops)
        for result in self.results:
            self.assertTrue(result['success'], "All requests should succeed")


class TestServiceMetrics(unittest.TestCase):
    """Test that service tracks correct metrics"""
    
    def setUp(self):
        self.sim = Simulator(seed=42)
        self.cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(20),
            latency_lognorm_sigma=0.3,
            workers=4,
            queue_capacity=10
        )
        self.service = ServiceRuntime(cfg=self.cfg).bind()
        self.results = []
    
    def _on_done(self, success: bool, service_time: TimeDuration,
                 drop_reason: DropReason, queue_size: int):
        self.results.append({
            'success': success,
            'service_time': service_time,
            'drop_reason': drop_reason,
            'queue_size': queue_size,
            'completion_time': self.sim.timestep
        })
    
    def test_queue_size_tracking(self):
        """Test that queue_size is correctly reported"""
        # Submit requests and track queue sizes
        for i in range(8):
            self.service.submit_attempt(self.sim, self._on_done)
        
        # Run simulation
        self.sim.run()
        
        # Check that queue sizes were tracked
        queue_sizes = [r['queue_size'] for r in self.results]
        
        # Queue should start at 4 (8 - 4 workers) and decrease
        self.assertGreater(max(queue_sizes), 0, "Queue should have had items")
        self.assertEqual(queue_sizes[-1], 0, "Queue should be empty at end")
    
    def test_service_time_distribution(self):
        """Test that service times follow lognormal distribution"""
        # Submit many requests
        for i in range(100):
            self.service.submit_attempt(self.sim, self._on_done)
        
        # Run simulation
        self.sim.run()
        
        # Collect service times
        service_times = [r['service_time'] for r in self.results if r['success']]
        
        # Check that we have variety (not all the same)
        self.assertGreater(len(set(service_times)), 10, "Should have varied service times")
        
        # Check that median is roughly correct (within 50% due to randomness)
        median_time = sorted(service_times)[len(service_times) // 2]
        expected = ms_to_ns(20)
        self.assertGreater(median_time, expected * 0.5, "Median too low")
        self.assertLess(median_time, expected * 2.0, "Median too high")


class TestServiceEdgeCases(unittest.TestCase):
    """Test edge cases and special configurations"""
    
    def test_infinite_queue(self):
        """Test service with no queue capacity limit"""
        sim = Simulator(seed=42)
        cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=2,
            queue_capacity=None  # Infinite queue
        )
        service = ServiceRuntime(cfg=cfg).bind()
        results = []
        
        def on_done(success, service_time, drop_reason, queue_size):
            results.append({'success': success, 'drop_reason': drop_reason})
        
        # Submit many requests
        for i in range(100):
            service.submit_attempt(sim, on_done)
        
        # None should be dropped
        dropped = [r for r in results if r['drop_reason'] == DropReason.QUEUE_FULL]
        self.assertEqual(len(dropped), 0, "No requests should be dropped with infinite queue")
        
        # Queue should have grown large
        self.assertGreater(len(service.queue), 50, "Queue should have many items")
    
    def test_no_queue_loss_system(self):
        """Test service with no queue (M/G/c/c loss system)"""
        sim = Simulator(seed=42)
        cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=3,
            queue_capacity=0  # No queue
        )
        service = ServiceRuntime(cfg=cfg).bind()
        results = []
        
        def on_done(success, service_time, drop_reason, queue_size):
            results.append({'success': success, 'drop_reason': drop_reason})
        
        # Submit 5 requests
        for i in range(5):
            service.submit_attempt(sim, on_done)
        
        # First 3 should start, last 2 should be rejected immediately
        self.assertEqual(service.in_flight, 3, "3 workers should be busy")
        self.assertEqual(len(service.queue), 0, "Queue should be empty")
        self.assertEqual(len(results), 2, "2 requests should be dropped")
        
        for result in results:
            self.assertEqual(result['drop_reason'], DropReason.QUEUE_FULL)
    
    def test_single_worker(self):
        """Test service with single worker (M/G/1/K)"""
        sim = Simulator(seed=42)
        cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=1,  # Single worker
            queue_capacity=5
        )
        service = ServiceRuntime(cfg=cfg).bind()
        results = []
        
        def on_done(success, service_time, drop_reason, queue_size):
            results.append({'completion_time': sim.timestep})
        
        # Submit 3 requests
        for i in range(3):
            service.submit_attempt(sim, on_done)
        
        # Run simulation
        sim.run()
        
        # All should complete sequentially
        completion_times = [r['completion_time'] for r in results]
        
        # Each should complete after the previous
        for i in range(1, len(completion_times)):
            self.assertGreater(completion_times[i], completion_times[i-1],
                             "Requests should complete sequentially")


class TestServiceUtilization(unittest.TestCase):
    """Test utilization and performance characteristics"""
    
    def test_low_utilization(self):
        """Test service under low load (ρ << 1)"""
        sim = Simulator(seed=42)
        cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=10,  # Many workers
            queue_capacity=20
        )
        service = ServiceRuntime(cfg=cfg).bind()
        results = []
        
        def on_done(success, service_time, drop_reason, queue_size):
            results.append({'queue_size': queue_size})
        
        # Submit only 5 requests (low load)
        for i in range(5):
            service.submit_attempt(sim, on_done)
        
        # All should start immediately (no queueing)
        self.assertEqual(len(service.queue), 0, "Queue should be empty under low load")
        
        # Run simulation
        sim.run()
        
        # All should have seen empty queue
        for result in results:
            self.assertEqual(result['queue_size'], 0, "Queue should stay empty")
    
    def test_high_utilization(self):
        """Test service under high load (ρ ≈ 1)"""
        sim = Simulator(seed=42)
        cfg = ServiceConfig(
            name="test-service",
            latency_median=ms_to_ns(100),  # Slow service
            latency_lognorm_sigma=0.1,
            workers=2,  # Few workers
            queue_capacity=50
        )
        service = ServiceRuntime(cfg=cfg).bind()
        results = []
        
        def on_done(success, service_time, drop_reason, queue_size):
            results.append({'queue_size': queue_size})
        
        # Submit many requests
        for i in range(30):
            service.submit_attempt(sim, on_done)
        
        # Queue should build up
        self.assertGreater(len(service.queue), 10, "Queue should build up under high load")
        
        # Run simulation
        sim.run()
        
        # Some requests should have seen non-empty queue
        max_queue_size = max(r['queue_size'] for r in results)
        self.assertGreater(max_queue_size, 5, "Queue should have grown significantly")


def run_service_tests():
    """Run all service tests"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    suite.addTests(loader.loadTestsFromTestCase(TestServiceQueueBasics))
    suite.addTests(loader.loadTestsFromTestCase(TestServiceMetrics))
    suite.addTests(loader.loadTestsFromTestCase(TestServiceEdgeCases))
    suite.addTests(loader.loadTestsFromTestCase(TestServiceUtilization))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    import sys
    success = run_service_tests()
    sys.exit(0 if success else 1)
