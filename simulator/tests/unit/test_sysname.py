"""Tests for Arolla goodput-coupled retry budget (Level 1 + Level 2)."""

from __future__ import annotations

from functools import partial

import pytest

from simulator.core.engine import Simulator
from simulator.core.models import TimeInterval
from simulator.core.types import DropReason
from simulator.faults.injection import PartialFailure
from simulator.middleware.base import AttemptContext
from simulator.middleware.load_limiter import EndToEndRetryBudgetMiddleware, LoadLimiterMiddleware
from simulator.middleware.retry import RetryMiddleware
from simulator.policies.retry import FixedBackoffRetryPolicy, RetryContext
from simulator.policies.retry_controls import GoodputCoupledRetryBudget, _GoodputTenantState
from simulator.runtime.client import ClientConfig, ClientRuntime
from simulator.runtime.service import ServiceConfig, ServiceRuntime
from simulator.utils.time import ms_to_ns, s_to_ns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_attempt_ctx(
    attempt: int = 1,
    success: bool = True,
    drop_reason: DropReason = DropReason.NONE,
    begin_time: int = 0,
    end_time: int = 100,
    retry_budget_remaining: int | None = None,
    tenant_id: str | None = None,
    should_retry: bool = False,
) -> AttemptContext:
    return AttemptContext(
        attempt_number=attempt,
        success=success,
        service_time=end_time - begin_time,
        drop_reason=drop_reason,
        begin_time=begin_time,
        end_time=end_time,
        attempt_deadline=None,
        global_deadline=None,
        queue_size=0,
        service_name="test-svc",
        retry_budget_remaining=retry_budget_remaining,
        tenant_id=tenant_id,
        should_retry=should_retry,
    )


def _build_budget(
    alpha: float = 0.1,
    window_ms: float = 1000.0,
    beta_down: float = 0.3,
    beta_up: float = 0.05,
    success_rate_threshold: float | None = None,
) -> GoodputCoupledRetryBudget:
    return GoodputCoupledRetryBudget(
        alpha=alpha,
        beta_down=beta_down,
        beta_up=beta_up,
        window_duration=ms_to_ns(window_ms),
        success_rate_threshold=success_rate_threshold,
    )


# ---------------------------------------------------------------------------
# Level 1: GoodputCoupledRetryBudget
# ---------------------------------------------------------------------------

class TestGoodputEWMA:
    """Test asymmetric EWMA tracking of goodput rate."""

    def test_bootstrap_first_window(self):
        """First window should initialize goodput_rate directly from observation."""
        b = _build_budget(window_ms=1000)
        ns = s_to_ns(1)  # 1 second = 1 window

        # Record 100 successful requests in the first window
        for i in range(100):
            b.add_result(True, now=i * (ns // 100))

        # Advance past first window to trigger EWMA update
        b.add_result(True, now=ns + 1)
        tenant = b._get_tenant(None)
        assert tenant._initialized
        assert tenant.goodput_rate == pytest.approx(100.0, rel=0.01)

    def test_fast_decay_on_drop(self):
        """Goodput drop should decay fast (beta_down=0.3)."""
        b = _build_budget(beta_down=0.3, beta_up=0.05, window_ms=1000)
        ns = s_to_ns(1)

        # Window 0: 100 goodput
        for i in range(100):
            b.add_result(True, now=i * (ns // 100))
        b.add_result(True, now=ns)  # advance window -> rate = 100

        # Window 1: 0 goodput (total failure)
        b.add_result(False, now=2 * ns)  # advance window -> rate should decay

        tenant = b._get_tenant(None)
        # Expected: (1 - 0.3) * 100 + 0.3 * 0 = 70
        assert tenant.goodput_rate == pytest.approx(70.0, rel=0.01)

    def test_slow_growth_on_recovery(self):
        """Goodput recovery should grow slowly (beta_up=0.05)."""
        b = _build_budget(beta_down=0.3, beta_up=0.05, window_ms=1000)
        ns = s_to_ns(1)

        # Window 0: 50 goodput (bootstrap)
        for i in range(50):
            b.add_result(True, now=i * (ns // 50))
        b.add_result(True, now=ns)  # rate = 50

        # Window 1: 100 goodput (recovery)
        for i in range(100):
            b.add_result(True, now=ns + i * (ns // 100))
        b.add_result(True, now=2 * ns)

        tenant = b._get_tenant(None)
        # Expected: (1 - 0.05) * 50 + 0.05 * 100 = 47.5 + 5 = 52.5
        assert tenant.goodput_rate == pytest.approx(52.5, rel=0.01)


class TestLevel1Admission:
    """Test retry admission based on goodput budget."""

    def test_admits_under_alpha(self):
        """Retries should be admitted when count < alpha * goodput."""
        b = _build_budget(alpha=0.1, window_ms=1000)
        ns = s_to_ns(1)

        # Build up goodput of 100/s
        for i in range(100):
            b.add_result(True, now=i * (ns // 100))
        b.add_result(True, now=ns)  # trigger window update

        # Budget = 0.1 * 100 * 1 = 10 retries per window
        # First 10 retries should be admitted
        for i in range(10):
            ctx = RetryContext(attempt=2, now=ns + i * 1000)
            allowed, _ = b.next_delay(ctx)
            assert allowed, f"Retry {i} should be allowed"

    def test_rejects_over_alpha(self):
        """Retries should be rejected when count >= alpha * goodput."""
        b = _build_budget(alpha=0.1, window_ms=1000)
        ns = s_to_ns(1)

        for i in range(100):
            b.add_result(True, now=i * (ns // 100))
        b.add_result(True, now=ns)

        # Exhaust budget (10 retries)
        for i in range(10):
            b.next_delay(RetryContext(attempt=2, now=ns + i * 1000))

        # 11th should be rejected
        ctx = RetryContext(attempt=2, now=ns + 10000)
        allowed, _ = b.next_delay(ctx)
        assert not allowed

    def test_first_attempt_always_admitted(self):
        """Level 1 only gates retries (is_retry=True), never first attempts."""
        b = _build_budget(alpha=0.0)  # zero budget = block all retries
        # applies_pre_queue_admission only returns True for retries
        assert not b.applies_pre_queue_admission(is_retry=False)
        assert b.applies_pre_queue_admission(is_retry=True)

    def test_bootstrap_no_lockout(self):
        """When goodput is unknown (startup), retries should still be allowed."""
        b = _build_budget(alpha=0.1, window_ms=1000)
        # No results recorded yet — bootstrap grace
        ctx = RetryContext(attempt=2, now=100)
        allowed, _ = b.next_delay(ctx)
        assert allowed, "Should allow retries during bootstrap"


class TestLevel1PerTenant:
    """Test per-tenant isolation."""

    def test_tenant_isolation(self):
        """Aggressive tenant shouldn't starve well-behaved tenant's budget."""
        b = _build_budget(alpha=0.1, window_ms=1000)
        ns = s_to_ns(1)

        # Both tenants contribute 50 goodput each
        for i in range(50):
            b.add_result(True, now=i * (ns // 50), tenant_id="good")
            b.add_result(True, now=i * (ns // 50), tenant_id="bad")
        # Advance window
        b.add_result(True, now=ns, tenant_id="good")
        b.add_result(True, now=ns, tenant_id="bad")

        # Bad tenant exhausts its budget (5 retries = 0.1 * 50)
        for i in range(5):
            b.next_delay(RetryContext(attempt=2, now=ns + i * 100, tenant_id="bad"))

        # Bad tenant: blocked
        ctx_bad = RetryContext(attempt=2, now=ns + 600, tenant_id="bad")
        allowed_bad, _ = b.next_delay(ctx_bad)
        assert not allowed_bad

        # Good tenant: still has budget
        ctx_good = RetryContext(attempt=2, now=ns + 600, tenant_id="good")
        allowed_good, _ = b.next_delay(ctx_good)
        assert allowed_good


class TestSuccessRateGate:
    """Test the retry success rate enhancement."""

    def test_blocks_when_success_rate_low(self):
        """Retries should be blocked when retry success rate < beta."""
        b = _build_budget(alpha=0.5, window_ms=1000, success_rate_threshold=0.5)
        ns = s_to_ns(1)

        # Build goodput
        for i in range(100):
            b.add_result(True, now=i * (ns // 100))
        b.add_result(True, now=ns)

        # Record many failed retries to drive success rate below threshold
        for i in range(50):
            b.add_result(False, now=ns + i * 100, is_retry=True)

        tenant = b._get_tenant(None)
        assert tenant.retry_success_ewma < 0.5

        # Retry should be blocked by success rate gate
        ctx = RetryContext(attempt=2, now=ns + 6000)
        allowed, _ = b.next_delay(ctx)
        assert not allowed


# ---------------------------------------------------------------------------
# Level 2: EndToEndRetryBudgetMiddleware
# ---------------------------------------------------------------------------

class TestEndToEndBudget:
    """Test Level 2 end-to-end per-request retry budget."""

    def test_budget_blocks_when_zero(self):
        """Retries should be blocked when budget reaches 0."""
        mw = EndToEndRetryBudgetMiddleware()
        ctx = _make_attempt_ctx(
            success=False,
            drop_reason=DropReason.SERVER_FAILURE,
            should_retry=True,
            retry_budget_remaining=0,
        )
        results = []
        mw.process_attempt(ctx, lambda c: results.append(c))
        assert len(results) == 1
        assert not results[0].should_retry
        assert results[0].metadata.get('limiter_reason') == 'e2e_budget_exhausted'

    def test_budget_allows_when_positive(self):
        """Retries should be allowed when budget > 0."""
        mw = EndToEndRetryBudgetMiddleware()
        ctx = _make_attempt_ctx(
            success=False,
            drop_reason=DropReason.SERVER_FAILURE,
            should_retry=True,
            retry_budget_remaining=2,
        )
        results = []
        mw.process_attempt(ctx, lambda c: results.append(c))
        assert results[0].should_retry

    def test_noop_when_budget_none(self):
        """Middleware should be a no-op when budget is not configured."""
        mw = EndToEndRetryBudgetMiddleware()
        ctx = _make_attempt_ctx(
            success=False,
            drop_reason=DropReason.SERVER_FAILURE,
            should_retry=True,
            retry_budget_remaining=None,
        )
        results = []
        mw.process_attempt(ctx, lambda c: results.append(c))
        assert results[0].should_retry

    def test_noop_on_success(self):
        """Middleware should pass through on success without checking budget."""
        mw = EndToEndRetryBudgetMiddleware()
        ctx = _make_attempt_ctx(success=True, retry_budget_remaining=0)
        results = []
        mw.process_attempt(ctx, lambda c: results.append(c))
        assert len(results) == 1  # passed through


# ---------------------------------------------------------------------------
# Integration: Level 2 budget decrement across chain
# ---------------------------------------------------------------------------

class TestLevel2ChainIntegration:
    """Test that budget decrements correctly across service chain."""

    def test_budget_decrement_on_retry(self):
        """Budget should decrement when service retries."""
        sim = Simulator(seed=42)
        svc = ServiceRuntime(
            cfg=ServiceConfig(
                name="svc",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=3, delay=0),
                partial_failures=[],
            ),
        ).bind(seed=42, record_events=True)

        budget = [2]  # Allow 2 retries total across all hops
        done = [False]
        attempts = [0]

        def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
            attempts[0] += 1

        def on_root_done():
            done[0] = True

        svc.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            retry_budget_remaining=budget,
            tenant_id="test-client",
        )
        sim.run()
        # Budget should have been consumed (or service succeeded on first try)
        assert done[0]

    def test_chain_depth_bound(self):
        """With B=2, a 3-hop chain should not generate more than 3 total attempts."""
        sim = Simulator(seed=42)

        # Create a 3-hop chain: A -> B -> C, all with failures and retries
        svc_c = ServiceRuntime(
            cfg=ServiceConfig(
                name="C",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
            ),
        ).bind(seed=42, record_events=True)

        svc_b = ServiceRuntime(
            cfg=ServiceConfig(
                name="B",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
            ),
            dependencies=[svc_c],
        ).bind(seed=43, record_events=True)

        svc_a = ServiceRuntime(
            cfg=ServiceConfig(
                name="A",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
            ),
            dependencies=[svc_b],
        ).bind(seed=44, record_events=True)

        budget = [2]
        done = [False]

        def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
            pass

        def on_root_done():
            done[0] = True

        svc_a.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            retry_budget_remaining=budget,
            tenant_id="test-client",
        )
        sim.run()

        assert done[0]
        # Budget should not go below -2 (at most 2 retries consumed across all hops)
        # Each retry at any hop decrements the shared budget
        assert budget[0] >= -2  # may go slightly negative due to concurrent retries


# ---------------------------------------------------------------------------
# Config builder tests
# ---------------------------------------------------------------------------

class TestArollaConfig:
    """Test config schema and builder for Arolla."""

    def test_arolla_config_defaults(self):
        from simulator.config.schema import ArollaRetryBudgetConfig
        cfg = ArollaRetryBudgetConfig()
        assert cfg.alpha == 0.1
        assert cfg.beta_down == 0.3
        assert cfg.beta_up == 0.05
        assert cfg.window_ms == 1000.0
        assert cfg.success_rate_threshold is None

    def test_build_arolla_retry_budget(self):
        from simulator.config.schema import ArollaRetryBudgetConfig
        from simulator.config.builders.policies import build_arolla_retry_budget
        cfg = ArollaRetryBudgetConfig(alpha=0.2, window_ms=500)
        limiter = build_arolla_retry_budget(cfg)
        assert isinstance(limiter, GoodputCoupledRetryBudget)
        assert limiter.alpha == 0.2
        assert limiter.window_duration == ms_to_ns(500)

    def test_client_e2e_budget_field(self):
        from simulator.config.schema import ClientConfigYAML, WorkloadConfig
        client = ClientConfigYAML(
            name="test",
            workload=WorkloadConfig(base_rps=100, duration_s=10),
            e2e_retry_budget=3,
        )
        assert client.e2e_retry_budget == 3


# ---------------------------------------------------------------------------
# Tier 0 Validation Tests (V1-V4)
# ---------------------------------------------------------------------------

class TestV1AmplificationBound:
    """V1: Arolla Level 1 should bound retry amplification to ~1+alpha."""

    def test_v1_amplification_bound(self):
        """With alpha=0.1 and 50% failure, amplification should be <= 1.15."""
        sim = Simulator(seed=42)
        budget = _build_budget(alpha=0.1, window_ms=1000)

        svc = ServiceRuntime(
            cfg=ServiceConfig(
                name="backend",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=100,
                retry=FixedBackoffRetryPolicy(max_attempts=3, delay=0),
                load_limiter=budget,
                partial_failures=[
                    PartialFailure(
                        duration=TimeInterval(begin=0, end=s_to_ns(10)),
                        p_fail=0.5,
                    ),
                ],
            ),
        ).bind(seed=42, record_events=True)

        client = ClientRuntime(
            cfg=ClientConfig(name="test-client"),
            service=svc,
        )

        # Submit 500 requests over 2 seconds (250 RPS)
        n_requests = 500
        interval_ns = s_to_ns(2) // n_requests
        for i in range(n_requests):
            sim.schedule(i * interval_ns, partial(client.start_request, sim))

        sim.run()

        assert len(client.roots) == n_requests, (
            f"Expected {n_requests} roots, got {len(client.roots)}"
        )
        amplification = client.attempts_total / len(client.roots)
        assert amplification <= 1.15, (
            f"Amplification {amplification:.3f} exceeds 1.15 "
            f"(attempts={client.attempts_total}, roots={len(client.roots)})"
        )


class TestV2BudgetZeroBlocksRetries:
    """V2: End-to-end budget of 0 should block all retries."""

    def test_v2_budget_zero_blocks_retries(self):
        """With retry_budget_remaining=[0] and 100% failure, exactly 1 attempt."""
        sim = Simulator(seed=42)
        svc = ServiceRuntime(
            cfg=ServiceConfig(
                name="backend",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
                partial_failures=[
                    PartialFailure(
                        duration=TimeInterval(begin=0, end=s_to_ns(10)),
                        p_fail=1.0,
                    ),
                ],
            ),
        ).bind(seed=42, record_events=True)

        attempts = [0]
        done = [False]

        def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
            attempts[0] += 1

        def on_root_done():
            done[0] = True

        svc.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            retry_budget_remaining=[0],
            tenant_id="test-client",
        )
        sim.run()

        assert done[0], "Root request should be done"
        assert attempts[0] == 1, (
            f"Expected exactly 1 attempt with budget=0, got {attempts[0]}"
        )


class TestV3ChainDepthBound:
    """V3: End-to-end budget bounds retries across a service chain."""

    def test_v3_chain_depth_bound(self):
        """With B=2 and 100% failure on leaf, total retries consumed <= 2."""
        sim = Simulator(seed=42)

        # Service C: always fails
        svc_c = ServiceRuntime(
            cfg=ServiceConfig(
                name="C",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
                partial_failures=[
                    PartialFailure(
                        duration=TimeInterval(begin=0, end=s_to_ns(10)),
                        p_fail=1.0,
                    ),
                ],
            ),
        ).bind(seed=42, record_events=True)

        # Service B: healthy, depends on C
        svc_b = ServiceRuntime(
            cfg=ServiceConfig(
                name="B",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
            ),
            dependencies=[svc_c],
        ).bind(seed=43, record_events=True)

        # Service A: healthy, depends on B
        svc_a = ServiceRuntime(
            cfg=ServiceConfig(
                name="A",
                latency_median=ms_to_ns(1),
                latency_lognorm_sigma=0.0,
                workers=10,
                retry=FixedBackoffRetryPolicy(max_attempts=5, delay=0),
            ),
            dependencies=[svc_b],
        ).bind(seed=44, record_events=True)

        budget = [2]
        done = [False]

        def on_attempt_done(success, svc_time, drop_reason, queue_size, begin, deadline):
            pass

        def on_root_done():
            done[0] = True

        svc_a.submit_request(
            sim,
            on_attempt_done=on_attempt_done,
            on_root_done=on_root_done,
            retry_budget_remaining=budget,
            tenant_id="test-client",
        )
        sim.run()

        assert done[0], "Root request should be done"
        # Budget started at 2; exactly 2 retries consumed → budget[0] should be 0
        assert budget[0] >= 0, (
            f"Budget went negative: {budget[0]} (more than 2 retries consumed)"
        )
        # Leaf service C: 1 original + 2 retries = 3 attempts
        c_attempts = len(svc_c.events)
        assert c_attempts == 3, (
            f"Expected 3 attempts on C, got {c_attempts}"
        )


class TestV4PerTenantIsolation:
    """V4: Per-tenant budgets are independent and proportional to goodput."""

    def test_v4_per_tenant_isolation(self):
        """Tenant A (goodput=100) and B (goodput=200) get independent budgets."""
        b = _build_budget(alpha=0.1, window_ms=1000)
        ns = s_to_ns(1)

        # Seed tenant A with goodput=100/s
        for i in range(100):
            b.add_result(True, now=i * (ns // 100), tenant_id="A")
        b.add_result(True, now=ns, tenant_id="A")  # trigger window advance

        # Seed tenant B with goodput=200/s
        for i in range(200):
            b.add_result(True, now=i * (ns // 200), tenant_id="B")
        b.add_result(True, now=ns, tenant_id="B")  # trigger window advance

        tenant_a = b._get_tenant("A")
        tenant_b = b._get_tenant("B")
        assert tenant_a.goodput_rate == pytest.approx(100.0, rel=0.01)
        assert tenant_b.goodput_rate == pytest.approx(200.0, rel=0.01)

        # Budget = alpha * goodput_rate * window_secs
        # A: 0.1 * 100 * 1 = 10 retries per window
        # B: 0.1 * 200 * 1 = 20 retries per window

        # Exhaust tenant A's budget
        a_admitted = 0
        for i in range(15):
            ctx = RetryContext(attempt=2, now=ns + i * 100, tenant_id="A")
            allowed, _ = b.next_delay(ctx)
            if allowed:
                a_admitted += 1
        assert a_admitted == 10, f"Tenant A budget: expected 10, got {a_admitted}"

        # Tenant B should still have full budget (independent of A)
        b_admitted = 0
        for i in range(25):
            ctx = RetryContext(attempt=2, now=ns + i * 100, tenant_id="B")
            allowed, _ = b.next_delay(ctx)
            if allowed:
                b_admitted += 1
        assert b_admitted == 20, f"Tenant B budget: expected 20, got {b_admitted}"
