import matplotlib.pyplot as plt
import os

def setup_plotting():
    plt.style.use('ggplot')

def plot_generic(x_data, results, xlabel, ylabel, title, filename, colors=None, linestyles=None, xlim=None, ylim=None, legend_loc='best', dashed_keys=None, legend_order=None):
    fig, ax = plt.subplots(figsize=(8, 5))
    
    for name, y_data in results.items():
        color = colors.get(name, 'black') if colors else None
        linestyle = linestyles.get(name, '-') if linestyles else ('--' if dashed_keys and name in dashed_keys else '-')
        alpha = 0.7 if dashed_keys and name in dashed_keys else 1.0
        linewidth = 1.5 if dashed_keys and name in dashed_keys else 2.0
        
        ax.plot(x_data, y_data, label=name, color=color, linestyle=linestyle, alpha=alpha, linewidth=linewidth)
    
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if xlim:
        ax.set_xlim(xlim)
    if ylim:
        ax.set_ylim(ylim)
    
    # Legend sorting
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    
    if legend_order:
        # Use custom order if provided
        sorted_handles = []
        sorted_labels = []
        for label in legend_order:
            if label in by_label:
                sorted_handles.append(by_label[label])
                sorted_labels.append(label)
    else:
        # Default order for basic scenarios
        order = ["no_retries", "fixed_retries", "circuit_breaker", "token_bucket"]
        
        sorted_handles = []
        sorted_labels = []
        for label in order:
            if label in by_label:
                sorted_handles.append(by_label[label])
                sorted_labels.append(label)
                
        # Add any remaining labels
        for label in labels:
            if label not in order:
                sorted_handles.append(by_label[label])
                sorted_labels.append(label)
            
    ax.legend(sorted_handles, sorted_labels, title="Retry Strategy", loc=legend_loc, frameon=False)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(filename, bbox_inches='tight')
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
