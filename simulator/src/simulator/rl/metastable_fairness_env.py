import gymnasium as gym
import numpy as np
from gymnasium import spaces

from simulator.config.loader import ConfigLoader
from simulator.config.schema import LoadSpikeConfig, PartialFailureConfig
from simulator.core.models import RootRequest
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.rl.random_scenario_env import (
    action_transition_metrics,
    bucket_fill_ratio,
    failure_rates_and_fractions,
    latency_pressure_from_metrics,
    stabilize_window_observation,
    token_bucket_indices_from_physical,
)
from simulator.runtime.workload import Workload
from simulator.utils.time import s_to_ns


def _root_begin_ns(root: RootRequest) -> int:
    return min(attempt.interval.begin for attempt in root.attempts)


def _root_end_ns(root: RootRequest) -> int:
    return max(attempt.interval.end for attempt in root.attempts)


def _root_succeeded(root: RootRequest) -> bool:
    return any(attempt.success for attempt in root.attempts)


def _root_success_latency_ms(root: RootRequest) -> float | None:
    if not _root_succeeded(root):
        return None
    return float((_root_end_ns(root) - _root_begin_ns(root)) / 1e6)


def _safe_ratio(num: float, den: float, default: float = 0.0) -> float:
    return float(num / den) if den > 0 else default


def metastable_client_window_metrics(
    clients: list,
    workloads: list[Workload],
    window_start_ns: int,
    window_end_ns: int,
    now_ns: int,
    fallback_success_rate: float,
    fallback_retry_ratio: float,
) -> dict[str, float]:
    """Compute per-window multi-client metrics that mirror benchmark goals."""
    completed_roots: list[RootRequest] = []
    retry_counts_by_client: dict[str, int] = {}
    client_success_rates: dict[str, float] = {}
    successful_retries = 0
    total_retries = 0
    total_attempts = 0
    latencies_ms: list[float] = []

    for client in clients:
        roots_in_window = [
            root for root in client.roots
            if root.attempts and window_start_ns <= _root_end_ns(root) <= window_end_ns
        ]
        completed_roots.extend(roots_in_window)

        if roots_in_window:
            client_success_rates[client.cfg.name] = float(
                sum(1 for root in roots_in_window if _root_succeeded(root)) / len(roots_in_window)
            )
        else:
            client_success_rates[client.cfg.name] = fallback_success_rate

        retries_for_client = sum(max(0, root.attempt_count() - 1) for root in roots_in_window)
        retry_counts_by_client[client.cfg.name] = retries_for_client
        total_retries += retries_for_client
        total_attempts += sum(root.attempt_count() for root in roots_in_window)

        for root in roots_in_window:
            lat_ms = _root_success_latency_ms(root)
            if lat_ms is not None:
                latencies_ms.append(lat_ms)
            for attempt in root.attempts[1:]:
                if attempt.success:
                    successful_retries += 1

    total_roots = len(completed_roots)
    agg_success = (
        float(sum(1 for root in completed_roots if _root_succeeded(root)) / total_roots)
        if total_roots > 0
        else fallback_success_rate
    )
    min_client_success = min(client_success_rates.values()) if client_success_rates else fallback_success_rate
    load_amp = _safe_ratio(total_attempts, total_roots, default=1.0 + fallback_retry_ratio)
    retry_eff = _safe_ratio(successful_retries, total_retries, default=0.0)

    offered_loads = [workload.rps_at(now_ns) for workload in workloads]
    total_offered = sum(offered_loads)
    offered_shares = {
        client.cfg.name: _safe_ratio(rps, total_offered, default=0.0)
        for client, rps in zip(clients, offered_loads)
    }
    retry_shares = {
        name: _safe_ratio(count, total_retries, default=0.0)
        for name, count in retry_counts_by_client.items()
    }
    fairness_gap = 0.5 * sum(
        abs(retry_shares.get(name, 0.0) - offered_shares.get(name, 0.0))
        for name in offered_shares
    )

    if latencies_ms:
        p50 = float(np.percentile(latencies_ms, 50))
        p95 = float(np.percentile(latencies_ms, 95))
        p99 = float(np.percentile(latencies_ms, 99))
    else:
        p50 = p95 = p99 = 0.0

    return {
        "agg_success": agg_success,
        "min_client_success": min_client_success,
        "load_amplification": load_amp,
        "retry_efficiency": retry_eff,
        "fairness_gap": float(np.clip(fairness_gap, 0.0, 1.0)),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
    }


def build_metastable_observation_vector(
    obs_dict: dict,
    queue_capacity: int,
    attempt_timeout_ms: float,
    decision_interval_ns: float,
    prev_success_rate: float,
    prev_retry_ratio: float,
    bucket_balance: float,
    refill_rate: int,
    bucket_capacity: int,
    current_refill_idx: int,
    current_capacity_idx: int,
    fault_active: bool,
    recovery_active: bool,
    client_window_metrics: dict[str, float],
) -> np.ndarray:
    """Observation tuned for metastability and multi-client fairness."""
    total_requests = max(1, int(obs_dict["total_requests"]))
    failure_stats = failure_rates_and_fractions(obs_dict, total_requests)
    queue_util = obs_dict["queue_avg"] / queue_capacity
    latency_pressure = latency_pressure_from_metrics(obs_dict, attempt_timeout_ms)
    delta_success = obs_dict["success_rate"] - prev_success_rate
    delta_retry = obs_dict["retry_ratio"] - prev_retry_ratio
    window_s = max(float(decision_interval_ns) / 1e9, 1e-9)
    attempt_rps = total_requests / window_s
    retry_rps = float(obs_dict["retries"]) / window_s
    effective_refill = max(float(refill_rate), 1.0)

    return np.array([
        obs_dict["success_rate"],
        obs_dict["retry_ratio"],
        latency_pressure,
        queue_util,
        failure_stats["server_fail_rate"],
        failure_stats["queue_fail_rate"],
        failure_stats["deadline_rate"],
        delta_success,
        delta_retry,
        client_window_metrics["agg_success"],
        client_window_metrics["min_client_success"],
        client_window_metrics["load_amplification"],
        client_window_metrics["retry_efficiency"],
        client_window_metrics["fairness_gap"],
        bucket_fill_ratio(bucket_balance, bucket_capacity),
        attempt_rps / effective_refill,
        retry_rps / effective_refill,
        float(fault_active),
        float(recovery_active),
        float(current_refill_idx) / 4.0,
        float(current_capacity_idx) / 4.0,
    ], dtype=np.float32)


def metastable_reward(
    success_rate: float,
    retry_ratio: float,
    deadline_rate: float,
    queue_fail_rate: float,
    latency_pressure: float,
    delta_success: float,
    action_distance: float,
    counteracting_change: bool,
    fault_active: bool,
    recovery_active: bool,
    client_window_metrics: dict[str, float],
) -> float:
    """Reward aligned with benchmark goals for metastable fairness scenarios."""
    amp_excess = max(0.0, client_window_metrics["load_amplification"] - 1.0)
    tail_penalty = min(latency_pressure, 3.0)
    positive_recovery_progress = max(delta_success, 0.0)
    reward = (
        1.35 * client_window_metrics["agg_success"]
        + 0.85 * client_window_metrics["min_client_success"]
        + 0.40 * client_window_metrics["retry_efficiency"]
        - 0.42 * amp_excess
        - 0.30 * client_window_metrics["fairness_gap"]
        - 0.30 * deadline_rate
        - 0.12 * queue_fail_rate
        - 0.14 * tail_penalty
        - 0.03 * action_distance
        - 0.04 * float(counteracting_change)
    )

    if fault_active:
        reward += (
            0.70 * client_window_metrics["agg_success"]
            + 0.45 * client_window_metrics["min_client_success"]
            + 0.20 * client_window_metrics["retry_efficiency"]
            - 0.22 * amp_excess
            - 0.08 * tail_penalty
        )

    if recovery_active:
        reward += (
            0.75 * positive_recovery_progress
            + 0.25 * success_rate
            + 0.30 * client_window_metrics["agg_success"]
            - 0.10 * amp_excess
        )
    else:
        reward += 0.10 * delta_success

    return reward


class MetastableFairnessSimEnv(gym.Env):
    SUPPORTED_SCENARIO_PROFILES = (
        "metastable_fairness",
        "switchback_adversarial",
    )
    OBSERVATION_FEATURES = [
        "success_rate",
        "retry_ratio",
        "latency_pressure",
        "queue_utilization",
        "server_fail_rate",
        "queue_fail_rate",
        "deadline_rate",
        "delta_success",
        "delta_retry_ratio",
        "window_success_agg",
        "window_min_client_success",
        "window_load_amplification",
        "window_retry_efficiency",
        "window_fairness_gap",
        "bucket_fill_ratio",
        "attempt_pressure_vs_refill",
        "retry_pressure_vs_refill",
        "fault_active",
        "recovery_active",
        "refill_idx_norm",
        "capacity_idx_norm",
    ]
    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([5, 5]): action[0] selects refill_rate from [5, 15, 30, 60, 90] rps; "
        "action[1] selects bucket_capacity from [5, 10, 20, 50, 80] tokens."
    )
    REWARD_DESCRIPTION = (
        "Reward emphasizing fault-window client success, useful retries, low load "
        "amplification, fair retry share, recovery progress, and low tail pressure."
    )

    def __init__(
        self,
        yaml_path: str,
        decision_interval_s: float = 2.0,
        randomize_scenarios: bool = True,
        scenario_profile: str = "metastable_fairness",
    ):
        super().__init__()
        self.base_config = ConfigLoader.load_from_file(yaml_path)
        if not self.base_config.clients:
            raise ValueError("MetastableFairnessSimEnv requires a multi-client config.")
        if scenario_profile not in self.SUPPORTED_SCENARIO_PROFILES:
            raise ValueError(
                f"Unknown scenario_profile={scenario_profile!r}; "
                f"expected one of {self.SUPPORTED_SCENARIO_PROFILES}"
            )
        self.randomize_scenarios = randomize_scenarios
        self.scenario_profile = scenario_profile
        self.decision_interval_ns = s_to_ns(decision_interval_s)

        self.action_space = spaces.MultiDiscrete([5, 5])
        self.refill_rate_map = [5, 15, 30, 60, 90]
        self.bucket_capacity_map = [5, 10, 20, 50, 80]
        self.current_refill_idx = 4
        self.current_capacity_idx = 4
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(21,), dtype=np.float32)

        self.sim = None
        self.service = None
        self.clients = []
        self.workloads = []
        self.queue_capacity = self.base_config.services[0].queue_capacity or 1
        self.next_decision_time = 0
        self.history = []
        self.fault_windows: list[tuple[str, float, float]] = []

        self.prev_success_rate = 1.0
        self.prev_retry_ratio = 0.0

    @classmethod
    def observation_space_description(cls) -> str:
        return "Observation vector: " + ", ".join(cls.OBSERVATION_FEATURES)

    @classmethod
    def action_space_description(cls) -> str:
        return cls.ACTION_SPACE_DESCRIPTION

    @classmethod
    def reward_function_description(cls) -> str:
        return cls.REWARD_DESCRIPTION

    def _current_token_bucket_state(self) -> tuple[float, int, int]:
        limiter = self.service.cfg.load_limiter
        if isinstance(limiter, GlobalRetryBudget):
            return float(limiter.balance), int(limiter.max_tokens), int(limiter.refill_rate)
        return 0.0, 1, 1

    def _sync_action_memory_from_service(self) -> None:
        limiter = self.service.cfg.load_limiter
        if isinstance(limiter, GlobalRetryBudget):
            self.current_refill_idx, self.current_capacity_idx = token_bucket_indices_from_physical(
                int(limiter.refill_rate),
                int(limiter.max_tokens),
                self.refill_rate_map,
                self.bucket_capacity_map,
            )
        else:
            self.current_refill_idx = 0
            self.current_capacity_idx = 0

    def _build_config_from_params(
        self,
        rng: np.random.Generator,
        *,
        duration_s: int,
        queue_capacity: int,
        latency_median_ms: int,
        attempt_timeout_ms: int,
        target_rps: int,
        max_burst: int,
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
        budget_cfg = (
            service_template.global_retry_budget.model_copy(
                update={"target_rps": target_rps, "max_burst": max_burst}
            )
            if service_template.global_retry_budget is not None
            else None
        )
        new_service = service_template.model_copy(update={
            "latency": service_template.latency.model_copy(update={"median_ms": latency_median_ms}),
            "queue_capacity": queue_capacity,
            "timeout": service_timeout,
            "global_retry_budget": budget_cfg,
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
        queue_capacity = int(rng.choice([1000, 1500, 3000, 10000]))
        latency_median_ms = int(rng.choice([55, 60, 65]))
        attempt_timeout_ms = int(rng.choice([160, 192, 224]))
        target_rps = int(rng.choice([25, 30, 35, 40]))
        max_burst = int(rng.choice([10, 25, 40, 60]))

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
                p_fail=round(float(rng.uniform(0.35, 0.65)), 2),
            ))
        spike_multipliers = None
        if fault_mode in {"load_spike", "compound"}:
            spike_multipliers = [
                max(1.4, round(float(rng.normal(3.0, 0.2)), 1)),
                max(1.4, round(float(rng.normal(2.6, 0.2)), 1)),
                max(1.4, round(float(rng.normal(2.2, 0.2)), 1)),
            ]

        return self._build_config_from_params(
            rng,
            duration_s=duration_s,
            queue_capacity=queue_capacity,
            latency_median_ms=latency_median_ms,
            attempt_timeout_ms=attempt_timeout_ms,
            target_rps=target_rps,
            max_burst=max_burst,
            partial_failures=partial_failures,
            spike_start=spike_start if spike_multipliers is not None else None,
            spike_end=spike_end if spike_multipliers is not None else None,
            spike_multipliers=spike_multipliers,
            total_scale_range=(0.9, 1.1),
            per_client_scale_range=(0.95, 1.05),
        )

    def _switchback_adversarial_config(self, rng: np.random.Generator):
        """Harder non-stationary regime where static must compromise across phases.

        The sampled episode always contains:
        1. an early partial-failure window that wants a *tight* budget
        2. a short recovery gap
        3. a later load spike that wants a *looser* budget

        This profile intentionally biases training toward scenarios where a
        single fixed token-bucket setting is not enough.
        """
        duration_s = int(rng.choice([90, 100, 110]))
        queue_capacity = int(rng.choice([400, 600, 800, 1000, 1500]))
        latency_median_ms = int(rng.choice([60, 65, 70]))
        attempt_timeout_ms = int(rng.choice([128, 144, 160, 192]))
        target_rps = int(rng.choice([15, 25, 30]))
        max_burst = int(rng.choice([10, 20, 50]))

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
                p_fail=round(float(rng.uniform(0.55, 0.80)), 2),
            )
        ]
        spike_multipliers = [
            round(float(rng.uniform(3.0, 3.8)), 1),
            round(float(rng.uniform(2.6, 3.3)), 1),
            round(float(rng.uniform(2.2, 2.9)), 1),
        ]

        return self._build_config_from_params(
            rng,
            duration_s=duration_s,
            queue_capacity=queue_capacity,
            latency_median_ms=latency_median_ms,
            attempt_timeout_ms=attempt_timeout_ms,
            target_rps=target_rps,
            max_burst=max_burst,
            partial_failures=partial_failures,
            spike_start=spike_start,
            spike_end=spike_end,
            spike_multipliers=spike_multipliers,
            total_scale_range=(1.0, 1.2),
            per_client_scale_range=(0.98, 1.08),
        )

    def _config_for_reset(self, seed):
        if not self.randomize_scenarios:
            return self.base_config.model_copy(update={"seed": int(seed) if seed is not None else self.base_config.seed})
        rng = np.random.default_rng(seed)
        if self.scenario_profile == "metastable_fairness":
            return self._metastable_config(rng)
        if self.scenario_profile == "switchback_adversarial":
            return self._switchback_adversarial_config(rng)
        raise ValueError(f"Unsupported scenario_profile={self.scenario_profile!r}")

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        config = self._config_for_reset(seed)

        self.sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
        self.clients = clients
        self.workloads = workloads
        self.service = services[config.services[0].name]
        self.service.enable_live_buffer()
        self.queue_capacity = self.service.cfg.queue_capacity or 1

        for workload, client in zip(self.workloads, self.clients):
            workload.drive(self.sim, lambda s, c=client: c.start_request(s))

        self.episode_end = max(s_to_ns(workload.duration_s) for workload in self.workloads)
        self.fault_windows = self._collect_fault_windows(config)

        self.next_decision_time = 0
        self.history = []

        self.prev_success_rate = 1.0
        self.prev_retry_ratio = 0.0
        self._sync_action_memory_from_service()

        obs = self._get_obs()
        return obs, {}

    def step(self, action):
        previous_refill_idx = self.current_refill_idx
        previous_capacity_idx = self.current_capacity_idx

        refill_rate = self.refill_rate_map[int(action[0])]
        bucket_capacity = self.bucket_capacity_map[int(action[1])]
        self.current_refill_idx = int(action[0])
        self.current_capacity_idx = int(action[1])

        self.service.update_token_bucket(refill_rate=refill_rate, bucket_capacity=bucket_capacity)
        self.next_decision_time = min(
            self.next_decision_time + self.decision_interval_ns,
            self.episode_end,
        )
        self.sim.run(until=self.next_decision_time)

        obs = self._get_obs()
        transition = action_transition_metrics(
            previous_refill_idx=previous_refill_idx,
            previous_capacity_idx=previous_capacity_idx,
            next_refill_idx=self.current_refill_idx,
            next_capacity_idx=self.current_capacity_idx,
        )
        obs_dict = stabilize_window_observation(
            self.service.live_buffer.get_observation(self.sim.timestep, self.decision_interval_ns),
            fallback_success_rate=self.prev_success_rate,
            fallback_retry_ratio=self.prev_retry_ratio,
        )
        fault_active, recovery_active = self._phase_flags(self.sim.timestep)
        client_metrics = self._client_window_metrics(obs_dict)

        sr = obs_dict["success_rate"]
        delta_success = float(obs[7])
        reward = metastable_reward(
            success_rate=sr,
            retry_ratio=obs_dict["retry_ratio"],
            deadline_rate=obs_dict["fail_deadline"] / max(1, int(obs_dict["total_requests"])),
            queue_fail_rate=obs_dict["fail_queue_full"] / max(1, int(obs_dict["total_requests"])),
            latency_pressure=latency_pressure_from_metrics(obs_dict, self._attempt_timeout_ms()),
            delta_success=delta_success,
            action_distance=float(transition["action_distance"]),
            counteracting_change=bool(transition["counteracting_change"]),
            fault_active=fault_active,
            recovery_active=recovery_active,
            client_window_metrics=client_metrics,
        )

        done = self.sim.timestep >= self.episode_end
        if done:
            self.sim.run()

        self.history.append({
            "time_s": self.sim.timestep / 1e9,
            "success_rate": obs_dict["success_rate"],
            "retry_ratio": obs_dict["retry_ratio"],
            "queue_avg": obs_dict["queue_avg"],
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
            "action_refill_rate": refill_rate,
            "action_bucket_capacity": bucket_capacity,
        })
        return obs, reward, done, False, {"metrics": obs_dict, "client_metrics": client_metrics}

    def _get_obs(self):
        obs_dict = stabilize_window_observation(
            self.service.live_buffer.get_observation(self.sim.timestep, self.decision_interval_ns),
            fallback_success_rate=self.prev_success_rate,
            fallback_retry_ratio=self.prev_retry_ratio,
        )
        bucket_balance, bucket_capacity, refill_rate = self._current_token_bucket_state()
        fault_active, recovery_active = self._phase_flags(self.sim.timestep)
        client_metrics = self._client_window_metrics(obs_dict)
        obs = build_metastable_observation_vector(
            obs_dict=obs_dict,
            queue_capacity=self.queue_capacity,
            attempt_timeout_ms=self._attempt_timeout_ms(),
            decision_interval_ns=self.decision_interval_ns,
            prev_success_rate=self.prev_success_rate,
            prev_retry_ratio=self.prev_retry_ratio,
            bucket_balance=bucket_balance,
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
            current_refill_idx=self.current_refill_idx,
            current_capacity_idx=self.current_capacity_idx,
            fault_active=fault_active,
            recovery_active=recovery_active,
            client_window_metrics=client_metrics,
        )
        self.prev_success_rate = obs_dict["success_rate"]
        self.prev_retry_ratio = obs_dict["retry_ratio"]
        return obs

    def _client_window_metrics(self, obs_dict: dict) -> dict[str, float]:
        window_end = self.sim.timestep
        window_start = max(0, window_end - self.decision_interval_ns)
        return metastable_client_window_metrics(
            clients=self.clients,
            workloads=self.workloads,
            window_start_ns=window_start,
            window_end_ns=window_end,
            now_ns=self.sim.timestep,
            fallback_success_rate=obs_dict["success_rate"],
            fallback_retry_ratio=obs_dict["retry_ratio"],
        )

    def _phase_flags(self, now_ns: int) -> tuple[bool, bool]:
        now_s = now_ns / 1e9
        active = any(start_s <= now_s <= end_s for _, start_s, end_s in self.fault_windows)
        if active or not self.fault_windows:
            return active, False
        past_ends = [end_s for _, _, end_s in self.fault_windows if end_s <= now_s]
        if not past_ends:
            return False, False
        recent_end = max(past_ends)
        recovery_active = recent_end < now_s <= recent_end + 12.0
        return False, recovery_active

    def _attempt_timeout_ms(self) -> float:
        timeout_cfg = self.service.cfg.timeout
        if timeout_cfg and timeout_cfg.get_attempt_timeout():
            return float(timeout_cfg.get_attempt_timeout() / 1e6)
        service_yaml_timeout = self.base_config.services[0].timeout
        if service_yaml_timeout and service_yaml_timeout.attempt_ms:
            return float(service_yaml_timeout.attempt_ms)
        return 192.0

    @staticmethod
    def _collect_fault_windows(config) -> list[tuple[str, float, float]]:
        windows = []
        for svc in config.services:
            for pf in svc.partial_failures:
                windows.append(("Partial Failure", pf.start_s, pf.end_s))
        for client in config.clients:
            for spike in client.workload.load_spikes:
                windows.append(("Load Spike", spike.start_s, spike.end_s))
        return windows
