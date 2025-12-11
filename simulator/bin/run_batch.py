#!/usr/bin/env python3
import sys
import os
import yaml
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from itertools import product
from pathlib import Path

# Add bin to path to import workflow
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from workflow import run_workflow

def run_sweep(base_yaml: str):
    # Load base config
    with open(base_yaml, 'r') as f:
        base_config = yaml.safe_load(f)
    
    # Define Sweep Ranges
    # RPS: Critical range around 40 (10-100), plus failure modes (200, 300)
    rps_values = [10, 20, 30, 40, 50, 60, 80, 100, 200, 300]
    
    # Burst: From strict (1) to loose (100)
    burst_values = [1, 5, 10, 20, 40, 80, 100]
    
    results = []
    
    # Check if results already exist
    if os.path.exists("batch_results_sweep.csv"):
        print("Found existing batch_results_sweep.csv. Skipping simulation.")
        df_res = pd.read_csv("batch_results_sweep.csv")
        plot_heatmaps(df_res, rps_values, burst_values)
        return

    print(f"Starting Sweep: {len(rps_values)} RPS values x {len(burst_values)} Burst values = {len(rps_values)*len(burst_values)} runs")
    
    for rps, burst in product(rps_values, burst_values):
        print(f"Running: RPS={rps}, Burst={burst}...", end="", flush=True)
        
        try:
            # Modify Config
            config = base_config.copy()
            svc = config['services'][0]
            if 'global_retry_budget' not in svc:
                svc['global_retry_budget'] = {}
                
            svc['global_retry_budget']['target_rps'] = rps
            svc['global_retry_budget']['max_burst'] = burst
            
            # Write temp config
            temp_yaml = f"experiments/yaml/temp_sweep_{rps}_{burst}.yaml"
            with open(temp_yaml, 'w') as f:
                yaml.dump(config, f)
                
            # Check for existing run
            stem = f"temp_sweep_{rps}_{burst}"
            candidates = sorted(Path("results").glob(f"{stem}_*"))
            
            latest_run = None
            if candidates:
                # Use latest if valid
                if (candidates[-1] / "output.csv").exists():
                    latest_run = candidates[-1]
                    print(" [Skip] ", end="")
            
            if not latest_run:
                # Run Workflow
                run_workflow(temp_yaml, verbose=False, plot=False)
                
                # Find result
                candidates = sorted(Path("results").glob(f"{stem}_*"))
                if candidates:
                    latest_run = candidates[-1]
            
            # Cleanup temp
            if os.path.exists(temp_yaml):
                os.remove(temp_yaml)

            if not latest_run:
                print(" Failed (No output)")
                continue
                
            # Analyze
            csv_path = latest_run / "output.csv"
            df = pd.read_csv(csv_path)
            
            # Metrics
            total_root = df['root_requests'].sum()
            goodput = df['success_root'].sum()
            total_load = df['total_request'].max()
            
            # Latency (P99 End)
            latency = df['p99'].tail(10).mean()
            
            # Amplification
            retries = df['retries'].sum()
            amp = (total_root + retries) / total_root if total_root > 0 else 1.0
            
            # Global Success
            global_success = goodput / total_load if total_load > 0 else 0
            
            # Retry Success
            retry_success = (retries - df['failure_retry'].sum()) / retries if retries > 0 else 0
            
            results.append({
                'rps': rps,
                'burst': burst,
                'goodput': goodput,
                'latency': latency,
                'amplification': amp,
                'global_success': global_success * 100,
                'retry_success': retry_success * 100
            })
            
            print(" Done")
            
        except Exception as e:
            print(f" Error: {e}")

    # Save Results
    df_res = pd.DataFrame(results)
    df_res.to_csv("batch_results_sweep.csv", index=False)
    print("\nSweep Complete. Results saved to batch_results_sweep.csv")
    
    plot_heatmaps(df_res, rps_values, burst_values)

def plot_heatmaps(df, rps_values, burst_values):
    # Pivot Data for Heatmaps
    # Rows: RPS (Y), Cols: Burst (X)
    
    metrics = [
        ('global_success', 'Global Success Rate (%)', 'Blues'),
        ('retry_success', 'Retry Success Rate (%)', 'Greens'),
        ('amplification', 'Amplification', 'Oranges'),
        ('latency', 'Final P99 Latency (ms)', 'Reds')
    ]
    
    fig, axs = plt.subplots(2, 2, figsize=(16, 14))
    fig.suptitle('Static Budget Parameter Sweep: Target RPS vs Max Burst', fontsize=20)
    
    for idx, (metric, title, cmap) in enumerate(metrics):
        ax = axs[idx // 2, idx % 2]
        
        # Pivot
        pivot = df.pivot(index='rps', columns='burst', values=metric)
        # Ensure correct sort
        pivot = pivot.reindex(index=rps_values, columns=burst_values)
        
        data = pivot.values
        
        # Plot Heatmap
        # origin='lower' puts the first index (lowest RPS) at the bottom
        im = ax.imshow(data, cmap=cmap, aspect='auto', origin='lower')
        
        # Labels
        ax.set_xticks(np.arange(len(burst_values)))
        ax.set_yticks(np.arange(len(rps_values)))
        ax.set_xticklabels(burst_values)
        ax.set_yticklabels(rps_values)
        
        ax.set_xlabel('Bucket Size', fontsize=12)
        ax.set_ylabel('Refill Rate (RPS)', fontsize=12)
        ax.set_title(title, fontsize=14, weight='bold')
        
        # Add values
        for i in range(len(rps_values)):
            for j in range(len(burst_values)):
                val = data[i, j]
                
                if metric == 'amplification':
                    text_color = "black"
                    text = f"{val:.2f}"
                else:
                    # Increase threshold to 0.85 so white text only appears on basically black-blue backgrounds
                    text_color = "white" if val > np.nanmax(data) * 0.85 else "black"
                    text = f"{val:.0f}"
                
                ax.text(j, i, text, ha="center", va="center", color=text_color, fontsize=9)
                
        # Colorbar
        plt.colorbar(im, ax=ax)

    plt.tight_layout(rect=[0, 0.03, 1, 0.95])
    plt.savefig("sweep_heatmap.png")
    print("Doen! Heatmap saved to sweep_heatmap.png")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python bin/run_batch.py <base_yaml>")
        sys.exit(1)
        
    run_sweep(sys.argv[1])
