"""Compatibility helpers for legacy YAML/config fields."""

from typing import List

from simulator.config.schema import DependencyConfig, ServiceConfigYAML


def get_effective_dependencies(svc_cfg: ServiceConfigYAML) -> List[DependencyConfig]:
    """
    Return effective dependency list, handling backward compatibility.

    Converts legacy `dependency: str` into `dependencies: [{service: ...}]`.
    """
    if svc_cfg.dependencies:
        return svc_cfg.dependencies
    if svc_cfg.dependency is not None:
        return [DependencyConfig(service=svc_cfg.dependency)]
    return []
