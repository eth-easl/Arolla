#!/usr/bin/env python3
"""
Unified plotting tool for simulator results.

Generates all standard plots from a simulation CSV file.
"""

import argparse
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from plotting.core import load_csv, get_time_range
from plotting.core.style import add_fault_events
import matplotlib.pyplot as plt


def plot_latency(df, output_dir, fault_events=None):
    """Plot latency percentiles over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("Latency over Time", "Time (s)", "Latency (ms)")
    
    t = df['timepoint']
    if 'p50' in df.columns:
        plt.plot(t, df['p50'], label='P50', linewidth=2)
    if 'p90' in df.columns:
        plt.plot(t, df['p90'], label='P90', linewidth=2)
    if 'p95' in df.columns:
        plt.plot(t, df['p95'], label='P95', linewidth=2)
    if 'p99' in df.columns:
        plt.plot(t, df['p99'], label='P99', linewidth=2)
    if 'Max' in df.columns:
        plt.plot(t, df['Max'], label='Max', linewidth=1, alpha=0.7)
    
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'latency.png')
    plt.close()


def plot_qps(df, output_dir, fault_events=None):
    """Plot QPS (requests, retries, failures) over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("QPS over Time", "Time (s)", "Requests per Second")
    
    t = df['timepoint']
    if 'root_requests' in df.columns:
        plt.plot(t, df['root_requests'], label='Root Requests', linewidth=2)
    if 'retries' in df.columns:
        plt.plot(t, df['retries'], label='Retries', linewidth=2)
    if 'failure_root' in df.columns:
        plt.plot(t, df['failure_root'], label='Failures', linewidth=2)
    
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'qps.png')
    plt.close()


def plot_queue(df, output_dir, fault_events=None):
    """Plot queue size over time."""
    from plotting.core import setup_plot, save_plot
    
    # Check for queue size column (could be queue_size or queue_avg_at_attempt_end)
    queue_col = None
    if 'queue_size' in df.columns:
        queue_col = 'queue_size'
    elif 'queue_avg_at_attempt_end' in df.columns:
        queue_col = 'queue_avg_at_attempt_end'
    
    if queue_col is None:
        print("⚠ Skipping queue plot: no queue column found")
        return
    
    setup_plot("Queue Size over Time", "Time (s)", "Average Queue Size")
    
    t = df['timepoint']
    plt.plot(t, df[queue_col], label='Queue Size', linewidth=2)
    
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'queue.png')
    plt.close()


def plot_failures(df, output_dir, fault_events=None):
    """Plot failure breakdown over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("Failures over Time", "Time (s)", "Failures per Second")
    
    t = df['timepoint']
    plotted = False
    
    if 'failure_queue' in df.columns:
        plt.plot(t, df['failure_queue'], label='Queue Full', linewidth=2)
        plotted = True
    if 'failure_deadline' in df.columns:
        plt.plot(t, df['failure_deadline'], label='Deadline', linewidth=2)
        plotted = True
    if 'failure_server' in df.columns:
        plt.plot(t, df['failure_server'], label='Server Failure', linewidth=2)
        plotted = True
    
    if not plotted:
        print("⚠ Skipping failures plot: no failure columns found")
        plt.close()
        return
    
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'failures.png')
    plt.close()


def plot_success_rate(df, output_dir, fault_events=None):
    """Plot success rate over time."""
    from plotting.core import setup_plot, save_plot, calculate_success_rate
    
    success_rate = calculate_success_rate(df)
    if success_rate is None:
        print("⚠ Skipping success rate plot: required columns not found")
        return
    
    setup_plot("Success Rate over Time", "Time (s)", "Success Rate (%)")
    
    t = df['timepoint']
    plt.plot(t, success_rate, label='Success Rate', linewidth=2, color='green')
    plt.ylim(0, 105)  # 0-100% with some headroom
    
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'success_rate.png')
    plt.close()


def plot_failure_breakdown(df, output_dir, fault_events=None):
    """Plot failure breakdown as stacked area chart."""
    from plotting.core import setup_plot, save_plot
    
    # Check if failure columns exist
    if 'failure_deadline' not in df.columns or 'failure_server' not in df.columns:
        print("⚠ Skipping failure breakdown plot: required columns not found")
        return
    
    setup_plot("Failures (%) over Time by Reason Breakdown", "Time (s)", "Failure rate (%)")
    
    t = df['timepoint']
    
    # Calculate total failures (all types combined)
    total_failures = df['failure_deadline'] + df['failure_server'] + df['failure_queue_full']
    
    # Calculate what percentage each failure type is of total failures
    deadline_pct = (df['failure_deadline'] / total_failures.replace(0, 1)) * 100
    server_pct = (df['failure_server'] / total_failures.replace(0, 1)) * 100
    queue_pct = (df['failure_queue_full'] / total_failures.replace(0, 1)) * 100
    
    # Replace NaN with 0
    deadline_pct = deadline_pct.fillna(0)
    server_pct = server_pct.fillna(0)
    queue_pct = queue_pct.fillna(0)
    
    # Calculate user-facing failure rate based on COMPLETED root requests
    # Use (success_root + failure_root) as denominator to avoid >100% due to timing artifacts
    completed_root = df['success_root'] + df['failure_root']
    user_failure_rate = (df['failure_root'] / completed_root.replace(0, 1)) * 100
    user_failure_rate = user_failure_rate.fillna(0)

    
    # Scale breakdown by user failure rate to get stacked failure rates
    deadline_rate = (deadline_pct / 100) * user_failure_rate
    server_rate = (server_pct / 100) * user_failure_rate
    queue_rate = (queue_pct / 100) * user_failure_rate
    
    # Stack plot - colors match reference image
    plt.stackplot(t, deadline_rate, server_rate, queue_rate,
                  labels=['deadline', 'server', 'queue_full'],
                  colors=['#1f77b4', '#ff7f0e', '#2ca02c'],
                  alpha=1.0)
    
    plt.ylim(0, 100)
    # Add fault events AFTER (blends with underlying colors to create purple)
    add_fault_events(fault_events)
    plt.legend(loc='upper left', bbox_to_anchor=(0, 1.0))
    save_plot(output_dir / 'failure_breakdown.png')
    plt.close()


def plot_request_amplification(df, output_dir, fault_events=None):
    """Plot request amplification over time."""
    from plotting.core import setup_plot, save_plot
    
    # Check if required columns exist
    if 'root_requests' not in df.columns or 'retries' not in df.columns:
        print("⚠ Skipping amplification plot: required columns not found")
        return
    
    setup_plot("Request Amplification Over Time", "Time (s)", "Amplification (%)")
    
    t = df['timepoint']
    
    # Calculate amplification as additional load percentage (0% = no retries, 25% = 1.25x load)
    amplification = (df['retries'] / df['root_requests'].replace(0, 1)) * 100
    amplification = amplification.fillna(0.0)
    
    plt.plot(t, amplification, label='Request amplification', linewidth=2, color='#1f77b4')
    
    # Add max sustainable amplification line (max additional load = 25%, total = 125%)
    plt.axhline(y=125, color='red', linestyle='--', linewidth=2, 
                label='Max Sustainable (125%)', alpha=0.7)
    
    plt.ylim(0, max(amplification.max() * 1.1, 350))
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'amplification.png')
    plt.close()


def plot_success_rate_detailed(df, output_dir, fault_events=None):
    """Plot detailed success rate over time with filled regions."""
    from plotting.core import setup_plot, save_plot
    
    # Check if required columns exist
    if 'success_root' not in df.columns or 'root_requests' not in df.columns:
        print("⚠ Skipping detailed success rate plot: required columns not found")
        return
    
    setup_plot("Success Rate over Time", "Time (s)", "Success Rate (%)")
    
    t = df['timepoint']
    
    # Calculate success rate
    success_rate = (df['success_root'] / df['root_requests'].replace(0, 1)) * 100
    success_rate = success_rate.fillna(100.0)
    
    plt.plot(t, success_rate, label='Success Rate', linewidth=2, color='green')
    plt.fill_between(t, success_rate, 100, alpha=0.3, color='green', label='Success Region')
    plt.fill_between(t, 0, success_rate, alpha=0.3, color='red', label='Failure Region')
    
    plt.ylim(0, 105)
    add_fault_events(fault_events)
    plt.legend(loc='lower left', bbox_to_anchor=(0, 0.05))
    save_plot(output_dir / 'success_rate_detailed.png')
    plt.close()



def main():
    parser = argparse.ArgumentParser(
        description='Generate all plots from simulation results',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Generate all plots
  python plotting/plot_all.py results/output.csv
  
  # Custom output directory
  python plotting/plot_all.py results/output.csv -o plots/
  
  # With fault events
  python plotting/plot_all.py results/output.csv --fault-events events.json
  
  # Specific time range
  python plotting/plot_all.py results/output.csv --time-range 0-60
        '''
    )
    
    parser.add_argument('csv_file', help='Input CSV file from simulation')
    parser.add_argument('-o', '--output', default='plots',
                       help='Output directory for plots (default: plots/)')
    parser.add_argument('--fault-events', 
                       help='JSON file with fault events (optional)')
    parser.add_argument('--time-range',
                       help='Time range to plot (e.g., "0-60" for first 60s)')
    parser.add_argument('--figsize', default='10,6',
                       help='Figure size as "width,height" (default: 10,6)')
    
    args = parser.parse_args()
    
    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load data
    try:
        print(f"Loading data from {args.csv_file}...")
        raw_df = load_csv(args.csv_file)
        print(f"✓ Loaded {len(raw_df)} rows")
        
        # Aggregate by timepoint if multiple rows per timepoint (e.g. multiple clients)
        # Check if 'client_id' or similar exists, or simply check duplicate timepoints
        if raw_df['timepoint'].duplicated().any():
            print("  Detected multiple entries per timepoint (multi-client). Aggregating...")
            
            # Define aggregation rules
            # Sum counts
            sum_cols = ['root_requests', 'retries', 'success_root', 'completed', 
                        'failure_root', 'failure_retry', 'failure_queue_full', 
                        'failure_deadline', 'failure_server', 'total_request', 'total_failure']
            sum_cols = [c for c in sum_cols if c in raw_df.columns]
            
            # Mean for queue if it represents size/state
            # Max for latencies (conservative) or Mean? 
            # Usually we want either overall P99 (hard to exact from sub-p99s) or just visualize average P99.
            # Let's take Mean for queue and Max for latencies to show worst case.
            
            agg_dict = {c: 'sum' for c in sum_cols}
            
            if 'queue_size' in raw_df.columns: agg_dict['queue_size'] = 'sum' # Total queue size across all
            if 'queue_avg_at_attempt_end' in raw_df.columns: agg_dict['queue_avg_at_attempt_end'] = 'mean'
            
            latency_cols = ['p50', 'p90', 'p95', 'p99', 'p99.9', 'Max']
            for lc in latency_cols:
                if lc in raw_df.columns:
                    agg_dict[lc] = 'max' # Show worst client latency
            
            df = raw_df.groupby('timepoint').agg(agg_dict).reset_index()
            print(f"✓ Aggregated to {len(df)} time buckets")
        else:
            df = raw_df
            print(f"✓ Loaded {len(df)} time buckets")
            
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1
    
    # Apply time range filter
    if args.time_range:
        try:
            start, end = map(float, args.time_range.split('-'))
            df = get_time_range(df, start, end)
            print(f"✓ Filtered to time range {start}-{end}s ({len(df)} buckets)")
        except ValueError:
            print(f"Error: Invalid time range format: {args.time_range}")
            print("Use format: START-END (e.g., 0-60)")
            return 1
    
    # Set figure size
    try:
        width, height = map(int, args.figsize.split(','))
        from plotting.core import style
        style.DEFAULT_FIGSIZE = (width, height)
    except ValueError:
        print(f"Warning: Invalid figsize format: {args.figsize}, using default")
    
    # Generate all plots
    print(f"\nGenerating plots in {output_dir}/...")
    
    plot_latency(df, output_dir, args.fault_events)
    plot_qps(df, output_dir, args.fault_events)
    plot_queue(df, output_dir, args.fault_events)
    
    # Additional analysis plots
    plot_failure_breakdown(df, output_dir, args.fault_events)
    plot_request_amplification(df, output_dir, args.fault_events)
    
    print(f"\n✅ All plots generated in {output_dir}/")
    print(f"   - latency.png")
    print(f"   - qps.png")
    print(f"   - queue.png (if available)")
    print(f"   - failure_breakdown.png (if available)")
    print(f"   - amplification.png (if available)")



    
    return 0


if __name__ == '__main__':
    sys.exit(main())
