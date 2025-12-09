"""ms_simulator - Microservice Latency Simulator

A discrete-event simulator for modeling microservice architectures with
retry policies, circuit breakers, rate limiters, and fault injection.
"""

__version__ = "2.0.0"

# Core simulation engine
from .core.engine import Simulator
from .core.types import TimePoint, TimeDuration, DropReason, MIN_SERVICE_TIME_NS
from .core.models import Request, RootRequest, TimeInterval, Summary

# Runtime components
from .runtime.service import ServiceRuntime, ServiceConfig
from .runtime.client import ClientRuntime, ClientConfig
from .runtime.workload import Workload

# Middleware
from .middleware.base import Middleware, AttemptContext, MiddlewareChain
from .middleware.retry import RetryMiddleware, NoRetryMiddleware
from .middleware.load_limiter import LoadLimiterMiddleware
from .middleware.metrics import MetricsMiddleware, MetricsSink

# Configuration
from .config.schema import ExperimentConfig
from .config.loader import ConfigLoader

# Metrics
from .metrics.collector import Metrics

# Utilities
from .utils.time import s_to_ns, ms_to_ns, ns_to_s, ns_to_ms

__all__ = [
    # Version
    "__version__",
    
    # Core
    "Simulator",
    "TimePoint",
    "TimeDuration",
    "DropReason",
    "MIN_SERVICE_TIME_NS",
    "Request",
    "RootRequest",
    "TimeInterval",
    "Summary",
    
    # Runtime
    "ServiceRuntime",
    "ServiceConfig",
    "ClientRuntime",
    "ClientConfig",
    "Workload",
    
    # Middleware
    "Middleware",
    "AttemptContext",
    "MiddlewareChain",
    "RetryMiddleware",
    "NoRetryMiddleware",
    "LoadLimiterMiddleware",
    "MetricsMiddleware",
    "MetricsSink",
    
    # Configuration
    "ExperimentConfig",
    "ConfigLoader",
    
    # Metrics
    "Metrics",
    
    # Utilities
    "s_to_ns",
    "ms_to_ns",
    "ns_to_s",
    "ns_to_ms",
]
