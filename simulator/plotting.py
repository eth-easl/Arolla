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
