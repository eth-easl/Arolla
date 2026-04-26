"""
Focused tests for the metastable multi-client RL environment.
"""

import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "src"))

from metastable_benchmark_suite import DEFAULT_BENCHMARK_SCENARIOS, resolve_benchmark_yaml_paths
from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv
from simulator.utils.time import s_to_ns


TRAIN_YAML = str(ROOT / "experiments" / "yaml" / "rl" / "metastable_token_bucket_fairness.yaml")
BENCHMARK_YAML = str(ROOT / "experiments" / "yaml" / "rl" / "metastable_benchmarks" / "metastable_failure_fairness.yaml")


def test_metastable_env_reset_and_step():
    env = MetastableFairnessSimEnv(TRAIN_YAML, decision_interval_s=2.0, randomize_scenarios=False)
    obs, _ = env.reset(seed=123)

    assert obs.shape == (21,)
    assert len(env.clients) == 3
    assert env.sim.timestep == 0

    next_obs, reward, done, truncated, info = env.step(np.array([2, 2], dtype=np.int64))

    assert next_obs.shape == (21,)
    assert np.isfinite(reward)
    assert done is False
    assert truncated is False
    assert "client_metrics" in info
    assert env.sim.timestep == s_to_ns(2)

    env.close()


def test_metastable_phase_flags_match_benchmark_windows():
    env = MetastableFairnessSimEnv(BENCHMARK_YAML, decision_interval_s=2.0, randomize_scenarios=False)
    env.reset(seed=42)

    active_fault, recovery_fault = env._phase_flags(s_to_ns(25))
    active_recovery, recovery_recovery = env._phase_flags(s_to_ns(45))

    assert active_fault is True
    assert recovery_fault is False
    assert active_recovery is False
    assert recovery_recovery is True

    env.close()


def test_default_metastable_benchmark_suite_has_multiple_scenarios():
    yaml_paths = resolve_benchmark_yaml_paths()

    assert len(DEFAULT_BENCHMARK_SCENARIOS) >= 3
    assert [path.stem for path in yaml_paths] == list(DEFAULT_BENCHMARK_SCENARIOS)
