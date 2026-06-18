from __future__ import annotations

import heapq
from collections import defaultdict
from functools import partial
from typing import Callable, Dict, Optional

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.policies.istio_retry_budget import IstioRetryBudget
from simulator.policies.retry import RetryContext


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
        limiter = self.cfg.load_limiter
        if limiter is not None:
            should_check = limiter.applies_pre_queue_admission(is_retry)
            if should_check:
                tenant = tenant_id or '__global__'
                self._admission_requested[tenant] += 1

                # RL: Istio-style budgets decide based on live concurrency, so
                # refresh active/pending/retry counters before asking.
                if isinstance(limiter, IstioRetryBudget):
                    self._refresh_retry_budget_runtime_state()

                check_ctx = RetryContext(attempt=1, now=sim.timestep, tenant_id=tenant_id)
                allowed, _ = limiter.next_delay(check_ctx)

                # RL: record the admission outcome for budget_reject_rate telemetry.
                if isinstance(limiter, IstioRetryBudget):
                    limiter.record_retry_admission(admitted=allowed, now_ns=sim.timestep)

                if not allowed:
                    # RL: mirror the rejected retry into the live buffer (no-op
                    # unless an RL env enabled it). The static event log is
                    # intentionally left untouched.
                    self._record_live_metrics(
                        timestamp_ns=sim.timestep,
                        latency_ns=0,
                        success=False,
                        drop_reason=DropReason.SERVER_FAILURE,
                        queue_size=len(self.queue),
                        attempt_num=2 if is_retry else 1,
                        is_retry=is_retry,
                    )
                    on_attempt_done(
                        False,
                        0,
                        DropReason.SERVER_FAILURE,
                        len(self.queue),
                        sim.timestep,
                        None,
                    )
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
            external_is_retry=is_retry,
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
        # RL: a client-managed retry stays a retry across its whole lifecycle.
        self.submit_attempt(
            sim,
            on_done,
            attempt_deadline=attempt_deadline,
            is_retry=(ctx.external_is_retry or ctx.attempt > 1),
            retry_budget_remaining=ctx.retry_budget_remaining,
            tenant_id=ctx.tenant_id,
        )

    def _start_next(self, sim: Simulator):
        while self.in_flight < self.cfg.workers and self.queue:
            item = heapq.heappop(self.queue)
            if item.is_retry:
                self.queued_retries = max(0, self.queued_retries - 1)
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
            QItem(enqueued_at=sim.timestep, seq=seq, start=start_cb, is_retry=is_retry),
        )
        if is_retry:
            self.queued_retries += 1

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
        if is_retry:
            self.in_flight_retries += 1

        if self.dependencies:
            start_t = sim.timestep

            def on_all_deps_done(all_success: bool, worst_reason: DropReason):
                if not all_success:
                    total_time = sim.timestep - start_t
                    self.in_flight -= 1
                    if is_retry:
                        self.in_flight_retries = max(0, self.in_flight_retries - 1)
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
                    if is_retry:
                        self.in_flight_retries = max(0, self.in_flight_retries - 1)
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
                    retry_budget_remaining=retry_budget_remaining,
                    tenant_id=tenant_id,
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
                    retry_budget_remaining=retry_budget_remaining,
                    tenant_id=tenant_id,
                )
            return

        service_time = self._sample_service_time(sim.timestep, sim)
        expiry = (
            min(sim.timestep + service_time, attempt_deadline)
            if attempt_deadline is not None
            else sim.timestep + service_time
        )
        sim.schedule(
            expiry,
            partial(
                self._finish_service, sim, on_done, service_time, attempt_deadline, is_retry
            ),
        )

    def _finish_service(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        service_time: TimeDuration,
        attempt_deadline: Optional[TimePoint],
        is_retry: bool = False,
    ):
        self.in_flight -= 1
        if is_retry:
            self.in_flight_retries = max(0, self.in_flight_retries - 1)

        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, service_time, DropReason.DEADLINE, len(self.queue))
        else:
            if not self._fails_now(sim.timestep, sim):
                on_done(True, service_time, DropReason.NONE, len(self.queue))
            else:
                on_done(False, service_time, DropReason.SERVER_FAILURE, len(self.queue))
        self._start_next(sim)
