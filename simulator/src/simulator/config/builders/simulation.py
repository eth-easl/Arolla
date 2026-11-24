"""High-level simulation assembly builders (topology + services + clients/workloads)."""

from __future__ import annotations

import random
from collections import deque
from typing import Dict, List

from simulator.config.schema import ExperimentConfig, ServiceConfigYAML
from simulator.faults.events import FaultEventsTracker
from simulator.policies.retry_controls import RetryBudgetPolicy
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.runtime.service import ServiceRuntime
from simulator.runtime.workload import Workload
from simulator.core.engine import Simulator

from . import policies as policy_builders
from . import runtime as runtime_builders
from .seeds import get_stable_seed


def resolve_service_topology(
    config: ExperimentConfig,
) -> tuple[Dict[str, ServiceConfigYAML], List[str]]:
    """
    Return services in topological order (leaf -> root), rejecting cycles.
    """
    svc_map: Dict[str, ServiceConfigYAML] = {s.name: s for s in config.services}
    dep_names: Dict[str, List[str]] = {}
    in_degree: Dict[str, int] = {s.name: 0 for s in config.services}

    for svc_cfg in config.services:
        names = [d.service for d in svc_cfg.dependencies]
        dep_names[svc_cfg.name] = names
        in_degree[svc_cfg.name] = len(names)

    queue = deque([name for name, deg in in_degree.items() if deg == 0])
    topo_order: List[str] = []

    while queue:
        name = queue.popleft()
        topo_order.append(name)
        for other_name, other_deps in dep_names.items():
            if name in other_deps:
                in_degree[other_name] -= 1
                if in_degree[other_name] == 0:
                    queue.append(other_name)

    if len(topo_order) != len(config.services):
        missing = set(s.name for s in config.services) - set(topo_order)
        raise ValueError(f"Circular dependency detected involving: {missing}")

    return svc_map, topo_order


def build_service_graph(
    config: ExperimentConfig,
    sim: Simulator,
    fault_tracker: FaultEventsTracker,
) -> Dict[str, ServiceRuntime]:
    svc_map, topo_order = resolve_service_topology(config)
    services: Dict[str, ServiceRuntime] = {}

    for name in topo_order:
        svc_cfg = svc_map[name]
        svc_seed = get_stable_seed(config.seed, name)

        if svc_cfg.dependencies:
            dep_runtimes: List[ServiceRuntime] = []
            dep_optionality: List[bool] = []
            for dep_cfg in svc_cfg.dependencies:
                dep_runtimes.append(services[dep_cfg.service])
                dep_optionality.append(dep_cfg.optional)

            call_pattern = (
                svc_cfg.dependency_call_pattern.value
                if hasattr(svc_cfg.dependency_call_pattern, "value")
                else str(svc_cfg.dependency_call_pattern)
            )
            services[name] = runtime_builders.build_service(
                svc_cfg,
                sim,
                dependencies=dep_runtimes,
                dependency_optionality=dep_optionality,
                dependency_call_pattern=call_pattern,
                seed=svc_seed,
            )
        else:
            services[name] = runtime_builders.build_service(svc_cfg, sim, seed=svc_seed)

    for svc_runtime in services.values():
        svc_runtime.cfg.register_fault_events(fault_tracker)

    return services


def build_clients_and_workloads(
    config: ExperimentConfig,
    services: Dict[str, ServiceRuntime],
    fault_tracker: FaultEventsTracker,
) -> tuple[List[ClientRuntime], List[Workload]]:
    entry_service = services[config.services[0].name]
    clients: List[ClientRuntime] = []
    workloads: List[Workload] = []
    shared_budgets: Dict[str, RetryBudgetPolicy] = {}

    if config.clients:
        entries_on_service: Dict[str, int] = {}
        for client_cfg_yaml in config.clients:
            replicas = getattr(client_cfg_yaml, "replicas", 1)
            target_name = client_cfg_yaml.target_service or entry_service.cfg.name
            position = entries_on_service.get(target_name, 0)
            entries_on_service[target_name] = position + 1
            for i in range(replicas):
                client_name = client_cfg_yaml.name if replicas <= 1 else f"{client_cfg_yaml.name}.{i}"
                client_seed = get_stable_seed(config.seed, client_name)
                client_rng = random.Random(client_seed)

                c_retry_policy = policy_builders.build_client_retry_policy(
                    client_cfg_yaml,
                    client_rng,
                    shared_budgets=shared_budgets,
                )
                c_timeout_policy = policy_builders.build_timeout_policy(client_cfg_yaml.timeout)
                c_cfg = ClientConfig(
                    name=client_name,
                    retry=c_retry_policy,
                    timeout=c_timeout_policy,
                    e2e_retry_budget=getattr(client_cfg_yaml, 'e2e_retry_budget', None),
                    propagate_deadline=client_cfg_yaml.propagate_deadline,
                )

                clients.append(ClientRuntime(cfg=c_cfg, service=services[target_name]))

                wl = runtime_builders.build_workload(
                    client_cfg_yaml.workload, position=position, replica=i
                )
                wl.register_fault_events(fault_tracker)
                workloads.append(wl)
    else:
        clients.append(ClientRuntime(cfg=ClientConfig(name="client"), service=entry_service))
        wl = runtime_builders.build_workload(config.workload)
        wl.register_fault_events(fault_tracker)
        workloads.append(wl)

    return clients, workloads


def build_simulation(config: ExperimentConfig):
    """
    Build complete simulation from experiment configuration.

    Returns:
        (simulator, clients, workloads, fault_tracker, services)
    """
    sim = Simulator(seed=config.seed)
    fault_tracker = FaultEventsTracker()
    services = build_service_graph(config, sim, fault_tracker)
    clients, workloads = build_clients_and_workloads(config, services, fault_tracker)
    return sim, clients, workloads, fault_tracker, services
