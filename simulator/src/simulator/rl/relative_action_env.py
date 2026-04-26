import numpy as np
from gymnasium import spaces

from simulator.rl.random_scenario_env import (
    RandomScenarioSimEnv,
    action_transition_metrics,
    bucket_fill_ratio,
    simple_reward,
    stabilize_window_observation,
)


ACTION_DELTAS = (-1, 0, 1)


def relative_action_to_indices(
    current_refill_idx: int,
    current_capacity_idx: int,
    action,
    refill_choices: int = 5,
    capacity_choices: int = 5,
) -> tuple[int, int]:
    """Convert relative up/keep/down actions into clamped absolute indices."""
    next_refill_idx = int(np.clip(current_refill_idx + ACTION_DELTAS[int(action[0])], 0, refill_choices - 1))
    next_capacity_idx = int(np.clip(current_capacity_idx + ACTION_DELTAS[int(action[1])], 0, capacity_choices - 1))
    return next_refill_idx, next_capacity_idx


class RelativeActionRandomScenarioSimEnv(RandomScenarioSimEnv):
    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([3, 3]): action[0] moves refill_rate index down/keep/up and "
        "action[1] moves bucket_capacity index down/keep/up. Indices are clamped to valid ranges."
    )

    def __init__(self, yaml_path: str, decision_interval_s: float = 2.0):
        super().__init__(yaml_path=yaml_path, decision_interval_s=decision_interval_s)
        self.action_space = spaces.MultiDiscrete([3, 3])

    def step(self, action):
        """Take a relative action in the environment."""
        previous_refill_idx = self.current_refill_idx
        previous_capacity_idx = self.current_capacity_idx

        next_refill_idx, next_capacity_idx = relative_action_to_indices(
            current_refill_idx=self.current_refill_idx,
            current_capacity_idx=self.current_capacity_idx,
            action=action,
            refill_choices=len(self.refill_rate_map),
            capacity_choices=len(self.bucket_capacity_map),
        )

        refill_rate = self.refill_rate_map[next_refill_idx]
        bucket_capacity = self.bucket_capacity_map[next_capacity_idx]

        self.current_refill_idx = next_refill_idx
        self.current_capacity_idx = next_capacity_idx

        self.service.update_token_bucket(
            refill_rate=refill_rate,
            bucket_capacity=bucket_capacity,
        )

        self.next_decision_time = min(
            self.next_decision_time + self.decision_interval_ns,
            self.episode_end,
        )
        self.sim.run(until=self.next_decision_time)

        obs = self._get_obs()

        transition = action_transition_metrics(
            previous_refill_idx=previous_refill_idx,
            previous_capacity_idx=previous_capacity_idx,
            next_refill_idx=next_refill_idx,
            next_capacity_idx=next_capacity_idx,
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
            latency_pressure=self._latency_pressure(obs_dict),
            delta_success=delta_success,
            action_distance=float(transition["action_distance"]),
            counteracting_change=bool(transition["counteracting_change"]),
        )

        done = self.sim.timestep >= self.episode_end
        if done:
            self.sim.run()

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

    def _latency_pressure(self, obs_dict: dict) -> float:
        return obs_dict["p99"] / self._attempt_timeout_ms() - 1.0 if obs_dict["p99"] > self._attempt_timeout_ms() else 0.0
