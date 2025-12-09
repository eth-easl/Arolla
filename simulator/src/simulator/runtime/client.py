from dataclasses import dataclass, field
from functools import partial
from typing import List, Optional

from simulator.core.types import TimePoint, TimeDuration, DropReason
from simulator.core.models import Request, RootRequest, TimeInterval
from simulator.utils.time import s_to_ns
from simulator.core.engine import Simulator
from simulator.runtime.service import ServiceRuntime
from simulator.metrics.collector import Metrics


@dataclass(frozen=True)
class ClientConfig:
    passme: str = "client"


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

        on_attempt_done = partial(self._on_attempt_done_from_service, ctx, sim)
        on_root_done = partial(self._on_root_done, ctx)

        # Call service once
        self.service.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            global_deadline=root_req.global_deadline,
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

    def _on_root_done(self, ctx: AttemptCtx):
        if not ctx.root.done:
            ctx.root.done = True
            self.roots.append(ctx.root)

    def metrics(self) -> Metrics:
        return Metrics(roots=self.roots, attempts_total=self.attempts_total)
