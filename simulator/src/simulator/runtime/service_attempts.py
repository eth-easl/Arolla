from __future__ import annotations

import heapq
from functools import partial
from typing import Callable, Optional

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.policies.retry import RetryContext


class _ServiceAttemptMixin:
    def submit_request(
        self,
        sim: Simulator,
        on_attempt_done: Callable[
            [bool, TimeDuration, DropReason, int, TimePoint, Optional[TimePoint]], None
        ],
        on_root_done: Callable[[], None],
        global_deadline: Optional[TimePoint] = None,
        is_retry: bool = False,
    ):
        if self.cfg.load_limiter is not None:
            should_check = self.cfg.load_limiter.applies_pre_queue_admission(is_retry)
            if should_check:
                check_ctx = RetryContext(attempt=1, now=sim.timestep)
                allowed, _ = self.cfg.load_limiter.next_delay(check_ctx)
                if not allowed:
                    on_attempt_done(
                        False,
                        0,
                        DropReason.SERVER_FAILURE,
                        len(self.queue),
                        sim.timestep,
                        None,
                    )
                    return

        from simulator.runtime.service import _SrvRetryCtx  # local import avoids cycle

        ctx = _SrvRetryCtx(
            attempt=0,
            global_deadline=global_deadline,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
        )
        self._start_attempt(sim, ctx)

    def _start_attempt(self, sim: Simulator, ctx: "_SrvRetryCtx"):
        if ctx.global_deadline is not None and sim.timestep >= ctx.global_deadline:
            ctx.on_root_done()
            return

        ctx.attempt += 1
        begin_time = sim.timestep
        attempt_deadline = self._min_deadline(
            ctx.global_deadline, self._attempt_deadline(begin_time)
        )
        on_done = partial(
            self._on_single_attempt_done, sim, ctx, begin_time, attempt_deadline
        )
        self.submit_attempt(
            sim,
            on_done,
            attempt_deadline=attempt_deadline,
            is_retry=(ctx.attempt > 1),
        )

    def _start_next(self, sim: Simulator):
        while self.in_flight < self.cfg.workers and self.queue:
            item = heapq.heappop(self.queue)
            item.start()

    def submit_attempt(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        attempt_deadline: Optional[TimePoint] = None,
        is_retry: bool = False,
    ):
        """
        Enqueue an attempt for this service.
        Execution is FCFS with 'workers' concurrency.
        """
        self._seq += 1
        seq = self._seq

        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, 0, DropReason.DEADLINE, len(self.queue))
            return

        start_cb = partial(self._begin_service, sim, on_done, attempt_deadline, is_retry)

        if self.in_flight < self.cfg.workers:
            start_cb()
            return

        if self.cfg.queue_capacity is not None and len(self.queue) >= self.cfg.queue_capacity:
            on_done(False, 0, DropReason.QUEUE_FULL, len(self.queue))
            return

        from simulator.runtime.service import QItem  # local import avoids cycle

        heapq.heappush(
            self.queue,
            QItem(enqueued_at=sim.timestep, seq=seq, start=start_cb),
        )

    def _begin_service(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        attempt_deadline: Optional[TimePoint],
        is_retry: bool,
    ):
        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, 0, DropReason.DEADLINE, len(self.queue))
            return

        self.in_flight += 1

        if self.dependencies:
            start_t = sim.timestep

            def on_all_deps_done(all_success: bool, worst_reason: DropReason):
                if not all_success:
                    total_time = sim.timestep - start_t
                    self.in_flight -= 1
                    on_done(False, total_time, worst_reason, len(self.queue))
                    self._start_next(sim)
                    return

                service_time = self._sample_service_time(sim.timestep, sim)
                expiry = (
                    min(sim.timestep + service_time, attempt_deadline)
                    if attempt_deadline is not None
                    else sim.timestep + service_time
                )

                def finish_after_deps():
                    total_time = sim.timestep - start_t
                    local_failed = self._fails_now(sim.timestep, sim)
                    timed_out = (
                        attempt_deadline is not None and sim.timestep >= attempt_deadline
                    )

                    self.in_flight -= 1
                    if timed_out:
                        on_done(False, total_time, DropReason.DEADLINE, len(self.queue))
                    elif local_failed:
                        on_done(
                            False, total_time, DropReason.SERVER_FAILURE, len(self.queue)
                        )
                    else:
                        on_done(True, total_time, DropReason.NONE, len(self.queue))
                    self._start_next(sim)

                sim.schedule(expiry, finish_after_deps)

            if self.dependency_call_pattern == "parallel":
                self._call_deps_parallel(
                    sim,
                    self.dependencies,
                    self.dependency_optionality,
                    on_all_deps_done,
                    attempt_deadline,
                    is_retry,
                )
            else:
                self._call_deps_sequential(
                    sim,
                    self.dependencies,
                    self.dependency_optionality,
                    0,
                    on_all_deps_done,
                    attempt_deadline,
                    is_retry,
                )
            return

        service_time = self._sample_service_time(sim.timestep, sim)
        expiry = (
            min(sim.timestep + service_time, attempt_deadline)
            if attempt_deadline is not None
            else sim.timestep + service_time
        )
        sim.schedule(
            expiry, partial(self._finish_service, sim, on_done, service_time, attempt_deadline)
        )

    def _finish_service(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        service_time: TimeDuration,
        attempt_deadline: Optional[TimePoint],
    ):
        self.in_flight -= 1

        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, service_time, DropReason.DEADLINE, len(self.queue))
        else:
            if not self._fails_now(sim.timestep, sim):
                on_done(True, service_time, DropReason.NONE, len(self.queue))
            else:
                on_done(False, service_time, DropReason.SERVER_FAILURE, len(self.queue))
        self._start_next(sim)
