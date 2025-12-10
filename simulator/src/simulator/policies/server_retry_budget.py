from dataclasses import dataclass, field
from typing import Optional, Tuple
from simulator.policies.load_limiter import LoadLimiter
from simulator.policies.retry import RetryContext
from simulator.core.types import TimeDuration, TimePoint


@dataclass
class GlobalRetryBudget(LoadLimiter):
    """
    A server-side global retry budget that issues 'tickets' for retries.
    
    Acts as a Token Bucket Rate Limiter:
    - Periodically refills tickets based on 'refill_rate' and 'period'.
    - Determining retry allowance consumes 1 ticket.
    """
    
    max_tokens: int
    """Maximum size of the token bucket (burst capacity)."""
    
    refill_rate: int
    """Number of tickets to refill per period."""
    
    period: TimeDuration
    """Refill period."""
    
    _tokens: float = field(init=False)
    _last_refill: Optional[TimePoint] = field(default=None, init=False)

    def __post_init__(self):
        self._tokens = float(self.max_tokens) # Start full

    def _refill(self, now: TimePoint):
        if self._last_refill is None:
            self._last_refill = now
            return
        
        elapsed = now - self._last_refill
        if elapsed > 0:
            # Calculate tokens to add: rate * (elapsed / period)
            # refill_rate is tokens per period
            tokens_to_add = (elapsed / self.period) * self.refill_rate
            self._tokens = min(float(self.max_tokens), self._tokens + tokens_to_add)
            self._last_refill = now

    def request_ticket(self, now: TimePoint) -> bool:
        """
        Attempt to acquire a retry ticket. 
        """
        self._refill(now)
        
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def balance(self) -> float:
        return self._tokens

    # --- LoadLimiter Implementation ---

    def add_result(self, success: bool, now: Optional[TimePoint] = None) -> None:
        # No-op: refill is time-based, not success-based
        pass

    def next_delay(self, context: RetryContext) -> Tuple[bool, TimeDuration]:
        if context.now is None:
            # Fallback if time not provided (shouldn't happen in sim)
            return False, 0
            
        if self.request_ticket(context.now):
            return True, 0
        return False, 0

