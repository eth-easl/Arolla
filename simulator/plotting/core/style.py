"""Plotting style configuration and utilities."""

import matplotlib.pyplot as plt
from typing import Optional, Tuple


# Default figure size
DEFAULT_FIGSIZE = (10, 6)

# Color palette
COLORS = {
    'primary': '#1f77b4',
    'success': '#2ca02c',
    'warning': '#ff7f0e',
    'danger': '#d62728',
    'info': '#9467bd',
}


def setup_plot(title: str, xlabel: str, ylabel: str, 
               figsize: Tuple[int, int] = DEFAULT_FIGSIZE) -> plt.Figure:
    """
    Setup a matplotlib plot with consistent styling.
    
    Args:
        title: Plot title
        xlabel: X-axis label
        ylabel: Y-axis label
        figsize: Figure size (width, height)
        
    Returns:
        Figure object
    """
    fig = plt.figure(figsize=figsize)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True, alpha=0.3)
    # Let matplotlib auto-scale both axes based on data
    return fig


def save_plot(output_file: str, dpi: int = 100):
    """
    Save plot to file with consistent settings.
    
    Args:
        output_file: Output filename
        dpi: Resolution (dots per inch)
    """
    plt.tight_layout()
    plt.savefig(output_file, dpi=dpi, bbox_inches='tight')
    print(f"✓ Saved plot to {output_file}")


def add_fault_events(fault_events_file: Optional[str] = None):
    """Add fault event markers to current plot."""
    if not fault_events_file:
        return
    
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from utils.fault_events import load_fault_events, plot_fault_events
        
        fault_events = load_fault_events(fault_events_file)
        plot_fault_events(fault_events)
    except Exception as e:
        print(f"Warning: Could not load fault events: {e}")
