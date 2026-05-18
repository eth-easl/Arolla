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
from simulator.policies.timeout import Timeout


@dataclass(frozen=True)
class ClientConfig:
    name: str = "client"
    retry: Optional['RetryPolicy'] = None
    timeout: Optional['Timeout'] = None
    e2e_retry_budget: Optional[int] = None  # Arolla Level 2: max retries across all hops


@dataclass
class AttemptCtx:
    root: RootRequest
    req: Request
    last_delay: TimeDuration = 0
    total_delay: TimeDuration = 0
    _retry_budget_remaining: Optional[list] = None  # Arolla Level 2: shared [B]


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
        
        # Priority: Client config > Service config
        if self.cfg.timeout is not None:
             global_timeout = self.cfg.timeout.get_global_timeout()
        elif self.service.cfg.timeout is not None:
            global_timeout = self.service.cfg.timeout.get_global_timeout()

        if global_timeout is not None:
            root_req.global_deadline = sim.timestep + global_timeout

        dummy_req = Request(
            service="",
            interval=TimeInterval(begin=sim.timestep, end=sim.timestep),
            deadline=None,
        )
        ctx = AttemptCtx(
            root=root_req, req=dummy_req, last_delay=0, total_delay=0,
            _retry_budget_remaining=(
                [self.cfg.e2e_retry_budget] if self.cfg.e2e_retry_budget is not None else None
            ),
        )

        # Trigger the first attempt
        self._make_attempt(sim, ctx, is_retry=False)

    def _make_attempt(self, sim: Simulator, ctx: AttemptCtx, is_retry: bool):
        # Update request interval for this specific attempt
        # (Start time is now)
        # Note: We create a dummy request object here just to signal intent, 
        # but the Service will create the actual context.
        # However, for our own tracking in _on_attempt_done_from_service, we'll verify timings.
        
        on_attempt_done = partial(self._on_attempt_done_from_service, ctx, sim)
        
        # Determine who manages root lifecycle
        if self.cfg.retry is None:
            # Legacy/Simple Mode: We trust the Service to tell us when Root is done.
            on_root_done = partial(self._on_root_done, ctx)
        else:
            # Modern Mode: Client manages retries.
            on_root_done = lambda: None
        
        # Schedule Local Timeout (Projected "Walking Away")
        # If client has a timeout configured, we schedule an event to fire at deadline.
        attempt_timeout = None
        if self.cfg.timeout:
            at = self.cfg.timeout.get_attempt_timeout()
            if at:
                attempt_timeout = sim.timestep + at
        
        attempt_id = len(ctx.root.attempts)
        attempt_begin_time = sim.timestep

        if attempt_timeout:
             sim.schedule(
                 attempt_timeout,
                 partial(self._on_local_timeout, ctx, sim, attempt_id, attempt_begin_time)
             )

        # Submit to service
        self.service.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            global_deadline=ctx.root.global_deadline,
            is_retry=is_retry,
            retry_budget_remaining=ctx._retry_budget_remaining,
            tenant_id=self.cfg.name,
        )

    def _on_local_timeout(self, ctx: AttemptCtx, sim: Simulator, attempt_id: int, attempt_begin_time: TimePoint):
        """Handler for client-side local timeout (active interrupt)"""
        if ctx.root.done:
            return
        if len(ctx.root.attempts) > attempt_id:
            # Attempt already completed via service callback
            return

        # Attempt is still in-flight at the service (zombie).
        # Fail it locally using the actual begin time for accurate metrics.
        self._on_attempt_done_from_service(
            ctx, sim,
            success=False,
            svc_time=0,
            drop_reason=DropReason.DEADLINE,
            queue_size=-1,
            begin_time=attempt_begin_time,
            attempt_deadline=sim.timestep,
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
        # If the root is already done (e.g. by local timeout or earlier callback),
        # ignore this late response to prevent duplicate processing.
        if ctx.root.done:
            return

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

        # --------------------------------------------------------------------
        # Client-Side Retry Logic
        # --------------------------------------------------------------------
        
        # If success, we are done (unless we want to verify something?)
        if success:
            if self.cfg.retry:
                retry_ctx = RetryContext(
                    attempt=len(ctx.root.attempts),
                    now=sim.timestep
                )
                self.cfg.retry.record_attempt(retry_ctx, True)
                self._on_root_done(ctx)
            else:
                # Legacy Mode: If success, we just wait for on_root_done from Service.
                # Do NOT call self._on_root_done(ctx) here.
                pass
            return
            
        # If failure, check policy
        if self.cfg.retry is None:
            # No retry policy -> Legacy Mode.
            # We fail/retry based entirely on Service's logic.
            # So we do NOTHING here. If Service is done, it calls on_root_done.
            # If Service retries, it calls nothing yet.
            return

        # Prepare context for policy
        retry_ctx = RetryContext(
             attempt=len(ctx.root.attempts), # 1-based count because we just added the failed attempt
             now=sim.timestep
        )
        
        if self.cfg.retry:
            self.cfg.retry.record_attempt(retry_ctx, success)
        
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
