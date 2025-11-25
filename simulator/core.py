import random
import collections

class Service:
    def __init__(self, failure_rate, retry_budget_rate=None, max_capacity=None):
        self.failure_rate = failure_rate
        self.total_requests = 0
        self.retry_budget_rate = retry_budget_rate
        # Simple token bucket for server budget
        self.budget_tokens = 100 
        self.budget_capacity = 100
        
        self.max_capacity = max_capacity
        self.current_tick_load = 0

    def new_tick(self):
        self.current_tick_load = 0

    def handle_request(self, is_retry=False):
        self.total_requests += 1
        
        # Refill budget based on traffic (simplified: 1 request adds 'rate' tokens)
        if self.retry_budget_rate is not None:
            self.budget_tokens = min(self.budget_capacity, self.budget_tokens + self.retry_budget_rate)
            
            if is_retry:
                if self.budget_tokens >= 1.0:
                    self.budget_tokens -= 1.0
                else:
                    # Budget exhausted, reject retry
                    # This is "cheap" rejection, does NOT count towards capacity
                    return False

        # Check Capacity
        if self.max_capacity is not None:
            self.current_tick_load += 1
            if self.current_tick_load > self.max_capacity:
                # Overload!
                return False

        if random.random() < self.failure_rate:
            return False # Failure
        return True # Success

class Strategy:
    def execute(self, service, make_call_func):
        raise NotImplementedError

class NoRetries(Strategy):
    def execute(self, service, make_call_func):
        return make_call_func(service, is_retry=False)

class NRetries(Strategy):
    def __init__(self, n):
        self.n = n

    def execute(self, service, make_call_func):
        # First attempt
        if make_call_func(service, is_retry=False):
            return True
        
        # Retries
        for _ in range(self.n):
            if make_call_func(service, is_retry=True):
                return True
        return False

class AdaptiveRetries(Strategy):
    def __init__(self, n, token_ratio=0.1, bucket_size=10):
        self.n = n
        self.token_ratio = token_ratio
        self.bucket_size = bucket_size
        self.tokens = bucket_size # Start full

    def execute(self, service, make_call_func):
        if make_call_func(service, is_retry=False):
            self.tokens = min(self.bucket_size, self.tokens + self.token_ratio)
            return True
        
        # Retry only if we have tokens
        retries = 0
        while retries < self.n:
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                retries += 1
                if make_call_func(service, is_retry=True):
                    self.tokens = min(self.bucket_size, self.tokens + self.token_ratio)
                    return True
            else:
                break
        return False

class CircuitBreakerRetries(Strategy):
    def __init__(self, n, threshold=0.5, window_size=100):
        self.n = n
        self.threshold = threshold
        self.history = collections.deque(maxlen=window_size)

    def _get_failure_rate(self):
        if not self.history:
            return 0.0
        return sum(self.history) / len(self.history)

    def _record(self, failed):
        self.history.append(1 if failed else 0)

    def execute(self, service, make_call_func):
        # Always make the first call
        failed = not make_call_func(service, is_retry=False)
        self._record(failed)
        
        if not failed:
            return True
        
        # Decide to retry
        if self._get_failure_rate() < self.threshold:
            for _ in range(self.n):
                failed = not make_call_func(service, is_retry=True)
                self._record(failed)
                if not failed:
                    return True
        
        return False

class Client:
    def __init__(self, strategy):
        self.strategy = strategy
        self.successes = 0
        self.failures = 0

    def make_request(self, service):
        # We pass a simple lambda or function to the strategy to perform the actual raw call
        # This allows the strategy to control the loop.
        
        def raw_call(svc, is_retry=False):
            return svc.handle_request(is_retry=is_retry)

        success = self.strategy.execute(service, raw_call)
        if success:
            self.successes += 1
        else:
            self.failures += 1

def run_simulation(strategy_factory, failure_rate, num_clients=100, requests_per_client=1000, retry_budget_rate=None, max_capacity=None):
    service = Service(failure_rate, retry_budget_rate=retry_budget_rate, max_capacity=max_capacity)
    clients = [Client(strategy_factory()) for _ in range(num_clients)]
    
    for _ in range(requests_per_client):
        service.new_tick() # Reset load for this tick
        for client in clients:
            client.make_request(service)
            
    total_attempts = num_clients * requests_per_client
    total_successes = sum(c.successes for c in clients)
    
    # Success Rate: Total Successes / Total Intended Requests
    success_rate = total_successes / total_attempts
    
    # Load: Total Service Requests / Total Intended Requests
    load = service.total_requests / total_attempts
    
    return success_rate, load
