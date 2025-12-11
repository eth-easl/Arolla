#!/usr/bin/env python3
import sys
import os
import glob
import pandas as pd
import yaml
import matplotlib.pyplot as plt
from typing import List, Dict, Optional

def plot_comparison(runs_data: List[tuple]):
    """Generate comparison plots"""
    # Increase global font size
    plt.rcParams.update({'font.size': 14})
    
    # Generate informative labels
    labels = []
    for label, res in runs_data:
        if "error" in res:
            labels.append(label)
            continue
            
        config = res.get('config', '')
        short_label = label.split(' (')[0] # e.g., "ep3", "ep4", "ep5"
        
        # User requested specific mappings
        if short_label == "ep3":
            labels.append("Baseline\n(without timeout)")
        elif short_label == "ep4":
            labels.append("Baseline\n(timeout=192ms)")
        elif "Global" in config:
            # Format: Global(rps=40, burst=10)
            try:
                clean = config.replace("Global(", "").replace(")", "")
                parts = clean.split(", ")
                rps = parts[0].split("=")[1]
                burst = parts[1].split("=")[1]
                labels.append(f"Static Global Retry Budget\n(refill_rate={rps}, bucket={burst})")
            except:
                labels.append(f"Static Budget\n{config}")
        elif "AIMD" in config:
            labels.append("Adaptive Global Retry Budget")
        else:
            labels.append(short_label)

    # Extract data
    goodput = []
    latency = []
    retry_success = []
    amplification = []
    
    for _, res in runs_data:
        if "error" in res:
            goodput.append(0)
            latency.append(0)
            retry_success.append(0)
            amplification.append(0)
        else:
            goodput.append(res.get('goodput', 0))
            latency.append(res.get('p99_end', 0))
            retry_success.append(res.get('retry_success_rate', 0) * 100)
            amplification.append(res.get('amplification', 1))

    # Create 2x2 grid
    fig, axs = plt.subplots(2, 2, figsize=(18, 12)) # Larger figure for larger fonts
    fig.suptitle('Policy Effectiveness: Higher is Better (Top) vs Lower is Better (Bottom)', fontsize=20, weight='bold')

    # Helper for bar plots with larger fonts
    def plot_bar(ax, data, title, ylabel, color, limit=None, hline=None):
        bars = ax.bar(labels, data, color=color, edgecolor='black', alpha=0.8)
        ax.set_title(title, fontsize=16, weight='bold')
        ax.set_ylabel(ylabel, fontsize=14)
        # Rotate labels to prevent overlap
        ax.tick_params(axis='x', rotation=15, labelsize=11)
        ax.tick_params(axis='y', labelsize=12)
        ax.grid(axis='y', alpha=0.3)
        if limit:
            ax.set_ylim(limit)
        if hline:
            val, color, style, label = hline
            ax.axhline(y=val, color=color, linestyle=style, linewidth=2, label=label)
            ax.legend(fontsize=12)

    # 1. Global Success Rate
    global_success = [res.get('efficiency', 0) * 100 if "error" not in res else 0 for _, res in runs_data]
    plot_bar(axs[0, 0], global_success, 'Global Success Rate (%)', 'Success Rate %',
             'skyblue', limit=(0, 100))
    
    # 2. Retry Success Rate
    plot_bar(axs[0, 1], retry_success, 'Retry Success Rate (%)', 'Success Rate %', 
             'lightgreen', limit=(0, 100))
    
    # 3. Amplification
    plot_bar(axs[1, 0], amplification, 'Amplification', 'Total Load / Root Requests',
             'orange', hline=(1.0, 'k', '--', 'Ideal (1.0x)'))

    # 4. Latency
    plot_bar(axs[1, 1], latency, 'Final P99 Latency (ms)', 'Latency (ms)',
             'salmon', hline=(192, 'r', '--', 'Timeout (192ms)'))

    plt.tight_layout(rect=[0, 0.05, 1, 0.95])
    output_path = "comparison_summary.png"
    plt.savefig(output_path)
    print(f"\n✅ Comparison summary saved to: {os.path.abspath(output_path)}")

def find_results(exp_name: str, limit: int = 5) -> List[str]:
    """Find timestamped result dirs for experiment, sorted newest first"""
    base = "results_0"
    pattern = f"{base}/{exp_name}_[0-9]*"
    candidates = glob.glob(pattern)
    candidates = [c for c in candidates if os.path.isdir(c)]
    # Sort by name (timestamp is in name YYYYMMDD_HHMMSS)
    candidates.sort(reverse=True)
    return candidates[:limit]

def get_label(label: str, path: str) -> str:
    """Helper to generate concise label with timestamp"""
    return label 

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
        
    # EP5 (All Static) - Show ALL for variation analysis
    ep5_paths = find_results("ep5", 20)
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
    
    print("\nGenerating Visualization...")
    plot_comparison(results)

if __name__ == "__main__":
    main()
