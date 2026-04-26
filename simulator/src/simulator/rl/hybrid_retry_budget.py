"""Hybrid global retry budget: time-based + event-based refill.

Extends the existing server-side ``GlobalRetryBudget`` non-invasively:
- Adds a ``success_reward`` field controlling how many tokens are added to the
  bucket on each successful attempt observed through the middleware chain.
- Overrides ``add_result`` so the existing ``LoadLimiterMiddleware`` hook
  feeds per-attempt success signals into event-based refill.

The RL env can upgrade a freshly-built ``GlobalRetryBudget`` instance in place
(via ``upgrade_limiter_to_hybrid``) so that the same object continues to live
inside ``ServiceConfig.load_limiter`` and inside the middleware chain. This
avoids touching the current simulator's config, loader, or runtime code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from simulator.core.types import TimePoint
from simulator.policies.server_retry_budget import GlobalRetryBudget


@dataclass
class HybridGlobalRetryBudget(GlobalRetryBudget):
    """Time-based bucket with an additional per-success event refill."""

    success_reward: float = 0.0

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        if success and self.success_reward > 0.0:
            self._tokens = min(
                float(self.max_tokens),
                self._tokens + float(self.success_reward),
            )


def upgrade_limiter_to_hybrid(
    limiter: GlobalRetryBudget,
    success_reward: float = 0.0,
) -> HybridGlobalRetryBudget:
    """Upgrade an existing GlobalRetryBudget instance in place to hybrid behaviour.

    The underlying object identity is preserved so references held by the
    service config and the middleware chain stay valid.
    """
    if not isinstance(limiter, GlobalRetryBudget):
        raise TypeError(f"Expected GlobalRetryBudget, got {type(limiter)!r}")
    limiter.__class__ = HybridGlobalRetryBudget
    limiter.success_reward = float(success_reward)
    return limiter  # type: ignore[return-value]


def update_event_reward(
    limiter: HybridGlobalRetryBudget,
    success_reward: float,
) -> None:
    """Runtime update of the event-based refill knob."""
    limiter.success_reward = max(0.0, float(success_reward))
