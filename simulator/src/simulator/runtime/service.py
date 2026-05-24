from __future__ import annotations
import heapq
import math
import random
from typing import List, Optional, Callable
from dataclasses import dataclass, field
from functools import partial

from simulator.core.types import TimePoint, TimeDuration, DropReason, MIN_SERVICE_TIME_NS
from simulator.faults.injection import LatencyInjection, PartialFailure
from simulator.core.engine import Simulator
from simulator.faults.events import FaultEventsTracker

from simulator.policies.retry import RetryPolicy, RetryContext
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.timeout import Timeout

from simulator.middleware.base import AttemptContext, MiddlewareChain
from simulator.middleware.retry import RetryMiddleware, NoRetryMiddleware
from simulator.middleware.load_limiter import LoadLimiterMiddleware
from simulator.policies.aimd_retry_budget import AIMDGlobalRetryBudget
from simulator.policies.retry import RetryBudgetPolicy
from simulator.policies.server_retry_budget import GlobalRetryBudget
from simulator.policies.istio_retry_budget import IstioRetryBudget

# RL
from simulator.metrics.live_buffer import LiveMetricsBuffer
from simulator.policies.retry import FixedBackoffRetryPolicy
from simulator.policies.load_limiter import LimiterRetryBudgetPolicy

@dataclass(frozen=True)
class ServiceConfig:
    name: str

    latency_median: TimeDuration
    latency_lognorm_sigma: float  # sigma of the underlying lognormal (shape)
    workers: int
    queue_capacity: Optional[int] = None  # None means unbounded
    latency_injections: List[LatencyInjection] = field(default_factory=list)
    partial_failures: List[PartialFailure] = field(default_factory=list)

    load_limiter: Optional[LoadLimiter] = None
    timeout: Optional[Timeout] = None
    retry: Optional[RetryPolicy] = None
    
    # NOTE: Removed __post_init__ hack that violated frozen=True.
    # Middleware pattern now handles policy composition cleanly.

    def register_fault_events(self, tracker: FaultEventsTracker):
        """Register all fault events with the tracker"""
        for latency_inj in self.latency_injections:
            tracker.add_latency_injection(
                latency_inj.duration,
                latency_inj.add_latency,
                latency_inj.multiplier
            )
        for partial_failure in self.partial_failures:
            tracker.add_partial_failure(
                partial_failure.duration,
                partial_failure.p_fail
            )


@dataclass(order=True)
class QItem:
    enqueued_at: TimePoint
    seq: int
    start: Callable[[], None] = field(compare=False)
    is_retry: bool = field(default=False, compare=False)


@dataclass
class _SrvRetryCtx:
    attempt: int
    external_is_retry: bool
    global_deadline: Optional[TimePoint]
    on_attempt_done: Callable[
        [bool, TimeDuration, DropReason, int, TimePoint, Optional[TimePoint]], None
    ]
    on_root_done: Callable[[], None]


@dataclass
class ServiceRuntime:
    cfg: ServiceConfig
    in_flight: int = 0
    in_flight_retries: int = 0
    queued_retries: int = 0
    queue: List[QItem] = field(default_factory=list)
    _seq: int = 0

    # Multi-dependency support (replaces old single 'dependency')
    dependencies: List[ServiceRuntime] = field(default_factory=list)
    dependency_optionality: List[bool] = field(default_factory=list)  # per-dep: True = optional
    dependency_call_pattern: str = "sequential"  # "sequential" or "parallel"

    # Legacy single-dependency property for backward compat
    @property
    def dependency(self) -> Optional['ServiceRuntime']:
        return self.dependencies[0] if self.dependencies else None

    _middleware_chain: Optional[MiddlewareChain] = field(default=None, init=False, repr=False)
    _rng: Optional[random.Random] = field(default=None, init=False, repr=False)

    # Per-service event recording (lightweight telemetry)
    _events: List[tuple] = field(default_factory=list, init=False, repr=False)
    _record_events: bool = field(default=False, init=False, repr=False)

    # Per-service live metrics buffer for real-time aggregation
    _live_buffer: Optional[LiveMetricsBuffer] = field(default=None, init=False, repr=False)

    def bind(self, seed: Optional[int] = None, record_events: bool = False, enable_live_buffer: bool = False):
        self.in_flight = 0
        self.in_flight_retries = 0
        self.queued_retries = 0
        self.queue.clear()
        self._seq = 0
        self._middleware_chain = self._build_middleware_chain()
        self._record_events = record_events
        self._events = []

        self._live_buffer = LiveMetricsBuffer() if enable_live_buffer else None
        
        initial_seed = seed if seed is not None else 0
        self._rng = random.Random(initial_seed)
        
        return self

    def _refresh_retry_budget_runtime_state(self) -> None:
        """Expose current service concurrency to Istio-style retry budgets."""
        limiter = self.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            limiter.update_runtime_state(
                active_requests=self.in_flight,
                pending_requests=len(self.queue),
                active_retries=self.in_flight_retries + self.queued_retries,
            )
    
    @property
    def events(self) -> List[tuple]:
        """Return recorded events: (timestamp_ns, latency_ns, success, drop_reason, queue_size, attempt_num, is_retry)"""
        return self._events

    @property
    def live_buffer(self) -> Optional[LiveMetricsBuffer]:
        return self._live_buffer

    def enable_live_buffer(self) -> None:
        """Activate the live metrics buffer (used by RL env). No-op if already active."""
        if self._live_buffer is None:
            self._live_buffer = LiveMetricsBuffer()

    def _record_attempt_metrics(
        self,
        timestamp_ns: TimePoint,
        latency_ns: TimeDuration,
        success: bool,
        drop_reason: DropReason,
        queue_size: int,
        attempt_num: int,
        is_retry: bool,
    ) -> None:
        """Record a finished or admission-rejected attempt into service telemetry."""
        if self._record_events:
            self._events.append((
                timestamp_ns,
                latency_ns,
                success,
                drop_reason,
                queue_size,
                attempt_num,
                is_retry,
            ))

        if self._live_buffer is not None:
            self._live_buffer.record_event(
                timestamp_ns,
                latency_ns,
                success,
                drop_reason,
                queue_size,
                attempt_num,
                is_retry,
            )

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
        # Admission control for client-managed retries only. Service-managed
        # retries are already gated by the middleware chain after each attempt.
        if self.cfg.load_limiter is not None and self.cfg.retry is None:
            should_check = False
            # Check if limiter applies to this request
            if is_retry:
                # Retry budgets always check retries
                if isinstance(self.cfg.load_limiter, (AIMDGlobalRetryBudget, GlobalRetryBudget, RetryBudgetPolicy, IstioRetryBudget)):
                    should_check = True
            
            # Rate limiters check EVERYTHING (not just retries) - assuming generic RateLimiter logic if needed
            # For now, we only focus on the requested AIMD/Budget behavior.
            
            if should_check:
                # Create context for check (attempt 1 is fine as placeholder, budget policies ignore it usually)
                self._refresh_retry_budget_runtime_state()
                check_ctx = RetryContext(attempt=1, now=sim.timestep)
                allowed, _ = self.cfg.load_limiter.next_delay(check_ctx)
                if isinstance(self.cfg.load_limiter, IstioRetryBudget):
                    self.cfg.load_limiter.record_retry_admission(
                        admitted=allowed,
                        now_ns=sim.timestep,
                    )

                if not allowed:
                    self._record_attempt_metrics(
                        timestamp_ns=sim.timestep,
                        latency_ns=0,
                        success=False,
                        drop_reason=DropReason.SERVER_FAILURE,
                        queue_size=len(self.queue),
                        attempt_num=2 if is_retry else 1,
                        is_retry=is_retry,
                    )
                    # Reject admission
                    on_attempt_done(
                        False, 0, DropReason.SERVER_FAILURE, len(self.queue), sim.timestep, None
                    )
                    # We treat this as a quick failure. 
                    # Note: We do NOT call on_root_done here because the caller (Client or Upstream) 
                    # expects on_attempt_done to fire, and THEY handle root completion or retries.
                    return

        ctx = _SrvRetryCtx(
            attempt=0,
            external_is_retry=is_retry,
            global_deadline=global_deadline,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
        )
        self._start_attempt(sim, ctx)

    def _attempt_deadline(self, now: TimePoint) -> Optional[TimePoint]:
        if self.cfg.timeout is None:
            return None
        at = self.cfg.timeout.get_attempt_timeout()
        if at is None:
            return None
        return now + at

    def _min_deadline(
        self, d1: Optional[TimePoint], d2: Optional[TimePoint]
    ) -> Optional[TimePoint]:
        if d1 is None:
            return d2
        if d2 is None:
            return d1
        return min(d1, d2)

    def _start_attempt(self, sim: Simulator, ctx: _SrvRetryCtx):
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
        # A retry can be generated inside the service middleware, or arrive as a
        # client-managed retry submitted as a new service request.
        is_retry = ctx.external_is_retry or ctx.attempt > 1
        self.submit_attempt(sim, on_done, attempt_deadline=attempt_deadline, is_retry=is_retry)

    def _adjust_latency(self, t: TimePoint, base: TimeDuration) -> TimeDuration:
        mult: int = 1
        add: int = 0
        for inj in self.cfg.latency_injections:
            if inj.active(t):
                mult *= inj.multiplier
                add += inj.add_latency
        return max(0, base * mult + add)

    def _sample_service_time(self, t: TimePoint, sim: Simulator) -> TimeDuration:
        """
        Lognormal with given median (exp(mu)) and sigma.
        If median=0, fall back to small constant.
        """
        # ISO-FIX: Use PRIVATE isolated RNG, not global sim.rng()
        rng = self._rng if self._rng else sim.rng()
        
        median = max(1, self.cfg.latency_median)
        sigma = max(1e-6, self.cfg.latency_lognorm_sigma)
        mu = math.log(median)  # since median = exp(mu)
        raw = rng.lognormvariate(mu, sigma)
        adj = self._adjust_latency(t, int(raw))
        return max(MIN_SERVICE_TIME_NS, adj)

    def _fails_now(self, t: TimePoint, sim: Simulator) -> bool:
        # ISO-FIX: Use PRIVATE isolated RNG, not global sim.rng()
        rng = self._rng if self._rng else sim.rng()
        
        p_fail = 0.0
        for pf in self.cfg.partial_failures:
            if pf.active(t):
                p_fail = max(p_fail, pf.p_fail)
        return rng.random() < p_fail

    def _start_next(self, sim: Simulator):
        while self.in_flight < self.cfg.workers and self.queue:
            item = heapq.heappop(self.queue)
            if item.is_retry:
                self.queued_retries = max(0, self.queued_retries - 1)
            item.start()

    def submit_attempt(
        self,
        sim: Simulator,
        on_done: Callable[
            [bool, TimeDuration, DropReason, int], None
        ],  # success, service_time, whydropped, queue_size
        attempt_deadline: Optional[TimePoint] = None,
        is_retry: bool = False,
    ):
        """
        Enqueue an attempt for this service.
        Execution is FCFS with 'workers' concurrency.
        """

        self._seq += 1
        seq = self._seq

        # Immediately drop the request if somehow it has already expired?
        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, 0, DropReason.DEADLINE, len(self.queue))
            raise RuntimeError("Attempt already expired at submission time")

        start_cb = partial(self._begin_service, sim, on_done, attempt_deadline, is_retry)

        if self.in_flight < self.cfg.workers:
            start_cb()  # Immediately start handling if we have free worker
            return

        # Drop request if queue is full (load shedding)
        if (
            self.cfg.queue_capacity is not None
            and len(self.queue) >= self.cfg.queue_capacity
        ):
            on_done(False, 0, DropReason.QUEUE_FULL, len(self.queue))  # dropped from queue
            return

        # Enqueue the request
        heapq.heappush(
            self.queue,
            QItem(
                enqueued_at=sim.timestep,
                seq=seq,
                start=start_cb,
                is_retry=is_retry,
            ),
        )
        if is_retry:
            self.queued_retries += 1

    # begin_service can be called either immediately or pass request
    def _begin_service(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        attempt_deadline: Optional[TimePoint],
        is_retry: bool,
    ):
        # If the attempt expired while waiting in the queue, drop immediately without
        # consuming a worker. We make sure we are not scheduling an event in the past.
        if attempt_deadline is not None and sim.timestep >= attempt_deadline:
            on_done(False, 0, DropReason.DEADLINE, len(self.queue))
            return

        self.in_flight += 1
        if is_retry:
            self.in_flight_retries += 1

        if self.dependencies:
            # Multi-dependency fan-out
            start_t = sim.timestep

            def on_all_deps_done(all_success: bool, worst_reason: DropReason):
                """Called when all dependencies have completed."""
                if not all_success:
                    # Dependencies failed — report failure immediately, no own processing
                    total_time = sim.timestep - start_t
                    self.in_flight -= 1
                    if is_retry:
                        self.in_flight_retries = max(0, self.in_flight_retries - 1)
                    on_done(False, total_time, worst_reason, len(self.queue))
                    self._start_next(sim)
                    return

                # Dependencies succeeded — now simulate this service's own processing time
                service_time = self._sample_service_time(sim.timestep, sim)

                # Respect deadline
                expiry = (
                    min(sim.timestep + service_time, attempt_deadline)
                    if attempt_deadline is not None
                    else sim.timestep + service_time
                )

                def finish_after_deps():
                    total_time = sim.timestep - start_t
                    local_failed = self._fails_now(sim.timestep, sim)
                    timed_out = attempt_deadline is not None and sim.timestep >= attempt_deadline

                    if timed_out:
                        self.in_flight -= 1
                        if is_retry:
                            self.in_flight_retries = max(0, self.in_flight_retries - 1)
                        on_done(False, total_time, DropReason.DEADLINE, len(self.queue))
                    elif local_failed:
                        self.in_flight -= 1
                        if is_retry:
                            self.in_flight_retries = max(0, self.in_flight_retries - 1)
                        on_done(False, total_time, DropReason.SERVER_FAILURE, len(self.queue))
                    else:
                        self.in_flight -= 1
                        if is_retry:
                            self.in_flight_retries = max(0, self.in_flight_retries - 1)
                        on_done(True, total_time, DropReason.NONE, len(self.queue))
                    self._start_next(sim)

                sim.schedule(expiry, finish_after_deps)

            if self.dependency_call_pattern == "parallel":
                self._call_deps_parallel(
                    sim, self.dependencies, self.dependency_optionality,
                    on_all_deps_done, attempt_deadline, is_retry
                )
            else:  # sequential (default)
                self._call_deps_sequential(
                    sim, self.dependencies, self.dependency_optionality, 0,
                    on_all_deps_done, attempt_deadline, is_retry
                )
            return

        service_time = self._sample_service_time(sim.timestep, sim)

        # Deadline propagation: if the request is interrupted throughout the service time
        # then we interrupt it and drop the request.
        # Note: When deadline is propagated, effective service time may be less than sampled time
        expiry = (
            min(sim.timestep + service_time, attempt_deadline)
            if attempt_deadline is not None
            else sim.timestep + service_time
        )

        finish_cb = partial(
            self._finish_service, sim, on_done, service_time, attempt_deadline, is_retry
        )
        sim.schedule(expiry, finish_cb)

    def _call_deps_parallel(
        self,
        sim: Simulator,
        deps: List['ServiceRuntime'],
        optionality: List[bool],
        on_all_done: Callable[[bool, DropReason], None],
        deadline: Optional[TimePoint],
        is_retry: bool,
    ):
        """Call all dependencies concurrently, barrier-wait for all to complete."""
        remaining = [len(deps)]  # mutable counter
        any_required_failed = [False]
        worst_reason = [DropReason.NONE]

        def on_dep_done(
            dep_idx: int,
            success: bool,
            svc_time: TimeDuration,
            drop_reason: DropReason,
            queue_size: int,
            _begin: TimePoint,
            _deadline: Optional[TimePoint],
        ):
            remaining[0] -= 1
            is_optional = optionality[dep_idx] if dep_idx < len(optionality) else False
            if not success and not is_optional:
                any_required_failed[0] = True
                worst_reason[0] = drop_reason
            if remaining[0] == 0:
                on_all_done(not any_required_failed[0], worst_reason[0])

        for i, dep in enumerate(deps):
            dep.submit_request(
                sim,
                on_attempt_done=partial(on_dep_done, i),
                on_root_done=lambda: None,
                global_deadline=deadline,
                is_retry=is_retry,
            )

    def _call_deps_sequential(
        self,
        sim: Simulator,
        deps: List['ServiceRuntime'],
        optionality: List[bool],
        idx: int,
        on_all_done: Callable[[bool, DropReason], None],
        deadline: Optional[TimePoint],
        is_retry: bool,
    ):
        """Call dependencies one after another. Short-circuit on required failure."""
        if idx >= len(deps):
            on_all_done(True, DropReason.NONE)
            return

        def on_dep_done(
            success: bool,
            svc_time: TimeDuration,
            drop_reason: DropReason,
            queue_size: int,
            _begin: TimePoint,
            _deadline: Optional[TimePoint],
        ):
            is_optional = optionality[idx] if idx < len(optionality) else False
            if not success and not is_optional:
                # Required dep failed — short-circuit
                on_all_done(False, drop_reason)
                return
            # Continue to next dependency
            self._call_deps_sequential(
                sim, deps, optionality, idx + 1,
                on_all_done, deadline, is_retry
            )

        deps[idx].submit_request(
            sim,
            on_attempt_done=on_dep_done,
            on_root_done=lambda: None,
            global_deadline=deadline,
            is_retry=is_retry,
        )

    def _finish_service(
        self,
        sim: Simulator,
        on_done: Callable[[bool, TimeDuration, DropReason, int], None],
        service_time: TimeDuration,  # Time spent handling the request, excluding any queuing
        attempt_deadline: Optional[TimePoint],
        is_retry: bool,
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

    def _on_single_attempt_done(
        self,
        sim: Simulator,
        ctx: _SrvRetryCtx,
        begin_time: TimePoint,
        attempt_deadline: Optional[TimePoint],
        success: bool,
        svc_time: TimeDuration,
        drop_reason: DropReason,
        queue_size: int,
    ):
        """
        Handle completion of a single attempt.
        
        This method now uses middleware pattern instead of embedding
        retry/timeout/load limiting logic directly.
        """
        end_time = sim.timestep
        
        self._record_attempt_metrics(
            timestamp_ns=end_time,
            latency_ns=svc_time,
            success=success,
            drop_reason=drop_reason,
            queue_size=queue_size,
            attempt_num=ctx.attempt,
            is_retry=ctx.external_is_retry or ctx.attempt > 1,
        )
        
        # Notify caller about attempt completion
        ctx.on_attempt_done(
            success,
            svc_time,
            drop_reason,
            queue_size,
            begin_time,
            attempt_deadline,
        )
        
        # Build attempt context for middleware
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

        # Build middleware chain from config (cached in bind())
        if self._middleware_chain is None:
            self._middleware_chain = self._build_middleware_chain()

        # Execute middleware chain for ALL outcomes (success and failure).
        # The chain handles load limiter state tracking and retry decisions.
        self._refresh_retry_budget_runtime_state()
        chain = self._middleware_chain

        def final_handler(attempt_ctx: AttemptContext):
            """Final handler after middleware chain"""
            if attempt_ctx.is_successful or not attempt_ctx.should_retry:
                # Success or no retry -> mark as done
                ctx.on_root_done()
            else:
                # Schedule retry
                next_start = end_time + attempt_ctx.retry_delay
                sim.schedule(next_start, partial(self._start_attempt, sim, ctx))

        chain.execute(attempt_ctx, final_handler)
    
    def _build_middleware_chain(self) -> MiddlewareChain:
        """Build middleware chain from service configuration"""
        middlewares = []
        
        # Add retry middleware if configured
        if self.cfg.retry is not None:
            middlewares.append(RetryMiddleware(self.cfg.retry))
        else:
            middlewares.append(NoRetryMiddleware())
        
        # Add load limiter middleware if configured (runs after retry)
        if self.cfg.load_limiter is not None:
            middlewares.append(LoadLimiterMiddleware(self.cfg.load_limiter))
        
        return MiddlewareChain(middlewares)

    def update_retry_config(
    self,
    max_attempts: Optional[int] = None,
    delay_ns: Optional[TimeDuration] = None,
    budget_ratio: Optional[float] = None,
    budget_max_retries: Optional[int] = None,
    ):
        """Swap policy objects in the middleware chain at runtime."""
        if self._middleware_chain is None:
            return

        for mw in self._middleware_chain.middlewares:
            if isinstance(mw, RetryMiddleware) and (max_attempts is not None or delay_ns is not None):
                old = mw.policy
                if isinstance(old, FixedBackoffRetryPolicy):
                    mw.policy = FixedBackoffRetryPolicy(
                        max_attempts=max_attempts if max_attempts is not None else old.max_attempts,
                        delay=delay_ns if delay_ns is not None else old.delay,
                    )

            if isinstance(mw, LoadLimiterMiddleware) and (budget_ratio is not None or budget_max_retries is not None):
                old = mw.limiter
                if isinstance(old, LimiterRetryBudgetPolicy):
                    mw.limiter = LimiterRetryBudgetPolicy(
                        budget_ratio=budget_ratio if budget_ratio is not None else old.budget_ratio,
                        max_retries=budget_max_retries if budget_max_retries is not None else old.max_retries,
                    )

    def update_token_bucket(self, refill_rate=None, bucket_capacity=None):
        """Update the token bucket parameters at runtime"""
        
        limiter = self.cfg.load_limiter
        if isinstance(limiter, GlobalRetryBudget):
            if refill_rate is not None:
                limiter.refill_rate = refill_rate
            if bucket_capacity is not None:
                limiter.max_tokens = bucket_capacity
                limiter._tokens = min(limiter._tokens, float(bucket_capacity))

    def update_istio_retry_budget(self, percent=None, min_retry_concurrency=None):
        """Update Istio-style retry budget parameters at runtime."""
        limiter = self.cfg.load_limiter
        if isinstance(limiter, IstioRetryBudget):
            limiter.update_params(
                percent=percent,
                min_retry_concurrency=min_retry_concurrency,
            )
