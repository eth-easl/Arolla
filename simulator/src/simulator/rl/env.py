import gymnasium as gym
import numpy as np
from gymnasium import spaces

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns, ms_to_ns


class RetrySimEnv(gym.Env):

    def __init__(self, yaml_path: str, decision_interval_s: float = 2.0):
        super().__init__()
        self.yaml_path = yaml_path
        self.decision_interval_ns = s_to_ns(decision_interval_s)

        # Action Space
        # [refill_rate_index, bucket_capacity_index]
        # refill_rate_index: 0=5, 1=15, 2=30, 3=60, 4=120
        # bucket_capacity_index: 0=5, 1=10, 2=20, 3=50, 4=100
        self.action_space = spaces.MultiDiscrete([5, 5])
        self.refill_rate_map = [5, 15, 30, 60, 120]
        self.bucket_capacity_map = [5, 10, 20, 50, 100]

        # Observation Space
        self.observation_space = spaces.Box(
            low=0.0, high=np.inf, shape=(13,), dtype=np.float32
        )

        self.sim = None
        self.service = None
        self.client = None
        self.next_decision_time = 0
        self.history = []

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        config = ConfigLoader.load_from_file(self.yaml_path)
        if seed is not None:
            config = config.model_copy(update={"seed": seed})

        self.sim, clients, workloads, _, services = ConfigLoader.build_simulation(config)
        self.client = clients[0]
        self.service = list(services.values())[0] if isinstance(services, dict) else services[config.services[0].name]
        self.service.enable_live_buffer()
        workload = workloads[0]
        self.episode_end = s_to_ns(workload.duration_s)

        # Wire workload to client
        workload.drive(self.sim, lambda s, c=self.client: c.start_request(s))

        # Advance to first decision point
        self.next_decision_time = self.decision_interval_ns
        self.sim.run(until=self.next_decision_time)
        
        obs = self._get_obs()
        return obs, {}

    def step(self, action):
        # Decode action
        refill_rate = self.refill_rate_map[action[0]]
        bucket_capacity = self.bucket_capacity_map[action[1]]

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
        reward = obs_dict["success_rate"] - 0.3 * obs_dict["retry_ratio"]

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
        obs_dict = self.service.live_buffer.get_observation(
            self.sim.timestep, self.decision_interval_ns
        )
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
        ], dtype=np.float32)