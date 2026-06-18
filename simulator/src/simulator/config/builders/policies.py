"""Policy builder functions for YAML config -> runtime policy objects."""

from __future__ import annotations

import random
from typing import Dict, Optional

from simulator.config.schema import (
    AIMDGlobalRetryBudgetConfig,
    CircuitBreakerConfig,
    CircuitBreakerType,
    JitterMode,
    RateLimiterConfig,
    RateLimiterType,
    RetryBudgetConfig,
    RetryConfig,
    RetryPolicyType,
    ArollaRetryBudgetConfig,
    IstioRetryBudgetConfig,
    TimeoutConfig,
    GlobalRetryBudgetConfig,
    ClientConfigYAML,
)
from simulator.utils.time import ms_to_ns, s_to_ns
from simulator.policies.retry import (
    RetryPolicy,
    NoRetryPolicy,
    FixedBackoffRetryPolicy,
    ExponentialBackoffRetryPolicy,
    ExponentialBackoffWithJitterRetryPolicy,
    JitterMode as PolicyJitterMode,
)
from simulator.policies.retry_controls import (
    RetryBudgetPolicy,
    RetryCircuitBreakerPolicy,
    TimeBasedCircuitBreakerPolicy,
    CountBasedCircuitBreakerPolicy,
    LimiterTimeBasedCircuitBreakerPolicy,
    LimiterRetryBudgetPolicy,
    GlobalRetryBudget,
    AIMDGlobalRetryBudget,
    GoodputCoupledRetryBudget,
)
from simulator.policies.timeout import Timeout, StaticTimeout
from simulator.policies.load_limiter import (
    LoadLimiter,
    LeakyRateLimiterPolicy,
    BurstyRateLimiterPolicy,
    FixedWindowBurstyLimiterPolicy,
)
from simulator.policies.istio_retry_budget import IstioRetryBudget


def build_retry_policy(
    cfg: Optional[RetryConfig], rng: random.Random
) -> Optional[RetryPolicy]:
    if cfg is None:
        return None

    if cfg.type == RetryPolicyType.NONE:
        return NoRetryPolicy()

    if cfg.type == RetryPolicyType.FIXED:
        return FixedBackoffRetryPolicy(
            max_attempts=cfg.max_attempts,
            delay=ms_to_ns(cfg.delay_ms),
        )

    if cfg.type == RetryPolicyType.EXPONENTIAL:
        return ExponentialBackoffRetryPolicy(
            max_attempts=cfg.max_attempts,
            initial_delay=ms_to_ns(cfg.initial_delay_ms),
            max_delay=ms_to_ns(cfg.max_delay_ms),
        )

    if cfg.type == RetryPolicyType.JITTERED:
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
            jitter_mode=jitter_mode_map.get(cfg.jitter_mode, PolicyJitterMode.FULL),
        )

    raise ValueError(f"Unknown retry policy type: {cfg.type}")


def build_timeout_policy(cfg: Optional[TimeoutConfig]) -> Optional[Timeout]:
    if cfg is None:
        return None

    global_timeout = ms_to_ns(cfg.global_ms) if cfg.global_ms is not None else None
    attempt_timeout = ms_to_ns(cfg.attempt_ms) if cfg.attempt_ms is not None else None
    return StaticTimeout(global_timeout=global_timeout, attempt_timeout=attempt_timeout)


def build_circuit_breaker(cfg: Optional[CircuitBreakerConfig]) -> Optional[LoadLimiter]:
    if cfg is None:
        return None

    if cfg.type == CircuitBreakerType.COUNT_BASED:
        if cfg.success_threshold is None:
            raise ValueError("success_threshold is required for count_based circuit breaker")
        failure_count = int(cfg.failure_threshold * cfg.failure_window_size)
        success_count = int(cfg.success_threshold * cfg.success_window_size)
        return CountBasedCircuitBreakerPolicy(
            failure_threshold_ratio=(failure_count, cfg.failure_window_size),
            success_threshold_ratio=(success_count, cfg.success_window_size),
            half_open_delay=ms_to_ns(cfg.half_open_delay_ms),
        )

    if cfg.type == CircuitBreakerType.TIME_BASED:
        success_thresh = cfg.success_threshold if cfg.success_threshold is not None else (1.0 - cfg.failure_threshold)
        return LimiterTimeBasedCircuitBreakerPolicy(
            failure_threshold_rate=cfg.failure_threshold,
            success_threshold_rate=success_thresh,
            min_requests=cfg.min_requests,
            window_duration=ms_to_ns(cfg.window_duration_ms),
            half_open_delay=ms_to_ns(cfg.half_open_delay_ms),
        )

    if cfg.type == CircuitBreakerType.RETRY_CIRCUIT_BREAKER:
        # This class is a RetryPolicy but is compatible with the limiter interface
        # used by service-side retry gating because it exposes `next_delay(...)`.
        return RetryCircuitBreakerPolicy(
            inner=None,
            failure_rate_threshold=cfg.failure_threshold,
            window_duration=ms_to_ns(cfg.window_duration_ms),
            min_requests=cfg.min_requests if cfg.min_requests else 100,
            wait_duration_in_open_state=(
                ms_to_ns(cfg.half_open_delay_ms) if cfg.half_open_delay_ms else 0
            ),
            min_window_size=cfg.min_requests if cfg.min_requests else 100,
        )

    raise ValueError(f"Unknown circuit breaker type: {cfg.type}")


def build_rate_limiter(cfg: Optional[RateLimiterConfig]) -> Optional[LoadLimiter]:
    if cfg is None:
        return None

    if cfg.type == RateLimiterType.LEAKY:
        return LeakyRateLimiterPolicy(
            max_requests=cfg.max_requests,
            max_attempts=cfg.max_attempts or cfg.max_requests,
            period=ms_to_ns(cfg.period_ms),
        )

    if cfg.type == RateLimiterType.BURSTY:
        return BurstyRateLimiterPolicy(
            max_requests=cfg.max_requests,
            refill_rate=cfg.refill_rate or 1,
            period=ms_to_ns(cfg.period_ms),
        )

    if cfg.type == RateLimiterType.FIXED_WINDOW:
        return FixedWindowBurstyLimiterPolicy(
            max_requests=cfg.max_requests,
            period=ms_to_ns(cfg.period_ms),
        )

    raise ValueError(f"Unknown rate limiter type: {cfg.type}")


def build_retry_budget(cfg: Optional[RetryBudgetConfig]) -> Optional[LoadLimiter]:
    if cfg is None:
        return None
    return LimiterRetryBudgetPolicy(budget_ratio=cfg.budget_ratio, max_retries=cfg.max_retries)


def build_global_retry_budget(cfg: Optional[GlobalRetryBudgetConfig]) -> Optional[LoadLimiter]:
    if cfg is None:
        return None
    return GlobalRetryBudget(
        max_tokens=cfg.max_burst,
        refill_rate=cfg.target_rps,
        period=s_to_ns(1),
    )


def build_istio_retry_budget(
    cfg: Optional[IstioRetryBudgetConfig],
) -> Optional[LoadLimiter]:
    if cfg is None:
        return None
    return IstioRetryBudget(
        percent=cfg.percent,
        min_retry_concurrency=cfg.min_retry_concurrency,
    )


def build_aimd_global_retry_budget(
    cfg: Optional[AIMDGlobalRetryBudgetConfig],
) -> Optional[LoadLimiter]:
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
        failure_threshold=cfg.failure_threshold,
    )


def build_arolla_retry_budget(
    cfg: Optional[ArollaRetryBudgetConfig],
) -> Optional[LoadLimiter]:
    if cfg is None:
        return None
    return GoodputCoupledRetryBudget(
        alpha=cfg.alpha,
        beta_down=cfg.beta_down,
        beta_up=cfg.beta_up,
        window_duration=ms_to_ns(cfg.window_ms),
        success_rate_threshold=cfg.success_rate_threshold,
        success_rate_beta=cfg.success_rate_beta,
        max_retry_ratio=cfg.max_retry_ratio,
    )


def build_load_limiter(
    circuit_breaker: Optional[CircuitBreakerConfig],
    rate_limiter: Optional[RateLimiterConfig],
    retry_budget: Optional[RetryBudgetConfig],
    global_retry_budget: Optional[GlobalRetryBudgetConfig],
    aimd_global_retry_budget: Optional[AIMDGlobalRetryBudgetConfig],
    arolla_retry_budget: Optional[ArollaRetryBudgetConfig] = None,
    istio_retry_budget: Optional[IstioRetryBudgetConfig] = None,
) -> Optional[LoadLimiter]:
    # Priority: arolla > circuit breaker > AIMD > istio > global > local > rate limiter
    if arolla_retry_budget is not None:
        return build_arolla_retry_budget(arolla_retry_budget)
    if circuit_breaker is not None:
        return build_circuit_breaker(circuit_breaker)
    if aimd_global_retry_budget is not None:
        return build_aimd_global_retry_budget(aimd_global_retry_budget)
    if istio_retry_budget is not None:
        return build_istio_retry_budget(istio_retry_budget)
    if global_retry_budget is not None:
        return build_global_retry_budget(global_retry_budget)
    if retry_budget is not None:
        return build_retry_budget(retry_budget)
    if rate_limiter is not None:
        return build_rate_limiter(rate_limiter)
    return None


def build_client_retry_policy(
    client_cfg_yaml: ClientConfigYAML,
    rng: random.Random,
    shared_budgets: Optional[Dict[str, RetryBudgetPolicy]] = None,
) -> Optional[RetryPolicy]:
    """
    Build the full client-side retry policy stack, including wrappers such as
    retry circuit breakers and local/shared retry budgets.
    """
    policy = build_retry_policy(client_cfg_yaml.retry, rng)
    if policy is None:
        return None

    if client_cfg_yaml.circuit_breaker is not None:
        cb_cfg = client_cfg_yaml.circuit_breaker
        if cb_cfg.type == CircuitBreakerType.RETRY_CIRCUIT_BREAKER:
            policy = RetryCircuitBreakerPolicy(
                inner=policy,
                failure_rate_threshold=cb_cfg.failure_threshold,
                window_duration=ms_to_ns(cb_cfg.window_duration_ms or 5000),
                min_window_size=cb_cfg.min_requests or 100,
                wait_duration_in_open_state=ms_to_ns(cb_cfg.half_open_delay_ms or 0),
            )
        elif cb_cfg.type == CircuitBreakerType.TIME_BASED:
            policy = TimeBasedCircuitBreakerPolicy(
                inner=policy,
                failure_rate_threshold=cb_cfg.failure_threshold,
                window_duration=ms_to_ns(cb_cfg.window_duration_ms or 5000),
                min_window_size=cb_cfg.min_requests or 100,
                wait_duration_in_open_state=ms_to_ns(cb_cfg.half_open_delay_ms or 1000),
            )

    if client_cfg_yaml.retry_budget is not None:
        budget_cfg = client_cfg_yaml.retry_budget
        shared_budgets = shared_budgets if shared_budgets is not None else {}

        if budget_cfg.shared_budget_id:
            existing = shared_budgets.get(budget_cfg.shared_budget_id)
            if existing is not None:
                return existing

            new_budget = RetryBudgetPolicy(
                inner=policy,
                budget_ratio=budget_cfg.budget_ratio,
                max_retries=budget_cfg.max_retries,
            )
            shared_budgets[budget_cfg.shared_budget_id] = new_budget
            return new_budget

        return RetryBudgetPolicy(
            inner=policy,
            budget_ratio=budget_cfg.budget_ratio,
            min_retries_per_sec=budget_cfg.min_retries_per_sec,
            max_retries=budget_cfg.max_retries,
        )

    return policy
