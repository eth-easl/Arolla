import json
import os
import matplotlib.pyplot as plt


def load_fault_events(fault_events_file):
    """Load fault events from JSON file"""
    if not fault_events_file or not os.path.exists(fault_events_file):
        return []

    with open(fault_events_file, "r") as f:
        return json.load(f)


def plot_fault_events(events):
    """Add fault events as background spans to the current plot"""
    # Deduplicate events to prevent alpha stacking (darker regions)
    seen_events = set()
    unique_events = []
    
    for event in events:
        # Create a hashable representation
        # Sort keys of params to ensure consistent ordering
        params_tuple = tuple(sorted(event.get("parameters", {}).items()))
        
        event_key = (
            event["event_type"],
            event["start_time_s"],
            event["end_time_s"],
            params_tuple
        )
        
        if event_key not in seen_events:
            seen_events.add(event_key)
            unique_events.append(event)
            
    for event in unique_events:
        start_time = event["start_time_s"]
        end_time = event["end_time_s"]
        event_type = event["event_type"]
        params = event["parameters"]

        # add vertical lines for injected failures
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
