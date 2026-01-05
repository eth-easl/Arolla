#!/usr/bin/env python3
"""
Generate experiment configurations for Client Diversity (N vs M) study.
N = Total Number of Clients
M = Number of independent configurations (Shared Budgets)

M=1: Low Diversity (All share 1 budget)
M=N: High Diversity (All independent)
"""

import yaml
import os
from pathlib import Path
import logging

def generate_config(n_clients: int, m_diversity: int, output_dir: Path):
    name = f"diversity_N{n_clients}_M{m_diversity}"
    
    # Scale capacity: 10% of N (e.g. 1000 clients -> 100 workers)
    # Each client sends 10 RPS. Total Load = 10 * N.
    # Capacity = 10 * N.
    # We want overload.
    # Base Capacity = 10 * N.
    # Spike = 2x Load.
    # Workers roughly 1 worker per 100 RPS?
    # Service: 10ms processing time. 1 worker = 100 RPS.
    # Total Load = 10 * N RPS.
    # Needed Workers = (10 * N) / 100 = N / 10.
    workers = 1600
    queue_capacity = n_clients * 100
    
    config = {
        "name": name,
        "seed": 42,
        "services": [{
            "name": "service",
            "latency": {"median_ms": 60, "lognorm_sigma": 0.5},
            "workers": workers,
            "queue_capacity": queue_capacity, # Large buffer
            "timeout": {"attempt_ms": 192}, # Timeout to force failures during overload
            "partial_failures": [
                {
                    "type": "partial_failure",
                    "start_s": 30,
                    "end_s": 40,
                    "p_fail": 0.5
                }
            ]
        }],
        "clients": [],
        "output_csv": "output.csv",
        "granularity_s": 1.0,
        "fault_events_json": "faults.json"
    }

    # Spike Overload Logic
    # Clients send 10 RPS.
    # Spike Multiplier = 3.0 (3x load).
    # With 3x load and 1x capacity -> massive failure -> retry storm.
    # spike_config = {
    #     "type": "load_spike",
    #     "start_s": 20,
    #     "end_s": 30, # 20s spike
    #     "rps_multiplier": 3.0
    # }
    
    common_workload = {
        "base_rps": 188,
        "duration_s": 100, # Long enough to see recovery
        "rng_seed": 123
    }

    # Randomization Ranges
    import random
    rng = random.Random(42)

    def get_varied_retry():
        # Always use exponential for consistency but vary params
        retry_type = rng.choice(["fixed", "exponential", "jittered"])
        if retry_type == "fixed":
            return {
                "type": "fixed",
                "max_attempts": rng.randint(4, 5),
                "delay_ms": rng.randint(10, 100)
            }
        elif retry_type == "exponential":
            return {
                "type": "exponential",
                "max_attempts": rng.randint(4, 5),
                "initial_delay_ms": rng.randint(10, 100),
                "max_delay_ms": rng.randint(500, 2000),
            }
        elif retry_type == "jittered":
            return {
                "type": "jittered",
                "max_attempts": rng.randint(4, 5),
                "initial_delay_ms": rng.randint(10, 100),
                "max_delay_ms": rng.randint(500, 2000),
                "jitter_mode": "full"
            }
        else:
            raise ValueError(f"Unknown retry type: {retry_type}")
    
    def get_varied_budget():
        return {
            "budget_ratio": rng.uniform(0.05, 0.20),
            "max_retries": rng.randint(10, 50)
        }
        
    def get_varied_cb():
        return {
            "type": "time_based",
            "failure_threshold": rng.uniform(0.1, 0.4),
            "window_duration_ms": rng.randint(1000, 5000),
            "min_requests": 10,
            "half_open_delay_ms": 1000
        }

    # Groups
    group_size = n_clients // m_diversity
    
    for i in range(m_diversity):
        count = group_size
        if i == m_diversity - 1:
            count += (n_clients % m_diversity)
            
        # Unique parameters per group
        retry_cfg = get_varied_retry()
        budget_cfg = get_varied_budget()
        cb_cfg = get_varied_cb()

        if m_diversity == 1:
            budget_cfg["shared_budget_id"] = "global_budget"
        else:
            budget_cfg["shared_budget_id"] = f"budget_{i}"

        # policy_choice = rng.choice(["none", 'budget', 'breaker'])
        policy_choice = rng.choice(["budget"])
        
        client_dict = {
            "name": f"group_{i}",
            "replicas": count,
            "target_service": "service",
            "workload": common_workload,
            "retry": retry_cfg
        }
        
        if policy_choice == "budget":
            client_dict["retry_budget"] = budget_cfg
        elif policy_choice == "breaker":
            client_dict["circuit_breaker"] = cb_cfg
        else:
            logging.warning("no retry mitigation")

        config["clients"].append(client_dict)

    # Saving
    out_file = output_dir / f"N{n_clients}_M{m_diversity}.yaml"
    with open(out_file, 'w') as f:
        yaml.dump(config, f, sort_keys=False)
    print(f"Generated {out_file}")

def main():
    output_dir = Path("experiments/yaml/diversity")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # N Scan (Log scale)
    N_list = [10, 100, 200] 
    
    for n in N_list:
        M = 1
        generate_config(n, M, output_dir)
    #     # M values
    #     # Always M=1 (Global)
    #     generate_config(n, 1, output_dir)
        
    #     # M=10 (Medium Diversity/Independence)
    #     if n >= 10:
    #          generate_config(n, 10, output_dir)
             
    #     # M=100 (High Diversity)
    #     if n >= 100:
    #          generate_config(n, 100, output_dir)
             
        # Only generating up to M=100 as per figure implications (1, 10, 100)
        # Assuming M does not go to N in that specific chart.


if __name__ == "__main__":
    main()
