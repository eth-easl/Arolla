import matplotlib.pyplot as plt
import os

def setup_plotting():
    plt.style.use('ggplot')

def plot_generic(x_data, results, xlabel, ylabel, title, filename, colors=None, linestyles=None, xlim=None, ylim=None, legend_loc='best', dashed_keys=None, legend_order=None):
    # Style: White background, large fonts
    plt.rcParams.update({
        'font.size': 24,
        'xtick.labelsize': 22,
        'ytick.labelsize': 22,
        'axes.labelsize': 22,
        'figure.figsize': (8, 6),
        'axes.facecolor': 'white',
        'figure.facecolor': 'white',
        'axes.grid': True,
        'grid.alpha': 1.0,
        'grid.color': '#cccccc',
        'lines.linewidth': 3.0,
        'axes.edgecolor': 'black',
        'text.color': 'black',
        'axes.labelcolor': 'black',
        'xtick.color': 'black',
        'ytick.color': 'black',
    })
    
    fig, ax = plt.subplots(figsize=(9, 6))
    
    # Track used styles for Legend Construction
    used_strategies = set()
    used_counts = set()
    
    # Map for clean legend names
    strategy_map = {
        'token_bucket': 'retry-token-bucket',
        'circuit_breaker': 'retry-circuit-breaker',
        'no_retries': 'no-retries',
        'fixed_retries': 'fixed-retries',
        'adaptive': 'retry-budget'
    }
    
    # Plotting loop
    for name, y_data in results.items():
        base_color = colors.get(name, 'black') if colors else 'black'
        linestyle = linestyles.get(name, '-') if linestyles else '-'
        
        # Check if we can extract info for the split legend
        # Name format: strategy_NUMclients
        if 'clients' in name:
            parts = name.split('_')
            # Assuming last part is NUMclients
            if parts[-1].endswith('clients'):
                try:
                    count_str = parts[-1].replace('clients', '')
                    count_val = int(count_str)
                    
                    # Strategy is everything before the last part
                    strat_key = "_".join(parts[:-1])
                    
                    if strat_key in strategy_map:
                        used_strategies.add(strat_key)
                        used_counts.add((count_val, linestyle))
                except:
                    pass
        
        ax.plot(x_data, y_data, label=name, color=base_color, linestyle=linestyle, linewidth=3.0, marker='o', markersize=6)
    
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if xlim:
        ax.set_xlim(xlim)
    if ylim:
        ax.set_ylim(ylim)
    
    # Grid Styling
    grid_lines = ax.get_xgridlines()
    gray = '#D3D3D3'
    grid_color = grid_lines[0].get_color() if grid_lines else gray
    for spine in ax.spines.values():
        spine.set_color(grid_color)
        spine.set_linewidth(1)

    # --- Manual Legend Construction ---
    if used_strategies and used_counts:
        # We detected a Client Count scenario -> Build Split Legend
        from matplotlib.lines import Line2D

        legend_elements = []

        # 1. Retry Strategy Header
        legend_elements.append(Line2D([0], [0], color='white', label='Retry Strategy', markersize=0))

        # Add standalone strategies (e.g. fixed_retries baseline) that don't have client count suffix
        standalone_strategies = [k for k in results.keys() if 'clients' not in k]
        for strat_key in standalone_strategies:
            c = colors.get(strat_key, 'black') if colors else 'black'
            ls = linestyles.get(strat_key, '-') if linestyles else '-'
            label = strategy_map.get(strat_key, strat_key)
            legend_elements.append(Line2D([0], [0], color=c, lw=3, linestyle=ls, label=label, marker='o'))

        # Strategies (Lines with Color)
        for Strat in sorted(list(used_strategies)):
            # Find color for this strategy (hacky: look it up from results or colors dict)
            # We know the map from Main:
            # token_bucket -> orange
            # circuit_breaker -> purple
            c = 'black'
            if 'token_bucket' in Strat: c = 'orange'
            if 'circuit_breaker' in Strat: c = 'purple'
            if 'adaptive' in Strat: c = 'orange'

            label = strategy_map.get(Strat, Strat)
            legend_elements.append(Line2D([0], [0], color=c, lw=3, label=label, marker='o'))
            
        # 2. Number of Clients Header
        legend_elements.append(Line2D([0], [0], color='white', label=' ', markersize=0)) # Spacer
        legend_elements.append(Line2D([0], [0], color='white', label='Number of Clients', markersize=0))
        
        # Counts (Lines with Style, Black)
        # Sort counts numerically
        sorted_counts = sorted(list(used_counts), key=lambda x: x[0])
        for count, ls in sorted_counts:
            legend_elements.append(Line2D([0], [0], color='black', lw=3, linestyle=ls, label=str(count), marker='o'))
            
        # Create the Legend
        # Explicitly use bbox (1.02, 0.5) and left alignment
        leg = ax.legend(handles=legend_elements, loc='center left', bbox_to_anchor=(1.02, 0.5), frameon=False, prop={'size': 18})
        leg._legend_box.align = "left"
        
        # Bold/Size the headers
        for text in leg.get_texts():
            if text.get_text() in ['Retry Strategy', 'Number of Clients']:
                text.set_fontsize(20)
                # We can't easily bold cleanly in all mpl versions, but usually weight='bold' works
                # text.set_weight('bold') 
                text.set_position((-20, 0)) # Shift left slightly
            
    else:
        # Fallback Standard Legend
        handles, labels = ax.get_legend_handles_labels()
        
        # Simple Sort or User Sort
        final_handles = []
        final_labels = []
        by_label = dict(zip(labels, handles))
        
        if legend_order:
            for label in legend_order:
                if label in by_label:
                    final_handles.append(by_label[label])
                    final_labels.append(label)
        else:
             final_handles = handles
             final_labels = labels

        leg = ax.legend(final_handles, final_labels, loc='center left', bbox_to_anchor=(1.02, 0.5), frameon=False, prop={'size': 18})
        leg._legend_box.align = "left"

    plt.tight_layout()
    plt.subplots_adjust(right=0.70) # Reserve space for legend
    
    fig.savefig(filename, bbox_inches='tight', dpi=100)
    print(f"Saved {filename}")

def plot_metastable_comparison(results, spike_info, filename_prefix):
    """
    Plot metastable failure scenario showing latency and request success/failure.
    Creates a 2x2 subplot figure similar to the reference image.
    """
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    
    configs = [('recoverable', '(a) Recoverable'), ('metastable', '(b) Metastable')]
    spike_start = spike_info['spike_start']
    spike_end = spike_info['spike_end']
    
    for idx, (config_name, title) in enumerate(configs):
        metrics = results[config_name]
        time_ticks = list(range(len(metrics['latency'])))
        
        # Top row: Latency
        ax_latency = axes[0, idx]
        latency_data = [max(0.01, l) for l in metrics['latency']]  # Avoid log(0)
        ax_latency.plot(time_ticks, latency_data, color='steelblue', linewidth=1.5)
        ax_latency.axvspan(spike_start, spike_end, alpha=0.2, color='gray', label='Trigger')
        ax_latency.set_ylabel('Latency (log seconds)', fontsize=10)
        ax_latency.set_yscale('log')
        ax_latency.set_xlim(0, max(time_ticks))
        ax_latency.set_title(title, fontsize=11)
        ax_latency.legend(loc='upper left', frameon=False, fontsize=9)
        ax_latency.grid(True, alpha=0.3)
        
        # Bottom row: Success/Fail stacked bar
        ax_requests = axes[1, idx]
        
        # Create stacked bar chart
        success_counts = [max(1, s) for s in metrics['success']]  # Avoid log(0)
        fail_counts = [max(0.1, f) for f in metrics['fail']]  # Avoid log(0)
        
        # Use narrower bars for better visibility
        width = 1.0
        ax_requests.bar(time_ticks, success_counts, label='success', color='steelblue', width=width)
        ax_requests.bar(time_ticks, fail_counts, bottom=success_counts, label='fail', 
                       color='darkorange', width=width)
        
        ax_requests.set_xlabel('Time (seconds)', fontsize=10)
        ax_requests.set_ylabel('Req/s (log scale)', fontsize=10)
        ax_requests.set_yscale('log')
        ax_requests.set_xlim(0, max(time_ticks))
        ax_requests.set_ylim(0.5, None)  # Start y-axis slightly below 1
        ax_requests.legend(loc='upper left', frameon=False, fontsize=9)
        ax_requests.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig(f"{filename_prefix}_metastable_comparison.png", bbox_inches='tight', dpi=100)
    print(f"Saved {filename_prefix}_metastable_comparison.png")
    plt.close()

def plot_metastable_metrics(results, spike_info, filename_prefix):
    """
    Plot additional metrics: queue length and capacity over time.
    """
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    
    configs = [('recoverable', 'Recoverable'), ('metastable', 'Metastable')]
    spike_start = spike_info['spike_start']
    spike_end = spike_info['spike_end']
    
    for idx, (config_name, title) in enumerate(configs):
        metrics = results[config_name]
        time_ticks = list(range(len(metrics['latency'])))
        
        # Top row: Queue Length
        ax_queue = axes[0, idx]
        ax_queue.plot(time_ticks, metrics['queue_length'], color='darkred', linewidth=1.5)
        ax_queue.axvspan(spike_start, spike_end, alpha=0.2, color='gray', label='Trigger')
        ax_queue.set_ylabel('Queue Length', fontsize=10)
        ax_queue.set_title(f'{title} - Queue Length', fontsize=11)
        ax_queue.legend(loc='upper left', frameon=False, fontsize=9)
        ax_queue.grid(True, alpha=0.3)
        ax_queue.set_xlim(0, max(time_ticks))
        
        # Bottom row: Capacity
        ax_capacity = axes[1, idx]
        ax_capacity.plot(time_ticks, metrics['capacity'], color='darkgreen', linewidth=1.5)
        ax_capacity.axvspan(spike_start, spike_end, alpha=0.2, color='gray', label='Trigger')
        ax_capacity.set_xlabel('Time (seconds)', fontsize=10)
        ax_capacity.set_ylabel('Capacity', fontsize=10)
        ax_capacity.set_title(f'{title} - Capacity', fontsize=11)
        ax_capacity.legend(loc='upper left', frameon=False, fontsize=9)
        ax_capacity.grid(True, alpha=0.3)
        ax_capacity.set_xlim(0, max(time_ticks))
    
    plt.tight_layout()
    plt.savefig(f"{filename_prefix}_queue_capacity.png", bbox_inches='tight', dpi=100)
    print(f"Saved {filename_prefix}_queue_capacity.png")
    plt.close()
