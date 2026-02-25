from __future__ import annotations

import math

from simulator.core.engine import Simulator
from simulator.core.types import MIN_SERVICE_TIME_NS, TimeDuration, TimePoint


class _ServiceTimingMixin:
    def _attempt_deadline(self, now: TimePoint) -> TimePoint | None:
        if self.cfg.timeout is None:
            return None
        at = self.cfg.timeout.get_attempt_timeout()
        if at is None:
            return None
        return now + at

    def _min_deadline(
        self, d1: TimePoint | None, d2: TimePoint | None
    ) -> TimePoint | None:
        if d1 is None:
            return d2
        if d2 is None:
            return d1
        return min(d1, d2)

    def _adjust_latency(self, t: TimePoint, base: TimeDuration) -> TimeDuration:
        mult = 1
        add = 0
        for inj in self.cfg.latency_injections:
            if inj.active(t):
                mult *= inj.multiplier
                add += inj.add_latency
        return max(0, base * mult + add)

    def _sample_service_time(self, t: TimePoint, sim: Simulator) -> TimeDuration:
        """Lognormal with configured median and sigma."""
        rng = self._rng if self._rng else sim.rng()

        median = max(1, self.cfg.latency_median)
        sigma = max(1e-6, self.cfg.latency_lognorm_sigma)
        mu = math.log(median)
        raw = rng.lognormvariate(mu, sigma)
        adj = self._adjust_latency(t, int(raw))
        return max(MIN_SERVICE_TIME_NS, adj)

    def _fails_now(self, t: TimePoint, sim: Simulator) -> bool:
        rng = self._rng if self._rng else sim.rng()

        p_fail = 0.0
        for pf in self.cfg.partial_failures:
            if pf.active(t):
                p_fail = max(p_fail, pf.p_fail)
        return rng.random() < p_fail
