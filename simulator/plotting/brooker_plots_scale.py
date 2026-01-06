#!/usr/bin/env python3
import sys
import argparse
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

# Add parent to path for shared plotting utils if needed in future
# sys.path.insert(0, str(Path(__file__).parent.parent))

def main():
    parser = argparse.ArgumentParser(
        description='Plot Scaling Analysis (Success Rate vs Failure Rate)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('results_dir', help='Input results directory')
    parser.add_argument('-o', '--output', default='scale_success_rate.pdf',
                       help='Output file path (default: scale_success_rate.png)')
    
    # Styling arguments matching plot_success_rps.py
    parser.add_argument('--figsize', default='10,6',
                       help='Figure size as "width,height" (default: 14,6)')
    parser.add_argument('--font-size', type=int, default=24,
                       help='Global font size')
    parser.add_argument('--tick-size', type=int, default=22,
                       help='Tick label size')
    parser.add_argument('--line-width', type=float, default=3.0,
                       help='Line width (default: 3.0)')
    parser.add_argument('--legend-loc', default='center left',
                       help='Legend location')
    parser.add_argument('--legend-bbox', default='1.02, 0.5',
                       help='Legend bbox_to_anchor (x,y)')
    parser.add_argument('--legend-size', type=int, default=18,
                       help='Legend text size')
    # Default to NO frame (True). Flag --with-legend-frame enables it (False).
    parser.add_argument('--with-legend-frame', action='store_false', dest='no_legend_frame',
                       help='Show legend frame')
    parser.set_defaults(no_legend_frame=True)
    parser.add_argument('--plot-right', type=float,
                       help='Right boundary of the axes (0.0-1.0), used to fix figure width by reserving space for legend.')
    
    args = parser.parse_args()
    
    # 1. Setup Input/Output Paths
    results_dir = Path(args.results_dir)
    output_path = Path(args.output)
    
    # If output is directory (or no extension), treat as dir and append filename
    if output_path.suffix == '' or output_path.is_dir():
        output_path.mkdir(parents=True, exist_ok=True)
        # Use parser default filename
        default_name = Path(parser.get_default('output')).name
        output_path = output_path / default_name
    else:
        # Ensure parent exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_path = results_dir / "summary.csv"
    if not summary_path.exists():
        print(f"Error: {summary_path} not found")
        sys.exit(1)
    
    # 2. Global Styling Configuration
    if args.font_size:
        plt.rcParams.update({'font.size': args.font_size})
    
    if args.tick_size:
        plt.rcParams.update({
            'xtick.labelsize': args.tick_size,
            'ytick.labelsize': args.tick_size
        })

    # Parse figsize
    figsize = (10, 6)
    if args.figsize:
        try:
            figsize = tuple(map(int, args.figsize.split(',')))
        except ValueError:
            pass

    # 3. Load and Process Data
    df = pd.read_csv(summary_path)
    
    # Identify relevant columns
    scale_col = "clients.0.replicas"
    if scale_col not in df.columns:
        candidates = [c for c in df.columns if 'replicas' in c]
        if candidates:
            scale_col = candidates[0]
        else:
            scale_col = None

    fail_col = "services.0.partial_failures.0.p_fail"
    if fail_col not in df.columns:
         candidates = [c for c in df.columns if 'p_fail' in c]
         if candidates:
             fail_col = candidates[0]
    
    print(f"Plotting Scale analysis. Scale Col: {scale_col}, Fail Col: {fail_col}")
    
    # Simplify Client Names
    def get_base_name(name):
        if '.' in name and name.split('.')[-1].isdigit():
            return name.rsplit('.', 1)[0]
        return name
        
    df['base_client'] = df['client_name'].apply(get_base_name)
    
    # Rename columns for friendly legend headers
    friendly_base = "Retry Strategy"
    friendly_scale = "Number of Clients"
    
    df.rename(columns={'base_client': friendly_base}, inplace=True)
    if scale_col:
        df.rename(columns={scale_col: friendly_scale}, inplace=True)
        # Update fail_col if needed? No, separate x-axis.
    
    # Update aggregation with new names
    group_cols = [fail_col, friendly_base]
    if scale_col:
        group_cols.append(friendly_scale)
    
    # Aggregation dictionary
    agg_cols = {
        'success_rate': 'mean',
        'goodput_rps': 'sum',
        'total_requests': 'sum'
    }
    # If total_attempts exists, sum it too
    if 'total_attempts' in df.columns:
        agg_cols['total_attempts'] = 'sum'
        
    agg_df = df.groupby(group_cols).agg(agg_cols).reset_index()
    
    # Calculate Load Percentage relative to p_fail=0
    # Group by [friendly_base, friendly_scale] to find baseline
    baseline_group_cols = [friendly_base]
    if scale_col:
        baseline_group_cols.append(friendly_scale)
        
    # Use total_attempts if available, else total_requests (compat)
    load_metric = "total_attempts" if "total_attempts" in agg_df.columns else "total_requests"
    print(f"Using load metric: {load_metric}")

    baseline_load = {}
    # Iterate groups to find baseline for each curve
    for name, group in agg_df.groupby(baseline_group_cols):
        # Find row with min risk/failure (usually 0.0)
        min_fail_row = group.loc[group[fail_col].idxmin()]
        baseline_load[name] = min_fail_row[load_metric]

    def calc_load_pct(row):
        if scale_col:
            # name from groupby with multiple cols is a tuple
            key = (row[friendly_base], row[friendly_scale])
        else:
            key = row[friendly_base]
            
        base = baseline_load.get(key)
        if base and base > 0:
            return 100.0 * row[load_metric] / base
        return 0.0

    agg_df['load_pct'] = agg_df.apply(calc_load_pct, axis=1)

    # Separate hue (Client Type) and style (Replicas)
    
    # Define custom palette
    custom_palette = {
        'circuit-breaker': 'purple',
        'retry-budget': 'orange',
        # Fallbacks/Extras
        'no-retries': 'blue',
        'three-retries': 'red',
        'exponential-backoff-jitter': 'green'
    }

    # Helper function for plotting
    def plot_metric(y_col, ylabel, output_name, legend_loc_override=None):
        fig, ax = plt.subplots(figsize=figsize)
        
        plot_kwargs = {
            'data': agg_df,
            'x': fail_col,
            'y': y_col,
            'hue': friendly_base,
            'palette': custom_palette,
            'marker': 'o', 
            'linewidth': args.line_width,
            'ax': ax
        }
        
        if scale_col:
            plot_kwargs['style'] = friendly_scale
            plot_kwargs['markers'] = True 
            plot_kwargs['dashes'] = True

        sns.lineplot(**plot_kwargs)
        
        ax.set_ylabel(ylabel)
        ax.set_xlabel('Server Failure Probability') 
        ax.grid(True, alpha=0.3)
        
        # Custom Frame Styling (match grid color)
        grid_lines = ax.get_xgridlines()
        if grid_lines:
            grid_color = grid_lines[0].get_color()
            for spine in ax.spines.values():
                spine.set_color(grid_color)
                spine.set_linewidth(1)
        else:
            gray = '#D3D3D3'
            for spine in ax.spines.values():
                spine.set_color(gray)

        # Legend Customization
        bbox = None
        if args.legend_bbox:
            try:
                bbox = tuple(map(float, args.legend_bbox.split(',')))
            except ValueError:
                pass
                
        curr_legend_loc = legend_loc_override or args.legend_loc
        
        legend_kwargs = {
            'loc': curr_legend_loc,
            'bbox_to_anchor': bbox,
            'frameon': not args.no_legend_frame
        }
        
        if args.legend_size:
            legend_kwargs['prop'] = {'size': args.legend_size}
            legend_kwargs['title_fontsize'] = args.legend_size

        handles, labels = ax.get_legend_handles_labels()
        if ax.get_legend():
            ax.get_legend().remove()
            
        leg = ax.legend(handles=handles, labels=labels, **legend_kwargs)
        
        # Legend Header Styling
        headers = {friendly_base, friendly_scale}
        if scale_col:
            headers.add(friendly_scale)

        leg._legend_box.align = "left"
        shift_est = 3 * args.legend_size

        for text in leg.get_texts():
            if text.get_text() in headers:
                text.set_fontsize(args.legend_size + 1)
                # text.set_weight('bold')
                text.set_ha('left')
                text.set_position((-shift_est, 0))

        # Saving
        final_out = output_path.parent / output_name if output_path.is_dir() else output_path.parent / output_name
        
        if args.plot_right:
            plt.subplots_adjust(right=args.plot_right)
            plt.savefig(final_out, dpi=100)
            print(f"✓ Saved plot to {final_out} (Fixed width)")
        else:
            plt.tight_layout()
            plt.savefig(final_out, dpi=100, bbox_inches='tight')
            print(f"✓ Saved plot to {final_out}")
            
        plt.close()

    # 4. Generate Plots
    success_name = output_path.name
    # Derive load filename
    if 'success' in success_name:
        load_name = success_name.replace('success_rate', 'load').replace('success', 'load')
    else:
        load_name = "load_" + success_name
        
    plot_metric('success_rate', 'Success Rate', success_name)
    plot_metric('load_pct', 'Load (%)', load_name)

if __name__ == "__main__":
    main()
