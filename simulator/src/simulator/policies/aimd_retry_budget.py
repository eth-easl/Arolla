from dataclasses import dataclass, field
from typing import Optional, Tuple
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryContext
from simulator.core.types import TimeDuration, TimePoint

@dataclass
class AIMDGlobalRetryBudget(LoadLimiter):
    """
    Adaptive Global Retry Budget using AIMD (Additive Increase, Multiplicative Decrease).
    
    Dynamically adjusts the allowed retry rate (RPS) based on the failure rate observed
    in the previous time window.
    
    - If failure_rate > threshold: Decrease RPS (Multiplicative).
    - If failure_rate <= threshold: Increase RPS (Additive).
    """
    
    min_rps: int
    max_rps: int
    initial_rps: int
    additive_step: int  # RPS to add per window
    decrease_factor: float  # Multiplier (e.g. 0.5)
    failure_threshold: float # e.g. 0.1 (10% failures triggers backoff)
    window_duration: TimeDuration # e.g. 1 second
    max_burst: int # Token bucket burst capacity

    # State: Token Bucket
    _tokens: float = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)
    _current_rps: float = field(init=False)
    
    # State: Window Stats
    _window_start: Optional[TimePoint] = field(default=None, init=False)
    _window_requests: int = 0
    _window_failures: int = 0

    def __post_init__(self):
        self._current_rps = float(self.initial_rps)
        self._tokens = float(self.max_burst)
        self._window_requests = 0
        self._window_failures = 0

    def _update_policy(self, now: TimePoint):
        """Evaluate stats and update target RPS at end of window"""
        if self._window_start is None:
            self._window_start = now
            return

        if now - self._window_start >= self.window_duration:
            # Analyze window
            if self._window_requests > 0:
                failure_rate = self._window_failures / self._window_requests
                
                if failure_rate > self.failure_threshold:
                    # Congestion Detected: Backoff
                    self._current_rps = max(
                        float(self.min_rps), 
                        self._current_rps * self.decrease_factor
                    )
                else:
                    # Healthy: Increase
                    self._current_rps = min(
                        float(self.max_rps), 
                        self._current_rps + self.additive_step
                    )
            
            # Reset window
            self._window_start = now
            self._window_requests = 0
            self._window_failures = 0

    def _refill(self, now: TimePoint):
        """Refill tokens based on current RPS"""
        self._update_policy(now) # Check for rate update first
        
        if self._last_refill is None:
            self._last_refill = now
            return
        
        elapsed = now - self._last_refill
        if elapsed > 0:
            # Rate is RPS. Period is 1s (1e9 ns).
            # Tokens = (elapsed_ns / 1e9) * current_rps
            tokens_to_add = (elapsed / 1_000_000_000.0) * self._current_rps
            self._tokens = min(float(self.max_burst), self._tokens + tokens_to_add)
            self._last_refill = now

    def request_ticket(self, now: TimePoint) -> bool:
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    # --- LoadLimiter Implementation ---

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        if now is None: return

        # Initialize window start if needed
        if self._window_start is None:
            self._window_start = now

        # Evaluate and reset the window even when no retry tickets are requested.
        # Without this, stats accumulate across windows indefinitely if all requests
        # succeed (no retries), causing incorrect backoff when retries eventually resume.
        self._update_policy(now)

        self._window_requests += 1
        if not success:
            self._window_failures += 1

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            return False, 0
            
        if self.request_ticket(context.now):
            return True, 0
        return False, 0


