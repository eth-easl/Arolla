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

class StatefulService:
    """Service with queuing, latency, and capacity degradation for metastability modeling."""
    def __init__(self, base_capacity, base_latency=1.0, queue_latency_factor=0.1, 
                 capacity_degradation_factor=0.0, timeout=10.0):
        self.base_capacity = base_capacity
        self.base_latency = base_latency
        self.queue_latency_factor = queue_latency_factor
        self.capacity_degradation_factor = capacity_degradation_factor
        self.timeout = timeout
        
        # State variables
        self.queue_length = 0
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.timeout_requests = 0
        
        # Per-tick metrics
        self.current_latency = base_latency
        self.current_capacity = base_capacity
        self.requests_this_tick = 0
        
    def get_current_latency(self):
        """Latency increases with queue length."""
        return self.base_latency + self.queue_length * self.queue_latency_factor
    
    def get_current_capacity(self):
        """Capacity degrades as queue grows (models resource contention, GC, etc.)."""
        # Capacity = base_capacity * (1 - degradation_factor * queue_length / base_capacity)
        # This creates feedback: more queue -> less capacity -> more queue
        if self.base_capacity == 0:
            return 0
        degradation = self.capacity_degradation_factor * (self.queue_length / self.base_capacity)
        degradation = min(degradation, 0.9)  # Cap at 90% degradation
        return self.base_capacity * (1.0 - degradation)
    
    def new_tick(self):
        """Process one time tick."""
        # Update current state based on queue
        self.current_latency = self.get_current_latency()
        self.current_capacity = self.get_current_capacity()
        
        # Total work to process: queued + newly arrived
        total_work = self.queue_length + self.requests_this_tick
        
        # How much can we process this tick?
        can_process = min(total_work, int(self.current_capacity))
        
        if can_process > 0:
            # Check for timeouts based on current latency
            if self.current_latency > self.timeout:
                # All requests timeout - they are processed but fail
                self.timeout_requests += can_process
                self.failed_requests += can_process
                # Even timeout processing takes resources, so queue still decreases
                self.queue_length = max(0, total_work - can_process)
            else:
                # Successfully process requests
                self.successful_requests += can_process
                self.queue_length = total_work - can_process
        else:
            # No capacity available, everything stays in queue
            self.queue_length = total_work
        
        # Reset for next tick
        self.requests_this_tick = 0
        
    def handle_request(self, is_retry=False, failure_rate=0.0):
        """
        Queue a request to be processed in this tick.
        Returns immediate failure signal if system is overloaded (for retry logic).
        """
        self.total_requests += 1
        self.requests_this_tick += 1
        
        # Return failure signal based on current system stress
        # This allows retry strategies to kick in and amplify load
        # Use a probabilistic model: higher latency/queue = higher chance of "perceived" failure
        stress_factor = min(1.0, (self.queue_length / max(1, self.base_capacity)) + 
                           (self.current_latency / max(1, self.timeout)))
        
        # With some probability based on stress, signal that request will fail
        # This triggers retries in the strategy
        import random
        if random.random() < stress_factor * 0.5:  # 50% of stress converts to failure signal
            return False  # Signal that request will likely fail (triggers retries)
        
        return True  # Request accepted
    
    def get_metrics(self):
        """Return current metrics."""
        return {
            'queue_length': self.queue_length,
            'latency': self.current_latency,
            'capacity': self.current_capacity,
            'total_requests': self.total_requests,
            'successful': self.successful_requests,
            'failed': self.failed_requests,
            'timeout': self.timeout_requests
        }

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

def run_metastable_simulation(strategy_factory, num_ticks, requests_per_tick, 
                              base_capacity, queue_latency_factor, capacity_degradation_factor,
                              timeout, spike_start, spike_end, spike_multiplier, num_clients=10):
    """
    Run time-series simulation for metastability.
    
    Returns time series of: latency, queue_length, success_count, fail_count per tick
    """
    service = StatefulService(
        base_capacity=base_capacity,
        queue_latency_factor=queue_latency_factor,
        capacity_degradation_factor=capacity_degradation_factor,
        timeout=timeout
    )
    
    clients = [Client(strategy_factory()) for _ in range(num_clients)]
    
    # Track metrics over time
    metrics = {
        'latency': [],
        'queue_length': [],
        'success': [],
        'fail': [],
        'capacity': [],
        'load': []
    }
    
    prev_successful = 0
    prev_failed = 0
    
    for tick in range(num_ticks):
        # Determine load for this tick
        if spike_start <= tick < spike_end:
            # During spike
            current_requests = int(requests_per_tick * spike_multiplier)
        else:
            current_requests = requests_per_tick
        
        # Clients make requests (these get queued in the service)
        for _ in range(current_requests):
            # Pick a random client and make request
            client = clients[tick % num_clients]
            client.make_request(service)
        
        # Service processes the tick
        service.new_tick()
        
        # Track metrics after processing
        m = service.get_metrics()
        
        metrics['latency'].append(m['latency'])
        metrics['queue_length'].append(m['queue_length'])
        metrics['capacity'].append(m['capacity'])
        metrics['load'].append(current_requests)
        
        # Calculate success/fail for this tick (difference from previous)
        success_delta = m['successful'] - prev_successful
        fail_delta = m['failed'] - prev_failed
        
        metrics['success'].append(max(0, success_delta))
        metrics['fail'].append(max(0, fail_delta))
        
        prev_successful = m['successful']
        prev_failed = m['failed']
    
    return metrics
