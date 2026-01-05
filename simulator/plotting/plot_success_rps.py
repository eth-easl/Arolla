#!/usr/bin/env python3
"""
Plot Successful RPS over time.
Similar to plot_all.py but focuses specifically on the throughput metric (x-axis: Time, y-axis: Successful RPS).
Supports multi-client comparison.
"""

import argparse
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from plotting.core import load_csv, get_time_range
from plotting.core.style import add_fault_events, setup_plot, save_plot
import matplotlib.pyplot as plt


def plot_success_rps(df, output_path, fault_events=None):
    """Plot Successful RPS (success_root per second) over time."""
    setup_plot("Successful RPS over Time", "Time (s)", "Successful RPS")
    
    t = df['timepoint']
    
    if 'client_id' in df.columns and df['client_id'].nunique() > 1:
        # Multi-client comparison
        clients = sorted(df['client_id'].unique())
        for i, client in enumerate(clients):
            subset = df[df['client_id'] == client].sort_values('timepoint')
            plt.plot(subset['timepoint'], subset['success_root'], 
                     label=f'{client}', linewidth=2)
    elif 'success_root' in df.columns:
        # Single client or aggregated
        plt.plot(t, df['success_root'], label='Successful RPS', linewidth=2, color='green')
    else:
        print("⚠ 'success_root' column missing.")
        return

    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_path)
    print(f"✓ Saved plot to {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description='Plot Successful RPS over Time',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('csv_file', help='Input CSV file from simulation')
    parser.add_argument('-o', '--output', default='success_rps.png',
                       help='Output file path (default: success_rps.png)')
    parser.add_argument('--fault-events', 
                       help='JSON file with fault events (optional)')
    parser.add_argument('--time-range',
                       help='Time range to plot (e.g., "0-60")')
    parser.add_argument('--smoothing-window', type=int, default=1,
                       help='Window size for rolling average smoothing (default: 10, set 1 to disable)')
    parser.add_argument('--figsize', default='10,6',
                       help='Figure size as "width,height" (default: 10,6)')
    
    args = parser.parse_args()
    
    # Load data
    try:
        input_path = Path(args.csv_file)
        if input_path.is_dir():
            print(f"Input is directory, looking for output.csv in {input_path}...")
            csv_file = input_path / "output.csv"
        else:
            csv_file = input_path

        print(f"Loading data from {csv_file}...")
        raw_df = load_csv(str(csv_file))
        print(f"✓ Loaded {len(raw_df)} rows")
        
        # Check for multi-client data
        if 'client_id' in raw_df.columns and raw_df['client_id'].nunique() > 1:
            print(f"  Detected multiple clients: {raw_df['client_id'].unique()}")
            df = raw_df # Keep all rows, let plot function handle grouping
        elif raw_df['timepoint'].duplicated().any():
            print("  Detected multiple entries per timepoint (unknown source). Aggregating...")
            
            sum_cols = ['root_requests', 'retries', 'success_root', 'completed', 
                        'failure_root', 'failure_retry', 'failure_queue_full', 
                        'failure_deadline', 'failure_server', 'total_request', 'total_failure']
            sum_cols = [c for c in sum_cols if c in raw_df.columns]
            
            agg_dict = {c: 'sum' for c in sum_cols}
            
            if 'queue_size' in raw_df.columns: agg_dict['queue_size'] = 'sum'
            if 'queue_avg_at_attempt_end' in raw_df.columns: agg_dict['queue_avg_at_attempt_end'] = 'mean'
            
            latency_cols = ['p50', 'p90', 'p95', 'p99', 'p99.9', 'Max']
            for lc in latency_cols:
                if lc in raw_df.columns:
                    agg_dict[lc] = 'max'
            
            df = raw_df.groupby('timepoint').agg(agg_dict).reset_index()
            print(f"✓ Aggregated to {len(df)} time buckets")
        else:
            df = raw_df
            print(f"✓ Loaded {len(df)} time buckets")
            
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1
    
    # Filter time range
    if args.time_range:
        try:
            start, end = map(float, args.time_range.split('-'))
            df = get_time_range(df, start, end)
        except ValueError:
            print(f"Error: Invalid time range: {args.time_range}")
            return 1

    # Optional Smoothing
    if args.smoothing_window > 1:
        print(f"Applying smoothing (window={args.smoothing_window})...")
        # Identify numeric columns to smooth
        numeric_cols = df.select_dtypes(include=['number']).columns
        # Exclude structural columns
        exclude = ['timepoint', 'rng_seed', 'replica_id']
        cols_to_smooth = [c for c in numeric_cols if c not in exclude]
        
        try:
            if 'client_id' in df.columns:
                # Group by client to smooth safely without mixing data across boundaries
                # usage of transform keeps index alignment
                df[cols_to_smooth] = df.groupby('client_id')[cols_to_smooth].transform(
                    lambda x: x.rolling(window=args.smoothing_window, min_periods=1).mean()
                )
            else:
                df[cols_to_smooth] = df[cols_to_smooth].rolling(
                    window=args.smoothing_window, min_periods=1
                ).mean()
        except Exception as e:
            print(f"Warning: Smoothing failed: {e}")

    # Set figsize if needed
    try:
        width, height = map(int, args.figsize.split(','))
        from plotting.core import style
        style.DEFAULT_FIGSIZE = (width, height)
    except ValueError:
        pass

    # Plot
    plot_success_rps(df, Path(args.output), args.fault_events)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
