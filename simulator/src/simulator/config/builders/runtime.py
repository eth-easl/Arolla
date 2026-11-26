"""Runtime object builders (services/workloads) used by ConfigLoader."""

from __future__ import annotations

import random
from typing import List, Optional

from simulator.config.schema import DependencyJoin, LocalWork, ServiceConfigYAML, WorkloadConfig
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.runtime.workload import Workload
from simulator.core.engine import Simulator
from simulator.utils.time import ms_to_ns

from .faults import build_latency_injections, build_partial_failures, build_load_spikes
from .policies import build_retry_policy, build_timeout_policy, build_load_limiter
from .seeds import get_stable_seed


def build_service(
    cfg: ServiceConfigYAML,
    sim: Simulator,
    dependencies: Optional[List[ServiceRuntime]] = None,
    dependency_optionality: Optional[List[bool]] = None,
    dependency_call_pattern: str = "sequential",
    seed: Optional[int] = None,
) -> ServiceRuntime:
    # Use a service-local RNG seed for policy construction to preserve isolation.
    svc_rng = random.Random(seed if seed is not None else 0)

    retry_policy = build_retry_policy(cfg.retry, svc_rng)
    timeout_policy = build_timeout_policy(cfg.timeout)
    load_limiter = build_load_limiter(
        cfg.circuit_breaker,
        cfg.rate_limiter,
        retry_budget=cfg.retry_budget,
        global_retry_budget=cfg.global_retry_budget,
        aimd_global_retry_budget=cfg.aimd_global_retry_budget,
        arolla_retry_budget=cfg.arolla_retry_budget,
    )

    service_cfg = ServiceConfig(
        name=cfg.name,
        latency_median=ms_to_ns(cfg.latency.median_ms),
        latency_lognorm_sigma=cfg.latency.lognorm_sigma,
        workers=cfg.workers,
        queue_capacity=cfg.queue_capacity,
        latency_injections=build_latency_injections(cfg.latency_injections),
        partial_failures=build_partial_failures(cfg.partial_failures),
        retry=retry_policy,
        timeout=timeout_policy,
        load_limiter=load_limiter,
    )

    return ServiceRuntime(
        cfg=service_cfg,
        dependencies=dependencies or [],
        dependency_optionality=dependency_optionality or [],
        dependency_call_pattern=dependency_call_pattern,
        dependency_fail_fast=cfg.dependency_join == DependencyJoin.FAIL_FAST,
        local_work_first=cfg.local_work == LocalWork.FIRST,
        propagate_deadline=cfg.propagate_deadline,
    ).bind(seed=seed, record_events=True)


def arrival_seed(rng_seed: Optional[int], position: int = 0, replica: int = 0) -> Optional[int]:
    """Seed for the arrival times of a client.
    To avoid all clients generating the same arrival times, we derive a deterministic seed from the base 
    `rng_seed` using the `position` and `replica` of the client.

    * `position`: the position of the client among the clients that target the same service (0, 1, 2, ...);
    * `replica`: the index of the client among its `replicas` (0, 1, 2, ...).
    """
    if position == 0 and replica == 0:
        return rng_seed
    base = rng_seed if rng_seed is not None else 0
    return get_stable_seed(base, f"arrivals:{position}:{replica}")


def build_workload(cfg: WorkloadConfig, position: int = 0, replica: int = 0) -> Workload:
    return Workload(
        base_rps=cfg.base_rps,
        duration_s=cfg.duration_s,
        load_spikes=build_load_spikes(cfg.load_spikes),
        rng_seed=arrival_seed(cfg.rng_seed, position, replica),
        exact_arrivals=cfg.exact_arrivals,
    )
