"""Hybrid 3-knob metastable RL environments.

Built on top of the existing metastable environments by subclassing them and
adding a third action dimension that controls the event-based refill
(``success_reward``) of the server-side retry budget. The underlying
simulator is not modified; instead, the live ``GlobalRetryBudget`` instance is
upgraded in place to ``HybridGlobalRetryBudget`` during ``reset``.

- ``HybridMetastableFairnessEnv`` uses absolute (index-based) control of all
  three knobs: refill rate, bucket capacity, event reward.
- ``HybridRelativeActionMetastableFairnessEnv`` uses relative {-1, 0, +1}
  moves on each of the three knob indices.
"""
from __future__ import annotations

import numpy as np
from gymnasium import spaces

from simulator.rl.hybrid_retry_budget import (
    HybridGlobalRetryBudget,
    update_event_reward,
    upgrade_limiter_to_hybrid,
)
from simulator.rl.metastable_fairness_env import MetastableFairnessSimEnv
from simulator.rl.metastable_relative_action_env import RelativeActionMetastableFairnessEnv


EVENT_REWARD_MAP = [0.0, 0.05, 0.15, 0.30, 0.60]
"""Tokens added to the bucket per successful attempt (event-based refill).

Indexed by the third action dimension. ``0.0`` means purely time-based
refill (equivalent to the current static/absolute behaviour); higher values
reward the system with more retry capacity the healthier it is.
"""


# Light penalty to discourage oscillating the new knob; tuned to be
# comparable to the churn penalty already used by the parent envs.
_EVENT_ACTION_CHURN_PENALTY = 0.005


def _augment_with_event_index(obs: np.ndarray, event_idx: int, max_idx: int) -> np.ndarray:
    """Append a normalised event-reward index to the base observation vector."""
    event_norm = float(event_idx) / float(max(max_idx, 1))
    return np.concatenate([obs, np.array([event_norm], dtype=np.float32)], axis=0)


class HybridMetastableFairnessEnv(MetastableFairnessSimEnv):
    """Absolute 3-knob control: refill rate, bucket capacity, event reward."""

    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([5, 5, 5]): "
        "action[0] selects refill_rate from [5, 15, 30, 60, 90] rps; "
        "action[1] selects bucket_capacity from [5, 10, 20, 50, 80] tokens; "
        "action[2] selects event_reward (tokens added per successful attempt) "
        "from [0.0, 0.05, 0.15, 0.30, 0.60]."
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
        self.event_reward_map = list(EVENT_REWARD_MAP)
        self.current_event_idx = 0
        self.action_space = spaces.MultiDiscrete([5, 5, 5])
        # Base env exposes a 21-dim observation; we append one dimension
        # (normalised event-reward index) so the policy can see the
        # event-based knob's current setting.
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(22,), dtype=np.float32
        )

    def reset(self, seed=None, options=None):
        obs, info = super().reset(seed=seed, options=options)
        self.current_event_idx = 0
        self._ensure_hybrid_limiter()
        return _augment_with_event_index(obs, self.current_event_idx, len(self.event_reward_map) - 1), info

    def step(self, action):
        action = np.asarray(action, dtype=np.int64).reshape(-1)
        assert action.shape[0] == 3, f"Expected 3-dim action, got shape {action.shape}"

        previous_event_idx = self.current_event_idx
        self.current_event_idx = int(action[2])
        event_reward = self.event_reward_map[self.current_event_idx]

        self._ensure_hybrid_limiter()
        update_event_reward(self.service.cfg.load_limiter, event_reward)

        parent_action = np.array([action[0], action[1]], dtype=np.int64)
        obs, reward, done, truncated, info = super().step(parent_action)

        if previous_event_idx != self.current_event_idx:
            reward -= _EVENT_ACTION_CHURN_PENALTY

        if self.history:
            self.history[-1]["action_event_reward"] = float(event_reward)

        return (
            _augment_with_event_index(obs, self.current_event_idx, len(self.event_reward_map) - 1),
            reward,
            done,
            truncated,
            info,
        )

    def _ensure_hybrid_limiter(self) -> None:
        """Upgrade the current limiter instance in place, preserving identity."""
        limiter = self.service.cfg.load_limiter
        if limiter is None:
            return
        if not isinstance(limiter, HybridGlobalRetryBudget):
            upgrade_limiter_to_hybrid(
                limiter,
                success_reward=self.event_reward_map[self.current_event_idx],
            )


class HybridRelativeActionMetastableFairnessEnv(RelativeActionMetastableFairnessEnv):
    """Relative 3-knob control: each action is in {-1, 0, +1}, clamped per-knob."""

    ACTION_SPACE_DESCRIPTION = (
        "MultiDiscrete([3, 3, 3]): action[i] shifts the corresponding index by "
        "{-1, 0, +1} and is clamped. Knob 0 is refill_rate, knob 1 is "
        "bucket_capacity, knob 2 is event_reward (tokens per successful "
        "attempt)."
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
        self.event_reward_map = list(EVENT_REWARD_MAP)
        self.current_event_idx = 0
        self.action_space = spaces.MultiDiscrete([3, 3, 3])
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(22,), dtype=np.float32
        )

    def reset(self, seed=None, options=None):
        obs, info = super().reset(seed=seed, options=options)
        self.current_event_idx = 0
        self._ensure_hybrid_limiter()
        return _augment_with_event_index(obs, self.current_event_idx, len(self.event_reward_map) - 1), info

    def step(self, action):
        action = np.asarray(action, dtype=np.int64).reshape(-1)
        assert action.shape[0] == 3, f"Expected 3-dim action, got shape {action.shape}"

        previous_event_idx = self.current_event_idx
        delta = int(action[2]) - 1  # map {0, 1, 2} -> {-1, 0, +1}
        self.current_event_idx = int(
            np.clip(self.current_event_idx + delta, 0, len(self.event_reward_map) - 1)
        )
        event_reward = self.event_reward_map[self.current_event_idx]

        self._ensure_hybrid_limiter()
        update_event_reward(self.service.cfg.load_limiter, event_reward)

        parent_action = np.array([action[0], action[1]], dtype=np.int64)
        obs, reward, done, truncated, info = super().step(parent_action)

        if previous_event_idx != self.current_event_idx:
            reward -= _EVENT_ACTION_CHURN_PENALTY

        if self.history:
            self.history[-1]["action_event_reward"] = float(event_reward)

        return (
            _augment_with_event_index(obs, self.current_event_idx, len(self.event_reward_map) - 1),
            reward,
            done,
            truncated,
            info,
        )

    def _ensure_hybrid_limiter(self) -> None:
        limiter = self.service.cfg.load_limiter
        if limiter is None:
            return
        if not isinstance(limiter, HybridGlobalRetryBudget):
            upgrade_limiter_to_hybrid(
                limiter,
                success_reward=self.event_reward_map[self.current_event_idx],
            )
