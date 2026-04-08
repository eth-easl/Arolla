import gymnasium as gym
import numpy as np
from gymnasium import spaces

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns
from simulator.config.schema import PartialFailureConfig, LoadSpikeConfig
from simulator.policies.server_retry_budget import GlobalRetryBudget


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


class RandomScenarioSimEnv(gym.Env):

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
        self.prev_action = np.array([4, 4]) 

        # Observation Space: 13 system metrics + 3 deltas + 2 (current action, normalized)
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(18,), dtype=np.float32
        )

        self.sim = None
        self.service = None
        self.client = None
        self.next_decision_time = 0
        self.history = []

        self.prev_success_rate = 1.0
        self.prev_error_rate = 0.0
        self.prev_retry_ratio = 0.0
        self.prev_sr_for_reward = 1.0

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
        workload = workloads[0]
        self.episode_end = s_to_ns(workload.duration_s)

        workload.drive(self.sim, lambda s, c=self.client: c.start_request(s))

        self.next_decision_time = self.decision_interval_ns
        self.sim.run(until=self.next_decision_time)
        self.history = []

        self.prev_success_rate = 1.0
        self.prev_error_rate = 0.0
        self.prev_retry_ratio = 0.0
        self.prev_sr_for_reward = 1.0

        self._sync_action_memory_from_service()
        self.prev_action = np.array(
            [self.current_refill_idx, self.current_capacity_idx], dtype=np.int64
        )

        obs = self._get_obs()
        return obs, {}

    def step(self, action):

        """Take a step in the environment"""

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
        self.next_decision_time += self.decision_interval_ns
        self.sim.run(until=min(self.next_decision_time, self.episode_end))

        # Observe
        obs = self._get_obs()

        # Reward
        obs_dict = self.service.live_buffer.get_observation(
            self.sim.timestep, self.decision_interval_ns
        )

        sr = obs_dict["success_rate"]
        retry_ratio = obs_dict["retry_ratio"]
        delta_sr = sr - self.prev_sr_for_reward
        self.prev_sr_for_reward = sr

        action_changed = (action[0] != self.prev_action[0]) or (action[1] != self.prev_action[1])
        self.prev_action = action.copy()

        reward = sr - 0.3 * retry_ratio + 0.5 * delta_sr - 0.01 * float(action_changed)

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
            "action_refill_rate": refill_rate,
            "action_bucket_capacity": bucket_capacity,
        })

        return obs, reward, done, False, {"metrics": obs_dict}

    def _get_obs(self):
        """Get the observation: 13 system metrics + 3 delta trends + 2 action features."""

        obs_dict = self.service.live_buffer.get_observation(
            self.sim.timestep, self.decision_interval_ns
        )

        delta_success = obs_dict["success_rate"] - self.prev_success_rate
        delta_error = obs_dict["error_rate"] - self.prev_error_rate
        delta_retry = obs_dict["retry_ratio"] - self.prev_retry_ratio

        self.prev_success_rate = obs_dict["success_rate"]
        self.prev_error_rate = obs_dict["error_rate"]
        self.prev_retry_ratio = obs_dict["retry_ratio"]

        return np.array([
            obs_dict["success_rate"],
            obs_dict["error_rate"],
            obs_dict["retry_ratio"],
            obs_dict["p50"],
            obs_dict["p99"],
            obs_dict["queue_avg"],
            obs_dict["total_requests"],
            obs_dict["success"],
            obs_dict["failure"],
            obs_dict["retries"],
            obs_dict["fail_queue_full"],
            obs_dict["fail_deadline"],
            obs_dict["fail_server"],
            delta_success,
            delta_error,
            delta_retry,
            *self._normalized_action_features(),
        ], dtype=np.float32)