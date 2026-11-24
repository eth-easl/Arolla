from collections.abc import Callable
from dataclasses import dataclass, field
from typing import List, Optional
import random
from simulator.core.types import TimePoint, TimeDuration
from simulator.utils.time import ns_to_s, s_to_ns
from simulator.faults.injection import LoadSpike
from simulator.core.engine import Simulator
from simulator.faults.events import FaultEventsTracker


@dataclass
class Workload:
    base_rps: float
    duration_s: int
    load_spikes: List[LoadSpike] = field(default_factory=list)
    rng_seed: Optional[int] = None
    exact_arrivals: bool = False  # see `_drive_exact`
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self):
        self._rng = random.Random(self.rng_seed if self.rng_seed is not None else 0)

    def register_fault_events(self, tracker: FaultEventsTracker):
        """Register all load spike events with the tracker"""
        for load_spike in self.load_spikes:
            tracker.add_load_spike(load_spike.duration, load_spike.rps_multiplier)

    def rps_at(self, time: TimePoint) -> float:
        rps = self.base_rps
        for spike in self.load_spikes:
            if spike.duration.contains(time):
                rps *= spike.rps_multiplier
        return max(0.0, rps)

    def _sample_interarrival(self, time: TimePoint) -> TimeDuration:
        rps = self.rps_at(time)
        if rps <= 0.0:
            # No requests should arrive at the current rate.
            # Check again in 0.5 seconds in case the rate changes.
            return s_to_ns(0.5)
        
        # Use a separate random generator so request timing is reproducible
        # for the same workload settings and seed.
        return s_to_ns(self._rng.expovariate(rps))

    def drive(self, sim: Simulator, on_arrival: Callable[[Simulator], None]):
        if self.exact_arrivals:
            self._drive_exact(sim, on_arrival)
            return

        end_at = s_to_ns(self.duration_s)

        def tick():
            if sim.timestep >= end_at:
                return
            on_arrival(sim)
            ia = max(1, self._sample_interarrival(sim.timestep))
            sim.schedule(sim.timestep + ia, tick)

        # Send the first request at t=0.
        sim.schedule(0, tick)

    def _drive_exact(self, sim: Simulator, on_arrival: Callable[[Simulator], None]):
        """Generate requests while accounting for changes in traffic rate.

        The default implementation chooses the next request time using the
        current RPS. If the RPS changes before that request arrives, the
        scheduled time is not updated. This can cause short load spikes
        to be missed entirely.

        This implementation accounts for any RPS changes that occur while
        waiting for the next request, so even short spikes affect traffic.

        Unlike the default, it does not send a request immediately at t=0.
        Without load spikes, both implementations generate the same
        subsequent request times when using the same random seed.
        """
        end_at = s_to_ns(self.duration_s)

        def schedule_next(after: TimePoint):
            arrival = self._next_arrival_after(after, end_at)
            if arrival is not None:
                sim.schedule(max(arrival, after + 1), arrive)  # at least 1 ns apart, as by default

        def arrive():
            if sim.timestep >= end_at:
                return
            on_arrival(sim)
            schedule_next(sim.timestep)

        schedule_next(0)

    def _next_rate_change(self, time: TimePoint, end_at: TimePoint) -> TimePoint:
        """Find when the RPS might change next.

        A load spike changes the RPS when it starts or ends.
        Return the nearest such time, or end_at if there are no
        more changes before the simulation ends.
        """
        change = end_at
        for spike in self.load_spikes:
            for edge in (spike.duration.begin, spike.duration.end):
                if time < edge < change:
                    change = edge
        return change

    def _next_arrival_after(self, time: TimePoint, end_at: TimePoint) -> Optional[TimePoint]:
        """Find the next random request time, accounting for load spikes.

        Normally, we would use the current RPS to calculate how long to
        wait for the next request. However, if the RPS changes during
        that wait, the calculated arrival time can be wrong.

        Instead, we draw a random countdown. The higher the RPS, the
        faster this countdown decreases. A new request arrives when it
        reaches zero.

        If the RPS changes before the countdown reaches zero, we
        continue counting down using the new RPS.

        For example:
        - Normal traffic is 1 RPS.
        - Traffic increases to 1000 RPS from 10.0s to 10.1s.
        - At 9.0s, we draw a countdown of 5.
        - By 10.0s, one second at 1 RPS reduces it from 5 to 4.
        - At 1000 RPS, the remaining 4 take just 0.004 seconds.
        - The next request therefore arrives at 10.004s.

        The default implementation would instead calculate a 5-second
        wait at 9.0s and schedule the request at 14.0s, completely
        missing the spike.

        Return None if no request arrives before the simulation ends.
        """
        countdown = self._rng.expovariate(1.0)
        while time < end_at:
            rate = self.rps_at(time)
            change = self._next_rate_change(time, end_at)
            decrease = rate * ns_to_s(change - time)  # until the rate changes
            if rate > 0.0 and countdown <= decrease:
                return time + s_to_ns(countdown / rate)
            countdown -= decrease
            time = change
        return None
