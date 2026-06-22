import gymnasium as gym
import numpy as np
from gymnasium import spaces

from simulator.config.schema import LoadSpikeConfig, PartialFailureConfig
from simulator.core.models import Request, RootRequest
from simulator.core.types import DropReason
from simulator.policies.istio_retry_budget import IstioRetryBudget
from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv
from simulator.utils.time import s_to_ns


PERCENT_MAP = [0.0, 5.0, 10.0, 20.0, 35.0, 50.0]
MIN_RETRY_CONCURRENCY_MAP = [0, 1, 2, 3, 5, 8]
DEFAULT_PERCENT_IDX = 3  # 20%
DEFAULT_MIN_IDX = 3  # minRetryConcurrency=3


def _safe_ratio(num: float, den: float, default: float = 0.0) -> float:
    return float(num / den) if den > 0 else default


def _attempt_latency_ms(attempt: Request) -> float:
    return float(attempt.interval.duration() / 1e6)


def _attempts_completed_in_window(
    clients: list,
    window_start_ns: int,
    window_end_ns: int,
) -> list[tuple[object, Request, RootRequest, int]]:
    """Return caller-side attempts whose completion falls in [start, end]."""
    completed: list[tuple[object, Request, RootRequest, int]] = []
    for client in clients:
        for root in client.roots:
            if not root.attempts:
                continue
            for attempt_idx, attempt in enumerate(root.attempts):
                if window_start_ns <= attempt.interval.end <= window_end_ns:
                    completed.append((client, attempt, root, attempt_idx))
    return completed


def istio_caller_window_metrics(
    clients: list,
    window_start_ns: int,
    window_end_ns: int,
    attempt_timeout_ms: float,
    istio_budget: IstioRetryBudget | None,
    fallback_success_rate: float,
    fallback_retry_ratio: float,
) -> dict[str, float]:
    """Caller-side, per-attempt window metrics aligned with the live prototype."""
    completed_attempts = _attempts_completed_in_window(clients, window_start_ns, window_end_ns)
    total_attempts = len(completed_attempts)
    total_retries = sum(1 for _, _, _, attempt_idx in completed_attempts if attempt_idx > 0)
    successful_retries = sum(
        1
        for _, attempt, _, attempt_idx in completed_attempts
        if attempt_idx > 0 and attempt.success
    )
    first_attempts = total_attempts - total_retries

    if total_attempts > 0:
        agg_success = _safe_ratio(
            sum(1 for _, attempt, _, _ in completed_attempts if attempt.success),
            total_attempts,
        )
        server_fail_rate = _safe_ratio(
            sum(
                1
                for _, attempt, _, _ in completed_attempts
                if attempt.drop_reason in {DropReason.SERVER_FAILURE, DropReason.QUEUE_FULL}
            ),
            total_attempts,
        )
        deadline_rate = _safe_ratio(
            sum(
                1
                for _, attempt, _, _ in completed_attempts
                if attempt.drop_reason == DropReason.DEADLINE
                or _attempt_latency_ms(attempt) >= float(attempt_timeout_ms)
            ),
            total_attempts,
        )
        retry_ratio = _safe_ratio(total_retries, total_attempts)
        load_amplification = _safe_ratio(total_attempts, first_attempts, default=1.0 + fallback_retry_ratio)
        retry_efficiency = _safe_ratio(successful_retries, total_retries)
        attempt_latencies_ms = [_attempt_latency_ms(attempt) for _, attempt, _, _ in completed_attempts]
        p95_ms = float(np.percentile(attempt_latencies_ms, 95))
        p50_ms = float(np.percentile(attempt_latencies_ms, 50))
        p99_ms = float(np.percentile(attempt_latencies_ms, 99))
    else:
        agg_success = fallback_success_rate
        server_fail_rate = 0.0
        deadline_rate = 0.0
        retry_ratio = fallback_retry_ratio
        load_amplification = 1.0 + fallback_retry_ratio
        retry_efficiency = 0.0
        p95_ms = p50_ms = p99_ms = 0.0

    per_client_totals: dict[str, int] = {}
    per_client_successes: dict[str, int] = {}
    per_client_retries: dict[str, int] = {}
    per_client_first_attempts: dict[str, int] = {}
    for client, attempt, _, attempt_idx in completed_attempts:
        name = client.cfg.name
        per_client_totals[name] = per_client_totals.get(name, 0) + 1
        if attempt.success:
            per_client_successes[name] = per_client_successes.get(name, 0) + 1
        if attempt_idx > 0:
            per_client_retries[name] = per_client_retries.get(name, 0) + 1
        else:
            per_client_first_attempts[name] = per_client_first_attempts.get(name, 0) + 1

    if per_client_totals:
        client_success_rates = {
            name: _safe_ratio(per_client_successes.get(name, 0), count, default=fallback_success_rate)
            for name, count in per_client_totals.items()
        }
        min_client_success = min(client_success_rates.values())
    else:
        min_client_success = fallback_success_rate

    total_first_attempts = sum(per_client_first_attempts.values())
    first_attempt_shares = {
        name: _safe_ratio(count, total_first_attempts)
        for name, count in per_client_first_attempts.items()
    }
    retry_shares = {
        name: _safe_ratio(count, total_retries)
        for name, count in per_client_retries.items()
    }
    profile_names = set(first_attempt_shares) | set(retry_shares)
    fairness_gap = 0.5 * sum(
        abs(retry_shares.get(name, 0.0) - first_attempt_shares.get(name, 0.0))
        for name in profile_names
    )

    if istio_budget is not None:
        rejected_retries, admitted_retries = istio_budget.window_admission_counts(
            window_start_ns,
            window_end_ns,
        )
        budget_reject_rate = _safe_ratio(
            rejected_retries,
            rejected_retries + admitted_retries,
        )
    else:
        budget_reject_rate = 0.0

    p95_latency_pressure = p95_ms / max(float(attempt_timeout_ms), 1.0)

    return {
        "agg_success": agg_success,
        "min_client_success": min_client_success,
        "retry_ratio": retry_ratio,
        "load_amplification": load_amplification,
        "retry_efficiency": retry_efficiency,
        "fairness_gap": float(np.clip(fairness_gap, 0.0, 1.0)),
        "p95_latency_pressure": p95_latency_pressure,
        "budget_reject_rate": budget_reject_rate,
        "server_fail_rate": server_fail_rate,
        "deadline_rate": deadline_rate,
        "retry_count": float(total_retries),
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "p99_ms": p99_ms,
    }


def istio_budget_indices_from_physical(
    percent: float,
    min_retry_concurrency: int,
    percent_map=None,
    min_map=None,
) -> tuple[int, int]:
    percent_map = list(percent_map if percent_map is not None else PERCENT_MAP)
    min_map = list(min_map if min_map is not None else MIN_RETRY_CONCURRENCY_MAP)
    percent_idx = int(np.argmin([abs(float(value) - float(percent)) for value in percent_map]))
    min_idx = int(np.argmin([abs(int(value) - int(min_retry_concurrency)) for value in min_map]))
    return percent_idx, min_idx


def build_istio_metastable_observation_vector(
    attempt_timeout_ms: float,
    metrics_window_ns: float,
    delta_success_agg: float,
    delta_window_load_amplification: float,
    budget_utilization: float,
    retry_concurrency_limit: float,
    percent: float,
    min_retry_concurrency: int,
    previous_percent: float,
    previous_min_retry_concurrency: int,
    client_window_metrics: dict[str, float],
) -> np.ndarray:
    """Observation vector for Istio retry-budget tuning.

    Caller-side, per-attempt features aligned with the live prototype controller.
    """
    window_s = max(float(metrics_window_ns) / 1e9, 1e-9)
    retry_rps = float(client_window_metrics["retry_count"]) / window_s
    effective_limit = max(float(retry_concurrency_limit), 1.0)

    return np.array([
        client_window_metrics["agg_success"],
        client_window_metrics["min_client_success"],
        client_window_metrics["retry_ratio"],
        client_window_metrics["load_amplification"],
        client_window_metrics["retry_efficiency"],
        client_window_metrics["fairness_gap"],
        client_window_metrics["p95_latency_pressure"],
        client_window_metrics["budget_reject_rate"],
        client_window_metrics["server_fail_rate"],
        client_window_metrics["deadline_rate"],
        delta_success_agg,
        delta_window_load_amplification,
        budget_utilization,
        retry_rps / effective_limit,
        float(percent) / 100.0,
        float(min_retry_concurrency) / 10.0,
        float(previous_percent) / 100.0,
        float(previous_min_retry_concurrency) / 10.0,
    ], dtype=np.float32)


def istio_retry_budget_reward(
    *,
    success_rate: float,
    retry_ratio: float,
    deadline_rate: float,
    queue_fail_rate: float,
    server_fail_rate: float,
    latency_pressure: float,
    budget_reject_rate: float,
    budget_utilization: float,
    retry_pressure_vs_limit: float,
    delta_success: float,
    delta_retry_pressure: float,
    action_distance: float,
    action_reversal: bool,
) -> float:
    """Server-side reward for concurrency-based retry-budget control.

    This reward intentionally avoids client/root-level benchmark metrics. It
    teaches the controller to preserve attempt success, suppress retries during
    overload, and avoid oscillating between retry-budget settings.
    """
    tail_pressure = min(float(latency_pressure), 3.0)
    retry_pressure = min(float(retry_pressure_vs_limit), 3.0)
    budget_reject_pressure = min(max(float(budget_reject_rate), 0.0), 3.0)
    overload = max(deadline_rate, queue_fail_rate, tail_pressure / 3.0, budget_reject_pressure)
    retry_storm = retry_ratio * overload
    budget_saturation = max(0.0, budget_utilization - 0.85)

    recovery_progress = max(delta_success, 0.0)
    retry_pressure_growth = max(delta_retry_pressure, 0.0)

    return float(
        2.2 * success_rate
        + 0.35 * recovery_progress
        - 0.45 * retry_ratio
        - 0.85 * retry_storm
        - 0.65 * deadline_rate
        - 0.50 * queue_fail_rate
        - 0.20 * server_fail_rate
        - 0.25 * tail_pressure
        - 0.25 * budget_reject_pressure
        - 0.20 * budget_saturation
        - 0.08 * retry_pressure_growth
        - 0.05 * action_distance
        - 0.04 * float(action_reversal)
    )


class IstioRetryBudgetMetastableEnv(MetastableFairnessSimEnv):
    """Metastable RL env that tunes Istio retry-budget fields server-side."""

    OBSERVATION_FEATURES = [
        "success_rate_agg",
        "min_client_success",
        "retry_ratio",
        "window_load_amplification",
        "window_retry_efficiency",
        "retry_fairness_gap",
        "p95_latency_pressure",
        "budget_reject_rate",
        "server_fail_rate",
        "deadline_rate",
        "delta_success_agg",
        "delta_window_load_amplification",
        "budget_utilization",
        "retry_pressure_vs_limit",
        "current_percent_norm",
        "current_min_retry_concurrency_norm",
        "previous_percent_norm",
        "previous_min_retry_concurrency_norm",
    ]
    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([6, 6]): action[0] selects retryBudget.percent from "
        "[0, 5, 10, 20, 35, 50]; action[1] selects minRetryConcurrency from "
        "[0, 1, 2, 3, 5, 8]."
    )
    REWARD_DESCRIPTION = (
        "Server-side reward: maximize attempt success and recovery progress, "
        "while penalizing retry pressure during overload, deadlines, budget rejections, "
        "tail pressure, budget saturation, and action oscillation."
    )

    def __init__(
        self,
        yaml_path: str,
        decision_interval_s: float = 2.0,
        observation_window_s: float | None = None,
        delta_window_s: float | None = None,
        randomize_scenarios: bool = True,
        scenario_profile: str = "metastable_fairness",
    ):
        super().__init__(
            yaml_path=yaml_path,
            decision_interval_s=decision_interval_s,
            randomize_scenarios=randomize_scenarios,
            scenario_profile=scenario_profile,
        )
        if observation_window_s is None:
            self.observation_window_ns = self.decision_interval_ns
        elif observation_window_s <= 0:
            raise ValueError("observation_window_s must be positive.")
        else:
            self.observation_window_ns = s_to_ns(observation_window_s)
        if delta_window_s is None:
            self.delta_window_ns = self.observation_window_ns
        elif delta_window_s <= 0:
            raise ValueError("delta_window_s must be positive.")
        else:
            self.delta_window_ns = s_to_ns(delta_window_s)
        self.action_space = spaces.MultiDiscrete([6, 6])
        self.percent_map = list(PERCENT_MAP)
        self.min_retry_concurrency_map = list(MIN_RETRY_CONCURRENCY_MAP)
        self.current_percent_idx = DEFAULT_PERCENT_IDX
        self.current_min_idx = DEFAULT_MIN_IDX
        self.previous_percent = self.percent_map[self.current_percent_idx]
        self.previous_min_retry_concurrency = self.min_retry_concurrency_map[self.current_min_idx]
        self.prev_window_load_amplification = 1.0
        self.prev_delta_success_rate = 1.0
        self.prev_delta_window_load_amplification = 1.0
        self.prev_reward_success_rate = 1.0
        self.prev_reward_retry_pressure = 0.0
        self.last_action_delta = (0, 0)
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(18,), dtype=np.float32)

    @classmethod
    def observation_space_description(cls) -> str:
        return "Observation vector: " + ", ".join(cls.OBSERVATION_FEATURES)

    @classmethod
    def action_space_description(cls) -> str:
        return cls.ACTION_SPACE_DESCRIPTION

    @classmethod
    def reward_function_description(cls) -> str:
        return cls.REWARD_DESCRIPTION

    def _current_istio_budget_state(self) -> tuple[float, float, float, int]:
        limiter = self.service.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            if hasattr(self.service, "_refresh_retry_budget_runtime_state"):
                self.service._refresh_retry_budget_runtime_state()
            return (
                float(limiter.utilization),
                float(limiter.concurrency_limit),
                float(limiter.percent),
                int(limiter.min_retry_concurrency),
            )
        return 0.0, 1.0, 20.0, 3

    def _metrics_window_ns(self) -> int:
        return int(getattr(self, "observation_window_ns", self.decision_interval_ns))

    def _delta_window_ns(self) -> int:
        return int(getattr(self, "delta_window_ns", self._metrics_window_ns()))

    def _istio_budget_limiter(self) -> IstioRetryBudget | None:
        limiter = self.service.cfg.load_limiter
        return limiter if isinstance(limiter, IstioRetryBudget) else None

    def _caller_metrics_for_window(self, window_ns: int) -> dict[str, float]:
        window_end = self.sim.timestep
        window_start = max(0, window_end - window_ns)
        return istio_caller_window_metrics(
            clients=self.clients,
            window_start_ns=window_start,
            window_end_ns=window_end,
            attempt_timeout_ms=self._attempt_timeout_ms(),
            istio_budget=self._istio_budget_limiter(),
            fallback_success_rate=self.prev_success_rate,
            fallback_retry_ratio=self.prev_retry_ratio,
        )

    def _caller_window_metrics(self) -> dict[str, float]:
        return self._caller_metrics_for_window(self._metrics_window_ns())

    def _delta_features(self) -> tuple[float, float, dict[str, float]]:
        delta_client_metrics = self._caller_metrics_for_window(self._delta_window_ns())
        delta_success_agg = delta_client_metrics["agg_success"] - self.prev_delta_success_rate
        delta_load_amplification = (
            delta_client_metrics["load_amplification"] - self.prev_delta_window_load_amplification
        )
        return delta_success_agg, delta_load_amplification, delta_client_metrics

    def _queue_fail_rate(self, client_metrics: dict[str, float], window_ns: int) -> float:
        window_end = self.sim.timestep
        window_start = max(0, window_end - window_ns)
        completed_attempts = _attempts_completed_in_window(self.clients, window_start, window_end)
        total_attempts = len(completed_attempts)
        if total_attempts == 0:
            return 0.0
        queue_failures = sum(
            1
            for _, attempt, _, _ in completed_attempts
            if attempt.drop_reason == DropReason.QUEUE_FULL
        )
        return _safe_ratio(queue_failures, total_attempts)

    def _sync_action_memory_from_service(self) -> None:
        limiter = self.service.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            self.current_percent_idx, self.current_min_idx = istio_budget_indices_from_physical(
                limiter.percent,
                limiter.min_retry_concurrency,
                self.percent_map,
                self.min_retry_concurrency_map,
            )
            self.previous_percent = float(limiter.percent)
            self.previous_min_retry_concurrency = int(limiter.min_retry_concurrency)
        else:
            self.current_percent_idx = DEFAULT_PERCENT_IDX
            self.current_min_idx = DEFAULT_MIN_IDX
            self.previous_percent = self.percent_map[self.current_percent_idx]
            self.previous_min_retry_concurrency = self.min_retry_concurrency_map[self.current_min_idx]
        self.prev_window_load_amplification = 1.0
        self.prev_delta_success_rate = 1.0
        self.prev_delta_window_load_amplification = 1.0
        self.prev_reward_success_rate = 1.0
        self.prev_reward_retry_pressure = 0.0
        self.last_action_delta = (0, 0)

    def _build_istio_config_from_params(
        self,
        rng: np.random.Generator,
        *,
        duration_s: int,
        queue_capacity: int,
        latency_median_ms: int,
        attempt_timeout_ms: int,
        percent: float,
        min_retry_concurrency: int,
        partial_failures: list[PartialFailureConfig],
        spike_start: float | None,
        spike_end: float | None,
        spike_multipliers: list[float] | None,
        total_scale_range: tuple[float, float],
        per_client_scale_range: tuple[float, float],
    ):
        service_template = self.base_config.services[0]
        service_timeout = (
            service_template.timeout.model_copy(update={"attempt_ms": attempt_timeout_ms})
            if service_template.timeout is not None
            else None
        )
        budget_template = service_template.istio_retry_budget
        if budget_template is None:
            raise ValueError("IstioRetryBudgetMetastableEnv requires service.istio_retry_budget in YAML.")
        budget_cfg = budget_template.model_copy(
            update={"percent": percent, "min_retry_concurrency": min_retry_concurrency}
        )
        new_service = service_template.model_copy(update={
            "latency": service_template.latency.model_copy(update={"median_ms": latency_median_ms}),
            "queue_capacity": queue_capacity,
            "timeout": service_timeout,
            "global_retry_budget": None,
            "aimd_global_retry_budget": None,
            "istio_retry_budget": budget_cfg,
            "partial_failures": partial_failures,
        })

        total_scale = float(rng.uniform(*total_scale_range))
        new_clients = []
        for idx, client_template in enumerate(self.base_config.clients):
            client_timeout = (
                client_template.timeout.model_copy(update={"attempt_ms": attempt_timeout_ms})
                if client_template.timeout is not None
                else None
            )
            client_base_scale = total_scale * float(rng.uniform(*per_client_scale_range))
            base_rps = max(1.0, client_template.workload.base_rps * client_base_scale)
            load_spikes = []
            if spike_start is not None and spike_end is not None and spike_multipliers is not None:
                multiplier = float(spike_multipliers[min(idx, len(spike_multipliers) - 1)])
                load_spikes.append(LoadSpikeConfig(
                    start_s=spike_start,
                    end_s=spike_end,
                    rps_multiplier=multiplier,
                ))
            new_workload = client_template.workload.model_copy(update={
                "base_rps": base_rps,
                "duration_s": duration_s,
                "load_spikes": load_spikes,
            })
            new_clients.append(client_template.model_copy(update={
                "workload": new_workload,
                "timeout": client_timeout,
            }))

        return self.base_config.model_copy(update={
            "seed": int(rng.integers(0, 2**31)),
            "services": [new_service],
            "clients": new_clients,
            "workload": None,
        })

    def _metastable_config(self, rng: np.random.Generator):
        duration_s = int(rng.choice([70, 80, 90, 100]))
        fault_mode = str(rng.choice(["load_spike", "partial_failure", "compound"]))
        queue_capacity = int(rng.choice([80, 120, 250, 600, 1200]))
        latency_median_ms = int(rng.choice([55, 60, 65]))
        attempt_timeout_ms = int(rng.choice([128, 160, 192, 224]))
        percent = float(rng.choice([10.0, 20.0, 35.0]))
        min_retry_concurrency = int(rng.choice([2, 3, 5]))

        fault_start = float(rng.integers(18, max(19, duration_s - 35)))
        fault_len = float(rng.integers(8, 15))
        fault_end = min(fault_start + fault_len, float(duration_s - 20))
        spike_start = fault_end if fault_mode == "compound" else float(rng.integers(18, max(19, duration_s - 18)))
        spike_len = float(rng.integers(8, 15))
        spike_end = min(spike_start + spike_len, float(duration_s - 5))

        partial_failures = []
        if fault_mode in {"partial_failure", "compound"}:
            partial_failures.append(PartialFailureConfig(
                start_s=fault_start,
                end_s=fault_end,
                p_fail=round(float(rng.uniform(0.35, 0.70)), 2),
            ))
        spike_multipliers = None
        if fault_mode in {"load_spike", "compound"}:
            spike_multipliers = [
                max(1.4, round(float(rng.normal(3.0, 0.25)), 1)),
                max(1.4, round(float(rng.normal(2.6, 0.25)), 1)),
                max(1.4, round(float(rng.normal(2.2, 0.25)), 1)),
            ]

        return self._build_istio_config_from_params(
            rng,
            duration_s=duration_s,
            queue_capacity=queue_capacity,
            latency_median_ms=latency_median_ms,
            attempt_timeout_ms=attempt_timeout_ms,
            percent=percent,
            min_retry_concurrency=min_retry_concurrency,
            partial_failures=partial_failures,
            spike_start=spike_start if spike_multipliers is not None else None,
            spike_end=spike_end if spike_multipliers is not None else None,
            spike_multipliers=spike_multipliers,
            total_scale_range=(0.9, 1.15),
            per_client_scale_range=(0.95, 1.08),
        )

    def _switchback_adversarial_config(self, rng: np.random.Generator):
        duration_s = int(rng.choice([90, 100, 110]))
        queue_capacity = int(rng.choice([60, 80, 120, 250, 600]))
        latency_median_ms = int(rng.choice([60, 65, 70]))
        attempt_timeout_ms = int(rng.choice([96, 128, 144, 160, 192]))
        percent = float(rng.choice([5.0, 10.0, 20.0]))
        min_retry_concurrency = int(rng.choice([1, 2, 3]))

        fault_start = float(rng.integers(16, 24))
        fault_len = float(rng.integers(16, 25))
        fault_end = min(fault_start + fault_len, float(duration_s - 35))
        recovery_gap = float(rng.integers(8, 16))
        spike_start = min(fault_end + recovery_gap, float(duration_s - 24))
        spike_len = float(rng.integers(16, 25))
        spike_end = min(spike_start + spike_len, float(duration_s - 8))

        partial_failures = [
            PartialFailureConfig(
                start_s=fault_start,
                end_s=fault_end,
                p_fail=round(float(rng.uniform(0.55, 0.85)), 2),
            )
        ]
        spike_multipliers = [
            round(float(rng.uniform(3.0, 3.8)), 1),
            round(float(rng.uniform(2.6, 3.3)), 1),
            round(float(rng.uniform(2.2, 2.9)), 1),
        ]

        return self._build_istio_config_from_params(
            rng,
            duration_s=duration_s,
            queue_capacity=queue_capacity,
            latency_median_ms=latency_median_ms,
            attempt_timeout_ms=attempt_timeout_ms,
            percent=percent,
            min_retry_concurrency=min_retry_concurrency,
            partial_failures=partial_failures,
            spike_start=spike_start,
            spike_end=spike_end,
            spike_multipliers=spike_multipliers,
            total_scale_range=(1.0, 1.25),
            per_client_scale_range=(0.98, 1.10),
        )

    def step(self, action):
        _, _, previous_percent, previous_min_retry_concurrency = self._current_istio_budget_state()
        self.previous_percent = previous_percent
        self.previous_min_retry_concurrency = previous_min_retry_concurrency
        previous_percent_idx = self.current_percent_idx
        previous_min_idx = self.current_min_idx

        percent = self.percent_map[int(action[0])]
        min_retry_concurrency = self.min_retry_concurrency_map[int(action[1])]
        self.current_percent_idx = int(action[0])
        self.current_min_idx = int(action[1])
        percent_delta = self.current_percent_idx - previous_percent_idx
        min_delta = self.current_min_idx - previous_min_idx
        action_distance = float(abs(percent_delta) + abs(min_delta))
        last_percent_delta, last_min_delta = self.last_action_delta
        action_reversal = (
            (percent_delta != 0 and last_percent_delta != 0 and percent_delta * last_percent_delta < 0)
            or (min_delta != 0 and last_min_delta != 0 and min_delta * last_min_delta < 0)
        )

        self.service.update_istio_retry_budget(
            percent=percent,
            min_retry_concurrency=min_retry_concurrency,
        )
        self.next_decision_time = min(
            self.next_decision_time + self.decision_interval_ns,
            self.episode_end,
        )
        self.sim.run(until=self.next_decision_time)

        obs = self._get_obs()
        fault_active, recovery_active = self._phase_flags(self.sim.timestep)
        client_metrics = self._caller_window_metrics()
        retry_pressure = float(obs[13])
        delta_success = client_metrics["agg_success"] - self.prev_reward_success_rate
        delta_retry_pressure = retry_pressure - self.prev_reward_retry_pressure

        reward = istio_retry_budget_reward(
            success_rate=client_metrics["agg_success"],
            retry_ratio=client_metrics["retry_ratio"],
            deadline_rate=client_metrics["deadline_rate"],
            queue_fail_rate=self._queue_fail_rate(client_metrics, self._metrics_window_ns()),
            server_fail_rate=client_metrics["server_fail_rate"],
            latency_pressure=client_metrics["p95_latency_pressure"],
            budget_reject_rate=client_metrics["budget_reject_rate"],
            budget_utilization=float(obs[12]),
            retry_pressure_vs_limit=retry_pressure,
            delta_success=delta_success,
            delta_retry_pressure=delta_retry_pressure,
            action_distance=action_distance,
            action_reversal=action_reversal,
        )
        self.prev_reward_success_rate = client_metrics["agg_success"]
        self.prev_reward_retry_pressure = retry_pressure
        self.last_action_delta = (
            percent_delta if percent_delta != 0 else last_percent_delta,
            min_delta if min_delta != 0 else last_min_delta,
        )

        done = self.sim.timestep >= self.episode_end
        if done:
            self.sim.run()

        _, retry_limit, _, _ = self._current_istio_budget_state()
        self.history.append({
            "time_s": self.sim.timestep / 1e9,
            "success_rate": client_metrics["agg_success"],
            "retry_ratio": client_metrics["retry_ratio"],
            "budget_reject_rate": client_metrics["budget_reject_rate"],
            "p50": client_metrics["p50_ms"],
            "p95": client_metrics["p95_ms"],
            "p99": client_metrics["p99_ms"],
            "window_success_agg": client_metrics["agg_success"],
            "window_min_client_success": client_metrics["min_client_success"],
            "window_load_amp": client_metrics["load_amplification"],
            "window_retry_eff": client_metrics["retry_efficiency"],
            "window_fairness_gap": client_metrics["fairness_gap"],
            "fault_active": int(fault_active),
            "recovery_active": int(recovery_active),
            "reward": reward,
            "action_percent": percent,
            "action_min_retry_concurrency": min_retry_concurrency,
            "action_distance": action_distance,
            "action_reversal": int(action_reversal),
            "retry_concurrency_limit": retry_limit,
        })
        return obs, reward, done, False, {"client_metrics": client_metrics}

    def _get_obs(self):
        budget_util, retry_limit, percent, min_retry_concurrency = self._current_istio_budget_state()
        client_metrics = self._caller_window_metrics()
        delta_success_agg, delta_window_load_amplification, delta_client_metrics = self._delta_features()
        obs = build_istio_metastable_observation_vector(
            attempt_timeout_ms=self._attempt_timeout_ms(),
            metrics_window_ns=self._metrics_window_ns(),
            delta_success_agg=delta_success_agg,
            delta_window_load_amplification=delta_window_load_amplification,
            budget_utilization=budget_util,
            retry_concurrency_limit=retry_limit,
            percent=percent,
            min_retry_concurrency=min_retry_concurrency,
            previous_percent=self.previous_percent,
            previous_min_retry_concurrency=self.previous_min_retry_concurrency,
            client_window_metrics=client_metrics,
        )
        self.prev_success_rate = client_metrics["agg_success"]
        self.prev_retry_ratio = client_metrics["retry_ratio"]
        self.prev_window_load_amplification = client_metrics["load_amplification"]
        self.prev_delta_success_rate = delta_client_metrics["agg_success"]
        self.prev_delta_window_load_amplification = delta_client_metrics["load_amplification"]
        return obs
