from __future__ import annotations

from pathlib import Path


BENCHMARKS_DIR = Path(__file__).resolve().parents[3] / "experiments" / "yaml" / "rl" / "metastable_benchmarks"

# Keep the default metastable benchmark suite explicit so training/evaluation
# does not silently change when unrelated YAMLs are added to the directory.
DEFAULT_BENCHMARK_SCENARIOS = (
    "metastable_partial_failure_fairness",
    "metastable_load_spike_fairness",
    "metastable_failure_fairness",
    # Explicit non-stationary scenario: fault (throttle) followed by spike
    # (admit), 15s apart, so no static setting can be optimal across both.
    "metastable_recovery_with_spike_fairness",
    # Harder adaptive-control stress test with a smaller queue, tighter
    # timeouts, a severe fault, and a later heavy spike.
    "metastable_switchback_adversarial_fairness",
)


def resolve_benchmark_yaml_paths(scenario_names: list[str] | tuple[str, ...] | None = None) -> list[Path]:
    names = tuple(scenario_names) if scenario_names else DEFAULT_BENCHMARK_SCENARIOS
    yaml_paths: list[Path] = []

    for name in names:
        yaml_path = BENCHMARKS_DIR / f"{name}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"Missing metastable benchmark YAML: {yaml_path}")
        yaml_paths.append(yaml_path)

    return yaml_paths
