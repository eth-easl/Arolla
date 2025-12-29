"""
Configuration loader for building simulations from YAML files.

This module provides factory methods to construct simulation objects
(policies, services, workloads) from declarative YAML configuration.
"""

import yaml
from typing import Dict, Optional
from simulator.config.schema import *
from simulator.core.models import TimeInterval
from simulator.utils.time import s_to_ns, ms_to_ns
from simulator.policies.retry import (
    RetryPolicy, NoRetryPolicy, FixedBackoffRetryPolicy,
    ExponentialBackoffRetryPolicy, ExponentialBackoffWithJitterRetryPolicy,
    JitterMode as PolicyJitterMode,
    RetryBudgetPolicy, CircuitBreakerPolicy
)
from simulator.policies.timeout import Timeout, StaticTimeout
from simulator.policies.load_limiter import (
    LoadLimiter, NoLoadLimiter, CountBasedCircuitBreakerPolicy,
    TimeBasedCircuitBreakerPolicy, LeakyRateLimiterPolicy,
    BurstyRateLimiterPolicy, FixedWindowBurstyLimiterPolicy,
    RetryBudgetPolicy
)
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.policies.aimd_retry_budget import AIMDGlobalRetryBudget
from simulator.faults.injection import LatencyInjection, PartialFailure, LoadSpike
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.runtime.workload import Workload
from simulator.core.engine import Simulator
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.faults.events import FaultEventsTracker
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
        if cfg is None:
            return None
        
        if cfg.type == RetryPolicyType.NONE:
            return NoRetryPolicy()
        
        elif cfg.type == RetryPolicyType.FIXED:
            return FixedBackoffRetryPolicy(
                max_attempts=cfg.max_attempts,
                delay=ms_to_ns(cfg.delay_ms)
            )
        
        elif cfg.type == RetryPolicyType.EXPONENTIAL:
            return ExponentialBackoffRetryPolicy(
                max_attempts=cfg.max_attempts,
                initial_delay=ms_to_ns(cfg.initial_delay_ms),
                max_delay=ms_to_ns(cfg.max_delay_ms)
            )
        
        elif cfg.type == RetryPolicyType.JITTERED:
            # Map schema jitter mode to policy jitter mode
            jitter_mode_map = {
                JitterMode.FULL: PolicyJitterMode.FULL,
                JitterMode.EQUAL: PolicyJitterMode.EQUAL,
                JitterMode.DECORRELATED: PolicyJitterMode.DECORRELATED,
            }
            return ExponentialBackoffWithJitterRetryPolicy(
                max_attempts=cfg.max_attempts,
                initial_delay=ms_to_ns(cfg.initial_delay_ms),
                max_delay=ms_to_ns(cfg.max_delay_ms),
                rng=rng,
                jitter_mode=jitter_mode_map.get(cfg.jitter_mode, PolicyJitterMode.FULL)
            )
        
            raise ValueError(f"Unknown retry policy type: {cfg.type}")
            
        # Wrap with Circuit Breaker if configured
        if cfg.cb_failure_threshold is not None:
            policy = CircuitBreakerPolicy(
                inner=policy,
                failure_rate_threshold=cfg.cb_failure_threshold,
                window_size=cfg.cb_window_size
            )

        # Wrap with Retry Budget if configured (Outer layer, checks tokens first)
        if cfg.budget_ratio is not None:
            policy = RetryBudgetPolicy(
                inner=policy,
                budget_ratio=cfg.budget_ratio
            )
            
        return policy
    
    @staticmethod
    def build_timeout_policy(cfg: Optional[TimeoutConfig]) -> Optional[Timeout]:
        """Build Timeout from configuration"""
        if cfg is None:
            return None
        
        global_timeout = ms_to_ns(cfg.global_ms) if cfg.global_ms is not None else None
        attempt_timeout = ms_to_ns(cfg.attempt_ms) if cfg.attempt_ms is not None else None
        
        return StaticTimeout(
            global_timeout=global_timeout,
            attempt_timeout=attempt_timeout
        )
    
    @staticmethod
    def build_circuit_breaker(cfg: Optional[CircuitBreakerConfig]) -> Optional[LoadLimiter]:
        """Build circuit breaker from configuration"""
        if cfg is None:
            return None
        
        if cfg.type == CircuitBreakerType.COUNT_BASED:
            # Convert failure_threshold to (failures, total) tuple
            failure_count = int(cfg.failure_threshold * cfg.failure_window_size)
            success_count = int(cfg.success_threshold * cfg.success_window_size)
            
            return CountBasedCircuitBreakerPolicy(
                failure_threshold_ratio=(failure_count, cfg.failure_window_size),
                success_threshold_ratio=(success_count, cfg.success_window_size),
                half_open_delay=ms_to_ns(cfg.half_open_delay_ms)
            )
        
        elif cfg.type == CircuitBreakerType.TIME_BASED:
            return TimeBasedCircuitBreakerPolicy(
                failure_threshold_rate=cfg.failure_threshold,
                success_threshold_rate=cfg.success_threshold,
                min_requests=cfg.min_requests,
                window_duration=ms_to_ns(cfg.window_duration_ms),
                half_open_delay=ms_to_ns(cfg.half_open_delay_ms)
            )
        
        else:
            raise ValueError(f"Unknown circuit breaker type: {cfg.type}")
    
    @staticmethod
    def build_rate_limiter(cfg: Optional[RateLimiterConfig]) -> Optional[LoadLimiter]:
        """Build rate limiter from configuration"""
        if cfg is None:
            return None
        
        if cfg.type == RateLimiterType.LEAKY:
            return LeakyRateLimiterPolicy(
                max_requests=cfg.max_requests,
                max_attempts=cfg.max_attempts or cfg.max_requests,
                period=ms_to_ns(cfg.period_ms)
            )
        
        elif cfg.type == RateLimiterType.BURSTY:
            return BurstyRateLimiterPolicy(
                max_requests=cfg.max_requests,
                refill_rate=cfg.refill_rate or 1,
                period=ms_to_ns(cfg.period_ms)
            )
        
        elif cfg.type == RateLimiterType.FIXED_WINDOW:
            return FixedWindowBurstyLimiterPolicy(
                max_requests=cfg.max_requests,
                period=ms_to_ns(cfg.period_ms)
            )
        
        else:
            raise ValueError(f"Unknown rate limiter type: {cfg.type}")
    
    @staticmethod
    def build_retry_budget(cfg: Optional[RetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build legacy/local retry budget from configuration"""
        if cfg is None:
            return None
        
        return RetryBudgetPolicy(
            budget_ratio=cfg.budget_ratio,
            max_retries=cfg.max_retries
        )

    @staticmethod
    def build_global_retry_budget(cfg: Optional[GlobalRetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build server-side global retry budget from configuration"""
        if cfg is None:
            return None
        
        return GlobalRetryBudget(
            max_tokens=cfg.max_burst,
            refill_rate=cfg.target_rps,
            period=s_to_ns(1)  # 1 second period
        )
    
    @staticmethod
    def build_aimd_global_retry_budget(cfg: Optional[AIMDGlobalRetryBudgetConfig]) -> Optional[LoadLimiter]:
        """Build AIMD global retry budget"""
        if cfg is None:
            return None
        
        return AIMDGlobalRetryBudget(
            min_rps=cfg.min_rps,
            max_rps=cfg.max_rps,
            initial_rps=cfg.initial_rps,
            max_burst=cfg.max_burst,
            additive_step=cfg.additive_step,
            decrease_factor=cfg.decrease_factor,
            window_duration=ms_to_ns(cfg.window_ms),
            failure_threshold=cfg.failure_threshold
        )

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
        if circuit_breaker is not None:
            return ConfigLoader.build_circuit_breaker(circuit_breaker)
        elif aimd_global_retry_budget is not None:
            return ConfigLoader.build_aimd_global_retry_budget(aimd_global_retry_budget)
        elif global_retry_budget is not None:
            return ConfigLoader.build_global_retry_budget(global_retry_budget)
        elif retry_budget is not None:
            return ConfigLoader.build_retry_budget(retry_budget)
        elif rate_limiter is not None:
            return ConfigLoader.build_rate_limiter(rate_limiter)
        else:
            return None
    
    # ========================================================================
    # Fault Injection Factories
    # ========================================================================
    
    @staticmethod
    def build_latency_injections(configs: List[LatencyInjectionConfig]) -> List[LatencyInjection]:
        """Build latency injections from configuration"""
        return [
            LatencyInjection(
                duration=TimeInterval(
                    begin=s_to_ns(cfg.start_s),
                    end=s_to_ns(cfg.end_s)
                ),
                add_latency=ms_to_ns(cfg.add_latency_ms),
                multiplier=cfg.multiplier
            )
            for cfg in configs
        ]
    
    @staticmethod
    def build_partial_failures(configs: List[PartialFailureConfig]) -> List[PartialFailure]:
        """Build partial failures from configuration"""
        return [
            PartialFailure(
                duration=TimeInterval(
                    begin=s_to_ns(cfg.start_s),
                    end=s_to_ns(cfg.end_s)
                ),
                p_fail=cfg.p_fail
            )
            for cfg in configs
        ]
    
    @staticmethod
    def build_load_spikes(configs: List[LoadSpikeConfig]) -> List[LoadSpike]:
        """Build load spikes from configuration"""
        return [
            LoadSpike(
                duration=TimeInterval(
                    begin=s_to_ns(cfg.start_s),
                    end=s_to_ns(cfg.end_s)
                ),
                rps_multiplier=cfg.rps_multiplier
            )
            for cfg in configs
        ]
    
    # ========================================================================
    # Service and Workload Factories
    # ========================================================================
    
    @staticmethod
    def build_service(
        cfg: ServiceConfigYAML,
        sim: Simulator,
        dependency: Optional[ServiceRuntime] = None
    ) -> ServiceRuntime:
        """Build ServiceRuntime from configuration"""
        
        # Build policies
        # NOTE: Retry policy is now Client-side. Service gets None.
        retry_policy = None 
        timeout_policy = ConfigLoader.build_timeout_policy(cfg.timeout)
        load_limiter = ConfigLoader.build_load_limiter(
            cfg.circuit_breaker,
            cfg.rate_limiter,
            retry_budget=cfg.retry_budget,
            global_retry_budget=cfg.global_retry_budget,
            aimd_global_retry_budget=cfg.aimd_global_retry_budget,
        )
        
        # Build fault injections
        latency_injections = ConfigLoader.build_latency_injections(cfg.latency_injections)
        partial_failures = ConfigLoader.build_partial_failures(cfg.partial_failures)
        
        # Create service config
        service_cfg = ServiceConfig(
            name=cfg.name,
            latency_median=ms_to_ns(cfg.latency.median_ms),
            latency_lognorm_sigma=cfg.latency.lognorm_sigma,
            workers=cfg.workers,
            queue_capacity=cfg.queue_capacity,
            latency_injections=latency_injections,
            partial_failures=partial_failures,
            retry=retry_policy,
            timeout=timeout_policy,
            load_limiter=load_limiter
        )
        
        # Create service runtime
        return ServiceRuntime(cfg=service_cfg, dependency=dependency).bind()
    
    @staticmethod
    def build_workload(cfg: WorkloadConfig) -> Workload:
        """Build Workload from configuration"""
        load_spikes = ConfigLoader.build_load_spikes(cfg.load_spikes)
        
        return Workload(
            base_rps=cfg.base_rps,
            duration_s=cfg.duration_s,
            load_spikes=load_spikes,
            rng_seed=cfg.rng_seed
        )
    
    # ========================================================================
    # Complete Simulation Builder
    # ========================================================================
    
    @staticmethod
    def build_simulation(config: ExperimentConfig):
        """
        Build complete simulation from experiment configuration.
        
        Returns:
            (simulator, client, workload, fault_tracker)
        """
        # Create simulator
        sim = Simulator(seed=config.seed)
        
        # Create fault events tracker
        fault_tracker = FaultEventsTracker()
        
        # Build services (resolve dependencies)
        services: Dict[str, ServiceRuntime] = {}
        
        # First pass: build services without dependencies
        for svc_cfg in config.services:
            if svc_cfg.dependency is None:
                services[svc_cfg.name] = ConfigLoader.build_service(svc_cfg, sim)
        
        # Second pass: build services with dependencies
        for svc_cfg in config.services:
            if svc_cfg.dependency is not None:
                dependency = services.get(svc_cfg.dependency)
                if dependency is None:
                    raise ValueError(f"Dependency {svc_cfg.dependency} not found for service {svc_cfg.name}")
                services[svc_cfg.name] = ConfigLoader.build_service(svc_cfg, sim, dependency)
        
        # Register fault events
        for svc_name, svc_runtime in services.items():
            svc_runtime.cfg.register_fault_events(fault_tracker)
        
        # Get entry service (first service in list)
        entry_service = services[config.services[0].name]
        
        clients: List[ClientRuntime] = []
        workloads: List[Workload] = []

        if config.clients:
            # Multi-client mode
            for client_cfg_yaml in config.clients:
                # Create client
                c_retry_policy = ConfigLoader.build_retry_policy(client_cfg_yaml.retry, sim.rng())
                c_timeout_policy = ConfigLoader.build_timeout_policy(client_cfg_yaml.timeout)
                c_cfg = ClientConfig(name=client_cfg_yaml.name, retry=c_retry_policy, timeout=c_timeout_policy)
                
                target_svc = entry_service
                if client_cfg_yaml.target_service:
                    target_svc = services.get(client_cfg_yaml.target_service)
                    if not target_svc:
                         raise ValueError(f"Target service {client_cfg_yaml.target_service} not found for client {client_cfg_yaml.name}")
                
                client_runtime = ClientRuntime(cfg=c_cfg, service=target_svc)
                clients.append(client_runtime)
                
                # Create workload
                wl = ConfigLoader.build_workload(client_cfg_yaml.workload)
                wl.register_fault_events(fault_tracker)
                workloads.append(wl)
                
        else:
            # Single-client mode (legacy)
            if not config.workload:
                 raise ValueError("No workload configuration found")
                 
            client_cfg = ClientConfig(name="client")
            client_runtime = ClientRuntime(cfg=client_cfg, service=entry_service)
            clients.append(client_runtime)
            
            # Build workload
            workload = ConfigLoader.build_workload(config.workload)
            workload.register_fault_events(fault_tracker)
            workloads.append(workload)
        
        return sim, clients, workloads, fault_tracker

