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
                     legend_size=None, line_width=2.0, no_legend_frame=False, figsize=None):
    """Plot Successful RPS (success_root per second) over time."""
    
    # Use provided figsize or let setup_plot use default
    kwargs = {}
    if figsize:
        kwargs['figsize'] = figsize
        
    fig = setup_plot("", "Time (s)", "Successful RPS", **kwargs)
    
    # Custom Frame Styling (User Request: Frame same color as grid)
    ax = plt.gca()
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
        import json
        import ast
        from cycler import cycler
        
        parsed = None
        # Try JSON first
        try:
            parsed = json.loads(line_color)
        except json.JSONDecodeError:
            # Try AST literal eval (handles single quotes typical in YAML/Python)
            try:
                parsed = ast.literal_eval(line_color)
            except (ValueError, SyntaxError):
                pass
        
        if parsed is not None:
             if isinstance(parsed, dict):
                color_map = parsed
             elif isinstance(parsed, list):
                plt.rcParams['axes.prop_cycle'] = cycler(color=parsed)
        else:
            # Not structured, treat as string fallback
            # Check for comma-separated list without brackets
            if ',' in line_color and '[' not in line_color:
                colors = [c.strip() for c in line_color.split(',')]
                plt.rcParams['axes.prop_cycle'] = cycler(color=colors)
            else:
                # Single color string
                pass

    t = df['timepoint']
    
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
                 # Check if we successfully parsed a list earlier (prop_cycle set)
                 # If prop_cycle was set, we don't need to do anything here.
                 # If it wasn't set, and it's a single string, apply it (all lines same color)
                 is_json_list = False
                 try: 
                     if isinstance(json.loads(line_color), list): is_json_list = True
                 except: pass
                 
                 if not is_json_list:
                     kwargs['color'] = line_color
            
            plt.plot(subset['timepoint'], subset['success_root'], **kwargs)
            
    elif 'success_root' in df.columns:
        # Single client or aggregated
        kwargs = {'label': 'Successful RPS', 'linewidth': line_width}
        if line_color:
             # Basic handling: just pass the string (or whatever it is) to color
             # If it was a list/dict, they might fail here if not handled, 
             # but for single line we expect single color.
             # If user passed a list for a single line, matplotlib might complain or cycle.
             # Let's trust the user or the prop_cycle we set above.
             if not (line_color.strip().startswith('[') or line_color.strip().startswith('{') or ',' in line_color):
                 kwargs['color'] = line_color
        else:
             kwargs['color'] = 'green'
             
        plt.plot(t, df['success_root'], **kwargs)
    else:
        print("⚠ 'success_root' column missing.")
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
                     figsize=figsize_tuple)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
