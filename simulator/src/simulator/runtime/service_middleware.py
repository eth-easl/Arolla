from __future__ import annotations

from functools import partial

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.middleware.base import AttemptContext, MiddlewareChain
from simulator.middleware.load_limiter import EndToEndRetryBudgetMiddleware, LoadLimiterMiddleware
from simulator.middleware.retry import NoRetryMiddleware, RetryMiddleware


class _ServiceMiddlewareMixin:
    def _on_single_attempt_done(
        self,
        sim: Simulator,
        ctx: "_SrvRetryCtx",
        begin_time: TimePoint,
        attempt_deadline: TimePoint | None,
        success: bool,
        svc_time: TimeDuration,
        drop_reason: DropReason,
        queue_size: int,
    ):
        """Handle completion of a single attempt using middleware composition."""
        end_time = sim.timestep

        if self._record_events:
            self._events.append(
                (
                    end_time,
                    svc_time,
                    success,
                    drop_reason,
                    queue_size,
                    ctx.attempt,
                    ctx.attempt > 1,
                )
            )

        # RL: mirror the finished attempt into the live buffer (no-op unless an RL
        # env enabled it). A client-managed retry stays a retry for telemetry.
        self._record_live_metrics(
            timestamp_ns=end_time,
            latency_ns=svc_time,
            success=success,
            drop_reason=drop_reason,
            queue_size=queue_size,
            attempt_num=ctx.attempt,
            is_retry=(ctx.external_is_retry or ctx.attempt > 1),
        )

        ctx.on_attempt_done(
            success,
            svc_time,
            drop_reason,
            queue_size,
            begin_time,
            attempt_deadline,
        )

        attempt_ctx = AttemptContext(
            attempt_number=ctx.attempt,
            success=success,
            service_time=svc_time,
            drop_reason=drop_reason,
            begin_time=begin_time,
            end_time=end_time,
            attempt_deadline=attempt_deadline,
            global_deadline=ctx.global_deadline,
            queue_size=queue_size,
            service_name=self.cfg.name,
            retry_budget_remaining=(
                ctx.retry_budget_remaining[0] if ctx.retry_budget_remaining is not None else None
            ),
            tenant_id=ctx.tenant_id,
        )

        if self._middleware_chain is None:
            self._middleware_chain = self._build_middleware_chain()

        # RL: keep Istio-style budget concurrency fresh before the chain decides.
        self._refresh_retry_budget_runtime_state()

        chain = self._middleware_chain

        def final_handler(a_ctx: AttemptContext):
            if a_ctx.is_successful or not a_ctx.should_retry:
                ctx.on_root_done()
            else:
                # Decrement Level 2 end-to-end budget on retry
                if ctx.retry_budget_remaining is not None:
                    ctx.retry_budget_remaining[0] -= 1
                next_start = end_time + a_ctx.retry_delay
                sim.schedule(next_start, partial(self._start_attempt, sim, ctx))

        chain.execute(attempt_ctx, final_handler)

    def _build_middleware_chain(self) -> MiddlewareChain:
        middlewares = []
        if self.cfg.retry is not None:
            middlewares.append(RetryMiddleware(self.cfg.retry))
        else:
            middlewares.append(NoRetryMiddleware())

        # Level 2: end-to-end budget check (before Level 1 load limiter)
        middlewares.append(EndToEndRetryBudgetMiddleware())

        if self.cfg.load_limiter is not None:
            middlewares.append(LoadLimiterMiddleware(self.cfg.load_limiter))

        return MiddlewareChain(middlewares)
