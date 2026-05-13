"""Focused tests for Istio-style retry budget control."""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from simulator.policies.istio_retry_budget import IstioRetryBudget
from simulator.policies.retry import RetryContext
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.core.engine import Simulator
from simulator.rl.istio_retry_budget_env import IstioRetryBudgetMetastableEnv
from simulator.utils.time import ms_to_ns, s_to_ns


TRAIN_YAML = str(ROOT / "experiments" / "yaml" / "rl" / "istio_retry_budget_metastable.yaml")


def test_istio_retry_budget_uses_percent_and_minimum_floor():
    budget = IstioRetryBudget(percent=20.0, min_retry_concurrency=3)

    budget.update_runtime_state(active_requests=25, pending_requests=0, active_retries=4)
    allowed, _ = budget.next_delay(RetryContext(attempt=2, now=0))
    assert allowed is True
    assert budget.concurrency_limit == 5.0

    budget.update_runtime_state(active_requests=25, pending_requests=0, active_retries=5)
    allowed, _ = budget.next_delay(RetryContext(attempt=2, now=0))
    assert allowed is False

    budget.update_runtime_state(active_requests=2, pending_requests=0, active_retries=2)
    allowed, _ = budget.next_delay(RetryContext(attempt=2, now=0))
    assert allowed is True
    assert budget.concurrency_limit == 3.0


def test_istio_retry_budget_env_reset_and_step():
    env = IstioRetryBudgetMetastableEnv(TRAIN_YAML, decision_interval_s=2.0, randomize_scenarios=False)
    obs, _ = env.reset(seed=123)

    assert obs.shape == (18,)
    assert len(env.clients) == 3

    next_obs, reward, done, truncated, info = env.step(np.array([2, 2], dtype=np.int64))

    assert next_obs.shape == (18,)
    assert np.isfinite(reward)
    assert done is False
    assert truncated is False
    assert "client_metrics" in info
    assert env.sim.timestep == s_to_ns(2)
    assert env.history[-1]["action_percent"] == 20.0
    assert env.history[-1]["action_min_retry_concurrency"] == 3

    env.close()


def test_client_managed_retry_counts_against_istio_concurrency_budget():
    limiter = IstioRetryBudget(percent=0.0, min_retry_concurrency=1)
    service = ServiceRuntime(
        cfg=ServiceConfig(
            name="svc",
            latency_median=ms_to_ns(100),
            latency_lognorm_sigma=0.01,
            workers=1,
            queue_capacity=10,
            load_limiter=limiter,
        )
    ).bind(seed=1)
    sim = Simulator(seed=1)
    callbacks = []

    def on_attempt_done(*args):
        callbacks.append(args)

    def on_root_done():
        return None

    service.submit_request(sim, on_attempt_done, on_root_done, is_retry=False)
    assert service.in_flight == 1

    service.submit_request(sim, on_attempt_done, on_root_done, is_retry=True)
    assert service.queued_retries == 1

    service.submit_request(sim, on_attempt_done, on_root_done, is_retry=True)
    assert service.queued_retries == 1
    assert len(callbacks) == 1
