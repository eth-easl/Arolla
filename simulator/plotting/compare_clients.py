#!/usr/bin/env python3
"""
Compare results from multiple clients.

Usage:
    python plotting/compare_clients.py results/test_multi/ -o results/test_multi/plots
"""

import argparse
import sys
import os
from pathlib import Path
import glob
import pandas as pd
import matplotlib.pyplot as plt

# Add parent to path for style imports if needed, though we'll keep it simple here
sys.path.insert(0, str(Path(__file__).parent.parent))

from plotting.core import load_csv, setup_plot, save_plot

def plot_combined_metric(dfs, client_names, metric_col, title, ylabel, output_path):
    """Plot a single metric for all clients on one chart."""
    setup_plot(title, "Time (s)", ylabel)
    
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
    
    for i, (df, name) in enumerate(zip(dfs, client_names)):
        color = colors[i % len(colors)]
        if metric_col in df.columns:
            plt.plot(df['timepoint'], df[metric_col], label=name, color=color, linewidth=2)
    
    plt.legend(loc='best')
    save_plot(output_path)
    plt.close()

def plot_success_rate_comparison(dfs, client_names, output_dir):
    """Compare success rates."""
    from plotting.core import calculate_success_rate
    
    setup_plot("Success Rate Comparison", "Time (s)", "Success Rate (%)")
    
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    
    for i, (df, name) in enumerate(zip(dfs, client_names)):
        sr = calculate_success_rate(df)
        if sr is not None:
            plt.plot(df['timepoint'], sr, label=name, color=colors[i % len(colors)], linewidth=2)
            
    plt.ylim(0, 105)
    plt.legend(loc='best')
    save_plot(output_dir / "compare_success_rate.png")
    plt.close()

def plot_service_aggregate(dfs, output_dir):
    """Plot aggregated service-side metrics (Sum of all clients)."""
    if not dfs:
        return
        
    # Assume all DFs have same timepoints. merged based on index?
    # Actually, we can just sum the relevant columns if they align.
    # Let's verify alignment or reindex.
    
    # Base frame from first df
    base_df = dfs[0].copy()
    
    # Columns to sum
    sum_cols = ['root_requests', 'retries', 'success_root', 'failure_root', 
                'failure_queue_full', 'failure_deadline', 'failure_server', 'total_request']
    
    # Columns to average (weighted? or just representative?)
    # Queue size is observed by clients. It's the SAME queue. 
    # So we can just take the max observation or mean across clients per bucket.
    queue_cols = ['queue_avg_at_attempt_end']
    
    # Initialize accumulators
    agg_df = base_df[sum_cols].copy()
    agg_df[queue_cols] = base_df[queue_cols]
    
    # Add others
    for df in dfs[1:]:
        # We assume timepoints match for simplicity in this MVP
        # In prod we would merge on 'timepoint'
        agg_df[sum_cols] = agg_df[sum_cols] + df[sum_cols]
        # For queue, let's take the max observed to be safe (worst case)
        # We explicitly select the column as a Series for cleaner concatenation
        q_col = 'queue_avg_at_attempt_end'
        if q_col in df.columns and q_col in agg_df.columns:
            combined_q = pd.concat([agg_df[q_col], df[q_col]], axis=1).max(axis=1)
            agg_df[q_col] = combined_q

    t = base_df['timepoint']
    
    # Plot 1: Total QPS (Load)
    setup_plot("Total Service Load (Aggregated)", "Time (s)", "Requests / sec")
    plt.plot(t, agg_df['root_requests'], label='Total Incoming (Roots)', linewidth=2, color='black')
    plt.plot(t, agg_df['success_root'], label='Total Success', linewidth=2, color='green')
    plt.plot(t, agg_df['failure_root'], label='Total Failed', linewidth=2, color='red')
    plt.legend(loc='best')
    save_plot(output_dir / "service_total_load.png")
    plt.close()
    
    # Plot 2: Service Queue Size
    setup_plot("Service Queue Size (Max Observed)", "Time (s)", "Queue Length")
    plt.plot(t, agg_df['queue_avg_at_attempt_end'], label='Queue Size', color='purple', linewidth=2)
    save_plot(output_dir / "service_queue_size.png")
    plt.close()

def main():
    parser = argparse.ArgumentParser(description='Compare client results')
    parser.add_argument('input_dir', help='Directory containing client CSVs')
    parser.add_argument('-o', '--output', default='plots', help='Output directory')
    parser.add_argument('--pattern', default='*.csv', help='File pattern to match (e.g. *.csv)')
    
    args = parser.parse_args()
    
    input_path = Path(args.input_dir)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find CSV files
    csv_files = sorted(list(input_path.glob(args.pattern)))
    if not csv_files:
        print(f"No files found matching {args.pattern} in {input_path}")
        return 1
        
    print(f"Found {len(csv_files)} client result files:")
    dfs = []
    names = []
    
    for f in csv_files:
        # Extract client name from filename (assuming format ..._clientName.csv)
        # Strategy: take the part after the last underscore before .csv? 
        # Or just use the whole stem. Let's use the stem for now, clean up if needed.
        name = f.stem.split('output_')[-1] # heuristic for multi_rogue_output_clientName
        print(f"  - {f.name} -> {name}")
        
        try:
            df = load_csv(str(f))
            dfs.append(df)
            names.append(name)
        except Exception as e:
            print(f"    Error loading: {e}")
            
    if not dfs:
        return 1
        
    print(f"\nGenerating comparison plots in {output_dir}...")
    
    # 0. Service Aggregate View
    plot_service_aggregate(dfs, output_dir)
    
    # 1. Success Rate
    plot_success_rate_comparison(dfs, names, output_dir)
    
    # 2. Latency P99
    plot_combined_metric(dfs, names, 'p99', 
                        "P99 Latency Comparison", "Latency (ms)", 
                        output_dir / "compare_latency_p99.png")
                        
    # 3. Latency Median
    plot_combined_metric(dfs, names, 'p50', 
                        "P50 Latency Comparison", "Latency (ms)", 
                        output_dir / "compare_latency_p50.png")

    # 4. QPS (Sent)
    plot_combined_metric(dfs, names, 'total_request',
                        "Throughput (Sent)", "Avg Requests/sec (bucketed)",
                        output_dir / "compare_throughput_sent.png")

    print("✅ Done!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
