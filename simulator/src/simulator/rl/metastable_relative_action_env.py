import numpy as np
from gymnasium import spaces

from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv, metastable_reward
from simulator.rl.random_scenario_env import action_transition_metrics, stabilize_window_observation


ACTION_DELTAS = (-1, 0, 1)


def relative_action_to_indices(
    current_refill_idx: int,
    current_capacity_idx: int,
    action,
    refill_choices: int = 5,
    capacity_choices: int = 5,
) -> tuple[int, int]:
    next_refill_idx = int(np.clip(current_refill_idx + ACTION_DELTAS[int(action[0])], 0, refill_choices - 1))
    next_capacity_idx = int(np.clip(current_capacity_idx + ACTION_DELTAS[int(action[1])], 0, capacity_choices - 1))
    return next_refill_idx, next_capacity_idx


class RelativeActionMetastableFairnessEnv(MetastableFairnessSimEnv):
    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([3, 3]): action[0] moves refill_rate index down/keep/up and "
        "action[1] moves bucket_capacity index down/keep/up."
    )

    def __init__(
        self,
        yaml_path: str,
        decision_interval_s: float = 2.0,
        randomize_scenarios: bool = True,
        scenario_profile: str = "metastable_fairness",
    ):
        super().__init__(
            yaml_path=yaml_path,
            decision_interval_s=decision_interval_s,
            randomize_scenarios=randomize_scenarios,
            scenario_profile=scenario_profile,
        )
        self.action_space = spaces.MultiDiscrete([3, 3])

    def step(self, action):
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
            next_refill_idx=next_refill_idx,
            next_capacity_idx=next_capacity_idx,
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
            latency_pressure=max(0.0, obs_dict["p99"] / self._attempt_timeout_ms() - 1.0),
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
