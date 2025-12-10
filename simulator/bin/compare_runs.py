#!/usr/bin/env python3
import sys
import os
import glob
import pandas as pd
import yaml
from typing import List, Dict, Optional

def find_results(exp_name: str, limit: int = 5) -> List[str]:
    """Find timestamped result dirs for experiment, sorted newest first"""
    base = "results"
    pattern = f"{base}/{exp_name}_[0-9]*"
    candidates = glob.glob(pattern)
    candidates = [c for c in candidates if os.path.isdir(c)]
    # Sort by name (timestamp is in name YYYYMMDD_HHMMSS)
    candidates.sort(reverse=True)
    return candidates[:limit]

def extract_config_summary(path: str) -> str:
    """Read config.yaml from results dir and extract budget info"""
    # Find any .yaml file in the dir
    yamls = glob.glob(os.path.join(path, "*.yaml"))
    
    if not yamls:
        return "No Config Found"
    
    try:
        with open(yamls[0], 'r') as f:
            cfg = yaml.safe_load(f)
        
        # Look for service 'svc-A' (assuming single service for simplicity or first one)
        svc = cfg.get('services', [{}])[0]
        
        if 'aimd_global_retry_budget' in svc:
            c = svc['aimd_global_retry_budget']
            return f"AIMD(init={c.get('initial_rps')}, min={c.get('min_rps')})"
        elif 'global_retry_budget' in svc:
            c = svc['global_retry_budget']
            return f"Global(rps={c.get('target_rps')}, burst={c.get('max_burst')})"
        elif 'retry_budget' in svc:
            c = svc['retry_budget']
            return f"Local(ratio={c.get('budget_ratio')}, min={c.get('min_retries_per_sec')})"
        else:
            return "No Budget Config"
            
    except Exception as e:
        return f"Config Error: {str(e)}"

def analyze_run(path: str) -> Dict:
    """Calculate metrics for a run"""
    csv_path = os.path.join(path, "output.csv")
    if not os.path.exists(csv_path):
        return {"error": "No output.csv"}
    
    config_summary = extract_config_summary(path)

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return {"error": f"Read failed: {e}"}
    
    # Metrics
    total_root = df['root_requests'].sum()
    total_goodput = df['success_root'].sum()
    
    # 'total_request' is cumulative counter, so take the last value (max)
    total_load = df['total_request'].max()
    
    # Retry stats (these seem Per-Bucket based on CSV snippet showing retries=0 when total_request increases)
    total_retries = df['retries'].sum()
    total_failed_retries = df['failure_retry'].sum()
    
    # Amplification = (Roots + Retries) / Roots
    amplification = (total_root + total_retries) / total_root if total_root > 0 else 1.0
    
    p99_peak = df['p99'].max()
    # Recovery check
    last_p99 = df['p99'].tail(10).mean()
    recovered = last_p99 < 200 # Threshold based on timeout/baseline
    
    # Cost (Total failures) - likely cumulative too
    total_failures = df['total_failure'].max()
    
    # Retry stats (these seem Per-Bucket based on CSV snippet showing retries=0 when total_request increases)
    # Wait, let's verify if 'retries' is cumulative. 
    # Snippet: 0, 0, 0... If it counts events in bucket, sum is correct.
    total_retries = df['retries'].sum()
    total_failed_retries = df['failure_retry'].sum()
    
    total_success_retries = total_retries - total_failed_retries
    retry_success_rate = (total_success_retries / total_retries) if total_retries > 0 else 0
    
    return {
        "config": config_summary,
        "goodput": total_goodput,
        "amplification": amplification,
        "p99_peak": p99_peak,
        "p99_end": last_p99,
        "recovered": recovered,
        "efficiency": (total_goodput / total_load) if total_load > 0 else 0,
        "retry_success_rate": retry_success_rate
    }

def print_table(runs_data: List[tuple]):
    """Print comparison table with dynamic columns"""
    # runs_data is list of (label, metrics_dict)
    
    headers = ["Metric"] + [label for label, _ in runs_data]
    
    metrics = [
        ("Configuration", "config", lambda x: str(x)[:40]), # Truncate check
        ("Recovered?", "recovered", lambda x: "YES" if x else "NO"),
        ("Total Goodput", "goodput", lambda x: f"{x:,.0f}"),
        ("Peak P99 Latency", "p99_peak", lambda x: f"{x:.1f} ms"),
        ("Final P99 Latency", "p99_end", lambda x: f"{x:.1f} ms"),
        ("Amplification", "amplification", lambda x: f"{x:.2f}x"),
        ("Retry Success Rate", "retry_success_rate", lambda x: f"{x*100:.1f}%"),
        ("Efficiency (Global)", "efficiency", lambda x: f"{x*100:.1f}%"),
    ]
    
    # Format string dynamic
    col_width = 25
    header_fmt = "{:<20} " + " ".join([f"{{:<{col_width}}}" for _ in range(len(runs_data))])
    
    print(header_fmt.format(*headers))
    print("-" * (20 + (col_width + 1) * len(runs_data)))
    
    for label, key, formatter in metrics:
        row = [label]
        for _, data in runs_data:
            if "error" in data:
                val = "Error"
            else:
                raw = data.get(key)
                val = formatter(raw) if raw is not None else "N/A"
            row.append(val)
        print(header_fmt.format(*row))

def main():
    # Define what to show
    # We want: 1 latest 'ep4', ALL 'ep5', 1 latest 'ep5_aimd'
    # Actually user said "show all ep5".
    
    runs_to_analyze = [] # List of (Label, Path)
    
    # Ep3
    ep3_paths = find_results("ep3", 1)
    if ep3_paths:
        runs_to_analyze.append(("ep3 (Baseline)", ep3_paths[0]))

    # EP4 (Baseline)
    ep4_paths = find_results("ep4", 1)
    if ep4_paths:
        runs_to_analyze.append(("ep4 (Baseline)", ep4_paths[0]))
        
    # EP5 (All Static) - Limit to 4 to verify variety
    ep5_paths = find_results("ep5", 4)
    for i, path in enumerate(ep5_paths):
        # determine timestamp or ID from path
        ts = path.split("_")[-1]
        runs_to_analyze.append((f"ep5 ({ts})", path))
        
    # EP5 AIMD
    aimd_paths = find_results("ep5_aimd", 1)
    if aimd_paths:
        runs_to_analyze.append(("ep5_aimd (AIMD)", aimd_paths[0]))

    # Analyze
    results = []
    print("Analyzing runs...")
    for label, path in runs_to_analyze:
        print(f"  {label}: {path}")
        metrics = analyze_run(path)
        results.append((label, metrics))
            
    print("\nComparison:")
    print_table(results)

if __name__ == "__main__":
    main()
