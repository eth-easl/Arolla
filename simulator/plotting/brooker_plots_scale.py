#!/usr/bin/env python3
import sys
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("Usage: plot.py <results_dir> [-o output_dir]")
        sys.exit(1)
        
    results_dir = Path(sys.argv[1])
    output_dir = results_dir / "plots"
    if len(sys.argv) >= 4 and sys.argv[2] == "-o":
        output_dir = Path(sys.argv[3])
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    summary_path = results_dir / "summary.csv"
    if not summary_path.exists():
        print(f"Error: {summary_path} not found")
        sys.exit(1)
        
    df = pd.read_csv(summary_path)
    
    # Identify relevant columns
    # We swept 'clients.0.replicas' so that column should exist
    scale_col = "clients.0.replicas"
    if scale_col not in df.columns:
        # Fallback if column name differs (e.g. if using lockstep params, keys might be different depending on run_sweep impl)
        # Check standard sweep columns
        candidates = [c for c in df.columns if 'replicas' in c]
        if candidates:
            scale_col = candidates[0]
        else:
            print("Warning: Could not find 'replicas' column. Using default.")
            scale_col = None

    # Failure rate column (assuming first service p_fail)
    fail_col = "services.0.partial_failures.0.p_fail"
    if fail_col not in df.columns:
         # Try to find any p_fail column
         candidates = [c for c in df.columns if 'p_fail' in c]
         if candidates:
             fail_col = candidates[0]
    
    print(f"Plotting Scale analysis. Scale Col: {scale_col}, Fail Col: {fail_col}")
    
    # Process Data
    # 1. Simplify Client Names (strip numeric suffix .0, .1 etc)
    # The client names are likely "circuit-breaker.0", "circuit-breaker.1", etc.
    # We want to group them by base name.
    
    def get_base_name(name):
        if '.' in name and name.split('.')[-1].isdigit():
            return name.rsplit('.', 1)[0]
        return name
        
    df['base_client'] = df['client_name'].apply(get_base_name)
    
    # 2. Group and Aggregate
    # If scale_col exists, group by [failure, base_client, scale]
    group_cols = [fail_col, 'base_client']
    if scale_col:
        group_cols.append(scale_col)
        
    # We want the mean success rate across all replicas for a given config
    agg_df = df.groupby(group_cols).agg({
        'success_rate': 'mean',
        'goodput_rps': 'sum', # Goodput sums up across replicas
        'total_requests': 'sum'
    }).reset_index()
    
    # create hue label: "client (N clients)"
    def make_label(row):
        lbl = row['base_client']
        if scale_col:
            lbl += f" ({int(row[scale_col])} clients)"
        return lbl
        
    agg_df['Variant'] = agg_df.apply(make_label, axis=1)
    
    # Plot 1: Success Rate vs Failure
    plt.figure(figsize=(10, 6))
    sns.lineplot(data=agg_df, x=fail_col, y='success_rate', hue='Variant', marker='o')
    plt.title('Success Rate vs Failure Rate (Scaled)')
    plt.ylabel('Success Rate')
    plt.xlabel('Server Failure Probability')
    plt.grid(True, alpha=0.3)
    plt.savefig(output_dir / "scale_success_rate.png")
    print(f"Saved {output_dir}/scale_success_rate.png")

if __name__ == "__main__":
    main()
