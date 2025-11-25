import numpy as np
from simulator.core import run_simulation, run_metastable_simulation, NoRetries, NRetries, AdaptiveRetries, CircuitBreakerRetries

def run_sweep(strategy_factory, failure_rates, num_clients=100, requests_per_client=500, retry_budget_rate=None, max_capacity=None):
    success_rates = []
    loads = []
    for rate in failure_rates:
        s_rate, load = run_simulation(
            strategy_factory, rate, 
            num_clients=num_clients, 
            requests_per_client=requests_per_client,
            retry_budget_rate=retry_budget_rate,
            max_capacity=max_capacity
        )
        success_rates.append(s_rate * 100)
        loads.append(load * 100)
    return success_rates, loads

def scenario_basic_comparison(num_clients=100, requests_per_client=500, token_ratio=0.1, bucket_size=10, breaker_threshold=0.1, num_retries=3):
    print("Running Basic Comparison Scenarios...")
    failure_rates = np.linspace(0, 0.1, 10)
    strategies = {
        'token_bucket': lambda: AdaptiveRetries(num_retries, token_ratio=token_ratio, bucket_size=bucket_size),
        'circuit_breaker': lambda: CircuitBreakerRetries(num_retries, threshold=breaker_threshold),
        'no_retries': lambda: NoRetries(),
        'fixed_retries': lambda: NRetries(num_retries)
    }
    results = {}
    for name, factory in strategies.items():
        print(f"  Simulating {name}...")
        results[name] = run_sweep(factory, failure_rates, num_clients=num_clients, requests_per_client=requests_per_client)
    return failure_rates * 100, results

def scenario_client_count(client_counts=[10, 100, 1000], total_requests=50000, token_ratio=0.1, bucket_size=10, breaker_threshold=0.1, num_retries=3):
    print("Running Client Count Effect Scenarios...")
    failure_rates = np.linspace(0, 0.1, 11)
    
    results = {}
    for count in client_counts:
        # Adaptive
        name_a = f'token_bucket_{count}clients'
        print(f"  Simulating {name_a}...")
        results[name_a] = run_sweep(
            lambda: AdaptiveRetries(num_retries, token_ratio=token_ratio, bucket_size=bucket_size),
            failure_rates,
            num_clients=count,
            requests_per_client=total_requests // count
        )
        # Breaker
        name_b = f'circuit_breaker_{count}clients'
        print(f"  Simulating {name_b}...")
        results[name_b] = run_sweep(
            lambda: CircuitBreakerRetries(num_retries, threshold=breaker_threshold),
            failure_rates,
            num_clients=count,
            requests_per_client=total_requests // count
        )
    return failure_rates * 100, results

def scenario_server_budget(num_clients=100, requests_per_client=500, retry_budget_rate=0.1, num_retries=3):
    print("Running Server Budget Scenarios...")
    failure_rates = np.linspace(0, 1.0, 10)
    
    results = {}
    # Unprotected
    print("  Simulating Unprotected...")
    results['Unprotected (No Budget)'] = run_sweep(
        lambda: NRetries(num_retries), failure_rates, num_clients=num_clients, requests_per_client=requests_per_client
    )
    # Protected
    print("  Simulating Protected...")
    results['Protected (10% Budget)'] = run_sweep(
        lambda: NRetries(num_retries), failure_rates, num_clients=num_clients, requests_per_client=requests_per_client, retry_budget_rate=retry_budget_rate
    )
    return failure_rates * 100, results

def scenario_strategies_with_budget_capacity(capacity=2000, num_clients=1000, requests_per_client=500, retry_budget_rate=0.1, token_ratio=0.1, bucket_size=10, breaker_threshold=0.1, num_retries=3):
    print("Running Strategies + Budget + Capacity Scenarios...")
    failure_rates = np.linspace(0, 1.0, 10)
    
    strategies = {
        'fixed_retries': lambda: NRetries(num_retries),
        'circuit_breaker': lambda: CircuitBreakerRetries(num_retries, threshold=breaker_threshold),
        'adaptive': lambda: AdaptiveRetries(num_retries, token_ratio=token_ratio, bucket_size=bucket_size)
    }
    
    results_success = {}
    results_load = {}
    dashed_keys = []
    
    for name, factory in strategies.items():
        print(f"  Simulating {name}...")
        # With Budget
        s_w, l_w = run_sweep(
            factory, failure_rates, num_clients=num_clients, requests_per_client=requests_per_client, 
            retry_budget_rate=retry_budget_rate, max_capacity=capacity
        )
        results_success[f"{name} (Budget)"] = s_w
        results_load[f"{name} (Budget)"] = l_w
        
        # Without Budget
        s_wo, l_wo = run_sweep(
            factory, failure_rates, num_clients=num_clients, requests_per_client=requests_per_client, 
            retry_budget_rate=None, max_capacity=capacity
        )
        wo_key = f"{name} (No Budget)"
        results_success[wo_key] = s_wo
        results_load[wo_key] = l_wo
        dashed_keys.append(wo_key)
        
    return failure_rates * 100, results_success, results_load, dashed_keys

def scenario_metastable_failure(num_ticks=300, requests_per_tick=10, base_capacity=100,
                                 spike_start=100, spike_end=200, spike_multiplier=2.0,
                                 timeout=10.0, num_retries=3, num_clients=10):
    """
    Scenario 3: Metastable Failure
    
    Demonstrates two configurations with SAME system characteristics but DIFFERENT retry parameters:
    1. Recoverable: Lower retry count, system recovers after trigger
    2. Metastable: Higher retry count amplifies load, prevents recovery
    
    The system experiences a load spike. With same system parameters but different
    retry settings, one configuration recovers while the other enters metastable state.
    """
    print("Running Metastable Failure Scenarios...")
    
    # System parameters - SAME for both configurations
    # Create a system at the threshold: during spike, system gets stressed
    # Key: retry amplification determines whether system recovers or not
    system_capacity = int(requests_per_tick * spike_multiplier)  # Exactly at spike load (no headroom)
    queue_latency_factor = 0.12  # Moderate latency growth  
    capacity_degradation_factor = 0.4  # Moderate-high capacity degradation
    
    results = {}
    
    # Configuration 1: Recoverable (NO retries - no load amplification)
    print("  Simulating Recoverable configuration (0 retries)...")
    metrics_recoverable = run_metastable_simulation(
        strategy_factory=lambda: NoRetries(),  # No retries at all
        num_ticks=num_ticks,
        requests_per_tick=requests_per_tick,
        base_capacity=system_capacity,
        queue_latency_factor=queue_latency_factor,
        capacity_degradation_factor=capacity_degradation_factor,
        timeout=timeout,
        spike_start=spike_start,
        spike_end=spike_end,
        spike_multiplier=spike_multiplier,
        num_clients=num_clients
    )
    results['recoverable'] = metrics_recoverable
    
    # Configuration 2: Metastable (many retries, excessive load amplification)
    print("  Simulating Metastable configuration (5 retries)...")
    metrics_metastable = run_metastable_simulation(
        strategy_factory=lambda: NRetries(5),  # 5 retries = up to 6x amplification
        num_ticks=num_ticks,
        requests_per_tick=requests_per_tick,
        base_capacity=system_capacity,
        queue_latency_factor=queue_latency_factor,
        capacity_degradation_factor=capacity_degradation_factor,
        timeout=timeout,
        spike_start=spike_start,
        spike_end=spike_end,
        spike_multiplier=spike_multiplier,
        num_clients=num_clients
    )
    results['metastable'] = metrics_metastable
    
    # Print summary statistics
    print(f"\n  Summary:")
    print(f"  Recoverable (0 retries) - Final queue: {metrics_recoverable['queue_length'][-1]}, "
          f"Final latency: {metrics_recoverable['latency'][-1]:.2f}, "
          f"Success rate: {sum(metrics_recoverable['success']) / (num_ticks * requests_per_tick) * 100:.1f}%")
    print(f"  Metastable  (5 retries) - Final queue: {metrics_metastable['queue_length'][-1]}, "
          f"Final latency: {metrics_metastable['latency'][-1]:.2f}, "
          f"Success rate: {sum(metrics_metastable['success']) / (num_ticks * requests_per_tick) * 100:.1f}%")
    
    return results, {'spike_start': spike_start, 'spike_end': spike_end}
