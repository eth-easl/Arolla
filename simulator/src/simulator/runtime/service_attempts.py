from __future__ import annotations

import heapq
from collections import defaultdict
from functools import partial
from typing import Callable, Dict, NamedTuple, Optional

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.policies.retry import RetryContext


class CallResult(NamedTuple):
    """Records the outcome of the last attempt made by the service"""
    success: bool
    svc_time: TimeDuration
    drop_reason: DropReason
    queue_size: int
    attempt_deadline: Optional[TimePoint]


class _ServiceAttemptMixin:
    # Per-tenant retry admission counters (set in ServiceRuntime.bind())
    _admission_requested: Dict[str, int]
    _admission_admitted: Dict[str, int]

    def submit_request(
        self,
        sim: Simulator,
        on_attempt_done: Callable[
            [bool, TimeDuration, DropReason, int, TimePoint, Optional[TimePoint]], None
        ],
        on_root_done: Callable[[], None],
        global_deadline: Optional[TimePoint] = None,
        is_retry: bool = False,
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
    ):
        if self.cfg.load_limiter is not None:
            should_check = self.cfg.load_limiter.applies_pre_queue_admission(is_retry)
            if should_check:
                tenant = tenant_id or '__global__'
                self._admission_requested[tenant] += 1

                check_ctx = RetryContext(attempt=1, now=sim.timestep, tenant_id=tenant_id)
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
                    on_root_done()
                    return
                self._admission_admitted[tenant] += 1

        from simulator.runtime.service import _SrvRetryCtx  # local import avoids cycle

        ctx = _SrvRetryCtx(
            attempt=0,
            global_deadline=global_deadline,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            retry_budget_remaining=retry_budget_remaining,
            tenant_id=tenant_id,
        )
        self._start_attempt(sim, ctx)

    def submit_call(
        self,
        sim: Simulator,
        on_result: Callable[[CallResult], None],
        global_deadline: Optional[TimePoint] = None,
        is_retry: bool = False,
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
    ):
        last_attempt = None

        def on_attempt_done(success, svc_time, drop_reason, queue_size, _begin_time, attempt_deadline):
            nonlocal last_attempt
            last_attempt = CallResult(success, svc_time, drop_reason, queue_size, attempt_deadline)

        def on_root_done():
            if last_attempt is None:
                on_result(CallResult(False, 0, DropReason.DEADLINE, len(self.queue), None))
            else:
                on_result(last_attempt)

        self.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            global_deadline=global_deadline,
            is_retry=is_retry,
            retry_budget_remaining=retry_budget_remaining,
            tenant_id=tenant_id,
        )

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
            retry_budget_remaining=ctx.retry_budget_remaining,
            tenant_id=ctx.tenant_id,
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
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
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

        start_cb = partial(
            self._begin_service, sim, on_done, attempt_deadline, is_retry,
            retry_budget_remaining, tenant_id,
        )

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
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
    ):
        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, 0, DropReason.DEADLINE, len(self.queue))
            return

        self.in_flight += 1

        if self.dependencies:
            self._serve_with_dependencies(
                sim, on_done, attempt_deadline, is_retry, retry_budget_remaining, tenant_id
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

    def _serve_with_dependencies(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        attempt_deadline: Optional[TimePoint],
        is_retry: bool,
        retry_budget_remaining: Optional[list],
        tenant_id: Optional[str],
    ):
        start_t = sim.timestep

        def report(success: bool, reason: DropReason):
            self.in_flight -= 1
            on_done(success, sim.timestep - start_t, reason, len(self.queue))
            self._start_next(sim)

        def timed_out() -> bool:
            return attempt_deadline is not None and sim.timestep >= attempt_deadline

        def do_work(then: Callable[[], None]):
            work_ends = sim.timestep + self._sample_service_time(sim.timestep, sim)
            if attempt_deadline is not None:
                work_ends = max(sim.timestep, min(work_ends, attempt_deadline))

            def work_done():
                failed = self._fails_now(sim.timestep, sim)  # drawn even after the deadline
                if timed_out():
                    report(False, DropReason.DEADLINE)
                elif failed:
                    report(False, DropReason.SERVER_FAILURE)
                else:
                    then()

            sim.schedule(work_ends, work_done)

        def call_dependencies(then: Callable[[], None]):
            def calls_done(all_success: bool, reason: DropReason):
                if all_success:
                    then()
                else:
                    report(False, reason)

            self._call_dependencies(
                sim, calls_done, attempt_deadline, is_retry, retry_budget_remaining, tenant_id
            )

        def finish():
            if timed_out():
                report(False, DropReason.DEADLINE)
            else:
                report(True, DropReason.NONE)

        if self.local_work_first:
            do_work(then=lambda: call_dependencies(then=finish))
        else:
            sim.schedule(sim.timestep, lambda: call_dependencies(then=lambda: do_work(then=finish)))

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
