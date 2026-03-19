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

        # Action: [max_attempts, delay_index, budget_index]
        # max_attempts: 0=1, 1=2, 2=3, 3=4
        # delay_index:  0=50ms, 1=100ms, 2=200ms, 3=300ms, 4=500ms, 5=750ms, 6=1000ms
        # budget_index: 0=0.02, 1=0.05, 2=0.10, 3=0.20, 4=0.50
        self.action_space = spaces.MultiDiscrete([4, 7, 5])
        self.max_attempts_map = [1, 2, 3, 4]
        self.delay_map = [ms_to_ns(d) for d in [50, 100, 200, 300, 500, 750, 1000]]
        self.budget_map = [0.02, 0.05, 0.10, 0.20, 0.50]

        # observation
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
        max_attempts = self.max_attempts_map[action[0]]
        delay_ns = self.delay_map[action[1]]
        budget_ratio = self.budget_map[action[2]]

        # Apply action
        self.service.update_retry_config(
            max_attempts=max_attempts,
            delay_ns=delay_ns,
            budget_ratio=budget_ratio,
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
            "action_max_attempts": max_attempts,
            "action_delay_ms": delay_ns / 1e6,
            "action_budget_ratio": budget_ratio,
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