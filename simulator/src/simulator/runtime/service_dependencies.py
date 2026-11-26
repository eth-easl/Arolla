from __future__ import annotations

from functools import partial
from typing import Callable, List, Optional

from simulator.core.engine import Simulator
from simulator.core.types import DropReason, TimePoint

from .service_attempts import CallResult


class _ServiceDependencyMixin:

    # Severity ranking: higher value = more severe
    _DROP_SEVERITY = {
        DropReason.NONE: 0,
        DropReason.QUEUE_FULL: 1,
        DropReason.SERVER_FAILURE: 2,
        DropReason.DEADLINE: 3,
    }

    def _call_dependencies(
        self,
        sim: Simulator,
        on_all_done: Callable[[bool, DropReason], None],
        deadline: Optional[TimePoint],
        is_retry: bool,
        retry_budget_remaining: Optional[list] = None,
        tenant_id: Optional[str] = None,
    ):
        """Call the service's dependencies, all at once or one after another."""
        gave_up = None
        if not self.propagate_deadline and deadline is not None:
            on_all_done, gave_up = self._watch_own_deadline(sim, deadline, on_all_done)
            deadline = None

        if self.dependency_call_pattern == "parallel":
            self._call_deps_parallel(
                sim, self.dependencies, self.dependency_optionality, on_all_done, deadline, is_retry,
                retry_budget_remaining=retry_budget_remaining, tenant_id=tenant_id,
            )
        else:
            self._call_deps_sequential(
                sim, self.dependencies, self.dependency_optionality, 0, on_all_done, deadline, is_retry,
                retry_budget_remaining=retry_budget_remaining, tenant_id=tenant_id, gave_up=gave_up,
            )

    def _watch_own_deadline(
        self,
        sim: Simulator,
        deadline: TimePoint,
        on_all_done: Callable[[bool, DropReason], None],
    ):
        """For a service that does not pass its deadline on to its dependencies."""
        state = "waiting"

        def give_up():
            nonlocal state
            if state == "waiting":
                state = "gave_up"
                on_all_done(False, DropReason.DEADLINE)

        def on_answer(all_success: bool, reason: DropReason):
            nonlocal state
            if state == "waiting":
                state = "answered"
                on_all_done(all_success, reason)

        sim.schedule(deadline, give_up)
        return on_answer, lambda: state == "gave_up"

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
        """Call all dependencies concurrently. Report once all of them are done, or,
        with `dependency_fail_fast`, as soon as a required one has failed."""
        remaining = len(deps)
        any_required_failed = False
        worst_reason = DropReason.NONE
        reported = False

        def on_dep_done(dep_idx: int, result: CallResult):
            nonlocal remaining, any_required_failed, worst_reason, reported
            remaining -= 1
            is_optional = optionality[dep_idx] if dep_idx < len(optionality) else False
            if not result.success and not is_optional:
                any_required_failed = True
                if self._DROP_SEVERITY.get(result.drop_reason, 0) > self._DROP_SEVERITY.get(worst_reason, 0):
                    worst_reason = result.drop_reason
            if reported:
                return
            if remaining == 0 or (self.dependency_fail_fast and any_required_failed):
                reported = True
                on_all_done(not any_required_failed, worst_reason)

        for i, dep in enumerate(deps):
            dep.submit_call(
                sim,
                partial(on_dep_done, i),
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
        gave_up: Optional[Callable[[], bool]] = None,
    ):
        """Call dependencies one after another. Stop at the first required one that fails."""
        if idx >= len(deps):
            on_all_done(True, DropReason.NONE)
            return

        def on_dep_done(result: CallResult):
            if gave_up is not None and gave_up():
                return
            is_optional = optionality[idx] if idx < len(optionality) else False
            if not result.success and not is_optional:
                on_all_done(False, result.drop_reason)
                return
            sim.schedule(sim.timestep, partial(
                self._call_deps_sequential,
                sim, deps, optionality, idx + 1, on_all_done, deadline, is_retry,
                retry_budget_remaining=retry_budget_remaining,
                tenant_id=tenant_id,
                gave_up=gave_up,
            ))

        deps[idx].submit_call(
            sim,
            on_dep_done,
            global_deadline=deadline,
            is_retry=is_retry,
            retry_budget_remaining=retry_budget_remaining,
            tenant_id=tenant_id,
        )
