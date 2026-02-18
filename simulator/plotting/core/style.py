"""Plotting style configuration and utilities."""

import json
import os
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


def _load_fault_events(fault_events_file):
    """Load fault events from JSON file."""
    if not fault_events_file or not os.path.exists(fault_events_file):
        return []
    with open(fault_events_file, "r") as f:
        return json.load(f)


def _plot_fault_events(events):
    """Add fault events as background spans to the current plot."""
    seen = set()
    for event in events:
        params_tuple = tuple(sorted(event.get("parameters", {}).items()))
        key = (event["event_type"], event["start_time_s"], event["end_time_s"], params_tuple)
        if key in seen:
            continue
        seen.add(key)

        start_time = event["start_time_s"]
        end_time = event["end_time_s"]
        event_type = event["event_type"]
        params = event["parameters"]

        if event_type == "latency_injection":
            add_latency_ms = params.get("add_latency_ms", 0)
            multiplier = params.get("multiplier", 1)
            label = f"Latency Injection ({add_latency_ms}ms, x{multiplier})"
            plt.axvspan(start_time, end_time, color="orange", alpha=0.1, label=label)
        elif event_type == "partial_failure":
            failure_rate = params.get("failure_rate", 0)
            label = f"Partial Failure ({failure_rate*100:.0f}%)      "
            plt.axvspan(start_time, end_time, color="red", alpha=0.1, label=label)
        elif event_type == "load_spike":
            multiplier = params.get("rps_multiplier", 1)
            label = f"Load Spike (x{multiplier})"
            plt.axvspan(start_time, end_time, color="purple", alpha=0.1, label=label)


def add_fault_events(fault_events_file: Optional[str] = None):
    """Add fault event markers to current plot."""
    if not fault_events_file:
        return
    try:
        events = _load_fault_events(fault_events_file)
        _plot_fault_events(events)
    except Exception as e:
        print(f"Warning: Could not load fault events: {e}")

