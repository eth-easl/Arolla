import argparse
import glob
import os
import json
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

def load_data(input_dir):
    # Match specific pattern for reproducibility
    files = glob.glob(os.path.join(input_dir, "output_*.csv"))
    clients = {}
    for f in files:
        # Extract client name from filename: output_CLIENTNAME.csv
        basename = os.path.basename(f)
        if basename == "output.csv":
             continue 
        
        client_name = basename.replace("output_", "").replace(".csv", "")
        df = pd.read_csv(f)
        clients[client_name] = df
    return clients

def get_fault_range(input_dir):
    # Try to find faults.json or demo_faults.json
    fault_files = glob.glob(os.path.join(input_dir, "*fault*.json"))
    if not fault_files:
        return None, None
        
    try:
        with open(fault_files[0], 'r') as f:
            events = json.load(f)
            # Find the first partial failure event
            for e in events:
                if e['event_type'] == 'partial_failure':
                    return e['start_time_s'], e['end_time_s']
    except Exception as e:
        print(f"Error reading fault settings: {e}")
    return None, None

def plot_reproduction(clients, output_dir, input_dir):
    # Dynamic Palette
    # Use tab10 colormap
    colors = plt.cm.tab10.colors
    unique_clients = sorted(clients.keys())
    palette = {name: colors[i % len(colors)] for i, name in enumerate(unique_clients)}

    # Setup Grid: 1 Row x 2 Cols (Server Uptime, RPS Amplification)
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    
    # Get outage range from file
    outage_start, outage_end = get_fault_range(input_dir)
    # Fallback/Empty if not found (don't break plotting)
    
    # 1. Server Uptime (%)
    ax = axes[0]
    for name, df in clients.items():
        if df is None: continue
        # Calculate success rate per bucket (Uptime)
        # Use completed attempts and actual successes (success_root)
        # This accurately reflects the ratio of successful attempts vs total attempts finished in that bucket
        total_completed = df['completed']
        successful = df['success_root']
        
        # Avoid div by zero
        success_rate = successful / total_completed
        success_rate = success_rate.fillna(1.0) # If no attempts, assume 100% uptime?
        
        ax.plot(df['timepoint'], success_rate * 100, label=name, color=palette.get(name, 'black'), linewidth=2)
        
    ax.set_title("Client Success Rate")
    ax.set_ylabel("Client Success Rate (%)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(-5, 105)
    
    # Add vertical lines for outage
    if outage_start is not None and outage_end is not None:
        ax.axvspan(outage_start, outage_end, color='red', alpha=0.1, label="Outage")
        ax.legend() # Update legend to include outage

    # 2. Server RPS Amplification
    ax = axes[1]
    TARGET_RPS = 100 # Default fallback
    # Try to infer target RPS from pre-outage data? or assume base?
    # For now keep 100 or making it relative to max?
    
    for name, df in clients.items():
        if df is None: continue
        if 'root_requests' in df.columns and 'retries' in df.columns:
            attempts = df['root_requests'] + df['retries']
        else:
            attempts = df['total_attempts'] if 'total_attempts' in df.columns else df['total_request']
            
        granularity = df['timepoint'].diff().mode()[0] if len(df) > 1 else 0.01
        
        rps = attempts / granularity
        rel_rps = (rps / TARGET_RPS) * 100
        
        ax.plot(df['timepoint'], rel_rps, label=name, color=palette.get(name, 'black'), linewidth=2)
        
    ax.set_title("Server RPS Amplification")
    ax.set_ylabel("Relative Server RPS (%)")
    ax.set_xlabel("Time (s)")
    
    # Add vertical lines for outage
    if outage_start is not None and outage_end is not None:
        ax.axvspan(outage_start, outage_end, color='red', alpha=0.1)
    
    # Legend
    handles, labels = ax.get_legend_handles_labels()
    # Remove duplicate labels (e.g. from multiple outage spans if we had them, or just ensuring cleanliness)
    by_label = dict(zip(labels, handles))
    fig.legend(by_label.values(), by_label.keys(), loc='upper center', bbox_to_anchor=(0.5, 1.05), ncol=len(by_label))
    
    plt.tight_layout()
    output_path = os.path.join(output_dir, "reproduced_figures_3x.png")
    plt.savefig(output_path, bbox_inches='tight')
    print(f"Saved to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", help="Directory containing CSV files")
    parser.add_argument("-o", "--output", help="Output directory (default: input_dir/plots)")
    args = parser.parse_args()
    
    output_dir = args.output if args.output else os.path.join(args.input_dir, "plots")
    os.makedirs(output_dir, exist_ok=True)
    
    clients = load_data(args.input_dir)
    plot_reproduction(clients, output_dir, args.input_dir)
