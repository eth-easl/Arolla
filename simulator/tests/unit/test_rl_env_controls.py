"""
Targeted tests for the RL token-bucket control loop.

These checks focus on the control-specific fixes:
1. Observation exposes bucket state and pressure vs refill.
2. Reward penalizes large/counteracting moves.
3. Runtime capacity updates clamp the live token balance.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from simulator.core.engine import Simulator
from simulator.core.types import DropReason
from simulator.metrics.live_buffer import LiveMetricsBuffer
from simulator.middleware.base import AttemptContext
from simulator.middleware.load_limiter import LoadLimiterMiddleware
from simulator.policies.retry_controls import GlobalRetryBudget
from simulator.rl.metastable_fairness_env import metastable_reward
from simulator.rl.random_scenario_env import (
    build_observation_vector,
    simple_reward,
    stabilize_window_observation,
)
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, s_to_ns


def test_build_observation_vector_exposes_budget_state_and_pressure():
    obs_dict = {
        "total_requests": 80,
        "success": 60,
        "failure": 20,
        "success_rate": 0.75,
        "error_rate": 0.25,
        "retries": 20,
        "retry_ratio": 0.25,
        "p50": 40.0,
        "p99": 75.0,
        "queue_avg": 10.0,
        "fail_queue_full": 8,
        "fail_deadline": 4,
        "fail_server": 8,
    }

    obs = build_observation_vector(
        obs_dict=obs_dict,
        queue_capacity=20,
        attempt_timeout_ms=50.0,
        decision_interval_ns=s_to_ns(2),
        prev_success_rate=0.65,
        prev_retry_ratio=0.15,
        prev_queue_util=0.30,
        bucket_balance=10.0,
        refill_rate=20,
        bucket_capacity=40,
        current_refill_idx=2,
        current_capacity_idx=3,
    )

    assert obs.shape == (15,)
    assert obs[7] == 0.10  # delta_success
    assert obs[8] == 0.10  # delta_retry
    assert obs[9] == 0.20  # delta_queue_util
    assert obs[10] == 2.0  # attempt pressure = 40 rps / 20 refill
    assert obs[11] == 0.5  # retry pressure = 10 rps / 20 refill
    assert obs[12] == 0.25  # bucket fill ratio


def test_simple_reward_penalizes_large_counteracting_moves():
    steady_reward = simple_reward(
        success_rate=0.95,
        retry_ratio=0.10,
        deadline_rate=0.01,
        queue_fail_rate=0.01,
        latency_pressure=0.20,
        delta_success=0.05,
        action_distance=0.0,
        counteracting_change=False,
    )
    noisy_reward = simple_reward(
        success_rate=0.95,
        retry_ratio=0.10,
        deadline_rate=0.01,
        queue_fail_rate=0.01,
        latency_pressure=0.20,
        delta_success=0.05,
        action_distance=2.0,
        counteracting_change=True,
    )

    assert steady_reward > noisy_reward


def test_stabilize_window_observation_reuses_previous_metrics_on_empty_window():
    obs_dict = LiveMetricsBuffer().get_observation(now_ns=0, window_ns=s_to_ns(2))

    stabilized = stabilize_window_observation(
        obs_dict,
        fallback_success_rate=0.8,
        fallback_retry_ratio=0.25,
        fallback_queue_util=0.4,
        queue_capacity=50,
    )

    assert stabilized["has_data"] is False
    assert stabilized["success_rate"] == 0.8
    assert stabilized["retry_ratio"] == 0.25
    assert stabilized["queue_avg"] == 20.0
    assert abs(stabilized["error_rate"] - 0.2) < 1e-9


def test_update_token_bucket_clamps_balance_when_capacity_shrinks():
    limiter = GlobalRetryBudget(
        max_tokens=20,
        refill_rate=10,
        period=s_to_ns(1),
    )
    limiter._tokens = 18.0

    service = ServiceRuntime(
        cfg=ServiceConfig(
            name="svc",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=1,
            load_limiter=limiter,
        )
    ).bind(seed=1)

    service.update_token_bucket(bucket_capacity=5)

    assert limiter.max_tokens == 5
    assert limiter.balance == 5.0


def test_load_limiter_only_consumes_budget_for_real_retries():
    limiter = GlobalRetryBudget(
        max_tokens=10,
        refill_rate=10,
        period=s_to_ns(1),
    )
    limiter._tokens = 7.0
    middleware = LoadLimiterMiddleware(limiter)

    ctx = AttemptContext(
        attempt_number=1,
        success=False,
        service_time=ms_to_ns(10),
        drop_reason=DropReason.SERVER_FAILURE,
        begin_time=0,
        end_time=ms_to_ns(10),
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="svc",
    )

    middleware.process_attempt(ctx, lambda _: None)

    assert limiter.balance == 7.0


def test_submit_request_prechecks_only_client_managed_retries():
    class CountingGlobalRetryBudget(GlobalRetryBudget):
        def __init__(self):
            super().__init__(
                max_tokens=10,
                refill_rate=10,
                period=s_to_ns(1),
            )
            self.calls = 0

        def next_delay(self, context):
            self.calls += 1
            return super().next_delay(context)

    sim = Simulator(seed=1)

    def on_attempt_done(*_args):
        return None

    def on_root_done():
        return None

    client_retry_limiter = CountingGlobalRetryBudget()
    client_retry_service = ServiceRuntime(
        cfg=ServiceConfig(
            name="client-retry-svc",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=1,
            load_limiter=client_retry_limiter,
        )
    ).bind(seed=1)

    client_retry_service.submit_request(
        sim,
        on_attempt_done,
        on_root_done,
        is_retry=True,
    )

    # Initial (non-retry) requests are never pre-checked: a retry budget only
    # gates retries, so applies_pre_queue_admission(is_retry=False) is False.
    initial_request_limiter = CountingGlobalRetryBudget()
    initial_request_service = ServiceRuntime(
        cfg=ServiceConfig(
            name="initial-request-svc",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=1,
            load_limiter=initial_request_limiter,
        )
    ).bind(seed=1)

    initial_request_service.submit_request(
        sim,
        on_attempt_done,
        on_root_done,
        is_retry=False,
    )

    assert client_retry_limiter.calls == 1
    assert initial_request_limiter.calls == 0


def test_budget_denial_is_recorded_in_live_buffer():
    limiter = GlobalRetryBudget(
        max_tokens=1,
        refill_rate=0,
        period=s_to_ns(1),
    )
    limiter._tokens = 0.0

    service = ServiceRuntime(
        cfg=ServiceConfig(
            name="svc",
            latency_median=ms_to_ns(10),
            latency_lognorm_sigma=0.1,
            workers=1,
            load_limiter=limiter,
        )
    ).bind(seed=1)
    service.enable_live_buffer()

    sim = Simulator(seed=1)

    def on_attempt_done(*_args):
        return None

    service.submit_request(
        sim,
        on_attempt_done=on_attempt_done,
        on_root_done=lambda: None,
        is_retry=True,
    )

    obs_dict = service.live_buffer.get_observation(sim.timestep, s_to_ns(2))
    assert obs_dict["has_data"] is True
    assert obs_dict["total_requests"] == 1
    assert obs_dict["retries"] == 1
    assert obs_dict["fail_server"] == 1
    assert obs_dict["success_rate"] == 0.0


def test_metastable_reward_prefers_better_benchmark_metrics():
    worse_metrics = {
        "agg_success": 0.45,
        "min_client_success": 0.25,
        "load_amplification": 2.2,
        "retry_efficiency": 0.15,
        "fairness_gap": 0.40,
    }
    better_metrics = {
        "agg_success": 0.82,
        "min_client_success": 0.72,
        "load_amplification": 1.10,
        "retry_efficiency": 0.60,
        "fairness_gap": 0.08,
    }

    worse_reward = metastable_reward(
        success_rate=0.5,
        retry_ratio=0.4,
        deadline_rate=0.12,
        queue_fail_rate=0.08,
        latency_pressure=1.4,
        delta_success=-0.05,
        action_distance=1.0,
        counteracting_change=True,
        fault_active=True,
        recovery_active=False,
        client_window_metrics=worse_metrics,
    )
    better_reward = metastable_reward(
        success_rate=0.82,
        retry_ratio=0.18,
        deadline_rate=0.03,
        queue_fail_rate=0.01,
        latency_pressure=0.25,
        delta_success=0.10,
        action_distance=1.0,
        counteracting_change=False,
        fault_active=True,
        recovery_active=False,
        client_window_metrics=better_metrics,
    )

    assert better_reward > worse_reward
