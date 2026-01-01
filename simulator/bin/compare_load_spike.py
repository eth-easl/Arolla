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
            
        # Use provided label directly as it's cleaner
        labels.append(label.split('\n')[0])

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
    fig, axs = plt.subplots(2, 2, figsize=(18, 12))
    fig.suptitle('Load Spike Metastability: Policy Effectiveness', fontsize=20, weight='bold')

    # Helper for bar plots
    def plot_bar(ax, data, title, ylabel, color, limit=None, hline=None):
        bars = ax.bar(labels, data, color=color, edgecolor='black', alpha=0.8)
        ax.set_title(title, fontsize=16, weight='bold')
        ax.set_ylabel(ylabel, fontsize=14)
        ax.tick_params(axis='x', rotation=15, labelsize=11)
        ax.tick_params(axis='y', labelsize=12)
        ax.grid(axis='y', alpha=0.3)
        if limit:
            ax.set_ylim(limit)
        if hline:
            val, color, style, label = hline
            ax.axhline(y=val, color=color, linestyle=style, linewidth=2, label=label)
            ax.legend(fontsize=12)

    # 1. Efficiency
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
    output_path = "comparison_load_spike.png"
    plt.savefig(output_path)
    print(f"\n✅ Comparison summary saved to: {os.path.abspath(output_path)}")

def find_results(exp_name: str, limit: int = 1) -> List[str]:
    """Find timestamped result dirs for experiment, sorted newest first"""
    base = "results"
    pattern = f"{base}/{exp_name}_[0-9]*"
    candidates = glob.glob(pattern)
    candidates = [c for c in candidates if os.path.isdir(c)]
    candidates.sort(reverse=True)
    return candidates[:limit]

def extract_config_summary(path: str) -> str:
    yamls = glob.glob(os.path.join(path, "*.yaml"))
    if not yamls: return "No Config"
    try:
        with open(yamls[0], 'r') as f:
            cfg = yaml.safe_load(f)
        svc = cfg.get('services', [{}])[0]
        if 'aimd_global_retry_budget' in svc:
            return "AIMD Budget"
        elif 'global_retry_budget' in svc:
            return "Static Budget"
        elif 'retry' in svc and svc['retry'].get('max_attempts', 0) > 1:
            return "Retries (Unsafe)"
        else:
            return "No Retries (Safe)"
    except:
        return "Config Error"

def analyze_run(path: str) -> Dict:
    csv_path = os.path.join(path, "output.csv")
    if not os.path.exists(csv_path):
        return {"error": "No output.csv"}
    
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return {"error": f"Read failed: {e}"}
    
    total_root = df['root_requests'].sum()
    total_goodput = df['success_root'].sum()
    total_load = df['total_request'].max()
    
    total_retries = df['retries'].sum()
    total_failed_retries = df['failure_retry'].sum()
    
    amplification = (total_root + total_retries) / total_root if total_root > 0 else 1.0
    
    p99_peak = df['p99'].max()
    last_p99 = df['p99'].tail(10).mean()
    recovered = last_p99 < 300 
    
    total_success_retries = total_retries - total_failed_retries
    retry_success_rate = (total_success_retries / total_retries) if total_retries > 0 else 0
    
    config_summary = extract_config_summary(path)

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
    headers = ["Metric"] + [label for label, _ in runs_data]
    metrics = [
        ("Description", "config", lambda x: str(x)),
        ("Recovered?", "recovered", lambda x: "YES" if x else "NO"),
        ("Amplification", "amplification", lambda x: f"{x:.2f}x"),
        ("Final P99 Latency", "p99_end", lambda x: f"{x:.1f} ms"),
        ("Efficiency", "efficiency", lambda x: f"{x*100:.1f}%"),
    ]
    
    col_width = 20
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
    runs_to_analyze = []
    
    # Order: Safe -> Unsafe Variants -> Static -> AIMD
    scenarios = [
        ("ep1", "0 retry"),
        ("ep2_1", "1 retry"),
        ("ep2_2", "2 retry"),
        ("ep2_3", "3 retry"),
        ("ep3", "static global\nretry budget"),
        ("ep4", "adaptive global\nretry budget")
    ]

    for exp, label in scenarios:
        paths = find_results(exp, 1)
        if paths:
            runs_to_analyze.append((label, paths[0]))
        else:
            print(f"Warning: No results found for {exp}")

    results = []
    print("Analyzing runs...")
    for label, path in runs_to_analyze:
        print(f"  {label.replace(chr(10), ' ')}: {path}")
        metrics = analyze_run(path)
        results.append((label, metrics))
            
    print("\nComparison:")
    print_table(results)
    
    print("\nGenerating Visualization...")
    plot_comparison(results)

if __name__ == "__main__":
    main()
