from dataclasses import dataclass, field
from functools import partial
from typing import List, Optional

from simulator.core.types import TimePoint, TimeDuration, DropReason
from simulator.core.models import Request, RootRequest, TimeInterval
from simulator.utils.time import s_to_ns
from simulator.core.engine import Simulator
from simulator.runtime.service import ServiceRuntime
from simulator.metrics.collector import Metrics
from simulator.policies.retry import RetryPolicy, RetryContext


@dataclass(frozen=True)
class ClientConfig:
    name: str = "client"
    retry: Optional['RetryPolicy'] = None


@dataclass
class AttemptCtx:
    root: RootRequest
    req: Request
    last_delay: TimeDuration = 0
    total_delay: TimeDuration = 0


@dataclass
class ClientRuntime:
    """
    Client runtime that generates requests and tracks results.
    
    The client runtime is responsible for initiating requests to a service,
    managing retries (delegated to the service), and collecting metrics about
    all completed requests.
    
    Attributes:
        cfg: Client configuration
        service: The service to send requests to
        roots: List of all completed root requests (for metrics)
        attempts_total: Total number of attempts made across all requests
    
    Methods:
        start_request: Initiate a new request to the service
        metrics: Get metrics for all completed requests
    """
    cfg: ClientConfig
    service: ServiceRuntime

    roots: List[RootRequest] = field(default_factory=list)
    attempts_total: int = 0

    def start_request(self, sim: Simulator):
        root_req = RootRequest()

        global_timeout = None
        if self.service.cfg.timeout is not None:
            global_timeout = self.service.cfg.timeout.get_global_timeout()

        if global_timeout is not None:
            root_req.global_deadline = sim.timestep + global_timeout

        dummy_req = Request(
            service="",
            interval=TimeInterval(begin=sim.timestep, end=sim.timestep),
            deadline=None,
        )
        ctx = AttemptCtx(root=root_req, req=dummy_req, last_delay=0, total_delay=0)

        # Trigger the first attempt
        self._make_attempt(sim, ctx, is_retry=False)

    def _make_attempt(self, sim: Simulator, ctx: AttemptCtx, is_retry: bool):
        # Update request interval for this specific attempt
        # (Start time is now)
        # Note: We create a dummy request object here just to signal intent, 
        # but the Service will create the actual context.
        # However, for our own tracking in _on_attempt_done_from_service, we'll verify timings.
        
        on_attempt_done = partial(self._on_attempt_done_from_service, ctx, sim)
        
        # Pass no-op for on_root_done because WE manage the root lifecycle now.
        # The Service calls on_root_done when IT thinks the request is finished (e.g. after 1 try).
        on_root_done = lambda: None
        
        self.service.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            global_deadline=ctx.root.global_deadline,
        )

    def _on_attempt_done_from_service(
        self,
        ctx: AttemptCtx,
        sim: Simulator,
        success: bool,
        svc_time: TimeDuration,
        drop_reason: DropReason,
        queue_size: int,
        begin_time: TimePoint,
        attempt_deadline: Optional[TimePoint],
    ):
        if ctx.root.done:
            raise RuntimeError("Root request was already done!")

        self.attempts_total += 1
        req = Request(
            service=self.service.cfg.name,
            interval=TimeInterval(begin=begin_time, end=sim.timestep),
            deadline=attempt_deadline,
        )
        req.success = success
        req.drop_reason = drop_reason
        req.queue_size_at_end = queue_size

        ctx.req = req
        ctx.root.add_attempt(req)

        # Record latency to timeout controller (for CSV parity)
        request_latency = req.interval.end - req.interval.begin
        if self.service.cfg.timeout is not None:
            self.service.cfg.timeout.record_result(success, request_latency)
            self.service.cfg.timeout.record_result(success, request_latency)

        # --------------------------------------------------------------------
        # Client-Side Retry Logic
        # --------------------------------------------------------------------
        
        # If success, we are done
        if success:
            self._on_root_done(ctx)
            return
            
        # If failure, check policy
        if self.cfg.retry is None:
            # No retry policy -> Fail immediately
            self._on_root_done(ctx)
            return

        # Prepare context for policy
        retry_ctx = RetryContext(
             attempt=len(ctx.root.attempts), # 1-based count because we just added the failed attempt
             now=sim.timestep
        )
        
        should_retry, delay = self.cfg.retry.next_delay(retry_ctx)
        
        if should_retry:
            # Check global deadline
            next_start = sim.timestep + delay
            if ctx.root.global_deadline is not None and next_start >= ctx.root.global_deadline:
                # Deadline exceeded -> give up
                self._on_root_done(ctx)
                return
            
            # Schedule retry
            sim.schedule(next_start, partial(self._make_attempt, sim, ctx, is_retry=True))
        else:
            # Policy says stop
            self._on_root_done(ctx)

    def _on_root_done(self, ctx: AttemptCtx):
        if not ctx.root.done:
            ctx.root.done = True
            self.roots.append(ctx.root)

    def metrics(self) -> Metrics:
        return Metrics(roots=self.roots, attempts_total=self.attempts_total)
