from __future__ import annotations

from functools import partial

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint
from simulator.middleware.base import AttemptContext, MiddlewareChain
from simulator.middleware.load_limiter import LoadLimiterMiddleware
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
        )

        if self._middleware_chain is None:
            self._middleware_chain = self._build_middleware_chain()

        chain = self._middleware_chain

        def final_handler(a_ctx: AttemptContext):
            if a_ctx.is_successful or not a_ctx.should_retry:
                ctx.on_root_done()
            else:
                next_start = end_time + a_ctx.retry_delay
                sim.schedule(next_start, partial(self._start_attempt, sim, ctx))

        chain.execute(attempt_ctx, final_handler)

    def _build_middleware_chain(self) -> MiddlewareChain:
        middlewares = []
        if self.cfg.retry is not None:
            middlewares.append(RetryMiddleware(self.cfg.retry))
        else:
            middlewares.append(NoRetryMiddleware())

        if self.cfg.load_limiter is not None:
            middlewares.append(LoadLimiterMiddleware(self.cfg.load_limiter))

        return MiddlewareChain(middlewares)
