from __future__ import annotations

from functools import partial
from typing import Callable, List, Optional

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimeDuration, TimePoint


class _ServiceDependencyMixin:
    # Severity ranking: higher value = more severe
    _DROP_SEVERITY = {
        DropReason.NONE: 0,
        DropReason.QUEUE_FULL: 1,
        DropReason.SERVER_FAILURE: 2,
        DropReason.DEADLINE: 3,
    }

    def _call_deps_parallel(
        self,
        sim: Simulator,
        deps: List["ServiceRuntime"],
        optionality: List[bool],
        on_all_done: Callable[[bool, DropReason], None],
        deadline: Optional[TimePoint],
        is_retry: bool,
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
    ):
        """Call all dependencies concurrently, barrier-wait for all to complete."""
        remaining = [len(deps)]
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
                if self._DROP_SEVERITY.get(drop_reason, 0) > self._DROP_SEVERITY.get(worst_reason[0], 0):
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
                retry_budget_remaining=retry_budget_remaining,
                tenant_id=tenant_id,
            )

    def _call_deps_sequential(
        self,
        sim: Simulator,
        deps: List["ServiceRuntime"],
        optionality: List[bool],
        idx: int,
        on_all_done: Callable[[bool, DropReason], None],
        deadline: Optional[TimePoint],
        is_retry: bool,
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
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
                on_all_done(False, drop_reason)
                return
            self._call_deps_sequential(
                sim, deps, optionality, idx + 1, on_all_done, deadline, is_retry,
                retry_budget_remaining=retry_budget_remaining,
                tenant_id=tenant_id,
            )

        deps[idx].submit_request(
            sim,
            on_attempt_done=on_dep_done,
            on_root_done=lambda: None,
            global_deadline=deadline,
            is_retry=is_retry,
            retry_budget_remaining=retry_budget_remaining,
            tenant_id=tenant_id,
        )
