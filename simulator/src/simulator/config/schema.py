"""
Pydantic schemas for declarative experiment configuration.

This module defines the schema for YAML-based experiment configuration,
enabling experiments to be defined declaratively rather than imperatively
in Python scripts.
"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Literal, Union
from enum import Enum


# ============================================================================
# Latency Configuration
# ============================================================================

class LatencyConfig(BaseModel):
    """Configuration for service latency distribution"""
    median_ms: float = Field(gt=0, description="Median latency in milliseconds")
    lognorm_sigma: float = Field(default=0.5, ge=0, description="Lognormal sigma (shape parameter)")


# ============================================================================
# Policy Configurations
# ============================================================================

class RetryPolicyType(str, Enum):
    """Types of retry policies"""
    NONE = "none"
    FIXED = "fixed"
    EXPONENTIAL = "exponential"
    JITTERED = "jittered"


class JitterMode(str, Enum):
    """Jitter modes for exponential backoff with jitter"""
    FULL = "full"
    EQUAL = "equal"
    DECORRELATED = "decorrelated"


class RetryConfig(BaseModel):
    """Configuration for retry policy"""
    type: RetryPolicyType
    max_attempts: int = Field(default=3, ge=1, description="Maximum number of attempts")
    delay_ms: Optional[float] = Field(default=None, ge=0, description="Fixed delay in ms (for fixed backoff)")
    initial_delay_ms: Optional[float] = Field(default=None, ge=0, description="Initial delay in ms (for exponential)")
    max_delay_ms: Optional[float] = Field(default=None, ge=0, description="Maximum delay in ms (for exponential)")
    jitter_mode: Optional[JitterMode] = Field(default=None, description="Jitter mode (for jittered backoff)")
    
    @field_validator('delay_ms')
    @classmethod
    def validate_fixed_delay(cls, v, info):
        if info.data.get('type') == RetryPolicyType.FIXED and v is None:
            raise ValueError("delay_ms is required for fixed backoff")
        return v
    
    @field_validator('initial_delay_ms', 'max_delay_ms')
    @classmethod
    def validate_exponential_delays(cls, v, info):
        policy_type = info.data.get('type')
        if policy_type in [RetryPolicyType.EXPONENTIAL, RetryPolicyType.JITTERED] and v is None:
            raise ValueError(f"{info.field_name} is required for {policy_type} backoff")
        return v


class TimeoutConfig(BaseModel):
    """Configuration for timeout policy"""
    global_ms: Optional[float] = Field(default=None, ge=0, description="Global timeout in ms")
    attempt_ms: Optional[float] = Field(default=None, ge=0, description="Per-attempt timeout in ms")


class CircuitBreakerType(str, Enum):
    """Types of circuit breakers"""
    COUNT_BASED = "count_based"
    TIME_BASED = "time_based"


class CircuitBreakerConfig(BaseModel):
    """Configuration for circuit breaker"""
    type: CircuitBreakerType
    failure_threshold: float = Field(description="Failure threshold (ratio or count)")
    success_threshold: float = Field(description="Success threshold (ratio or count)")
    half_open_delay_ms: float = Field(ge=0, description="Delay before half-open state in ms")
    
    # Count-based specific
    failure_window_size: Optional[int] = Field(default=None, ge=1, description="Window size for count-based")
    success_window_size: Optional[int] = Field(default=None, ge=1, description="Window size for count-based")
    
    # Time-based specific
    window_duration_ms: Optional[float] = Field(default=None, ge=0, description="Window duration for time-based")
    min_requests: Optional[int] = Field(default=None, ge=1, description="Minimum requests for time-based")


class RateLimiterType(str, Enum):
    """Types of rate limiters"""
    LEAKY = "leaky"
    BURSTY = "bursty"
    FIXED_WINDOW = "fixed_window"


class RateLimiterConfig(BaseModel):
    """Configuration for rate limiter"""
    type: RateLimiterType
    max_requests: int = Field(ge=1, description="Maximum requests")
    period_ms: float = Field(ge=0, description="Period in ms")
    max_attempts: Optional[int] = Field(default=None, ge=1, description="Max attempts (for leaky)")
    refill_rate: Optional[int] = Field(default=None, ge=1, description="Refill rate (for bursty)")


class RetryBudgetConfig(BaseModel):
    """Configuration for retry budget"""
    budget_ratio: float = Field(ge=0, le=1, description="Retry budget as ratio of successes (e.g., 0.1 = 10%)")
    max_retries: int = Field(ge=1, description="Maximum consecutive retries")


# ============================================================================
# Fault Injection Configurations
# ============================================================================

class FaultType(str, Enum):
    """Types of fault injections"""
    LATENCY_INJECTION = "latency_injection"
    PARTIAL_FAILURE = "partial_failure"
    LOAD_SPIKE = "load_spike"


class LatencyInjectionConfig(BaseModel):
    """Configuration for latency injection"""
    type: Literal[FaultType.LATENCY_INJECTION] = FaultType.LATENCY_INJECTION
    start_s: float = Field(ge=0, description="Start time in seconds")
    end_s: float = Field(ge=0, description="End time in seconds")
    add_latency_ms: float = Field(default=0, ge=0, description="Additive latency in ms")
    multiplier: int = Field(default=1, ge=1, description="Multiplicative factor")
    
    @field_validator('end_s')
    @classmethod
    def validate_time_range(cls, v, info):
        if v <= info.data.get('start_s', 0):
            raise ValueError("end_s must be greater than start_s")
        return v


class PartialFailureConfig(BaseModel):
    """Configuration for partial failure injection"""
    type: Literal[FaultType.PARTIAL_FAILURE] = FaultType.PARTIAL_FAILURE
    start_s: float = Field(ge=0, description="Start time in seconds")
    end_s: float = Field(ge=0, description="End time in seconds")
    p_fail: float = Field(ge=0, le=1, description="Probability of failure")
    
    @field_validator('end_s')
    @classmethod
    def validate_time_range(cls, v, info):
        if v <= info.data.get('start_s', 0):
            raise ValueError("end_s must be greater than start_s")
        return v


class LoadSpikeConfig(BaseModel):
    """Configuration for load spike"""
    type: Literal[FaultType.LOAD_SPIKE] = FaultType.LOAD_SPIKE
    start_s: float = Field(ge=0, description="Start time in seconds")
    end_s: float = Field(ge=0, description="End time in seconds")
    rps_multiplier: float = Field(gt=0, description="RPS multiplier")
    
    @field_validator('end_s')
    @classmethod
    def validate_time_range(cls, v, info):
        if v <= info.data.get('start_s', 0):
            raise ValueError("end_s must be greater than start_s")
        return v


FaultConfig = Union[LatencyInjectionConfig, PartialFailureConfig, LoadSpikeConfig]


# ============================================================================
# Service Configuration
# ============================================================================

class ServiceConfigYAML(BaseModel):
    """Configuration for a service"""
    name: str = Field(description="Service name")
    latency: LatencyConfig
    workers: int = Field(ge=1, description="Number of worker threads")
    queue_capacity: Optional[int] = Field(default=None, ge=0, description="Queue capacity (None = unbounded)")
    
    # Policies
    retry: Optional[RetryConfig] = None
    timeout: Optional[TimeoutConfig] = None
    circuit_breaker: Optional[CircuitBreakerConfig] = None
    rate_limiter: Optional[RateLimiterConfig] = None
    retry_budget: Optional[RetryBudgetConfig] = None
    
    # Faults
    latency_injections: List[LatencyInjectionConfig] = Field(default_factory=list)
    partial_failures: List[PartialFailureConfig] = Field(default_factory=list)
    
    # Dependencies
    dependency: Optional[str] = Field(default=None, description="Name of dependency service")


# ============================================================================
# Workload Configuration
# ============================================================================

class WorkloadConfig(BaseModel):
    """Configuration for workload generation"""
    base_rps: float = Field(gt=0, description="Base requests per second")
    duration_s: int = Field(gt=0, description="Simulation duration in seconds")
    load_spikes: List[LoadSpikeConfig] = Field(default_factory=list)
    rng_seed: Optional[int] = Field(default=None, description="RNG seed for reproducibility")


# ============================================================================
# Experiment Configuration
# ============================================================================

class ExperimentConfig(BaseModel):
    """Top-level experiment configuration"""
    name: str = Field(description="Experiment name")
    seed: int = Field(default=42, description="Simulator RNG seed")
    
    services: List[ServiceConfigYAML] = Field(min_length=1, description="List of services")
    workload: WorkloadConfig
    
    # Output configuration
    output_csv: str = Field(default="output.csv", description="Output CSV file path")
    fault_events_json: Optional[str] = Field(default=None, description="Fault events JSON file path")
    granularity_s: float = Field(default=1.0, gt=0, description="Metrics granularity in seconds")
    
    @field_validator('services')
    @classmethod
    def validate_service_dependencies(cls, services):
        """Validate that service dependencies exist"""
        service_names = {svc.name for svc in services}
        for svc in services:
            if svc.dependency and svc.dependency not in service_names:
                raise ValueError(f"Service {svc.name} depends on unknown service {svc.dependency}")
        return services
    
    def get_service_by_name(self, name: str) -> Optional[ServiceConfigYAML]:
        """Get service configuration by name"""
        for svc in self.services:
            if svc.name == name:
                return svc
        return None


# ============================================================================
# Example YAML
# ============================================================================

EXAMPLE_YAML = """
name: default_experiment
seed: 42

services:
  - name: svc-A
    latency:
      median_ms: 20
      lognorm_sigma: 0.5
    workers: 16
    queue_capacity: 20
    
    retry:
      type: fixed
      max_attempts: 2
      delay_ms: 100
    
    timeout:
      attempt_ms: 50
    
    retry_budget:
      budget_ratio: 0.1
      max_retries: 10
    
    partial_failures:
      - type: partial_failure
        start_s: 50
        end_s: 70
        p_fail: 0.5

workload:
  base_rps: 300
  duration_s: 300
  load_spikes:
    - type: load_spike
      start_s: 20
      end_s: 40
      rps_multiplier: 2.0

output_csv: output.csv
fault_events_json: fault_events.json
granularity_s: 1.0
"""
