import numpy as np
from simulator.core import run_simulation, NoRetries, NRetries, AdaptiveRetries, CircuitBreakerRetries

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
