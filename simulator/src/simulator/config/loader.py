"""
Configuration loader for building simulations from YAML files.

This module provides factory methods to construct simulation objects
(policies, services, workloads) from declarative YAML configuration.
"""

import yaml
from typing import List, Optional

from simulator.config.schema import (
    AIMDGlobalRetryBudgetConfig,
    CircuitBreakerConfig,
    DependencyConfig,
    ExperimentConfig,
    GlobalRetryBudgetConfig,
    LatencyInjectionConfig,
    LoadSpikeConfig,
    PartialFailureConfig,
    RateLimiterConfig,
    RetryBudgetConfig,
    RetryConfig,
    ServiceConfigYAML,
    TimeoutConfig,
    WorkloadConfig,
)
from simulator.policies.retry import RetryPolicy
from simulator.policies.timeout import Timeout
from simulator.policies.load_limiter import LoadLimiter
from simulator.faults.injection import LatencyInjection, PartialFailure, LoadSpike
from simulator.runtime.service import ServiceRuntime
from simulator.runtime.workload import Workload
from simulator.core.engine import Simulator
from simulator.config.compat import get_effective_dependencies
from simulator.config.builders import policies as policy_builders
from simulator.config.builders import faults as fault_builders
from simulator.config.builders import runtime as runtime_builders
from simulator.config.builders import simulation as simulation_builders
import random


class ConfigLoader:
    """Loads experiment configuration from YAML and builds simulation objects"""
    
    @staticmethod
    def load_from_file(yaml_path: str) -> ExperimentConfig:
        """
        Load experiment configuration from YAML file.
        
        Args:
            yaml_path: Path to YAML configuration file
            
        Returns:
            Validated ExperimentConfig object
            
        Raises:
            FileNotFoundError: If YAML file doesn't exist
            ValueError: If YAML is invalid or configuration is malformed
        """
        try:
            with open(yaml_path, 'r') as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Configuration file not found: {yaml_path}\n"
                f"Please check the path and try again."
            )
        except yaml.YAMLError as e:
            raise ValueError(
                f"Invalid YAML syntax in {yaml_path}:\n{e}\n"
                f"Please fix the YAML syntax and try again."
            )
        except Exception as e:
            raise ValueError(f"Error reading {yaml_path}: {e}")
        
        try:
            return ExperimentConfig(**data)
        except Exception as e:
            raise ValueError(
                f"Invalid configuration in {yaml_path}:\n{e}\n"
                f"Please check your configuration against the schema."
            )
    
    @staticmethod
    def load_from_string(yaml_str: str) -> ExperimentConfig:
        """
        Load experiment configuration from YAML string.
        
        Args:
            yaml_str: YAML configuration as string
            
        Returns:
            Validated ExperimentConfig object
        """
        data = yaml.safe_load(yaml_str)
        return ExperimentConfig(**data)
    
    # ========================================================================
    # Policy Factories
    # ========================================================================
    
    @staticmethod
    def build_retry_policy(cfg: Optional[RetryConfig], rng: random.Random) -> Optional[RetryPolicy]:
        """Build RetryPolicy from configuration"""
        return policy_builders.build_retry_policy(cfg, rng)
    
    @staticmethod
    def build_timeout_policy(cfg: Optional[TimeoutConfig]) -> Optional[Timeout]:
        """Build Timeout from configuration"""
        return policy_builders.build_timeout_policy(cfg)
    
    @staticmethod
    def build_circuit_breaker(cfg: Optional[CircuitBreakerConfig]) -> Optional[LoadLimiter]:
        """Build circuit breaker from configuration"""
        return policy_builders.build_circuit_breaker(cfg)
    
    @staticmethod
    def build_rate_limiter(cfg: Optional[RateLimiterConfig]) -> Optional[LoadLimiter]:
        """Build rate limiter from configuration"""
        return policy_builders.build_rate_limiter(cfg)
    
    @staticmethod
    def build_retry_budget(cfg: Optional[RetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build legacy/local retry budget from configuration"""
        return policy_builders.build_retry_budget(cfg)

    @staticmethod
    def build_global_retry_budget(cfg: Optional[GlobalRetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build server-side global retry budget from configuration"""
        return policy_builders.build_global_retry_budget(cfg)
    
    @staticmethod
    def build_aimd_global_retry_budget(cfg: Optional[AIMDGlobalRetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build AIMD global retry budget"""
        return policy_builders.build_aimd_global_retry_budget(cfg)

    @staticmethod
    def build_load_limiter(
        circuit_breaker: Optional[CircuitBreakerConfig],
        rate_limiter: Optional[RateLimiterConfig],
        retry_budget: Optional[RetryBudgetConfig],
        global_retry_budget: Optional[GlobalRetryBudgetConfig],
        aimd_global_retry_budget: Optional[AIMDGlobalRetryBudgetConfig]
    ) -> Optional[LoadLimiter]:
        """
        Build load limiter from configuration.
        
        Priority: circuit_breaker > aimd > global > retry_budget > rate_limiter
        """
        return policy_builders.build_load_limiter(
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            retry_budget=retry_budget,
            global_retry_budget=global_retry_budget,
            aimd_global_retry_budget=aimd_global_retry_budget,
        )
    
    # ========================================================================
    # Fault Injection Factories
    # ========================================================================
    
    @staticmethod
    def build_latency_injections(configs: List[LatencyInjectionConfig]) -> List[LatencyInjection]:
        """Build latency injections from configuration"""
        return fault_builders.build_latency_injections(configs)
    
    @staticmethod
    def build_partial_failures(configs: List[PartialFailureConfig]) -> List[PartialFailure]:
        """Build partial failures from configuration"""
        return fault_builders.build_partial_failures(configs)
    
    @staticmethod
    def build_load_spikes(configs: List[LoadSpikeConfig]) -> List[LoadSpike]:
        """Build load spikes from configuration"""
        return fault_builders.build_load_spikes(configs)
    
    # ========================================================================
    # Service and Workload Factories
    # ========================================================================
    
    @staticmethod
    def build_service(
        cfg: ServiceConfigYAML,
        sim: Simulator,
        dependencies: Optional[List[ServiceRuntime]] = None,
        dependency_optionality: Optional[List[bool]] = None,
        dependency_call_pattern: str = "sequential",
        seed: Optional[int] = None
    ) -> ServiceRuntime:
        """Build ServiceRuntime from configuration"""
        return runtime_builders.build_service(
            cfg=cfg,
            sim=sim,
            dependencies=dependencies,
            dependency_optionality=dependency_optionality,
            dependency_call_pattern=dependency_call_pattern,
            seed=seed,
        )
    
    @staticmethod
    def build_workload(cfg: WorkloadConfig) -> Workload:
        """Build Workload from configuration"""
        return runtime_builders.build_workload(cfg)
    
    # ========================================================================
    # Complete Simulation Builder
    # ========================================================================
    
    @staticmethod
    def _get_effective_deps(svc_cfg: ServiceConfigYAML) -> List[DependencyConfig]:
        """
        Return effective dependency list, handling backward compat.
        Converts legacy 'dependency: str' to 'dependencies: [{service: str}]'.
        """
        return get_effective_dependencies(svc_cfg)

    @staticmethod
    def build_simulation(config: ExperimentConfig):
        """
        Build complete simulation from experiment configuration.
        Uses topological sort to resolve multi-level dependencies.
        
        Returns:
            (simulator, clients, workloads, fault_tracker)
        """
        return simulation_builders.build_simulation(config)
