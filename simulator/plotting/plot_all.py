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

from plotting.core import load_and_aggregate, get_time_range
from plotting.core.style import add_fault_events
import matplotlib.pyplot as plt

# Default color mapping (matching plot_success_rps.py)
CLIENT_COLORS = {
    'no_retries': 'grey',
    'three_retries': 'cornflowerblue',
    'exponential_backoff_jitter': 'green',
    'circuit_breaker': 'purple',
    'retry_budget': 'orange'
}


def plot_latency(df, output_dir, fault_events=None, **kwargs):
    """Plot latency percentiles over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("Latency over Time", "Time (s)", "Latency (ms)")
    
    t = df['timepoint']
    lw = kwargs.get('line_width', 2.0)
    
    if 'p50' in df.columns:
        plt.plot(t, df['p50'], label='P50', linewidth=lw)
    if 'p90' in df.columns:
        plt.plot(t, df['p90'], label='P90', linewidth=lw)
    if 'p95' in df.columns:
        plt.plot(t, df['p95'], label='P95', linewidth=lw)
    if 'p99' in df.columns:
        plt.plot(t, df['p99'], label='P99', linewidth=lw)
    if 'Max' in df.columns:
        plt.plot(t, df['Max'], label='Max', linewidth=max(1.0, lw*0.5), alpha=0.7)
    
    add_fault_events(fault_events)
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'latency.{fmt}')
    plt.close()


def plot_qps(df, output_dir, fault_events=None, **kwargs):
    """Plot QPS (requests, retries, failures) over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("", "Time (s)", "Requests per Second")
    
    t = df['timepoint']
    lw = kwargs.get('line_width', 2.0)
    
    if 'root_requests' in df.columns:
        plt.plot(t, df['root_requests'], label='Root Requests', linewidth=lw, color='gray')
    if 'retries' in df.columns:
        plt.plot(t, df['retries'], label='Retries', linewidth=lw, color='orange')
    if 'failure_root' in df.columns:
        plt.plot(t, df['failure_root'], label='Failures', linewidth=lw, color='red')
    
    
    add_fault_events(fault_events)
    
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
                     
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'qps.{fmt}')
    plt.close()


def plot_queue(df, output_dir, fault_events=None, **kwargs):
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
    lw = kwargs.get('line_width', 2.0)
    plt.plot(t, df[queue_col], label='Queue Size', linewidth=lw)
    
    add_fault_events(fault_events)
    
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}

    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'queue.{fmt}')
    plt.close()


def plot_failures(df, output_dir, fault_events=None, **kwargs):
    """Plot failure breakdown over time."""
    from plotting.core import setup_plot, save_plot
    
    setup_plot("Failures over Time", "Time (s)", "Failures per Second")
    
    t = df['timepoint']
    plotted = False
    lw = kwargs.get('line_width', 2.0)
    
    if 'failure_queue' in df.columns:
        plt.plot(t, df['failure_queue'], label='Queue Full', linewidth=lw)
        plotted = True
    if 'failure_deadline' in df.columns:
        plt.plot(t, df['failure_deadline'], label='Deadline', linewidth=lw)
        plotted = True
    if 'failure_server' in df.columns:
        plt.plot(t, df['failure_server'], label='Server Failure', linewidth=lw)
        plotted = True
    
    if not plotted:
        print("⚠ Skipping failures plot: no failure columns found")
        plt.close()
        return
    
    add_fault_events(fault_events)
    
    
    # helper for legend
    # helper for legend
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'failures.{fmt}')
    plt.close()


def plot_success_rate(df, output_dir, fault_events=None, **kwargs):
    """Plot success rate over time."""
    from plotting.core import setup_plot, save_plot, calculate_success_rate
    
    success_rate = calculate_success_rate(df)
    if success_rate is None:
        print("⚠ Skipping success rate plot: required columns not found")
        return
    
    setup_plot("", "Time (s)", "Success Rate (%)")
    
    t = df['timepoint']
    lw = kwargs.get('line_width', 2.0)
    plt.plot(t, success_rate, label='Success Rate', linewidth=lw, color='green')
    plt.ylim(0, 105)  # 0-100% with some headroom
    
    add_fault_events(fault_events)
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'success_rate.{fmt}')
    plt.close()


def plot_success_rate_comparison(df, output_dir, fault_events=None, **kwargs):
    """Plot success rate comparison for multiple clients on one chart."""
    from plotting.core import setup_plot, save_plot
    
    # Check key columns
    if 'base_client' not in df.columns:
        return

    setup_plot("", "Time (s)", "Success Rate (%)")
    
    clients = sorted(df['base_client'].unique())

    # Apply custom sort order if provided
    legend_order = kwargs.get('legend_order')
    if legend_order:
        priority_list = [c.strip() for c in legend_order.split(',')]
        def sort_key(name):
            try:
                return (0, priority_list.index(name))
            except ValueError:
                return (1, name)
        clients = sorted(clients, key=sort_key)
        
    lw = kwargs.get('line_width', 2.0)
    
    for client in clients:
        subset = df[df['base_client'] == client].sort_values('timepoint')
        t = subset['timepoint']
        
        # Calculate success rate for this client
        # success_rate = success_root / (root_requests) * 100? or (success + failure)?
        # Using (success + failure) is safer for strict "processed" rate
        processed = subset['success_root'] + subset['failure_root']
        rate = (subset['success_root'] / processed.replace(0, 1)) * 100
        rate = rate.fillna(0.0)
        
        # Determine color
        color = CLIENT_COLORS.get(client, None)
        
        # Determine label
        label = client
        if 'legend_labels' in kwargs and kwargs['legend_labels']:
            labels_map = kwargs['legend_labels']
            if client in labels_map:
                label = labels_map[client]
        
        plt.plot(t, rate, label=label, linewidth=lw, color=color)

    plt.ylim(0, 105)
    add_fault_events(fault_events)
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'success_rate_comparison.{fmt}')
    plt.close()


def plot_failure_breakdown(df, output_dir, fault_events=None, **kwargs):
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
    
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'upper left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'failure_breakdown.{fmt}')
    plt.close()


def plot_request_amplification(df, output_dir, fault_events=None, **kwargs):
    """Plot request amplification over time."""
    from plotting.core import setup_plot, save_plot
    
    # Check if required columns exist
    if 'root_requests' not in df.columns or 'retries' not in df.columns:
        print("⚠ Skipping amplification plot: required columns not found")
        return
    
    setup_plot("Request Amplification Over Time", "Time (s)", "Amplification (%)")
    
    t = df['timepoint']
    lw = kwargs.get('line_width', 2.0)
    
    # Calculate amplification as additional load percentage (0% = no retries, 25% = 1.25x load)
    amplification = (df['retries'] / df['root_requests'].replace(0, 1)) * 100
    amplification = amplification.fillna(0.0)
    
    plt.plot(t, amplification, label='Request amplification', linewidth=lw, color='#1f77b4')
    
    # Add max sustainable amplification line (max additional load = 25%, total = 125%)
    plt.axhline(y=125, color='red', linestyle='--', linewidth=lw, 
                label='Max Sustainable (125%)', alpha=0.7)
    
    plt.ylim(0, max(amplification.max() * 1.1, 350))
    add_fault_events(fault_events)
    
    legend_kwargs = {'loc': kwargs.get('legend_loc', 'lower left'), 
                     'frameon': not kwargs.get('no_legend_frame', False)}
    if kwargs.get('legend_bbox'): legend_kwargs['bbox_to_anchor'] = kwargs.get('legend_bbox')
    if kwargs.get('legend_size'): legend_kwargs['prop'] = {'size': kwargs.get('legend_size')}
    
    plt.legend(**legend_kwargs)
    fmt = kwargs.get('format', 'png')
    save_plot(output_dir / f'amplification.{fmt}')
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
    # Note: This function doesn't take kwargs yet in definitions above, so skipping dynamic format, keeping png
    # Wait, I should probably update it too if it's used. But it's not called in generate_plot_set.
    save_plot(output_dir / 'success_rate_detailed.png')
    plt.close()



def _plot_comparison_metric(df, clients, metric_col, title, ylabel, output_path,
                            fault_events=None, **kwargs):
    """Plot a single metric for all clients on one chart (comparison view)."""
    from plotting.core import setup_plot, save_plot
    from plotting.core.style import add_fault_events as _add_faults

    setup_plot(title, "Time (s)", ylabel)
    lw = kwargs.get('line_width', 2.0)
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']

    for i, client in enumerate(clients):
        subset = df[df['base_client'] == client].sort_values('timepoint')
        if metric_col in subset.columns:
            color = CLIENT_COLORS.get(client, colors[i % len(colors)])
            plt.plot(subset['timepoint'], subset[metric_col],
                     label=client, color=color, linewidth=lw)

    _add_faults(fault_events)
    plt.legend(loc='best')
    save_plot(output_path)
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
    parser.add_argument('--smoothing-window', type=int, default=1,
                       help='Window size for rolling average smoothing (default: 1, disabled)')
    parser.add_argument('--figsize', default='10,6',
                       help='Figure size as "width,height" (default: 10,6)')
    parser.add_argument('--font-size', type=int,
                       help='Global font size')
    parser.add_argument('--tick-size', type=int,
                       help='Tick label size')
    parser.add_argument('--format', default='png', choices=['png', 'pdf', 'svg', 'jpg'],
                       help='Output format (default: png)')
    parser.add_argument('--line-width', type=float, default=2.0,
                       help='Line width (default: 2.0)')
    parser.add_argument('--legend-loc', default='lower left',
                       help='Legend location (default: lower left)')
    parser.add_argument('--legend-bbox',
                       help='Legend bbox_to_anchor (x,y)')
    parser.add_argument('--legend-size', type=int,
                       help='Legend text size')
    parser.add_argument('--no-legend-frame', action='store_true',
                       help='Remove legend frame')
    parser.add_argument('--legend-order',
                       help='Comma-separated list of client names to order legend')
    parser.add_argument('--legend-labels',
                       help='Mapping of client names to labels (e.g. "client_a=Label A,client_b=Label B")')
    
    args = parser.parse_args()
    
    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load data
    try:
        input_path = Path(args.csv_file)
        if input_path.is_dir():
             print(f"Input is directory, looking for output.csv in {input_path}...")
             csv_file_path = input_path / "output.csv"
        else:
             csv_file_path = input_path

        print(f"Loading data from {csv_file_path}...")
        df_by_client, df_global, clients = load_and_aggregate(str(csv_file_path))
        print(f"✓ Loaded data, identified {len(clients)} clients: {clients}")
            
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1
    
    
    # Prepare style kwargs
    style_kwargs = {
        'line_width': args.line_width,
        'legend_loc': args.legend_loc,
        'legend_size': args.legend_size,
        'no_legend_frame': args.no_legend_frame,
        'format': args.format,
        'legend_order': args.legend_order
    }
    
    if args.legend_labels:
        try:
            # Parse "k=v,k2=v2"
            label_map = {}
            for pair in args.legend_labels.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    label_map[k.strip()] = v.strip()
            style_kwargs['legend_labels'] = label_map
        except Exception as e:
            print(f"Warning: Failed to parse legend_labels: {e}")
    
    if args.legend_bbox:
        try:
             style_kwargs['legend_bbox'] = tuple(map(float, args.legend_bbox.split(',')))
        except ValueError:
             print(f"Warning: Invalid legend_bbox: {args.legend_bbox}")
    
    # Apply global font settings
    if args.font_size:
        plt.rcParams.update({'font.size': args.font_size})
    if args.tick_size:
        plt.rcParams.update({
             'xtick.labelsize': args.tick_size,
             'ytick.labelsize': args.tick_size
        })

    # helper for generating plot set
    def generate_plot_set(df, out_dir, prefix=""):
        # Time range filter
        if args.time_range:
            try:
                start, end = map(float, args.time_range.split('-'))
                df = get_time_range(df, start, end)
            except ValueError:
                pass
        
        # Apply Smoothing
        if args.smoothing_window > 1:
            print(f"  Applying smoothing (window={args.smoothing_window}) to {prefix}...")
            # Identify numeric columns to smooth
            numeric_cols = df.select_dtypes(include=['number']).columns
            # Exclude structural columns
            exclude = ['timepoint', 'rng_seed', 'replica_id']
            cols_to_smooth = [c for c in numeric_cols if c not in exclude]
            
            try:
                # Use simple rolling mean for all numeric columns
                df[cols_to_smooth] = df[cols_to_smooth].rolling(
                    window=args.smoothing_window, min_periods=1
                ).mean()
            except Exception as e:
                print(f"  Warning: Smoothing failed: {e}")

        plot_latency(df, out_dir, args.fault_events, **style_kwargs)
        plot_qps(df, out_dir, args.fault_events, **style_kwargs)
        plot_queue(df, out_dir, args.fault_events, **style_kwargs)
        plot_failures(df, out_dir, args.fault_events, **style_kwargs)
        plot_success_rate(df, out_dir, args.fault_events, **style_kwargs)
        plot_failure_breakdown(df, out_dir, args.fault_events, **style_kwargs)
        plot_request_amplification(df, out_dir, args.fault_events, **style_kwargs)
        print(f"  ✓ Generated plots for {prefix} in {out_dir}")

    # Set figure size
    try:
        width, height = map(int, args.figsize.split(','))
        from plotting.core import style
        style.DEFAULT_FIGSIZE = (width, height)
    except ValueError:
        print(f"Warning: Invalid figsize format: {args.figsize}, using default")
    
    print(f"\nGenerating plots in {output_dir}/...")

    # --- analysis/ folder: cross-client comparisons only (multi-client) ---
    if len(clients) > 1 and 'base_client' in df_by_client.columns:
        analysis_dir = output_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)

        print("  Generating cross-client comparison plots...")
        plot_success_rate_comparison(df_by_client, analysis_dir, args.fault_events, **style_kwargs)

        # Additional comparison plots (absorbed from compare_clients.py)
        for metric, title, ylabel, fname in [
            ('p99', 'P99 Latency Comparison', 'Latency (ms)', 'compare_latency_p99'),
            ('p50', 'P50 Latency Comparison', 'Latency (ms)', 'compare_latency_p50'),
            ('total_request', 'Throughput (Sent)', 'Avg Requests/sec', 'compare_throughput'),
        ]:
            _plot_comparison_metric(
                df_by_client, clients, metric, title, ylabel,
                analysis_dir / f'{fname}.{args.format}', args.fault_events, **style_kwargs
            )

    # --- clients/ folder: per-client individual plots ---
    clients_dir = output_dir / "clients"
    for client in clients:
        client_dir = clients_dir / client
        client_dir.mkdir(parents=True, exist_ok=True)
        subset = df_by_client[df_by_client['base_client'] == client]
        print(f"Processing client: {client}...")
        generate_plot_set(subset, client_dir, f"Client: {client}")
    
    print(f"\n✅ All plots generated in {output_dir}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
