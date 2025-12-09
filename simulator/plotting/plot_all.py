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
    plt.legend()
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
    plt.legend()
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
    plt.legend()
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
    plt.legend()
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
    plt.legend()
    save_plot(output_dir / 'success_rate.png')
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
        df = load_csv(args.csv_file)
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
    plot_failures(df, output_dir, args.fault_events)
    plot_success_rate(df, output_dir, args.fault_events)
    
    print(f"\n✅ All plots generated in {output_dir}/")
    print(f"   - latency.png")
    print(f"   - qps.png")
    print(f"   - queue.png (if available)")
    print(f"   - failures.png (if available)")
    print(f"   - success_rate.png (if available)")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
