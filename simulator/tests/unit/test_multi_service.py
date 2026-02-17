"""
Tests for multi-service dependency support.

Tests cover:
- Linear chains (A→B→C)
- Fan-out parallel (A→{B,C})
- Fan-out sequential (A→{B,C})
- Optional dependencies
- Failure propagation
- Backward compatibility with legacy single 'dependency' field
- Topological sort / circular dependency detection
- YAML config loading with dependencies
"""

import pytest
from simulator.core.engine import Simulator
from simulator.core.types import DropReason
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.config.schema import (
    ExperimentConfig, ServiceConfigYAML, WorkloadConfig,
    LatencyConfig, DependencyConfig, DependencyCallPattern,
)
from simulator.config.loader import ConfigLoader
from simulator.utils.time import ms_to_ns, ns_to_ms


# ============================================================================
# Helpers
# ============================================================================

def make_service(name, median_ms=10, sigma=0.01, workers=10, queue_capacity=None, seed=42):
    """Create a simple ServiceRuntime for testing."""
    cfg = ServiceConfig(
        name=name,
        latency_median=ms_to_ns(median_ms),
        latency_lognorm_sigma=sigma,
        workers=workers,
        queue_capacity=queue_capacity,
    )
    return ServiceRuntime(cfg=cfg).bind(seed=seed)


def make_service_with_deps(name, deps, optionality=None, call_pattern="sequential",
                           median_ms=10, sigma=0.01, workers=10, seed=42):
    """Create a ServiceRuntime with dependencies."""
    cfg = ServiceConfig(
        name=name,
        latency_median=ms_to_ns(median_ms),
        latency_lognorm_sigma=sigma,
        workers=workers,
    )
    return ServiceRuntime(
        cfg=cfg,
        dependencies=deps,
        dependency_optionality=optionality or [False] * len(deps),
        dependency_call_pattern=call_pattern,
    ).bind(seed=seed)


def run_single_request(sim, service, timeout_ms=5000):
    """Submit one request and run the simulation. Returns (success, latency_ns, drop_reason)."""
    result = {}

    def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
        result['success'] = success
        result['latency_ns'] = svc_time
        result['drop_reason'] = drop_reason

    def on_root_done():
        result['root_done'] = True

    service.submit_request(
        sim,
        on_attempt_done=on_attempt_done,
        on_root_done=on_root_done,
    )
    sim.run(until=ms_to_ns(timeout_ms))
    return result


# ============================================================================
# Test: No Dependencies (Leaf Service)
# ============================================================================

class TestLeafService:
    def test_leaf_service_succeeds(self):
        sim = Simulator(seed=42)
        svc = make_service("leaf", median_ms=10, sigma=0.01)
        result = run_single_request(sim, svc)
        assert result['success'] is True
        assert result['drop_reason'] == DropReason.NONE

    def test_leaf_service_latency_reasonable(self):
        sim = Simulator(seed=42)
        svc = make_service("leaf", median_ms=50, sigma=0.01)
        result = run_single_request(sim, svc)
        latency_ms = ns_to_ms(result['latency_ns'])
        assert 30 < latency_ms < 100  # ~50ms ± variance


# ============================================================================
# Test: Linear Chain (A→B→C)
# ============================================================================

class TestLinearChain:
    def test_two_service_chain(self):
        """A depends on B. Total latency should include B's processing time."""
        sim = Simulator(seed=42)
        svc_b = make_service("B", median_ms=20, sigma=0.01, seed=1)
        svc_a = make_service_with_deps("A", [svc_b], median_ms=10, sigma=0.01, seed=2)

        result = run_single_request(sim, svc_a)
        assert result['success'] is True
        # A's latency should be >= B's latency (since A waits for B)
        latency_ms = ns_to_ms(result['latency_ns'])
        assert latency_ms > 15  # At least B's contribution

    def test_three_service_chain(self):
        """A→B→C. Total latency includes all three."""
        sim = Simulator(seed=42)
        svc_c = make_service("C", median_ms=15, sigma=0.01, seed=1)
        svc_b = make_service_with_deps("B", [svc_c], median_ms=15, sigma=0.01, seed=2)
        svc_a = make_service_with_deps("A", [svc_b], median_ms=10, sigma=0.01, seed=3)

        result = run_single_request(sim, svc_a)
        assert result['success'] is True
        latency_ms = ns_to_ms(result['latency_ns'])
        assert latency_ms > 10  # At least some downstream contribution


# ============================================================================
# Test: Fan-Out Parallel
# ============================================================================

class TestParallelFanOut:
    def test_parallel_two_deps(self):
        """A calls B and C in parallel. Latency ≈ max(B, C)."""
        sim = Simulator(seed=42)
        svc_b = make_service("B", median_ms=20, sigma=0.01, seed=1)
        svc_c = make_service("C", median_ms=40, sigma=0.01, seed=2)
        svc_a = make_service_with_deps("A", [svc_b, svc_c], call_pattern="parallel", seed=3)

        result = run_single_request(sim, svc_a)
        assert result['success'] is True

    def test_parallel_one_dep_fails(self):
        """A calls B(ok) and C(failing) in parallel. Required dep failure → A fails."""
        from simulator.faults.injection import PartialFailure
        from simulator.core.models import TimeInterval

        sim = Simulator(seed=42)
        svc_b = make_service("B", median_ms=10, sigma=0.01, seed=1)

        # C always fails
        cfg_c = ServiceConfig(
            name="C",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.01,
            workers=10,
            partial_failures=[PartialFailure(
                duration=TimeInterval(begin=0, end=ms_to_ns(10000)),
                p_fail=1.0
            )]
        )
        svc_c = ServiceRuntime(cfg=cfg_c).bind(seed=2)

        svc_a = make_service_with_deps("A", [svc_b, svc_c], call_pattern="parallel", seed=3)
        result = run_single_request(sim, svc_a)
        assert result['success'] is False

    def test_parallel_optional_dep_failure_ok(self):
        """A calls B(ok) and C(failing, optional). A should succeed."""
        from simulator.faults.injection import PartialFailure
        from simulator.core.models import TimeInterval

        sim = Simulator(seed=42)
        svc_b = make_service("B", median_ms=10, sigma=0.01, seed=1)

        cfg_c = ServiceConfig(
            name="C",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.01,
            workers=10,
            partial_failures=[PartialFailure(
                duration=TimeInterval(begin=0, end=ms_to_ns(10000)),
                p_fail=1.0
            )]
        )
        svc_c = ServiceRuntime(cfg=cfg_c).bind(seed=2)

        # C is optional
        svc_a = make_service_with_deps(
            "A", [svc_b, svc_c],
            optionality=[False, True],  # B required, C optional
            call_pattern="parallel",
            seed=3
        )
        result = run_single_request(sim, svc_a)
        assert result['success'] is True


# ============================================================================
# Test: Fan-Out Sequential
# ============================================================================

class TestSequentialFanOut:
    def test_sequential_two_deps(self):
        """A calls B then C sequentially. Latency ≈ B + C."""
        sim = Simulator(seed=42)
        svc_b = make_service("B", median_ms=20, sigma=0.01, seed=1)
        svc_c = make_service("C", median_ms=20, sigma=0.01, seed=2)
        svc_a = make_service_with_deps("A", [svc_b, svc_c], call_pattern="sequential", seed=3)

        result = run_single_request(sim, svc_a)
        assert result['success'] is True
        latency_ms = ns_to_ms(result['latency_ns'])
        assert latency_ms > 30  # At least B + C

    def test_sequential_short_circuit_on_failure(self):
        """A calls B(failing) then C. B fails → A fails, C never called."""
        from simulator.faults.injection import PartialFailure
        from simulator.core.models import TimeInterval

        sim = Simulator(seed=42)

        cfg_b = ServiceConfig(
            name="B",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.01,
            workers=10,
            partial_failures=[PartialFailure(
                duration=TimeInterval(begin=0, end=ms_to_ns(10000)),
                p_fail=1.0
            )]
        )
        svc_b = ServiceRuntime(cfg=cfg_b).bind(seed=1)
        svc_c = make_service("C", median_ms=10, sigma=0.01, seed=2)

        svc_a = make_service_with_deps("A", [svc_b, svc_c], call_pattern="sequential", seed=3)
        result = run_single_request(sim, svc_a)
        assert result['success'] is False

    def test_sequential_optional_failure_continues(self):
        """A calls B(failing, optional) then C. B fails but optional → continues to C → succeeds."""
        from simulator.faults.injection import PartialFailure
        from simulator.core.models import TimeInterval

        sim = Simulator(seed=42)

        cfg_b = ServiceConfig(
            name="B",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.01,
            workers=10,
            partial_failures=[PartialFailure(
                duration=TimeInterval(begin=0, end=ms_to_ns(10000)),
                p_fail=1.0
            )]
        )
        svc_b = ServiceRuntime(cfg=cfg_b).bind(seed=1)
        svc_c = make_service("C", median_ms=10, sigma=0.01, seed=2)

        svc_a = make_service_with_deps(
            "A", [svc_b, svc_c],
            optionality=[True, False],  # B optional, C required
            call_pattern="sequential",
            seed=3
        )
        result = run_single_request(sim, svc_a)
        assert result['success'] is True


# ============================================================================
# Test: Backward Compatibility (legacy 'dependency' field)
# ============================================================================

class TestBackwardCompatibility:
    def test_legacy_dependency_property(self):
        """Legacy .dependency property returns first dep."""
        sim = Simulator(seed=42)
        svc_b = make_service("B", seed=1)
        svc_a = make_service_with_deps("A", [svc_b], seed=2)
        assert svc_a.dependency is svc_b

    def test_legacy_no_dependency(self):
        """Leaf service has .dependency = None."""
        svc = make_service("leaf")
        assert svc.dependency is None


# ============================================================================
# Test: Config Loader - Topological Sort
# ============================================================================

class TestConfigLoaderMultiDep:
    def test_simple_chain_yaml(self):
        """Load a YAML with A→B chain using new dependencies syntax."""
        config = ExperimentConfig(
            name="test_chain",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="B",
                    latency=LatencyConfig(median_ms=20),
                    workers=2,
                ),
                ServiceConfigYAML(
                    name="A",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="B")],
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        # Should build without error
        assert len(clients) == 1

    def test_fan_out_yaml(self):
        """Load a YAML with A→{B,C} fan-out."""
        config = ExperimentConfig(
            name="test_fanout",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="B",
                    latency=LatencyConfig(median_ms=20),
                    workers=2,
                ),
                ServiceConfigYAML(
                    name="C",
                    latency=LatencyConfig(median_ms=30),
                    workers=2,
                ),
                ServiceConfigYAML(
                    name="A",
                    latency=LatencyConfig(median_ms=5),
                    workers=4,
                    dependencies=[
                        DependencyConfig(service="B"),
                        DependencyConfig(service="C", optional=True),
                    ],
                    dependency_call_pattern=DependencyCallPattern.PARALLEL,
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        assert len(clients) == 1

    def test_legacy_single_dependency_compat(self):
        """Legacy 'dependency' field still works."""
        config = ExperimentConfig(
            name="test_legacy",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="backend",
                    latency=LatencyConfig(median_ms=20),
                    workers=2,
                ),
                ServiceConfigYAML(
                    name="frontend",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependency="backend",
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        assert len(clients) == 1

    def test_circular_dependency_error(self):
        """Circular dependency should raise ValueError."""
        config = ExperimentConfig(
            name="test_circular",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="A",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="B")],
                ),
                ServiceConfigYAML(
                    name="B",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="A")],
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        with pytest.raises(ValueError, match="Circular dependency"):
            ConfigLoader.build_simulation(config)

    def test_missing_dependency_error(self):
        """Reference to non-existent service should raise ValueError."""
        config = ExperimentConfig(
            name="test_missing",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="A",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="nonexistent")],
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        with pytest.raises(ValueError, match="not found"):
            ConfigLoader.build_simulation(config)

    def test_diamond_dependency(self):
        """Diamond: D→{B,C}, B→A, C→A. A built once, shared."""
        config = ExperimentConfig(
            name="test_diamond",
            seed=42,
            services=[
                ServiceConfigYAML(
                    name="A",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                ),
                ServiceConfigYAML(
                    name="B",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="A")],
                ),
                ServiceConfigYAML(
                    name="C",
                    latency=LatencyConfig(median_ms=10),
                    workers=2,
                    dependencies=[DependencyConfig(service="A")],
                ),
                ServiceConfigYAML(
                    name="D",
                    latency=LatencyConfig(median_ms=5),
                    workers=4,
                    dependencies=[
                        DependencyConfig(service="B"),
                        DependencyConfig(service="C"),
                    ],
                    dependency_call_pattern=DependencyCallPattern.PARALLEL,
                ),
            ],
            workload=WorkloadConfig(base_rps=10, duration_s=5),
        )

        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        assert len(clients) == 1


# ============================================================================
# Test: YAML String Loading
# ============================================================================

class TestYAMLStringLoading:
    def test_multi_dep_yaml_string(self):
        """Load multi-dep config from YAML string."""
        yaml_str = """
name: online_boutique_mini
seed: 42
services:
  - name: productcatalog
    latency:
      median_ms: 12
      lognorm_sigma: 0.4
    workers: 2

  - name: payment
    latency:
      median_ms: 15
      lognorm_sigma: 0.3
    workers: 2

  - name: checkout
    latency:
      median_ms: 5
      lognorm_sigma: 0.2
    workers: 4
    dependencies:
      - service: productcatalog
      - service: payment
    dependency_call_pattern: sequential

workload:
  base_rps: 50
  duration_s: 10
"""
        config = ConfigLoader.load_from_string(yaml_str)
        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        assert len(clients) == 1
        assert len(config.services) == 3

    def test_multi_dep_parallel_yaml_string(self):
        """Load parallel fan-out from YAML string."""
        yaml_str = """
name: parallel_test
seed: 42
services:
  - name: ads
    latency: { median_ms: 8, lognorm_sigma: 0.5 }
    workers: 2

  - name: recommendations
    latency: { median_ms: 20, lognorm_sigma: 0.3 }
    workers: 2

  - name: frontend
    latency: { median_ms: 3, lognorm_sigma: 0.1 }
    workers: 4
    dependencies:
      - service: ads
        optional: true
      - service: recommendations
    dependency_call_pattern: parallel

workload:
  base_rps: 100
  duration_s: 5
"""
        config = ConfigLoader.load_from_string(yaml_str)
        assert len(config.services) == 3
        fe = config.services[2]
        assert len(fe.dependencies) == 2
        assert fe.dependencies[0].optional is True
        assert fe.dependencies[1].optional is False
        assert fe.dependency_call_pattern == DependencyCallPattern.PARALLEL

        sim, clients, workloads, _ = ConfigLoader.build_simulation(config)
        assert len(clients) == 1
