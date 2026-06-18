import gymnasium as gym
import numpy as np
from gymnasium import spaces

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns
from simulator.config.schema import PartialFailureConfig, LoadSpikeConfig
from simulator.policies.retry_controls import GlobalRetryBudget


def token_bucket_indices_from_physical(
    refill_rate: int,
    bucket_capacity: int,
    refill_rate_map: list,
    bucket_capacity_map: list,
) -> tuple[int, int]:
    """Map physical token-bucket params to nearest discrete action indices."""
    r_arr = np.array(refill_rate_map, dtype=np.float64)
    c_arr = np.array(bucket_capacity_map, dtype=np.float64)
    refill_idx = int(np.argmin(np.abs(r_arr - float(refill_rate))))
    cap_idx = int(np.argmin(np.abs(c_arr - float(bucket_capacity))))
    return refill_idx, cap_idx


def failure_rates_and_fractions(obs_dict: dict, total_requests: int) -> dict[str, float]:
    """Return load-invariant failure rates plus failure-type fractions."""
    total_fail = (
        obs_dict["fail_server"]
        + obs_dict["fail_queue_full"]
        + obs_dict["fail_deadline"]
    )
    if total_fail > 0:
        fail_server_frac = obs_dict["fail_server"] / total_fail
        fail_queue_frac = obs_dict["fail_queue_full"] / total_fail
        fail_deadline_frac = obs_dict["fail_deadline"] / total_fail
    else:
        fail_server_frac = 0.0
        fail_queue_frac = 0.0
        fail_deadline_frac = 0.0

    return {
        "server_fail_rate": obs_dict["fail_server"] / total_requests,
        "queue_fail_rate": obs_dict["fail_queue_full"] / total_requests,
        "deadline_rate": obs_dict["fail_deadline"] / total_requests,
        "fail_server_frac": fail_server_frac,
        "fail_queue_frac": fail_queue_frac,
        "fail_deadline_frac": fail_deadline_frac,
    }


def latency_pressure_from_metrics(obs_dict: dict, attempt_timeout_ms: float) -> float:
    """Return normalized tail-latency pressure relative to the timeout."""
    return max(0.0, obs_dict["p99"] / attempt_timeout_ms - 1.0)


def bucket_fill_ratio(balance: float, bucket_capacity: int) -> float:
    """Return current bucket fill level in [0, 1]."""
    return float(np.clip(balance / max(float(bucket_capacity), 1.0), 0.0, 1.0))


def stabilize_window_observation(
    obs_dict: dict,
    fallback_success_rate: float,
    fallback_retry_ratio: float,
    fallback_queue_util: float | None = None,
    queue_capacity: int | None = None,
) -> dict:
    """Replace empty-window placeholders with the previous live state."""
    if obs_dict.get("has_data", True):
        return obs_dict

    stabilized = dict(obs_dict)
    stabilized["success_rate"] = fallback_success_rate
    stabilized["retry_ratio"] = fallback_retry_ratio
    stabilized["error_rate"] = max(0.0, 1.0 - fallback_success_rate)
    if fallback_queue_util is not None and queue_capacity is not None:
        stabilized["queue_avg"] = fallback_queue_util * queue_capacity
    return stabilized


def action_transition_metrics(
    previous_refill_idx: int,
    previous_capacity_idx: int,
    next_refill_idx: int,
    next_capacity_idx: int,
) -> dict[str, float | bool]:
    """Describe how far and in which direction the controller moved."""
    refill_delta = int(next_refill_idx) - int(previous_refill_idx)
    capacity_delta = int(next_capacity_idx) - int(previous_capacity_idx)
    action_distance = abs(refill_delta) + abs(capacity_delta)
    counteracting_change = refill_delta * capacity_delta < 0
    return {
        "refill_delta": refill_delta,
        "capacity_delta": capacity_delta,
        "action_distance": float(action_distance),
        "counteracting_change": counteracting_change,
    }


def build_observation_vector(
    obs_dict: dict,
    queue_capacity: int,
    attempt_timeout_ms: float,
    decision_interval_ns: float,
    prev_success_rate: float,
    prev_retry_ratio: float,
    prev_queue_util: float,
    bucket_balance: float,
    refill_rate: int,
    bucket_capacity: int,
    current_refill_idx: int,
    current_capacity_idx: int,
) -> np.ndarray:
    """Build an observation that separates burst pressure from steady-state pressure."""
    total_requests = max(1, int(obs_dict["total_requests"]))
    failure_stats = failure_rates_and_fractions(obs_dict, total_requests)
    queue_util = obs_dict["queue_avg"] / queue_capacity
    latency_pressure = latency_pressure_from_metrics(obs_dict, attempt_timeout_ms)
    delta_success = obs_dict["success_rate"] - prev_success_rate
    delta_retry = obs_dict["retry_ratio"] - prev_retry_ratio
    delta_queue_util = queue_util - prev_queue_util
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
        delta_queue_util,
        attempt_rps / effective_refill,
        retry_rps / effective_refill,
        bucket_fill_ratio(bucket_balance, bucket_capacity),
        float(current_refill_idx) / 4.0,
        float(current_capacity_idx) / 4.0,
    ], dtype=np.float32)


def simple_reward(
    success_rate: float,
    retry_ratio: float,
    deadline_rate: float,
    queue_fail_rate: float,
    latency_pressure: float,
    delta_success: float,
    action_distance: float,
    counteracting_change: bool,
) -> float:
    """Reward healthy recovery while charging for unnecessary control movement."""
    return (
        1.50 * success_rate
        - 0.20 * retry_ratio
        - 0.65 * deadline_rate
        - 0.25 * queue_fail_rate
        - 0.04 * min(latency_pressure, 3.0)
        + 0.15 * delta_success
        - 0.02 * action_distance
        - 0.04 * float(counteracting_change)
    )


class RandomScenarioSimEnv(gym.Env):
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
        "delta_queue_utilization",
        "attempt_pressure_vs_refill",
        "retry_pressure_vs_refill",
        "bucket_fill_ratio",
        "refill_idx_norm",
        "capacity_idx_norm",
    ]
    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([5, 5]): action[0] selects refill_rate from [5, 15, 30, 60, 90] rps; "
        "action[1] selects bucket_capacity from [5, 10, 20, 50, 80] tokens."
    )
    REWARD_DESCRIPTION = (
        "reward = 1.50*success_rate - 0.20*retry_ratio - 0.65*deadline_rate "
        "- 0.25*queue_fail_rate - 0.04*min(latency_pressure, 3.0) + 0.15*delta_success "
        "- 0.02*action_distance - 0.04*counteracting_change."
    )

    def __init__(self, yaml_path: str, decision_interval_s: float = 2.0):
        super().__init__()
        self.base_config = ConfigLoader.load_from_file(yaml_path)
        self.decision_interval_ns = s_to_ns(decision_interval_s)

        # Action Space
        # [refill_rate_index, bucket_capacity_index]
        # refill_rate_index: 0=5, 1=15, 2=30, 3=60, 4=120
        # bucket_capacity_index: 0=5, 1=10, 2=20, 3=50, 4=100
        self.action_space = spaces.MultiDiscrete([5, 5])
        self.refill_rate_map = [5, 15, 30, 60, 90]
        self.bucket_capacity_map = [5, 10, 20, 50, 80]
        self.current_refill_idx = 4
        self.current_capacity_idx = 4

        # Observation Space: health, pressure, trends, budget state, and action memory.
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(15,), dtype=np.float32
        )

        self.sim = None
        self.service = None
        self.client = None
        self.queue_capacity = self.base_config.services[0].queue_capacity or 1
        self.next_decision_time = 0
        self.history = []

        self.prev_success_rate = 1.0
        self.prev_retry_ratio = 0.0
        self.prev_queue_util = 0.0

    @classmethod
    def observation_space_description(cls) -> str:
        return "Observation vector: " + ", ".join(cls.OBSERVATION_FEATURES)

    @classmethod
    def action_space_description(cls) -> str:
        return cls.ACTION_SPACE_DESCRIPTION

    @classmethod
    def reward_function_description(cls) -> str:
        return cls.REWARD_DESCRIPTION

    def _sync_action_memory_from_service(self) -> None:
        """Set current_refill_idx / current_capacity_idx from the live token bucket (YAML initial)."""
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

    def _normalized_action_features(self) -> np.ndarray:
        """Last applied discrete actions in [0, 1] (5 choices each → divide by 4)."""
        return np.array(
            [
                float(self.current_refill_idx) / 4.0,
                float(self.current_capacity_idx) / 4.0,
            ],
            dtype=np.float32,
        )

    def _current_token_bucket_state(self) -> tuple[float, int, int]:
        """Return live token bucket balance, capacity, and refill rate."""
        limiter = self.service.cfg.load_limiter
        if isinstance(limiter, GlobalRetryBudget):
            return float(limiter.balance), int(limiter.max_tokens), int(limiter.refill_rate)
        return 0.0, 1, 1

    def _randomize_config(self, rng: np.random.Generator):

        """Randomize the configuration"""

        duration_s = int(rng.choice([120, 180, 240, 300]))

        num_faults = int(rng.integers(0, 4))
        partial_failures = []

        for _ in range(num_faults):
            start = float(rng.integers(10, duration_s - 20))
            length = float(rng.integers(5, 40))
            end = min(start + length, float(duration_s - 1))
            partial_failures.append(PartialFailureConfig(
                start_s=start,
                end_s=end,
                p_fail=round(float(rng.uniform(0.1, 0.9)), 2),
            ))

        num_spikes = int(rng.integers(0, 3))
        load_spikes = []

        for _ in range(num_spikes):
            start = float(rng.integers(5, duration_s - 20))
            length = float(rng.integers(10, 50))
            end = min(start + length, float(duration_s - 1))
            load_spikes.append(LoadSpikeConfig(
                start_s=start,
                end_s=end,
                rps_multiplier=round(float(rng.uniform(1.2, 3.0)), 1),
            ))

        base_rps = float(rng.integers(150, 500))

        new_service = self.base_config.services[0].model_copy(update={
            "partial_failures": partial_failures,
        })

        new_workload = self.base_config.workload.model_copy(update={
            "base_rps": base_rps,
            "duration_s": duration_s,
            "load_spikes": load_spikes,
        })

        return self.base_config.model_copy(update={
            "seed": int(rng.integers(0, 2**31)),
            "services": [new_service],
            "workload": new_workload,
        })

    def reset(self, seed=None, options=None):

        """Reset the environment"""

        super().reset(seed=seed)
        
        rng = np.random.default_rng(seed)
        config = self._randomize_config(rng)

        self.sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
        self.client = clients[0]
        self.service = list(services.values())[0] if isinstance(services, dict) else services[config.services[0].name]
        self.service.enable_live_buffer()
        self.queue_capacity = self.service.cfg.queue_capacity or 1
        workload = workloads[0]
        self.episode_end = s_to_ns(workload.duration_s)

        workload.drive(self.sim, lambda s, c=self.client: c.start_request(s))

        self.next_decision_time = 0
        self.history = []

        self.prev_success_rate = 1.0
        self.prev_retry_ratio = 0.0
        self.prev_queue_util = 0.0

        self._sync_action_memory_from_service()

        obs = self._get_obs()
        return obs, {}

    def step(self, action):

        """Take a step in the environment"""

        previous_refill_idx = self.current_refill_idx
        previous_capacity_idx = self.current_capacity_idx

        # Decode action
        refill_rate = self.refill_rate_map[action[0]]
        bucket_capacity = self.bucket_capacity_map[action[1]]

        # Update current action indices
        self.current_refill_idx = action[0]
        self.current_capacity_idx = action[1]

        # Apply action
        self.service.update_token_bucket(
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
        )

        # Advance simulation
        self.next_decision_time = min(
            self.next_decision_time + self.decision_interval_ns,
            self.episode_end,
        )
        self.sim.run(until=self.next_decision_time)

        # Observe
        obs = self._get_obs()

        transition = action_transition_metrics(
            previous_refill_idx=previous_refill_idx,
            previous_capacity_idx=previous_capacity_idx,
            next_refill_idx=int(action[0]),
            next_capacity_idx=int(action[1]),
        )

        obs_dict = stabilize_window_observation(
            self.service.live_buffer.get_observation(
                self.sim.timestep, self.decision_interval_ns
            ),
            fallback_success_rate=self.prev_success_rate,
            fallback_retry_ratio=self.prev_retry_ratio,
            fallback_queue_util=self.prev_queue_util,
            queue_capacity=self.queue_capacity,
        )

        sr = obs_dict["success_rate"]
        retry_ratio = obs_dict["retry_ratio"]
        delta_success = float(obs[7])

        reward = simple_reward(
            success_rate=sr,
            retry_ratio=retry_ratio,
            deadline_rate=obs_dict["fail_deadline"] / max(1, int(obs_dict["total_requests"])),
            queue_fail_rate=obs_dict["fail_queue_full"] / max(1, int(obs_dict["total_requests"])),
            latency_pressure=latency_pressure_from_metrics(obs_dict, self._attempt_timeout_ms()),
            delta_success=delta_success,
            action_distance=float(transition["action_distance"]),
            counteracting_change=bool(transition["counteracting_change"]),
        )

        # Done?
        done = self.sim.timestep >= self.episode_end
        if done:
            self.sim.run()  # drain remaining

        self.history.append({
            "time_s": self.sim.timestep / 1e9,
            "success_rate": obs_dict["success_rate"],
            "error_rate": obs_dict["error_rate"],
            "retry_ratio": obs_dict["retry_ratio"],
            "p50": obs_dict["p50"],
            "p99": obs_dict["p99"],
            "queue_avg": obs_dict["queue_avg"],
            "fail_server": obs_dict["fail_server"],
            "fail_deadline": obs_dict["fail_deadline"],
            "fail_queue_full": obs_dict["fail_queue_full"],
            "reward": reward,
            "action_distance": transition["action_distance"],
            "counteracting_change": int(bool(transition["counteracting_change"])),
            "action_refill_rate": refill_rate,
            "action_bucket_capacity": bucket_capacity,
            "bucket_fill_ratio": bucket_fill_ratio(*self._current_token_bucket_state()[:2]),
        })

        return obs, reward, done, False, {"metrics": obs_dict}

    def _get_obs(self):
        """Get the observation vector used by the RL agent."""

        obs_dict = self.service.live_buffer.get_observation(
            self.sim.timestep, self.decision_interval_ns
        )
        obs_dict = stabilize_window_observation(
            obs_dict,
            fallback_success_rate=self.prev_success_rate,
            fallback_retry_ratio=self.prev_retry_ratio,
            fallback_queue_util=self.prev_queue_util,
            queue_capacity=self.queue_capacity,
        )
        bucket_balance, bucket_capacity, refill_rate = self._current_token_bucket_state()
        queue_util = obs_dict["queue_avg"] / self.queue_capacity

        obs = build_observation_vector(
            obs_dict=obs_dict,
            queue_capacity=self.queue_capacity,
            attempt_timeout_ms=self._attempt_timeout_ms(),
            decision_interval_ns=self.decision_interval_ns,
            prev_success_rate=self.prev_success_rate,
            prev_retry_ratio=self.prev_retry_ratio,
            prev_queue_util=self.prev_queue_util,
            bucket_balance=bucket_balance,
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
            current_refill_idx=self.current_refill_idx,
            current_capacity_idx=self.current_capacity_idx,
        )
        self.prev_success_rate = obs_dict["success_rate"]
        self.prev_retry_ratio = obs_dict["retry_ratio"]
        self.prev_queue_util = queue_util
        return obs

    def _attempt_timeout_ms(self) -> float:
        timeout_cfg = self.base_config.services[0].timeout
        if timeout_cfg and timeout_cfg.attempt_ms:
            return float(timeout_cfg.attempt_ms)
        return 50.0