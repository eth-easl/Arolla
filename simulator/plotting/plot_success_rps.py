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


# Default color mapping for known clients
CLIENT_COLORS = {
    'no_retries': 'grey',
    'three_retries': 'cornflowerblue',
    'exponential_backoff_jitter': 'green',
    'circuit_breaker': 'purple',
    'retry_budget': 'orange'
}

def plot_success_rps(df, output_path, fault_events=None, line_color=None, 
                     legend_loc='lower left', legend_bbox=None, legend_order=None,
                     legend_size=None, line_width=2.0, no_legend_frame=False, figsize=None,
                     y_label="Successful RPS"):
    """Plot Successful RPS (success_root per second) over time."""
    
    # Use provided figsize or let setup_plot use default
    kwargs = {}
    if figsize:
        kwargs['figsize'] = figsize
        
    fig = setup_plot("", "Time (s)", y_label, **kwargs)
    
    # ... (skipping frame styling for brevity in match, but included in file) ...
    # Re-fetch ax in case setup_plot changed it
    ax = plt.gca()
    
    # Custom Frame Styling (User Request: Frame same color as grid)
    # Get grid color (setup_plot enables grid)
    grid_lines = ax.get_xgridlines()
    if grid_lines:
        grid_color = grid_lines[0].get_color()
        # Set all spines to grid color
        for spine in ax.spines.values():
            spine.set_color(grid_color)
            spine.set_linewidth(1) # Ensure visible
    else:
        # Fallback if no grid lines found immediately
        gray = '#D3D3D3' # Light gray
        for spine in ax.spines.values():
            spine.set_color(gray)
    
    # Process line_color
    color_map = None
    
    # Use default map if no override provided
    if not line_color:
        color_map = CLIENT_COLORS.copy()
    
    if line_color:
        # ... (same parsing) ...
        import json
        import ast
        from cycler import cycler
        
        parsed = None
        try:
             parsed = json.loads(line_color)
        except:
             try: parsed = ast.literal_eval(line_color)
             except: pass
        
        if parsed is not None:
             if isinstance(parsed, dict):
                color_map = parsed
             elif isinstance(parsed, list):
                plt.rcParams['axes.prop_cycle'] = cycler(color=parsed)
        else:
            if ',' in line_color and '[' not in line_color:
                colors = [c.strip() for c in line_color.split(',')]
                plt.rcParams['axes.prop_cycle'] = cycler(color=colors)
    
    t = df['timepoint']
    y_col = 'calculated_metric' if 'calculated_metric' in df.columns else 'success_root'
    
    if 'client_id' in df.columns and df['client_id'].nunique() > 1:
        # Multi-client comparison
        clients = sorted(df['client_id'].unique())
        
        # Apply custom sort order if provided
        if legend_order:
            priority_list = [c.strip() for c in legend_order.split(',')]
            # Sort clients: items in priority list come first in that order, then others sorted alphabetically
            def sort_key(name):
                try:
                    return (0, priority_list.index(name))
                except ValueError:
                    return (1, name)
            
            clients = sorted(clients, key=sort_key)
            
        for i, client in enumerate(clients):
            subset = df[df['client_id'] == client].sort_values('timepoint')
            
            kwargs = {'label': f'{client}', 'linewidth': line_width}
            
            # Apply color if mapped
            if color_map and client in color_map:
                kwargs['color'] = color_map[client]
            # Apply single color override if strictly single string provided and NO cycle set
            elif line_color and not color_map and ',' not in line_color:
                 is_json_list = False
                 try: 
                     if isinstance(json.loads(line_color), list): is_json_list = True
                 except: pass
                 
                 if not is_json_list:
                     kwargs['color'] = line_color
            
            plt.plot(subset['timepoint'], subset[y_col], **kwargs)
            
    elif y_col in df.columns:
        # Single client or aggregated
        kwargs = {'label': 'Metric', 'linewidth': line_width}
        if line_color:
             if not (line_color.strip().startswith('[') or line_color.strip().startswith('{') or ',' in line_color):
                 kwargs['color'] = line_color
        else:
             kwargs['color'] = 'green'
             
        plt.plot(t, df[y_col], **kwargs)
    else:
        print(f"⚠ '{y_col}' column missing.")
        return

    add_fault_events(fault_events)
    
    # Process legend bbox if provided
    bbox = None
    if legend_bbox:
        try:
            bbox = tuple(map(float, legend_bbox.split(',')))
        except ValueError:
            print(f"Warning: Invalid legend_bbox format: {legend_bbox}")
            
    # Prepare legend kwargs
    legend_kwargs = {'loc': legend_loc, 'bbox_to_anchor': bbox, 'frameon': not no_legend_frame}
    if legend_size:
        legend_kwargs['prop'] = {'size': legend_size}
        
    plt.legend(**legend_kwargs)
    save_plot(output_path)
    print(f"✓ Saved plot to {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description='Plot Successful RPS over Time',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('csv_file', help='Input CSV file from simulation')
    parser.add_argument('-o', '--output', default='success_rps.pdf',
                       help='Output file path (default: success_rps.pdf)')
    parser.add_argument('--fault-events', 
                       help='JSON file with fault events (optional)')
    parser.add_argument('--time-range',
                       help='Time range to plot (e.g., "0-60")')
    parser.add_argument('--smoothing-window', type=int, default=1,
                       help='Window size for rolling average smoothing (default: 10, set 1 to disable)')
    parser.add_argument('--figsize', default='6,6',
                       help='Figure size as "width,height" (default: 10,6)')
    parser.add_argument('--font-size', type=int,
                       help='Global font size')
    parser.add_argument('--tick-size', type=int,
                       help='Tick label size')
    parser.add_argument('--line-color',
                       help='Line color (name, list of names, or JSON mapping)')
    parser.add_argument('--line-width', type=float, default=2.0,
                       help='Line width (default: 2.0)')
    parser.add_argument('--legend-loc', default='lower left',
                       help='Legend location (default: lower left)')
    parser.add_argument('--legend-bbox',
                       help='Legend bbox_to_anchor (x,y)')
    parser.add_argument('--legend-order',
                       help='Comma-separated list of client names to order legend')
    parser.add_argument('--legend-size', type=int,
                       help='Legend text size')
    parser.add_argument('--no-legend-frame', action='store_true',
                       help='Remove legend frame')
    parser.add_argument('--metric', choices=['rps', 'success_rate'], default='rps',
                       help='Metric to plot: "rps" (Success RPS) or "success_rate" (Success Rate %)')
    
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
            
            # Helper to extract base name
            def get_base_name(name):
                parts = name.rsplit('.', 1)
                if len(parts) == 2 and parts[1].isdigit():
                    return parts[0]
                return name

            raw_df['base_client'] = raw_df['client_id'].apply(get_base_name)
            
            # Check if aggregation is needed
            if raw_df['base_client'].nunique() < raw_df['client_id'].nunique():
                print("  Consolidating replicas (summing metrics)...")
                
                # Metrics to sum
                sum_cols = ['root_requests', 'retries', 'success_root', 'completed', 
                            'failure_root', 'failure_retry', 'failure_queue_full', 
                            'failure_deadline', 'failure_server', 'total_request', 'total_failure']
                
                # Filter to existing columns
                agg_dict = {c: 'sum' for c in sum_cols if c in raw_df.columns}
                
                if 'queue_size' in raw_df.columns: agg_dict['queue_size'] = 'sum'
                if 'queue_avg_at_attempt_end' in raw_df.columns: agg_dict['queue_avg_at_attempt_end'] = 'mean'
                
                # Perform Groupby
                df = raw_df.groupby(['timepoint', 'base_client']).agg(agg_dict).reset_index()
                
                # Rename base_client back to client_id so the plotting function uses the group name
                df.rename(columns={'base_client': 'client_id'}, inplace=True)
                
                print(f"  Merged {raw_df['client_id'].nunique()} clients into {df['client_id'].nunique()} groups: {df['client_id'].unique()}")
            else:
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

        # Calculate Metric
        if args.metric == 'success_rate':
            # success_rate = success_root / (success_root + failure_root)
            # We use summing of success/failure columns from aggregation
            df['total_finished'] = df['success_root'] + df['failure_root']
            df['calculated_metric'] = df.apply(
                lambda row: (row['success_root'] / row['total_finished'] * 100.0) if row['total_finished'] > 0 else 0.0, 
                axis=1
            )
            y_label = "Success Rate (%)"
        else:
            df['calculated_metric'] = df['success_root']
            y_label = "Successful RPS"
            
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
    figsize_tuple = None
    try:
        width, height = map(int, args.figsize.split(','))
        figsize_tuple = (width, height)
    except ValueError:
        pass

    # Handle output path
    output_path = Path(args.output)
    
    # If it ends in a known extension or existing file, treat as file
    if output_path.suffix or output_path.is_file():
         # If relative filename (not absolute), anchor to plots dir in input dir
         if not output_path.is_absolute() and Path(args.csv_file).is_dir():
             plots_dir = Path(args.csv_file) / "plots"
             plots_dir.mkdir(parents=True, exist_ok=True)
             output_path = plots_dir / output_path
    else:
        # Treat as directory (existing or not)
        output_path.mkdir(parents=True, exist_ok=True)
        # Use the default filename from parser configuration
        default_name = Path(parser.get_default('output')).name
        output_path = output_path / default_name

    # Style customization
    if args.font_size:
        plt.rcParams.update({'font.size': args.font_size})
    
    if args.tick_size:
        plt.rcParams.update({
            'xtick.labelsize': args.tick_size,
            'ytick.labelsize': args.tick_size
        })

    # Plot
    plot_success_rps(df, output_path, args.fault_events, 
                     line_color=args.line_color,
                     legend_loc=args.legend_loc,
                     legend_bbox=args.legend_bbox,
                     legend_order=args.legend_order,
                     legend_size=args.legend_size,
                     line_width=args.line_width,
                     no_legend_frame=args.no_legend_frame,
                     figsize=figsize_tuple,
                     y_label=y_label)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
